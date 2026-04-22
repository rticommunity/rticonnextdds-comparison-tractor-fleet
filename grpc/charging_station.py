#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0
"""
Charging Station — Pure gRPC: gRPC + Zeroconf

Manages a single charging dock for the robot fleet.  Only one robot can
charge at a time; additional robots queue up in FIFO order and wait near
the station until the dock becomes free.

Multiple stations can run simultaneously (e.g. one at each corner of the
arena) to provide redundancy and reduce wait times.

gRPC architecture:
  • Three RPCs handle the two-phase slot negotiation:
    - RequestSlot   → SlotOffer      (non-committing: offer + wait estimate)
    - ConfirmSlot   → SlotAssignment (committing: confirm queue position)
    - ReleaseSlot   → SlotReleaseAck        (free the dock when done)
  • StreamStatus is a server-streaming RPC that pushes StationStatusResponse
    snapshots to the UI whenever queue state changes.
  • Zeroconf (mDNS) advertises the station so robots discover it without
    hard-coded addresses.

See charging_station_spec.md for the full specification.
"""

import grpc
from concurrent import futures
import threading
import time
import argparse
import uuid
import math
from datetime import datetime
from collections import OrderedDict
from typing import Optional

import robot_pb2
import robot_pb2_grpc
from fleet_discovery import FleetDiscovery


# ═════════════════════════════════════════════════════════════════════════════
# Constants
# ═════════════════════════════════════════════════════════════════════════════

# Conservative estimate for wait-time calculations.  In the simulation,
# charging refills at ~2 %/s so a full recharge from 0 takes ~50 s.
CHARGE_DURATION_ESTIMATE = 45


# ═════════════════════════════════════════════════════════════════════════════
# Compact log timestamp
# ═════════════════════════════════════════════════════════════════════════════


def _ts():
    """Return a compact HH:MM:SS timestamp for log lines."""
    return datetime.now().strftime("%H:%M:%S")


# ═════════════════════════════════════════════════════════════════════════════
# ChargingStationServicer — gRPC charging dock manager
# ═════════════════════════════════════════════════════════════════════════════


