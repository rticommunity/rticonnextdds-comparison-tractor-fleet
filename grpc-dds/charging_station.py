#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0
"""
Charging Station — the hybrid approach: Hybrid gRPC + DDS

Manages a single charging dock for the robot fleet.  Only one robot can
charge at a time; additional robots queue up in FIFO order.

Hybrid architecture:
  • gRPC server for slot negotiation:
    - RequestSlot  → SlotOffer      (non-committing: offer + wait estimate)
    - ConfirmSlot  → SlotAssignment (commit queue position)
    - ReleaseSlot  → SlotReleaseAck        (free the dock when done)
  • DDS publisher for StationStatus:
    - Queue state published after every mutation so robots and UI get
      real-time queue updates via DDS pub-sub.
  • Zeroconf (mDNS) advertises the gRPC port so robots discover it.

Compare with the gRPC approach (Full Mesh gRPC):
  • StationStatus uses DDS pub-sub instead of gRPC StreamStatus.
  • Robots monitor queue rank changes via DDS (no polling needed).

Compare with the DDS approach (Pure DDS):
  • Slot negotiation uses gRPC instead of rti.rpc.SimpleReplier.
  • Still publishes StationStatus via DDS for queue monitoring.

See charging_station_spec.md for the full specification.
"""

import rti.connextdds as dds
import grpc
from concurrent import futures
import threading
import time
import argparse
import uuid
import math
import os
from datetime import datetime
from collections import OrderedDict
from typing import Optional

# DDS types
from robot_types import StationStatus, QueueEntry

# gRPC protobuf stubs
import robot_pb2
import robot_pb2_grpc

# Zeroconf for gRPC address advertisement
from fleet_discovery import FleetDiscovery


# ═════════════════════════════════════════════════════════════════════════════
# Constants
# ═════════════════════════════════════════════════════════════════════════════

CHARGE_DURATION_ESTIMATE = 45


def _ts():
    """Return a compact HH:MM:SS timestamp for log lines."""
    return datetime.now().strftime("%H:%M:%S")


# ═════════════════════════════════════════════════════════════════════════════
# ChargingStationServicer — gRPC charging dock manager
# ═════════════════════════════════════════════════════════════════════════════

class ChargingStationServicer(robot_pb2_grpc.ChargingStationServicer):
    """Manages a single-dock charging station with a FIFO queue.

    gRPC RPCs handle slot negotiation (same as the gRPC approach):
        RequestSlot   → SlotOffer      (non-committing offer)
        ConfirmSlot   → SlotAssignment (commit queue position)
        ReleaseSlot   → SlotReleaseAck        (free dock)

    DDS publishes StationStatus after every state mutation (from the DDS approach).

    Threads:
        gRPC ThreadPoolExecutor (max_workers=10)
        DDS internal threads (for StationStatus publication)
    """

    def __init__(self, station_id: str, dock_x: float, dock_y: float,
                 dds_writer: dds.DataWriter):
        self.station_id = station_id
        self.dock_x = dock_x
        self.dock_y = dock_y
        self.lock = threading.Lock()
        self._dds_writer = dds_writer

        # ── Queue state (protected by self.lock) ──────────────────────────
        self._queue: OrderedDict = OrderedDict()
        self._charging: Optional[str] = None   # slot_id on the dock

    # ═════════════════════════════════════════════════════════════════════
    # Helpers
    # ═════════════════════════════════════════════════════════════════════

    def _publish_status(self):
        """Publish a DDS StationStatus sample with current queue state.

        Called after every state mutation so that robots and the UI
        get real-time queue updates via DDS pub-sub.

        Compare with the gRPC approach: the gRPC approach uses a gRPC StreamStatus
        (server-streaming) with a Condition variable for push notifications.
        The hybrid uses DDS pub-sub instead — simpler and decoupled.
        """
        docked_robot = ""
        if self._charging:
            info = self._queue.get(self._charging)
            if info:
                docked_robot = info["robot_id"]
            else:
                self._charging = None

        entries = []
        for rank, (sid, info) in enumerate(self._queue.items()):
            entries.append(QueueEntry(
                robot_id=info["robot_id"],
                rank=rank,
                confirmed=info["confirmed"],
            ))

        status = StationStatus(
            station_id=self.station_id,
            dock_x=self.dock_x,
            dock_y=self.dock_y,
            is_occupied=(self._charging is not None),
            docked_robot_id=docked_robot,
            queue=entries,
        )

        self._dds_writer.write(status)

    def _queue_rank(self, slot_id: str) -> int:
        """Return 0-based rank in the queue (-1 if not found)."""
        for i, sid in enumerate(self._queue):
            if sid == slot_id:
                return i
        return -1

    def _wait_position(self, rank: int) -> tuple[float, float]:
        """Compute a waiting position near the station for a given queue rank."""
        spacing = 8
        offset = 10 + rank * spacing
        dx = 1 if self.dock_x < 50 else -1
        dy = 1 if self.dock_y < 50 else -1
        if abs(50 - self.dock_x) >= abs(50 - self.dock_y):
            return (self.dock_x + dx * offset, self.dock_y)
        else:
            return (self.dock_x, self.dock_y + dy * offset)

    def _expire_stale_offers(self):
        """Remove unconfirmed offers older than 15 s."""
        now = time.time()
        stale = [sid for sid, info in self._queue.items()
                 if not info["confirmed"] and now - info["ts"] > 15]
        for sid in stale:
            rid = self._queue[sid]["robot_id"]
            del self._queue[sid]
            print(f"[{self.station_id} {_ts()}] Expired stale offer for {rid}")

    def _advance_queue(self):
        """Promote the next confirmed robot in the queue to the dock."""
        for sid, info in self._queue.items():
            if info["confirmed"]:
                self._charging = sid
                print(f"[{self.station_id} {_ts()}] {info['robot_id']} "
                      f"→ DOCKING (advanced, slot {sid})")
                return

    # ═════════════════════════════════════════════════════════════════════
    # gRPC RPCs
    # ═════════════════════════════════════════════════════════════════════

    def RequestSlot(self, request, context):
        """Non-committing: return an offer with estimated wait time."""
        with self.lock:
            self._expire_stale_offers()
            rank = len(self._queue)
            wait_s = rank * CHARGE_DURATION_ESTIMATE

            if request.info_only:
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

            self._queue[slot_id] = {
                "robot_id": request.robot_id,
                "confirmed": False,
                "ts": time.time(),
            }

            print(f"[{self.station_id} {_ts()}] SlotRequest from {request.robot_id} "
                  f"(batt={request.battery_level:.0f}%) → offer {slot_id} "
                  f"rank={rank} wait≈{wait_s}s")

            self._publish_status()
            return robot_pb2.SlotOffer(
                station_id=self.station_id,
                slot_id=slot_id,
                queue_rank=rank,
                wait_time_s=wait_s,
                dock_x=self.dock_x,
                dock_y=self.dock_y,
            )

    def ConfirmSlot(self, request, context):
        """Commit a previously offered slot — reserves the queue position."""
        with self.lock:
            info = self._queue.get(request.slot_id)
            if not info or info["robot_id"] != request.robot_id:
                return robot_pb2.SlotAssignment(granted=False)

            info["confirmed"] = True
            rank = self._queue_rank(request.slot_id)
            wx, wy = self._wait_position(rank)

            if rank == 0 and self._charging is None:
                self._charging = request.slot_id
                print(f"[{self.station_id} {_ts()}] {request.robot_id} "
                      f"→ DOCKING (slot {request.slot_id})")
                self._publish_status()
                return robot_pb2.SlotAssignment(
                    granted=True, queue_rank=0,
                    wait_x=self.dock_x, wait_y=self.dock_y,
                    dock_x=self.dock_x, dock_y=self.dock_y,
                )

            print(f"[{self.station_id} {_ts()}] {request.robot_id} "
                  f"confirmed slot {request.slot_id} rank={rank}")
            self._publish_status()
            return robot_pb2.SlotAssignment(
                granted=True, queue_rank=rank,
                wait_x=wx, wait_y=wy,
                dock_x=self.dock_x, dock_y=self.dock_y,
            )

    def ReleaseSlot(self, request, context):
        """Robot finished charging — free the dock and advance the queue."""
        with self.lock:
            info = self._queue.get(request.slot_id)
            if not info:
                return robot_pb2.SlotReleaseAck(success=False)

            rid = info["robot_id"]
            del self._queue[request.slot_id]

            if self._charging == request.slot_id:
                self._charging = None
                print(f"[{self.station_id} {_ts()}] {rid} released dock "
                      f"(slot {request.slot_id})")
                self._advance_queue()
            else:
                print(f"[{self.station_id} {_ts()}] {rid} released waiting "
                      f"slot {request.slot_id}")

            self._publish_status()
            return robot_pb2.SlotReleaseAck(success=True)