class ChargingStationServicer(robot_pb2_grpc.ChargingStationServicer):
    """Manages a single-dock charging station with a FIFO queue.

    Queue state:
        _queue:    OrderedDict  slot_id → {"robot_id": str, "confirmed": bool, "ts": float}
        _charging: slot_id or None  (the robot currently on the dock)

    gRPC RPCs:
        RequestSlot   → SlotOffer      (non-committing offer)
        ConfirmSlot   → SlotAssignment (commit queue position)
        ReleaseSlot   → SlotReleaseAck        (free dock)
        StreamStatus  → server-streaming StationStatusResponse

    Threads:
        gRPC ThreadPoolExecutor (max_workers=10)
        Condition variable for StreamStatus push notifications
    """

    def __init__(self, station_id: str, dock_x: float, dock_y: float):
        self.station_id = station_id
        self.dock_x = dock_x
        self.dock_y = dock_y
        self.lock = threading.Lock()

        # ── Queue state (protected by self.lock) ──────────────────────────
        self._queue: OrderedDict = OrderedDict()
        self._charging: Optional[str] = None   # slot_id on the dock

        # Condition variable — notified on every state mutation so that
        # StreamStatus subscribers wake up and yield the new snapshot.
        self._status_cv = threading.Condition(self.lock)

    # ═════════════════════════════════════════════════════════════════════
    # Helpers
    # ═════════════════════════════════════════════════════════════════════

    def _notify_status_change(self):
        """Wake all StreamStatus subscribers.  Must be called with self.lock held."""
        self._status_cv.notify_all()

    def _build_status(self) -> robot_pb2.StationStatusResponse:
        """Build a StationStatusResponse from current queue state.

        Includes dock coordinates, the docked robot (if any), and a
        ranked list of all queue entries.  Caller must hold self.lock.
        """
        dock_robot = ""
        if self._charging:
            info = self._queue.get(self._charging)
            if info:
                dock_robot = info["robot_id"]

        entries = []
        for rank, (sid, info) in enumerate(self._queue.items()):
            entries.append(robot_pb2.QueueEntry(
                robot_id=info["robot_id"],
                rank=rank,
                confirmed=info["confirmed"],
            ))

        return robot_pb2.StationStatusResponse(
            station_id=self.station_id,
            dock_x=self.dock_x,
            dock_y=self.dock_y,
            dock_robot=dock_robot,
            queue=entries,
        )

    def _queue_rank(self, slot_id: str) -> int:
        """Return 0-based rank in the queue (-1 if not found).
        Must be called with self.lock held."""
        for i, sid in enumerate(self._queue):
            if sid == slot_id:
                return i
        return -1

    def _wait_position(self, rank: int) -> tuple[float, float]:
        """Compute a waiting position near the station for a given queue rank.

        Robots line up in a column away from the nearest arena edge,
        each separated by ~8 units so collision avoidance doesn't
        kick in.  The direction is chosen so that the queue extends
        toward the centre of the arena (never off the map).
        """
        spacing = 8
        offset = 10 + rank * spacing
        # Choose queue direction: toward the centre (50,50) on each axis
        dx = 1 if self.dock_x < 50 else -1
        dy = 1 if self.dock_y < 50 else -1
        # Queue along whichever axis has more room
        if abs(50 - self.dock_x) >= abs(50 - self.dock_y):
            return (self.dock_x + dx * offset, self.dock_y)
        else:
            return (self.dock_x, self.dock_y + dy * offset)

    def _expire_stale_offers(self):
        """Remove unconfirmed offers older than 15 s.

        Prevents ghost entries from lingering in the queue when a robot
        crashes or disconnects between RequestSlot and ConfirmSlot.
        Must be called with self.lock held.
        """
        now = time.time()
        stale = [sid for sid, info in self._queue.items()
                 if not info["confirmed"] and now - info["ts"] > 15]
        for sid in stale:
            rid = self._queue[sid]["robot_id"]
            del self._queue[sid]
            print(f"[{self.station_id} {_ts()}] Expired stale offer for {rid}")

    # ═════════════════════════════════════════════════════════════════════
    # gRPC RPCs
    # ═════════════════════════════════════════════════════════════════════

    def RequestSlot(self, request, context):
        """Non-committing: return an offer with estimated wait time.

        If ``info_only`` is set, return dock coordinates and current queue
        length without creating a queue entry.  The returned ``slot_id``
        will be empty and cannot be confirmed.

        Otherwise, creates an unconfirmed queue entry that expires after
        15 s if ConfirmSlot is never called.
        """
        with self._status_cv:
            self._expire_stale_offers()
            rank = len(self._queue)
            wait_s = rank * CHARGE_DURATION_ESTIMATE

            if request.info_only:
                # Read-only probe — no state change
                print(f"[{self.station_id} {_ts()}] InfoOnly probe from {request.robot_id}")
                return robot_pb2.SlotOffer(
                    station_id=self.station_id,
                    slot_id="",
                    queue_rank=rank,
                    wait_time_s=wait_s,
                    dock_x=self.dock_x,
                    dock_y=self.dock_y,
                )

            slot_id = str(uuid.uuid4())[:8]

            # Add as unconfirmed offer
            self._queue[slot_id] = {
                "robot_id": request.robot_id,
                "confirmed": False,
                "ts": time.time(),
            }

            print(f"[{self.station_id} {_ts()}] SlotRequest from {request.robot_id} "
                  f"(batt={request.battery_level:.0f}%) → offer {slot_id} rank={rank} wait≈{wait_s}s")

            self._notify_status_change()
            return robot_pb2.SlotOffer(
                station_id=self.station_id,
                slot_id=slot_id,
                queue_rank=rank,
                wait_time_s=wait_s,
                dock_x=self.dock_x,
                dock_y=self.dock_y,
            )

    def ConfirmSlot(self, request, context):
        """Commit a previously offered slot — reserves the queue position.

        Returns a SlotAssignment with waiting coordinates.  If the robot
        is first in line and the dock is free, it docks immediately.
        """
        with self._status_cv:
            info = self._queue.get(request.slot_id)
            if not info or info["robot_id"] != request.robot_id:
                return robot_pb2.SlotAssignment(granted=False)

            info["confirmed"] = True
            rank = self._queue_rank(request.slot_id)
            wx, wy = self._wait_position(rank)

            # If this robot is first in line and dock is free, they can dock now
            if rank == 0 and self._charging is None:
                self._charging = request.slot_id
                print(f"[{self.station_id} {_ts()}] {request.robot_id} → DOCKING (slot {request.slot_id})")
                self._notify_status_change()
                return robot_pb2.SlotAssignment(
                    granted=True, queue_rank=0,
                    wait_x=self.dock_x, wait_y=self.dock_y,
                    dock_x=self.dock_x, dock_y=self.dock_y,
                )

            print(f"[{self.station_id} {_ts()}] {request.robot_id} confirmed slot {request.slot_id} rank={rank}")
            self._notify_status_change()
            return robot_pb2.SlotAssignment(
                granted=True, queue_rank=rank,
                wait_x=wx, wait_y=wy,
                dock_x=self.dock_x, dock_y=self.dock_y,
            )

    def ReleaseSlot(self, request, context):
        """Robot finished charging — free the dock and advance the queue.

        Removes the slot from the queue.  If the released slot was on the
        dock, promotes the next confirmed robot via _advance_queue().
        """
        with self._status_cv:
            info = self._queue.get(request.slot_id)
            if not info:
                return robot_pb2.SlotReleaseAck(success=False)

            rid = info["robot_id"]
            del self._queue[request.slot_id]

            if self._charging == request.slot_id:
                self._charging = None
                print(f"[{self.station_id} {_ts()}] {rid} released dock (slot {request.slot_id})")
                # Advance: next confirmed robot gets the dock
                self._advance_queue()
            else:
                print(f"[{self.station_id} {_ts()}] {rid} released waiting slot {request.slot_id}")

            self._notify_status_change()
            return robot_pb2.SlotReleaseAck(success=True)

    def StreamStatus(self, request, context):
        """Server-streaming: yield a StationStatusResponse whenever state changes.

        The UI subscribes once and receives push updates — no polling needed.
        An initial snapshot is sent immediately on connect.
        """
        print(f"[{self.station_id} {_ts()}] StreamStatus subscriber connected")
        try:
            with self._status_cv:
                while context.is_active():
                    # Expire stale unconfirmed offers on each heartbeat so they
                    # don't linger in the queue (and the UI panel) forever.
                    self._expire_stale_offers()
                    yield self._build_status()
                    # Wait for the next state mutation (or 5 s heartbeat so the
                    # stream stays alive and detects client disconnects promptly).
                    self._status_cv.wait(timeout=5.0)
        except Exception as e:
            print(f"[{self.station_id} {_ts()}] StreamStatus subscriber disconnected: {e}")

    # ═════════════════════════════════════════════════════════════════════
    # Queue Helpers
    # ═════════════════════════════════════════════════════════════════════

    def _advance_queue(self):
        """Promote the next confirmed robot in the queue to the dock.

        The promoted robot learns of its new status via the StreamStatus
        push that follows from _notify_status_change().

        Must be called with self.lock held.
        """
        for sid, info in self._queue.items():
            if info["confirmed"]:
                self._charging = sid
                print(f"[{self.station_id} {_ts()}] {info['robot_id']} → DOCKING (advanced, slot {sid})")
                return