# ═════════════════════════════════════════════════════════════════════════════
# main
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Charging Station — the hybrid approach: Hybrid gRPC + DDS")
    parser.add_argument("--port", type=int, default=50060,
                        help="gRPC port for slot negotiation")
    parser.add_argument("--station-id", default="station1",
                        help="Station identifier")
    parser.add_argument("--dock-x", type=float, default=50.0,
                        help="Dock X coordinate (0–100)")
    parser.add_argument("--dock-y", type=float, default=5.0,
                        help="Dock Y coordinate (0–100)")
    parser.add_argument("--domain", type=int, default=0,
                        help="DDS Domain ID")
    args = parser.parse_args()

    # ── Initialize DDS (StationStatus writer only) ────────────────────────
    print(f"[{args.station_id} {_ts()}] Initializing DDS on domain {args.domain}...")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    qos_xml = os.path.join(script_dir, "robot_qos.xml")
    qos_provider = dds.QosProvider(qos_xml)

    participant = dds.DomainParticipant(args.domain)
    station_topic = dds.Topic(participant, "StationStatus", StationStatus)
    publisher = dds.Publisher(participant)
    status_w_qos = qos_provider.datawriter_qos_from_profile(
        "RobotQosLibrary::StationStatusProfile")
    status_writer = dds.DataWriter(publisher, station_topic, status_w_qos)

    print(f"[{args.station_id} {_ts()}] DDS initialized — 1 StationStatus writer")

    # ── Start gRPC server ───────────────────────────────────────────────
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    servicer = ChargingStationServicer(
        args.station_id, args.dock_x, args.dock_y, status_writer)
    robot_pb2_grpc.add_ChargingStationServicer_to_server(servicer, server)
    server.add_insecure_port(f"[::]:{args.port}")
    server.start()

    # ── Advertise via mDNS ────────────────────────────────────────────────
    zc, zc_info = FleetDiscovery.register_station(args.station_id, args.port)

    # Publish initial empty status via DDS
    with servicer.lock:
        servicer._publish_status()

    print(f"[{args.station_id} {_ts()}] ⚡ Charging station running")
    print(f"[{args.station_id} {_ts()}]   gRPC on port {args.port}")
    print(f"[{args.station_id} {_ts()}]   DDS StationStatus on domain {args.domain}")
    print(f"[{args.station_id} {_ts()}]   Dock at ({args.dock_x}, {args.dock_y})")

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        print(f"\n[{args.station_id} {_ts()}] Shutting down…")
        zc.unregister_service(zc_info)
        zc.close()
        server.stop(0)
        participant.close()


if __name__ == "__main__":
    main()