# ═════════════════════════════════════════════════════════════════════════════
# main
# ═════════════════════════════════════════════════════════════════════════════

def main():
    """Parse CLI args, start gRPC server, register on mDNS, block until Ctrl-C."""
    parser = argparse.ArgumentParser(
        description="Charging Station — gRPC + Zeroconf")
    parser.add_argument("--port", type=int, default=50060, help="gRPC port")
    parser.add_argument("--station-id", default="station1", help="Station identifier")
    parser.add_argument("--dock-x", type=float, default=50.0, help="Dock X coordinate")
    parser.add_argument("--dock-y", type=float, default=5.0, help="Dock Y coordinate")
    args = parser.parse_args()

    # ── Start gRPC server ───────────────────────────────────────────────
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    servicer = ChargingStationServicer(args.station_id, args.dock_x, args.dock_y)
    robot_pb2_grpc.add_ChargingStationServicer_to_server(servicer, server)
    server.add_insecure_port(f"[::]:{args.port}")
    server.start()

    # ── Advertise via mDNS so robots and UI discover us automatically ─
    zc, zc_info = FleetDiscovery.register_station(args.station_id, args.port)

    print(f"[{args.station_id} {_ts()}] ⚡ Charging station on port {args.port}")
    print(f"[{args.station_id} {_ts()}]   Dock at ({args.dock_x}, {args.dock_y})")

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        print(f"\n[{args.station_id} {_ts()}] Shutting down…")
        zc.unregister_service(zc_info)
        zc.close()
        server.stop(0)


if __name__ == "__main__":
    main()
