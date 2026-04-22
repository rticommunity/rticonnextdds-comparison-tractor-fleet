#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0
"""
Robot Node — the gRPC approach: Full-Mesh gRPC Streams

A live robot node that maintains server-streaming gRPC connections with
every other robot and with the UI.  Each peer relationship requires four
independent server-streaming RPCs (KinematicState, OperationalState,
Intent, Telemetry) plus a unary RPC for commands.

Architecture:
    RobotNode              – robot state, movement simulation, collision
                             avoidance, charging state machine, peer tracking.
    RobotCommandServicer   – gRPC unary servicer for operator commands
                             (STOP, GOTO, FOLLOW_PATH, RESUME, SET_PATH,
                             CHARGE).
    RobotStreamingServicer – gRPC server-streaming servicer; 5 delivery
                             loops per subscriber (4 state streams +
                             video), each blocking a ThreadPoolExecutor
                             slot.
    FleetDiscovery         – Zeroconf / mDNS for peer + station discovery.

Threading model (N-robot fleet):
    • 1 position-update thread (10 Hz tick loop)
    • 1 status-print thread (every 5 s)
    • 4 × (N−1) outbound streaming-receive threads (one per stream per peer)
    • 4 × (N−1) + 4 × (UI count) inbound delivery-loop threads on the
      gRPC ThreadPoolExecutor
    • 1 connection-management thread per peer (reconnect loop)
    • 1 video render thread per active video client
    → ~22 app threads for a 3-robot fleet + 1 UI

Connection management:
    • Peers discovered via mDNS or --fleet-port-list CLI flag.
    • Each peer spawns a reconnect thread that opens a gRPC channel,
      waits for READY, starts 4 streaming-receive threads, then blocks
      on connectivity changes.
    • If the channel drops (TRANSIENT_FAILURE / SHUTDOWN), all 4 streams
      are torn down and the reconnect loop retries every 3 s.

Charging stations:
    • Discovered via mDNS (FleetDiscovery).  Dock position learned by a
      one-shot gRPC probe (info_only SlotRequest) on first discovery.
    • Slot negotiation uses blocking gRPC unary calls: RequestSlot →
      ConfirmSlot → ReleaseSlot, each requiring a separate channel per
      station.

Compare with the DDS approach (Pure DDS):
    • the DDS approach uses 6 DDS DataReaders total (flat, regardless of fleet
      size) instead of 4 × (N−1) per-peer streaming threads.
    • the DDS approach has zero reconnect loops — DDS SPDP handles discovery
      and the middleware rebuilds routes automatically.
    • the DDS approach uses rti.rpc Requester/SimpleReplier instead of gRPC
      servicers — one send_request() fans out to all stations.
    • the DDS approach thread count is ~9 fixed vs ~22+ that scales with N.

Usage:
    python robot_node.py --id tractor_1 --fleet-port 50051 \\
        [--fleet-port-list 50051,50052,50053] [--same-host]
"""

import grpc
from concurrent import futures
import time
import threading
import argparse
import random
import math
import os
from datetime import datetime
from typing import Dict, List
import sys
import psutil


def _ts():
    """Return a compact HH:MM:SS timestamp for log lines."""
    return datetime.now().strftime("%H:%M:%S")

# Import generated protobuf code
import robot_pb2
import robot_pb2_grpc

# PyBullet 3D video renderer (shared across all approaches)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared'))
from video_renderer import VideoRenderer

# Zeroconf peer discovery (mDNS / DNS-SD)
from fleet_discovery import FleetDiscovery, CMD_SERVICE_TYPE, STREAM_SERVICE_TYPE

# Both charge-related intents — used for guards and movement checks so we
# don't have to enumerate the two values everywhere.
_CHARGE_INTENTS = (
    robot_pb2.INTENT_CHARGE_QUEUE,
    robot_pb2.INTENT_CHARGE_DOCK,
)


class RobotNode:
    """
    A robot node using full-mesh gRPC for all communications.

    Pub-sub (emulated via server-streaming RPCs):
      KinematicState (10 Hz), OperationalState (1 Hz), Intent (1 Hz),
      Telemetry (1 Hz) — each delivered by a per-subscriber delivery loop
      on the gRPC ThreadPoolExecutor.

    Commands:
      Unary gRPC RPC ``SendCommand`` on ``RobotCommandServicer``.

    Video:
      Server-streaming ``RequestVideo`` on ``RobotCommandServicer`` —
      yields JPEG frames rendered by PyBullet at ~5 fps.

    Charging station interaction:
      Blocking unary gRPC calls: ``RequestSlot`` → ``ConfirmSlot`` →
      ``ReleaseSlot``, each requiring a dedicated gRPC channel per station.

    Thread scaling:
      ~4 × (N−1) streaming-receive threads + 4 × (N−1) delivery-loop
      threads + per-peer reconnect threads.  For a 5-robot fleet this
      is ~32+ threads before counting video, charging, and status.

    Compare with the DDS approach:
      • 4 × (N−1) per-peer streams → 6 total DDS DataReaders (flat).
      • Per-peer reconnect loops → zero (DDS SPDP handles discovery).
      • Per-station gRPC channels → one Requester fans out to all.
      • ~32+ threads → ~9 fixed threads regardless of fleet size.
    """
    
    def __init__(self, robot_id: str, port: int, fleet_port_list: str = ""):
        self.robot_id = robot_id
        self.port = port
        self.address = f"localhost:{port}"

        # Build the ordered list of robot IDs for the whole fleet.
        # demo_start.sh names robots "robot1" … "robotN" by port-list order.
        # This is used by the 3D renderer so colours match the 2D UI.
        self._fleet_robot_ids: List[str] = []
        if fleet_port_list:
            for i, _ in enumerate(fleet_port_list.split(","), start=1):
                self._fleet_robot_ids.append(f"robot{i}")
        
        # State
        self.position = {"x": random.uniform(0, 100), "y": random.uniform(0, 100), "z": 0}
        self.status = robot_pb2.STATUS_MOVING   # RobotStatus: STATUS_MOVING, STATUS_HOLDING, or STATUS_IDLE
        self.intent = robot_pb2.INTENT_FOLLOW_PATH   # RobotIntent: FOLLOW_PATH, GOTO (moving+holding), IDLE, CHARGE_*
        self.battery_level = 100.0
        self.goal = None             # {"x": float, "y": float} – set by GOTO command
        self.hold_position = None    # {"x": float, "y": float} – set when arriving at GOTO goal
        self.speed = 2.0             # units per second – set by GOTO command
        self.heading = 0.0           # radians – direction the robot is currently facing
        self.velocity_x = 0.0        # units per second – current x velocity
        self.velocity_y = 0.0        # units per second – current y velocity
        self.velocity_z = 0.0        # units per second – current z velocity
        self.signal_strength = 95.0  # link quality 0–100 % (varies with position on the field)
        
        # Path route – a loop of waypoints the robot cycles through
        self.path_waypoints = self._generate_path_route()
        self.path_index = 0        # current waypoint target
        
        # Known peers (other robots)
        self.peers: Dict[str, str] = {}  # robot_id -> address
        self.peer_connections: Dict[str, grpc.Channel] = {}
        self.peer_streams: Dict[str, Dict] = {}  # robot_id -> {state, intent, telemetry}
        
        # Received data from other robots
        self.other_robots_intent: Dict[str, robot_pb2.IntentUpdate] = {}
        self.other_robots_state: Dict[str, robot_pb2.KinematicState] = {}
        self.other_robots_operational: Dict[str, robot_pb2.OperationalState] = {}
        self._nearby_peers: set = set()  # peer ids currently within avoidance radius
        
        # gRPC server
        self.server = None
        self.running = False
        
        # Locks
        self.peers_lock = threading.Lock()
        self.data_lock = threading.Lock()

        # Video renderer (PyBullet headless 3D) — pass fleet IDs for colour sync
        self.video_renderer = VideoRenderer(robot_id, self._fleet_robot_ids)
        self.active_video_streams = 0

        # Active delivery loops — counts concurrent server-side streaming
        # handlers (one per subscriber per stream type).  Each loop is a
        # blocking `while: yield; sleep` occupying a ThreadPoolExecutor slot.
        # In DDS this number is always 0: the middleware delivers from a
        # single write() call.
        self._delivery_loop_count = 0
        self._delivery_loop_lock = threading.Lock()

        # ── charging state ────────────────────────────────────────────────
        self._charging_station_addresses: List[str] = []   # ["host:port", …]
        self._charging_station_docks: Dict[str, dict] = {} # addr → {"x","y","station_id","active"}
        self._charge_slot_id: str | None = None
        self._charge_station_id: str | None = None
        self._charge_station_address: str | None = None    # gRPC address for release
        self._charge_dock: dict | None = None              # {"x": float, "y": float}
        self._charge_wait_pos: dict | None = None          # hold position while queued
        self._charge_queue_rank: int = -1                   # -1 = not queued
        self._pre_charge_intent = None                      # stashed intent before charging
        self._pre_charge_status = None
        self._pre_charge_speed: float = 2.0
        self._pre_charge_goal: dict | None = None          # stashed GOTO goal
        self._pre_charge_hold: dict | None = None          # stashed hold position (GOTO arrival)
        self._charge_requested: bool = False                # set by CMD_CHARGE or auto-trigger
        self.CHARGE_THRESHOLD = 20.0                        # auto-charge below this %
        self.CHARGE_FULL = 95.0                             # stop charging at this %
        self.CHARGE_RATE = 2.0                              # %/s while docked

        # ── Pre-idle state (saved by CMD_STOP, restored by CMD_RESUME) ────
        self._pre_idle_status = None

        # ── Coverage tracking ─────────────────────────────────────────────────
        self._cov_seq: int = 0
        self._last_cov_x: float = float('nan')
        self._last_cov_y: float = float('nan')
        self._cov_working: bool = False
        self._coverage_points: list = []  # list of robot_pb2.CoveragePoint

    def _enter_delivery_loop(self):
        """Increment the active delivery loop counter (called on servicer entry)."""
        with self._delivery_loop_lock:
            self._delivery_loop_count += 1

    def _exit_delivery_loop(self):
        """Decrement the active delivery loop counter (called on servicer exit)."""
        with self._delivery_loop_lock:
            self._delivery_loop_count = max(0, self._delivery_loop_count - 1)

    @property
    def delivery_loop_count(self):
        with self._delivery_loop_lock:
            return self._delivery_loop_count

    # ── charging station interaction ─────────────────────────────────────

    def register_charging_station(self, address: str):
        """Record a charging station address and probe for its dock position.

        Called when Zeroconf/mDNS discovers a charging station on the
        network.  Appends the address and spawns a background thread to
        probe the station's dock coordinates via gRPC.

        Compare with the DDS approach: no explicit registration — DDS SPDP
        discovers charging stations and the robot's StationStatus
        DataReader receives dock positions automatically.
        """
        self._charging_station_addresses.append(address)
        print(f"[{self.robot_id} {_ts()}] Registered charging station at {address}")
        # Probe the station in the background to learn its dock position
        threading.Thread(target=self._probe_station_dock, args=(address,),
                         daemon=True).start()

    def _probe_station_dock(self, address: str):
        """Send an info-only gRPC request to learn the station's dock position.

        Opens a gRPC channel, sends ``RequestSlot(info_only=True)``, and
        caches the dock coordinates in ``_charging_station_docks`` for use
        by the 3D video renderer.  This is a **one-shot probe** — if the
        station restarts after the probe, the cached position is stale.

        Compare with the DDS approach: the robot subscribes to the
        ``StationStatus`` DDS topic (TRANSIENT_LOCAL durability).
        Station dock positions arrive automatically — no probe needed,
        late-joining stations deliver their last-known state, and position
        updates are received continuously.
        """
        try:
            channel = grpc.insecure_channel(address)
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = robot_pb2_grpc.ChargingStationStub(channel)
            offer = stub.RequestSlot(
                robot_pb2.SlotRequest(
                    robot_id=self.robot_id,
                    battery_level=100.0,
                    info_only=True,
                ),
                timeout=5,
            )
            channel.close()
            self._charging_station_docks[address] = {
                "x": offer.dock_x,
                "y": offer.dock_y,
                "station_id": offer.station_id,
                "active": False,
            }
            print(f"[{self.robot_id} {_ts()}] Probed station {offer.station_id} "
                  f"dock=({offer.dock_x:.0f},{offer.dock_y:.0f})")
        except Exception as e:
            print(f"[{self.robot_id} {_ts()}] Station probe failed at {address}: {e}")

    def _initiate_charge(self):
        """Begin the charging workflow via blocking gRPC unary calls.

        Iterates over all known charging station addresses, opens a
        separate gRPC channel to each one, sends a ``RequestSlot`` RPC,
        collects offers, picks the best (lowest rank, then lowest wait
        time), confirms with ``ConfirmSlot``, and navigates to the dock.

        Each station requires its own ``insecure_channel()`` →
        ``channel_ready_future()`` → stub call — N stations = N channels.

        Compare with the DDS approach: one ``slot_requester.send_request()``
        fans out to ALL stations via DDS multicast.  No per-station
        channels, no ``channel_ready_future()``, no reconnect handling.

        Callers MUST set ``_charge_requested = True`` before spawning the
        thread that invokes this method (acts as a simple mutex so only one
        charge attempt can be in flight at a time).
        """
        if self.intent in _CHARGE_INTENTS:
            self._charge_requested = False   # reset so future attempts aren't blocked
            return  # already navigating / queued / docked

        # Stash current state so we can resume after charging
        self._pre_charge_intent = self.intent
        self._pre_charge_status = self.status
        self._pre_charge_speed = self.speed
        self._pre_charge_goal = dict(self.goal) if self.goal else None
        self._pre_charge_hold = dict(self.hold_position) if self.hold_position else None
        self._cov_working = False  # stop recording coverage during charge cycle

        print(f"[{self.robot_id} {_ts()}] ⚡ Battery {self.battery_level:.0f}% — requesting charge slot")

        # Fan out RequestSlot to all known stations, collect all offers
        offers: list[tuple] = []  # (offer, address)
        for addr in self._charging_station_addresses:
            try:
                channel = grpc.insecure_channel(addr)
                grpc.channel_ready_future(channel).result(timeout=3)
                stub = robot_pb2_grpc.ChargingStationStub(channel)
                offer = stub.RequestSlot(
                    robot_pb2.SlotRequest(
                        robot_id=self.robot_id,
                        battery_level=self.battery_level,
                    ),
                    timeout=5,
                )
                channel.close()
                offers.append((offer, addr))
            except Exception as e:
                print(f"[{self.robot_id} {_ts()}] Failed to reach station at {addr}: {e}")

        if not offers:
            print(f"[{self.robot_id} {_ts()}] ✗ No charging station reachable — staying on task")
            self._charge_requested = False
            return

        # Pick the offer with the shortest wait time; break ties by distance
        def _score(t):
            offer = t[0]
            dx = offer.dock_x - self.position["x"]
            dy = offer.dock_y - self.position["y"]
            dist = math.sqrt(dx * dx + dy * dy)
            return (offer.wait_time_s, dist)

        offers.sort(key=_score)
        best_offer, best_address = offers[0]
        rejected = offers[1:]  # offers we won't use

        # Immediately release rejected offers so they don't linger
        for rej_offer, rej_addr in rejected:
            try:
                ch = grpc.insecure_channel(rej_addr)
                grpc.channel_ready_future(ch).result(timeout=2)
                st = robot_pb2_grpc.ChargingStationStub(ch)
                st.ReleaseSlot(robot_pb2.SlotRelease(
                    robot_id=self.robot_id,
                    slot_id=rej_offer.slot_id,
                ), timeout=3)
                ch.close()
                print(f"[{self.robot_id} {_ts()}] Released rejected offer on {rej_offer.station_id}")
            except Exception:
                pass  # best-effort; station will expire it eventually

        # Confirm the best slot
        try:
            channel = grpc.insecure_channel(best_address)
            grpc.channel_ready_future(channel).result(timeout=3)
            stub = robot_pb2_grpc.ChargingStationStub(channel)
            assignment = stub.ConfirmSlot(
                robot_pb2.SlotConfirm(
                    robot_id=self.robot_id,
                    slot_id=best_offer.slot_id,
                ),
                timeout=5,
            )
            channel.close()
        except Exception as e:
            print(f"[{self.robot_id} {_ts()}] ✗ Failed to confirm slot: {e}")
            self._charge_requested = False
            return

        if not assignment.granted:
            print(f"[{self.robot_id} {_ts()}] ✗ Slot not granted")
            self._charge_requested = False
            return

        # Store charge state
        self._charge_slot_id = best_offer.slot_id
        self._charge_station_id = best_offer.station_id
        self._charge_dock = {"x": assignment.dock_x, "y": assignment.dock_y}
        self._charge_wait_pos = {"x": assignment.wait_x, "y": assignment.wait_y}
        self._charge_queue_rank = assignment.queue_rank
        self._charge_station_address = best_address

        # Navigate to the station (dock if rank 0, otherwise wait position)
        if assignment.queue_rank == 0:
            self.goal = dict(self._charge_dock)
            self.intent = robot_pb2.INTENT_CHARGE_DOCK
            print(f"[{self.robot_id} {_ts()}] ⚡ Heading to dock at ({self.goal['x']:.0f},{self.goal['y']:.0f})")
        else:
            self.goal = dict(self._charge_wait_pos)
            self.intent = robot_pb2.INTENT_CHARGE_QUEUE
            print(f"[{self.robot_id} {_ts()}] ⚡ Heading to wait position rank={assignment.queue_rank}")

        self.status = robot_pb2.STATUS_MOVING
        self.hold_position = None
        self.speed = 3.0  # move briskly to the station

        # Start polling immediately for queued robots — don't wait for
        # arrival.  The dock may free up while we're still en route, and
        # if avoidance prevents reaching the wait position we'd be stuck
        # forever without a poll thread.
        if self.intent == robot_pb2.INTENT_CHARGE_QUEUE:
            threading.Thread(
                target=self._poll_charge_queue, daemon=True,
            ).start()

    def _poll_charge_queue(self):
        """Background thread: re-check queue rank every 3 s while waiting.

        When the rank drops to 0 (dock is free for us), navigate to the dock.
        Uses the idempotent ConfirmSlot RPC — already-confirmed slots simply
        return the updated rank.
        """
        while self.running:
            time.sleep(3)

            # Only poll while we're still in the charge workflow
            if not (self.intent in _CHARGE_INTENTS
                    and self.status in (robot_pb2.STATUS_HOLDING,
                                        robot_pb2.STATUS_MOVING)
                    and self._charge_slot_id
                    and self._charge_station_address):
                return  # no longer waiting — exit thread

            try:
                channel = grpc.insecure_channel(self._charge_station_address)
                grpc.channel_ready_future(channel).result(timeout=3)
                stub = robot_pb2_grpc.ChargingStationStub(channel)
                assignment = stub.ConfirmSlot(
                    robot_pb2.SlotConfirm(
                        robot_id=self.robot_id,
                        slot_id=self._charge_slot_id,
                    ),
                    timeout=5,
                )
                channel.close()
            except Exception as e:
                print(f"[{self.robot_id} {_ts()}] ⚡ Queue poll failed: {e}")
                continue

            if not assignment.granted:
                # Slot was lost (expired / cancelled) — give up
                print(f"[{self.robot_id} {_ts()}] ⚡ Slot lost — aborting charge")
                self._charge_requested = False
                self.intent = self._pre_charge_intent or robot_pb2.INTENT_FOLLOW_PATH
                self.status = robot_pb2.STATUS_MOVING
                self.speed = self._pre_charge_speed
                self.goal = self._pre_charge_goal
                self.hold_position = None
                return

            old_rank = self._charge_queue_rank
            self._charge_queue_rank = assignment.queue_rank

            if assignment.queue_rank == 0:
                # Our turn — head to the dock!
                self.goal = {"x": assignment.dock_x, "y": assignment.dock_y}
                self.hold_position = None
                self.intent = robot_pb2.INTENT_CHARGE_DOCK
                self.status = robot_pb2.STATUS_MOVING
                print(f"[{self.robot_id} {_ts()}] ⚡ Queue advanced → heading to dock")
                return  # exit poll thread; arrival logic handles the rest
            elif assignment.queue_rank != old_rank:
                # Rank changed but not yet 0 — move to updated wait position
                self.goal = {"x": assignment.wait_x, "y": assignment.wait_y}
                self.hold_position = None
                # Intent stays INTENT_CHARGE_QUEUE (still waiting)
                self.status = robot_pb2.STATUS_MOVING
                print(f"[{self.robot_id} {_ts()}] ⚡ Queue rank {old_rank} → {assignment.queue_rank}")

    def _abort_charge(self):
        """Cancel an in-progress charge (navigating, queued, or docked).

        Called when the operator overrides with GOTO / FOLLOW_PATH / STOP while
        the robot has an active charge session.  Always uses ReleaseSlot —
        the station doesn't need to know the reason.

        Does NOT restore intent/status/goal — the caller is responsible
        for setting those (since the caller already knows the new intent).
        """
        if self._charge_slot_id and self._charge_station_address:
            try:
                channel = grpc.insecure_channel(self._charge_station_address)
                grpc.channel_ready_future(channel).result(timeout=3)
                stub = robot_pb2_grpc.ChargingStationStub(channel)
                stub.ReleaseSlot(
                    robot_pb2.SlotRelease(
                        robot_id=self.robot_id,
                        slot_id=self._charge_slot_id,
                    ),
                    timeout=5,
                )
                channel.close()
                print(f"[{self.robot_id} {_ts()}] ⚡ Charge aborted (slot released)")
            except Exception as e:
                print(f"[{self.robot_id} {_ts()}] Warning: ReleaseSlot failed: {e}")

        # Clear all charge state
        self._charge_slot_id = None
        self._charge_station_id = None
        self._charge_station_address = None
        self._charge_dock = None
        self._charge_wait_pos = None
        self._charge_queue_rank = -1
        self._charge_requested = False
        self._pre_charge_intent = None
        self._pre_charge_status = None
        self._pre_charge_speed = 2.0
        self._pre_charge_goal = None
        self._pre_charge_hold = None

    def _release_charge_slot(self):
        """Release the slot at the charging station and resume prior task."""
        if self._charge_slot_id and self._charge_station_address:
            try:
                channel = grpc.insecure_channel(self._charge_station_address)
                grpc.channel_ready_future(channel).result(timeout=3)
                stub = robot_pb2_grpc.ChargingStationStub(channel)
                stub.ReleaseSlot(
                    robot_pb2.SlotRelease(
                        robot_id=self.robot_id,
                        slot_id=self._charge_slot_id,
                    ),
                    timeout=5,
                )
                channel.close()
            except Exception as e:
                print(f"[{self.robot_id} {_ts()}] Warning: failed to release slot: {e}")

        print(f"[{self.robot_id} {_ts()}] ⚡ Charging complete ({self.battery_level:.0f}%) — resuming")

        # Restore pre-charge state.
        # INTENT_GOTO + STATUS_HOLDING (arrived at GOTO destination) was
        # stashed with goal=None and hold_position set.  Re-issue as a
        # GOTO so the robot navigates back to the original hold spot.
        if (self._pre_charge_intent == robot_pb2.INTENT_GOTO
                and self._pre_charge_goal is None
                and self._pre_charge_hold is not None):
            self.intent = robot_pb2.INTENT_GOTO
            self.goal = dict(self._pre_charge_hold)
            self.hold_position = None
            self.status = robot_pb2.STATUS_MOVING
            self.speed = self._pre_charge_speed
            print(f"[{self.robot_id} {_ts()}] Returning to hold position "
                  f"({self.goal['x']:.0f},{self.goal['y']:.0f})")
        else:
            self.intent = self._pre_charge_intent or robot_pb2.INTENT_FOLLOW_PATH
            self.status = robot_pb2.STATUS_MOVING
            self.speed = self._pre_charge_speed
            self.goal = self._pre_charge_goal      # restore GOTO target (None for FOLLOW_PATH)
            self.hold_position = None

        # Clear charge state
        self._charge_slot_id = None
        self._charge_station_id = None
        self._charge_station_address = None
        self._charge_dock = None
        self._charge_wait_pos = None
        self._charge_queue_rank = -1
        self._charge_requested = False
        self._pre_charge_intent = None
        self._pre_charge_status = None
        self._pre_charge_speed = 2.0
        self._pre_charge_goal = None
        self._pre_charge_hold = None
        # Coverage must re-earn _cov_working by reaching a waypoint
        self._cov_working = False
        self._last_cov_x = float('nan')
        self._last_cov_y = float('nan')

    # ── path route generation ──────────────────────────────────────────
    def _generate_path_route(self):
        """Generate a path route – a loop of waypoints that covers an area.

        Each robot gets a slightly different route seeded by its id so
        they naturally spread out across the arena.
        """
        seed = hash(self.robot_id) & 0xFFFFFFFF
        rng = random.Random(seed)

        # Pick a "home" quadrant based on the seed so robots spread out
        cx = rng.uniform(20, 80)
        cy = rng.uniform(20, 80)
        radius = rng.uniform(15, 30)

        # Build a roughly circular path with 5-8 waypoints + some jitter
        n_pts = rng.randint(5, 8)
        waypoints = []
        for i in range(n_pts):
            angle = 2 * math.pi * i / n_pts
            jx = rng.uniform(-5, 5)
            jy = rng.uniform(-5, 5)
            wx = max(5, min(95, cx + radius * math.cos(angle) + jx))
            wy = max(5, min(95, cy + radius * math.sin(angle) + jy))
            waypoints.append({"x": wx, "y": wy})

        return waypoints

    @property
    def path_target(self):
        """The waypoint the robot is currently heading toward on its path."""
        return self.path_waypoints[self.path_index]
        
    def start_server(self):
        """Start the gRPC server.

        The thread pool must be large enough to hold all concurrent
        server-streaming handlers *plus* headroom for unary RPCs
        (SendCommand).  Each peer occupies 4 server-side
        stream threads (KinematicState, OperationalState, Intent, Telemetry).
        With N fleet members that's (N-1)*4 streaming threads per peer plus
        the UI's 4 streams; we add extra for commands and video.
        """
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=50))
        robot_pb2_grpc.add_RobotCommandServicer_to_server(
            RobotCommandServicer(self), self.server
        )
        robot_pb2_grpc.add_RobotStreamingServicer_to_server(
            RobotStreamingServicer(self), self.server
        )
        self.server.add_insecure_port(f'[::]:{self.port}')
        self.server.start()
        self.running = True
        print(f"[{self.robot_id} {_ts()}] Server started on {self.address}")
        
    def register_peer(self, address: str):
        """Register a peer robot by address and establish connection.

        Spawns a background thread that opens a gRPC channel, waits for
        it to become READY, and starts 4 streaming-receive threads.
        The peer's robot_id is discovered automatically from the first
        message received on the server-streaming subscriptions.

        Compare with the DDS approach: no equivalent is needed — DDS SPDP
        discovers all participants automatically and the middleware
        routes data without any per-peer setup.
        """
        # Skip if this is our own address
        if address == self.address:
            return

        with self.peers_lock:
            # Use address as temporary key until we learn the real robot_id
            if address in [v for v in self.peers.values()]:
                return

            placeholder = f"pending-{address}"
            self.peers[placeholder] = address
            print(f"[{self.robot_id} {_ts()}] Connecting to peer at {address}…")

            threading.Thread(
                target=self._connect_to_peer,
                args=(placeholder, address),
                daemon=True,
            ).start()
    
    def _connect_to_peer(self, peer_key: str, address: str):
        """Establish gRPC streams with a peer robot (with reconnection).

        Opens a gRPC channel to *address*, waits for READY (5 s timeout),
        then spawns 4 streaming-receive threads (kinematic, operational,
        intent, telemetry).  After setup, blocks on channel connectivity
        changes — if the channel drops, tears everything down and retries
        every 3 s.

        *peer_key* starts as a placeholder; once we learn the real robot_id
        from the first state message we promote the key in our dicts.

        Compare with the DDS approach: DDS has no equivalent — the middleware
        handles connection setup, monitoring, and failover internally.
        There are no per-peer threads and no reconnect loops.
        """
        while self.running:
            try:
                channel = grpc.insecure_channel(address)

                try:
                    grpc.channel_ready_future(channel).result(timeout=5)
                except grpc.FutureTimeoutError:
                    print(f"[{self.robot_id} {_ts()}] Timeout connecting to {address}, retrying in 3s…")
                    time.sleep(3)
                    continue

                stub = robot_pb2_grpc.RobotStreamingStub(channel)

                with self.peers_lock:
                    self.peer_connections[peer_key] = channel
                    self.peer_streams[peer_key] = {}

                # Start server-streaming subscriptions
                self._start_kinematic_stream(peer_key, stub)
                self._start_operational_stream(peer_key, stub)
                self._start_intent_stream(peer_key, stub)
                self._start_telemetry_stream(peer_key, stub)

                print(f"[{self.robot_id} {_ts()}] Connected to peer at {address} (4 streams)")

                # Wait until the channel drops or we are shutting down.
                # Use subscribe() to get notified of connectivity changes.
                connectivity_event = threading.Event()

                def _on_connectivity_change(new_state):
                    if new_state in (grpc.ChannelConnectivity.TRANSIENT_FAILURE,
                                     grpc.ChannelConnectivity.SHUTDOWN):
                        connectivity_event.set()

                channel.subscribe(_on_connectivity_change)
                try:
                    while self.running and not connectivity_event.is_set():
                        connectivity_event.wait(timeout=2)
                finally:
                    channel.unsubscribe(_on_connectivity_change)

                raise Exception("channel lost")

            except Exception as e:
                with self.peers_lock:
                    self.peer_connections.pop(peer_key, None)
                    self.peer_streams.pop(peer_key, None)
                if not self.running:
                    return
                print(f"[{self.robot_id} {_ts()}] Lost connection to {address}: {e}  — reconnecting in 3s…")
                time.sleep(3)
    
    def _start_kinematic_stream(self, robot_id: str, stub):
        """Subscribe to peer's kinematic state stream (10 Hz).

        Spawns a daemon thread that opens a server-streaming RPC to the
        peer and stores each received KinematicState in
        ``other_robots_state``.  One thread per peer per data type.

        Compare with the DDS approach: a single DDS DataReader with a listener
        callback replaces N of these threads — the middleware delivers
        samples from every writer in the domain automatically.
        """
        def receive_kinematic():
            try:
                req = robot_pb2.SubscribeRequest(subscriber_id=self.robot_id)
                for ks in stub.StreamKinematicState(req):
                    if not self.running:
                        return
                    with self.data_lock:
                        self.other_robots_state[ks.robot_id] = ks
            except grpc.RpcError:
                pass  # expected on shutdown
            except Exception as e:
                if self.running:
                    print(f"[{self.robot_id} {_ts()}] Kinematic stream from {robot_id} ended: {e}")

        try:
            threading.Thread(target=receive_kinematic, daemon=True).start()
        except Exception as e:
            print(f"[{self.robot_id} {_ts()}] Error starting kinematic stream with {robot_id}: {e}")

    def _start_operational_stream(self, robot_id: str, stub):
        """Subscribe to peer's operational state stream (1 Hz).

        Same pattern as ``_start_kinematic_stream`` — one daemon thread
        per peer.  In the DDS approach this is a single DDS DataReader.
        """
        def receive_operational():
            try:
                req = robot_pb2.SubscribeRequest(subscriber_id=self.robot_id)
                for os_msg in stub.StreamOperationalState(req):
                    if not self.running:
                        return
                    with self.data_lock:
                        self.other_robots_operational[os_msg.robot_id] = os_msg
            except grpc.RpcError:
                pass  # expected on shutdown
            except Exception as e:
                if self.running:
                    print(f"[{self.robot_id} {_ts()}] Operational stream from {robot_id} ended: {e}")

        try:
            threading.Thread(target=receive_operational, daemon=True).start()
        except Exception as e:
            print(f"[{self.robot_id} {_ts()}] Error starting operational stream with {robot_id}: {e}")
    
    def _start_intent_stream(self, robot_id: str, stub):
        """Subscribe to peer's intent stream (server-streaming).

        Same pattern as ``_start_kinematic_stream`` — one daemon thread
        per peer.  In the DDS approach this is a single DDS DataReader.
        """
        def receive_intents():
            try:
                req = robot_pb2.SubscribeRequest(subscriber_id=self.robot_id)
                for intent in stub.StreamIntent(req):
                    if not self.running:
                        return
                    with self.data_lock:
                        self.other_robots_intent[intent.robot_id] = intent
            except grpc.RpcError:
                pass  # expected on shutdown
            except Exception as e:
                if self.running:
                    print(f"[{self.robot_id} {_ts()}] Intent stream from {robot_id} ended: {e}")
        
        try:
            threading.Thread(target=receive_intents, daemon=True).start()
        except Exception as e:
            print(f"[{self.robot_id} {_ts()}] Error starting intent stream with {robot_id}: {e}")
    
    def _start_telemetry_stream(self, robot_id: str, stub):
        """Subscribe to peer's telemetry stream (server-streaming).

        Same pattern as ``_start_kinematic_stream`` — one daemon thread
        per peer.  In the DDS approach this is a single DDS DataReader.
        """
        def receive_telemetry():
            try:
                req = robot_pb2.SubscribeRequest(subscriber_id=self.robot_id)
                for telemetry in stub.StreamTelemetry(req):
                    if not self.running:
                        return
                    pass  # silently update — logged in status report
            except grpc.RpcError:
                pass  # expected on shutdown
            except Exception as e:
                if self.running:
                    print(f"[{self.robot_id} {_ts()}] Telemetry stream from {robot_id} ended: {e}")
        
        try:
            threading.Thread(target=receive_telemetry, daemon=True).start()
        except Exception as e:
            print(f"[{self.robot_id} {_ts()}] Error starting telemetry stream with {robot_id}: {e}")
    
    # ── collision avoidance ────────────────────────────────────────────────
    AVOIDANCE_RADIUS = 12.0   # start steering when a peer is this close
    AVOIDANCE_STRENGTH = 4.0  # base repulsion speed at the boundary
    MIN_SEPARATION = 4.0      # hard floor — override goal velocity below this

    @property
    def _is_docking(self) -> bool:
        """True when this robot is actively navigating to the dock (rank 0)."""
        return (self.intent == robot_pb2.INTENT_CHARGE_DOCK
                and self.goal is not None)

    def _compute_avoidance(self):
        """Return a (dx, dy) repulsion vector from nearby peers.

        Uses an **inverse-square** falloff so that repulsion grows without
        bound as distance shrinks, making collisions physically impossible
        regardless of goal speed:

            strength = AVOIDANCE_STRENGTH × (AVOIDANCE_RADIUS / dist)²

        If any peer is closer than MIN_SEPARATION the caller should skip
        the goal velocity entirely and only apply the avoidance vector.

        **Docking priority** — uses the explicit ``INTENT_CHARGE_DOCK`` /
        ``INTENT_CHARGE_QUEUE`` intents broadcast by each robot:

        * *Docking robot* (``INTENT_CHARGE_DOCK``, navigating to dock):
          shrunk avoidance radius and damped repulsion so goal-attraction
          toward the dock dominates.  ``hard_override`` is never set.
        * *Waiting-at-station robot* (``INTENT_CHARGE_QUEUE``): weakened
          avoidance so it is easily pushed aside.  ``hard_override`` is
          never set.  Additionally, if a peer broadcasts
          ``INTENT_CHARGE_DOCK`` the waiting robot triples repulsion
          from that peer, actively clearing the docking path.
        * *All other robots*: full avoidance radius and strength.
        """
        ax, ay = 0.0, 0.0
        hard_override = False
        mx, my = self.position["x"], self.position["y"]
        docking = self._is_docking

        # Determine which avoidance regime this robot is in.
        near_station = (
            self.intent in _CHARGE_INTENTS
            and self._charge_dock is not None
        )

        if docking:
            # Rank 0, heading to dock — shrink bubble, dampen repulsion
            eff_radius = self.AVOIDANCE_RADIUS * 0.4   # 4.8 instead of 12
            eff_strength = self.AVOIDANCE_STRENGTH * 0.3
        elif near_station:
            # Waiting / shuffling near station — be easy to push aside
            eff_radius = self.AVOIDANCE_RADIUS * 0.5
            eff_strength = self.AVOIDANCE_STRENGTH * 0.25
        else:
            eff_radius = self.AVOIDANCE_RADIUS
            eff_strength = self.AVOIDANCE_STRENGTH

        with self.data_lock:
            peers = {
                rid: (s.position.x, s.position.y)
                for rid, s in self.other_robots_state.items()
                if rid != self.robot_id
            }
            # Identify peers broadcasting INTENT_CHARGE_DOCK — these are
            # the robots assigned rank 0, actively heading to the dock.
            # Unlike the old STATUS_MOVING check, this is unambiguous:
            # only the docking robot ever sets INTENT_CHARGE_DOCK.
            docking_peers: set = set()
            if near_station and not docking:
                for rid in peers:
                    intent_msg = self.other_robots_intent.get(rid)
                    if (intent_msg
                            and intent_msg.intent_type == robot_pb2.INTENT_CHARGE_DOCK):
                        docking_peers.add(rid)

        for rid, (px, py) in peers.items():
            dx = mx - px
            dy = my - py
            dist = math.sqrt(dx * dx + dy * dy)

            was_nearby = rid in self._nearby_peers
            is_nearby = dist < eff_radius
            # Hysteresis: enter at eff_radius, clear at 115% to avoid
            # log flicker when peers hover near the boundary.
            is_clear = dist > eff_radius * 1.15

            if is_nearby and not was_nearby:
                print(f"[{self.robot_id} {_ts()}] ⚠ Peer {rid} nearby ({dist:.1f} units) — avoiding")
                self._nearby_peers.add(rid)
            elif is_clear and was_nearby:
                print(f"[{self.robot_id} {_ts()}] ✓ Peer {rid} clear ({dist:.1f} units)")
                self._nearby_peers.discard(rid)

            if is_nearby and dist > 0.01:
                # Inverse-square repulsion using effective parameters
                ratio = eff_radius / dist
                strength = eff_strength * ratio * ratio

                # Yield to a docking peer: they broadcast INTENT_CHARGE_DOCK
                # which is unambiguous.  Triple repulsion clears their path.
                if near_station and rid in docking_peers:
                    strength *= 3.0

                ax += (dx / dist) * strength
                ay += (dy / dist) * strength

                # hard_override makes the robot abandon its goal and flee.
                # Never do this for robots involved in charging — docking
                # robots must keep pushing toward the dock, and waiting
                # robots rely on the hold-position spring to drift back.
                if dist < self.MIN_SEPARATION and not near_station and not docking:
                    hard_override = True

        return ax, ay, hard_override

    def update_position(self):
        """Simulate robot movement based on intent and status.

        Each tick, the robot computes a desired velocity toward its goal
        (GOTO target or next path waypoint) and blends in a repulsion
        vector from nearby peers to avoid collisions.
        """
        dt = 0.1  # seconds per tick
        while self.running:
            if self.status == robot_pb2.STATUS_MOVING:
                # Start with zero desired velocity
                vx, vy = 0.0, 0.0

                if self.intent in (robot_pb2.INTENT_GOTO, *_CHARGE_INTENTS) and self.goal is not None:
                    # Navigate toward goal (GOTO target or charging dock)
                    dx = self.goal["x"] - self.position["x"]
                    dy = self.goal["y"] - self.position["y"]
                    dist = (dx**2 + dy**2) ** 0.5

                    if dist <= self.speed * dt:
                        # Arrived at goal
                        self.position["x"] = self.goal["x"]
                        self.position["y"] = self.goal["y"]
                        # Preserve arrival heading; zero velocity
                        if dist > 0.01:
                            self.heading = math.atan2(dy, dx)
                        self.velocity_x = 0.0
                        self.velocity_y = 0.0

                        if self.intent == robot_pb2.INTENT_CHARGE_DOCK:
                            # At the dock → start charging
                            print(f"[{self.robot_id} {_ts()}] Arrived at charging dock → CHARGING")
                            self.goal = None
                            self.status = robot_pb2.STATUS_CHARGING
                        elif self.intent == robot_pb2.INTENT_CHARGE_QUEUE:
                            # At wait position; the poll thread (spawned in
                            # _initiate_charge) will keep checking the queue
                            # and advance us to the dock when our rank hits 0.
                            print(f"[{self.robot_id} {_ts()}] Arrived at charge wait pos (rank {self._charge_queue_rank})")
                            self.hold_position = {"x": self.goal["x"], "y": self.goal["y"]}
                            self.goal = None
                            self.status = robot_pb2.STATUS_HOLDING
                        else:
                            # Normal GOTO → hold position (intent stays GOTO)
                            print(f"[{self.robot_id} {_ts()}] Reached goal ({self.goal['x']:.1f}, {self.goal['y']:.1f}) → HOLDING")
                            self.hold_position = {"x": self.goal["x"], "y": self.goal["y"]}
                            self.goal = None
                            # Intent stays INTENT_GOTO — status tells us
                            # we've arrived (HOLDING) vs en route (MOVING).
                            self.status = robot_pb2.STATUS_HOLDING
                    else:
                        vx = dx / dist * self.speed
                        vy = dy / dist * self.speed

                elif self.intent == robot_pb2.INTENT_FOLLOW_PATH:
                    # Navigate toward the current path waypoint
                    wp = self.path_target
                    dx = wp["x"] - self.position["x"]
                    dy = wp["y"] - self.position["y"]
                    dist = (dx**2 + dy**2) ** 0.5

                    if dist <= self.speed * dt:
                        # Arrived at waypoint → advance to next one
                        self.position["x"] = wp["x"]
                        self.position["y"] = wp["y"]
                        self.path_index = (self.path_index + 1) % len(self.path_waypoints)
                        self._cov_working = True  # reached a waypoint — coverage starts
                        # Immediately aim toward the NEW next waypoint so the
                        # reported velocity/heading is the upcoming travel
                        # direction rather than zero (which lets avoidance
                        # alone dictate an erratic heading for one tick).
                        nwp = self.path_target
                        ndx = nwp["x"] - self.position["x"]
                        ndy = nwp["y"] - self.position["y"]
                        ndist = (ndx**2 + ndy**2) ** 0.5
                        if ndist > 0.01:
                            vx = ndx / ndist * self.speed
                            vy = ndy / ndist * self.speed
                    else:
                        vx = dx / dist * self.speed
                        vy = dy / dist * self.speed

                # Blend in collision avoidance from peer state data
                if self.status == robot_pb2.STATUS_MOVING:
                    avoid_x, avoid_y, hard_override = self._compute_avoidance()
                    if hard_override:
                        # Peer dangerously close — abandon goal, pure repulsion
                        vx, vy = avoid_x, avoid_y
                    else:
                        vx += avoid_x
                        vy += avoid_y

                    # Apply combined velocity
                    self.position["x"] += vx * dt
                    self.position["y"] += vy * dt
                    # Update heading and stored velocity from velocity direction
                    self.velocity_x = vx
                    self.velocity_y = vy
                    if abs(vx) + abs(vy) > 0.01:
                        self.heading = math.atan2(vy, vx)

                    # ── Coverage point (active path work only) ────────────────
                    if self.intent == robot_pb2.INTENT_FOLLOW_PATH:
                        if self._cov_working:
                            cx, cy = self.position["x"], self.position["y"]
                            dx_c = cx - self._last_cov_x
                            dy_c = cy - self._last_cov_y
                            # NaN comparison is always False → first point always publishes
                            if not (dx_c * dx_c + dy_c * dy_c < 1.0):
                                self._cov_seq += 1
                                pt = robot_pb2.CoveragePoint(
                                    robot_id=self.robot_id,
                                    sequence_number=self._cov_seq,
                                    x=cx, y=cy,
                                    timestamp=int(time.time() * 1000),
                                )
                                self._coverage_points.append(pt)
                                self._last_cov_x = cx
                                self._last_cov_y = cy

            elif self.status == robot_pb2.STATUS_HOLDING:
                # Holding position — no goal velocity, but avoidance is active.
                # A gentle spring pulls the robot back toward its hold point.
                vx, vy = 0.0, 0.0
                if self.hold_position:
                    dx = self.hold_position["x"] - self.position["x"]
                    dy = self.hold_position["y"] - self.position["y"]
                    dist = math.sqrt(dx * dx + dy * dy)
                    if dist > 0.05:
                        # Gentle spring: drift back at 0.5 units/s max
                        spring_speed = min(0.5, dist)
                        vx = dx / dist * spring_speed
                        vy = dy / dist * spring_speed

                avoid_x, avoid_y, hard_override = self._compute_avoidance()
                if hard_override:
                    vx, vy = avoid_x, avoid_y
                else:
                    vx += avoid_x
                    vy += avoid_y

                self.position["x"] += vx * dt
                self.position["y"] += vy * dt
                # Publish actual velocity (may be zero when parked).
                # Heading is preserved in self.heading, so zeroing
                # velocity won't make the icon spin — the UI falls
                # back to the last known heading when speed ≈ 0.
                self.velocity_x = vx
                self.velocity_y = vy
                if abs(vx) + abs(vy) > 0.01:
                    self.heading = math.atan2(vy, vx)

            # else: STATUS_IDLE → parked, no movement, no avoidance

            elif self.status == robot_pb2.STATUS_CHARGING:
                # Docked at a charging station — refill battery
                self.velocity_x = 0.0
                self.velocity_y = 0.0
                self.battery_level = min(100.0, self.battery_level + self.CHARGE_RATE * dt)
                if self.battery_level >= self.CHARGE_FULL:
                    self._release_charge_slot()

            # else: STATUS_IDLE → parked, no movement, no avoidance

            # Clamp to arena bounds
            self.position["x"] = max(0, min(100, self.position["x"]))
            self.position["y"] = max(0, min(100, self.position["y"]))

            # Simulate battery drain — fixed baseline (electronics, comms) plus
            # a kinetic term proportional to actual speed.
            #   baseline:  0.01 %/tick  =  0.1 %/s  (always on)
            #   kinetic:   0.01 %/tick per u/s  →  at 2 u/s: +0.02 = 0.3 %/s total
            #                                       at 10 u/s: +0.10 = 1.1 %/s total
            # Skip drain when docked at a charging station.
            if self.status != robot_pb2.STATUS_CHARGING:
                actual_speed = math.sqrt(self.velocity_x**2 + self.velocity_y**2)
                drain = 0.01 + 0.01 * actual_speed
                self.battery_level = max(0, self.battery_level - drain)

            # Simulate signal strength — weaker near field edges, stronger
            # near centre.  Adds random jitter to mimic real radio fading.
            cx = abs(self.position["x"] - 50.0)  # distance from centre X
            cy = abs(self.position["y"] - 50.0)  # distance from centre Y
            edge_dist = max(cx, cy)              # 0 at centre, 50 at edge
            base_signal = max(10.0, 100.0 - edge_dist * 1.6)  # 100% at centre → ~20% at edge
            self.signal_strength = max(0.0, min(100.0,
                base_signal + random.uniform(-3.0, 3.0)))

            # Auto-charge trigger: when battery drops below threshold, request
            # a charging slot.  The gRPC calls are blocking, so spawn a thread.
            if (self.battery_level < self.CHARGE_THRESHOLD
                    and not self._charge_requested
                    and self._charging_station_addresses
                    and self.status != robot_pb2.STATUS_CHARGING):
                self._charge_requested = True
                threading.Thread(target=self._initiate_charge, daemon=True).start()

            time.sleep(dt)
    
    def print_status(self):
        """Periodically print a one-line system status summary"""
        prev_status = None
        prev_intent = None
        ticks = 0
        while self.running:
            time.sleep(5)
            ticks += 1
            with self.data_lock:
                with self.peers_lock:
                    conns = len(self.peer_connections)
                    peers = len(self.peers)
                status_name = robot_pb2.RobotStatus.Name(self.status)
                intent_name = robot_pb2.RobotIntent.Name(self.intent)
                x, y = self.position['x'], self.position['y']
                batt = self.battery_level
            changed = (status_name != prev_status or intent_name != prev_intent)
            prev_status = status_name
            prev_intent = intent_name
            if not changed and ticks < 6:
                continue
            ticks = 0
            print(f"[{self.robot_id} {_ts()}] pos=({x:.1f},{y:.1f}) {status_name}/{intent_name} batt={batt:.0f}% peers={conns}/{peers} streams={conns*4}")
    
    def run(self):
        """Start the gRPC server (if needed) and launch background threads.

        Spawns the position-update and status-print threads, then blocks
        on the main loop until Ctrl+C.  Peer connections are established
        separately by ``register_peer()`` calls from the discovery layer.

        Compare with the DDS approach: ``run()`` calls ``initialize_dds()`` which
        creates the participant, all writers/readers, and RPC objects in
        one place — no separate server start or peer registration step.
        """
        if not self.running:
            self.start_server()
        
        # Start background threads
        threading.Thread(target=self.update_position, daemon=True).start()
        threading.Thread(target=self.print_status, daemon=True).start()
        
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            print(f"\n[{self.robot_id} {_ts()}] Shutting down...")
            self.running = False
            self.server.stop(0)


class RobotCommandServicer(robot_pb2_grpc.RobotCommandServicer):
    """gRPC unary servicer for operator commands.

    ``SendCommand`` handles STOP, GOTO, FOLLOW_PATH, RESUME, SET_PATH,
    and CHARGE.

    Compare with the DDS approach: command handling is a ``SimpleReplier``
    callback (``_handle_command``) — same logic, but correlation is
    handled by DDS SampleIdentity instead of HTTP/2 stream IDs.
    """
    
    def __init__(self, robot_node: RobotNode):
        self.robot = robot_node
    
    def SendCommand(self, request, context):
        """Handle an operator command and return a CommandResponse.

        Each command mutates the robot's state (status, intent, goal,
        speed) and returns a synchronous response.  For CHARGE, the
        actual slot negotiation runs in a background thread because the
        gRPC calls to charging stations are blocking.

        Compare with the DDS approach: identical command logic lives in
        ``_handle_command()``, invoked by the ``SimpleReplier``.
        """
        cmd_name = robot_pb2.Command.Name(request.command)
        print(f"[{self.robot.robot_id} {_ts()}] Received command: {cmd_name}")
        
        # Process command
        if request.command == robot_pb2.CMD_STOP:
            # Save current status so RESUME can restore it
            self.robot._pre_idle_status = self.robot.status
            self.robot.status = robot_pb2.STATUS_IDLE
            self.robot.velocity_x = 0.0
            self.robot.velocity_y = 0.0
            return robot_pb2.CommandResponse(
                success=True, description="Idle (paused)",
                resulting_status=self.robot.status,
                resulting_intent=self.robot.intent,
            )
        elif request.command == robot_pb2.CMD_FOLLOW_PATH:
            # If charging or heading to charge, release/cancel the slot first
            if self.robot._charge_slot_id:
                self.robot._abort_charge()
            self.robot.goal = None
            self.robot.hold_position = None
            self.robot.intent = robot_pb2.INTENT_FOLLOW_PATH
            self.robot.status = robot_pb2.STATUS_MOVING
            self.robot._cov_working = False
            self.robot._last_cov_x = float('nan')
            self.robot._last_cov_y = float('nan')
            # Apply speed if provided
            if request.HasField("goto_params") and request.goto_params.speed > 0:
                self.robot.speed = request.goto_params.speed
            return robot_pb2.CommandResponse(
                success=True, description=f"Following path at speed {self.robot.speed:.1f}",
                resulting_status=self.robot.status,
                resulting_intent=self.robot.intent,
            )
        elif request.command == robot_pb2.CMD_GOTO:
            # If charging or heading to charge, release/cancel the slot first
            if self.robot._charge_slot_id:
                self.robot._abort_charge()
            gp = request.goto_params
            gx = gp.x
            gy = gp.y
            spd = gp.speed if gp.speed > 0 else 2.0
            self.robot.goal = {"x": gx, "y": gy}
            self.robot.hold_position = None
            self.robot.speed = spd
            self.robot.intent = robot_pb2.INTENT_GOTO
            self.robot.status = robot_pb2.STATUS_MOVING
            return robot_pb2.CommandResponse(
                success=True,
                description=f"Moving to ({gx}, {gy}) at speed {spd}",
                resulting_status=self.robot.status,
                resulting_intent=self.robot.intent,
            )
        elif request.command == robot_pb2.CMD_RESUME:
            # Resume from IDLE or HOLDING.
            if request.HasField("goto_params") and request.goto_params.speed > 0:
                self.robot.speed = request.goto_params.speed
            if self.robot._pre_idle_status is not None:
                # Restore whatever status we had before STOP
                self.robot.status = self.robot._pre_idle_status
                self.robot._pre_idle_status = None
            elif self.robot.intent == robot_pb2.INTENT_FOLLOW_PATH:
                self.robot.hold_position = None
                self.robot.status = robot_pb2.STATUS_MOVING
            elif self.robot.intent == robot_pb2.INTENT_GOTO:
                if self.robot.goal is not None:
                    # Still en route — just resume moving
                    self.robot.hold_position = None
                    self.robot.status = robot_pb2.STATUS_MOVING
                elif self.robot.hold_position is not None:
                    # Arrived — re-navigate to the hold position
                    self.robot.goal = dict(self.robot.hold_position)
                    self.robot.hold_position = None
                    self.robot.status = robot_pb2.STATUS_MOVING
                else:
                    # GOTO with nowhere to go — hold where we are
                    self.robot.hold_position = {
                        "x": self.robot.position["x"],
                        "y": self.robot.position["y"],
                    }
                    self.robot.status = robot_pb2.STATUS_HOLDING
            elif self.robot.intent in _CHARGE_INTENTS:
                if self.robot.goal is not None:
                    self.robot.status = robot_pb2.STATUS_MOVING
                elif self.robot.hold_position is not None:
                    self.robot.status = robot_pb2.STATUS_HOLDING
                else:
                    self.robot.status = robot_pb2.STATUS_MOVING
            else:
                # INTENT_IDLE — hold at current position
                self.robot.hold_position = {
                    "x": self.robot.position["x"],
                    "y": self.robot.position["y"],
                }
                self.robot.status = robot_pb2.STATUS_HOLDING
            intent_name = robot_pb2.RobotIntent.Name(self.robot.intent)
            return robot_pb2.CommandResponse(
                success=True, description=f"Resumed ({intent_name}) at speed {self.robot.speed:.1f}",
                resulting_status=self.robot.status,
                resulting_intent=self.robot.intent,
            )
        elif request.command == robot_pb2.CMD_SET_PATH:
            wps = [{"x": wp.x, "y": wp.y} for wp in request.set_path_params.waypoints]
            if len(wps) < 2:
                return robot_pb2.CommandResponse(
                    success=False, description="Need at least 2 waypoints",
                )
            self.robot.path_waypoints = wps
            self.robot.path_index = self.robot.path_index % len(wps)
            print(f"[{self.robot.robot_id} {_ts()}] Path route updated: {len(wps)} waypoints")
            return robot_pb2.CommandResponse(
                success=True, description=f"Path route set ({len(wps)} waypoints)",
                resulting_status=self.robot.status,
                resulting_intent=self.robot.intent,
            )
        elif request.command == robot_pb2.CMD_CHARGE:
            if not self.robot._charging_station_addresses:
                return robot_pb2.CommandResponse(
                    success=False,
                    description="No charging stations registered",
                )
            if self.robot.status == robot_pb2.STATUS_CHARGING:
                return robot_pb2.CommandResponse(
                    success=False,
                    description="Already charging",
                )
            if self.robot._charge_requested:
                return robot_pb2.CommandResponse(
                    success=False,
                    description="Charge already in progress",
                )
            self.robot._charge_requested = True
            threading.Thread(target=self.robot._initiate_charge, daemon=True).start()
            return robot_pb2.CommandResponse(
                success=True,
                description="Heading to charging station",
                resulting_status=self.robot.status,
                resulting_intent=self.robot.intent,
            )
        else:
            return robot_pb2.CommandResponse(
                success=False, description=f"Unknown command: {cmd_name}",
            )


class RobotStreamingServicer(robot_pb2_grpc.RobotStreamingServicer):
    """gRPC server-streaming servicer — 5 delivery loops per subscriber.

    Each method runs a ``while: yield; sleep`` loop inside a
    ThreadPoolExecutor slot for the lifetime of the subscription.
    With (N−1) peer subscribers plus the UI, that's up to
    5 × N concurrent executor slots per robot.

    Compare with the DDS approach: the DDS middleware delivers data from a
    single ``writer.write()`` call.  No delivery loops, no executor
    slots — the publisher thread count is always 1 per topic
    regardless of how many subscribers exist.
    """
    
    def __init__(self, robot_node: RobotNode):
        self.robot = robot_node
    
    def StreamKinematicState(self, request, context):
        """Server-streaming: yield this robot's kinematic state at 10 Hz.

        This delivery loop occupies one ThreadPoolExecutor slot for the
        entire lifetime of the subscription.  With (N−1) peers + 1 UI,
        that's N concurrent instances of this loop per robot.

        Compare with the DDS approach: ``publish_kinematic_state()`` calls
        ``writer.write()`` once per tick — the middleware delivers to
        all matched readers.  Zero delivery loops, zero executor slots.
        """
        self.robot._enter_delivery_loop()
        try:
            while self.robot.running and context.is_active():
                ks = robot_pb2.KinematicState(
                    robot_id=self.robot.robot_id,
                    position=robot_pb2.Position(
                        x=self.robot.position['x'],
                        y=self.robot.position['y'],
                        z=self.robot.position['z'],
                    ),
                    velocity=robot_pb2.Velocity3(
                        x=self.robot.velocity_x,
                        y=self.robot.velocity_y,
                        z=self.robot.velocity_z,
                    ),
                    heading=self.robot.heading,
                    timestamp=int(time.time() * 1000),
                )
                yield ks
                time.sleep(0.1)
        finally:
            self.robot._exit_delivery_loop()

    def StreamOperationalState(self, request, context):
        """Server-streaming: yield this robot's operational state at 1 Hz.

        Same delivery-loop pattern as ``StreamKinematicState`` — one
        executor slot per subscriber.

        Compare with the DDS approach: ``publish_operational_state()`` publishes
        on-change with a single ``writer.write()``.
        """
        self.robot._enter_delivery_loop()
        try:
            while self.robot.running and context.is_active():
                os_msg = robot_pb2.OperationalState(
                    robot_id=self.robot.robot_id,
                    status=self.robot.status,
                    battery_level=self.robot.battery_level,
                    timestamp=int(time.time() * 1000),
                )
                yield os_msg
                time.sleep(1.0)
        finally:
            self.robot._exit_delivery_loop()
    
    def StreamIntent(self, request, context):
        """Server-streaming: yield this robot's intent at 1 Hz.

        Intent assembly is the most complex of the four streams — the
        target position, path waypoints, and path index all depend on
        ``intent_type``.  Despite that complexity the delivery loop
        pattern is the same: one executor slot per subscriber, sleeping
        between yields.

        Compare with the DDS approach: ``publish_intent()`` writes a single
        DDS sample; the middleware delivers it to every matched reader
        with no per-subscriber loop.
        """
        self.robot._enter_delivery_loop()
        try:
            while self.robot.running and context.is_active():
                with self.robot.data_lock:
                    intent_type = self.robot.intent
                    path_wps = []
                    path_idx = 0
                    if intent_type == robot_pb2.INTENT_GOTO:
                        if self.robot.goal is not None:
                            tx, ty = self.robot.goal["x"], self.robot.goal["y"]
                        elif self.robot.hold_position is not None:
                            # Arrived — report hold position as the target
                            tx, ty = self.robot.hold_position["x"], self.robot.hold_position["y"]
                        else:
                            tx, ty = self.robot.position["x"], self.robot.position["y"]
                    elif intent_type in _CHARGE_INTENTS:
                        # Show dock as target (or wait position if queued)
                        if self.robot._charge_dock:
                            tx, ty = self.robot._charge_dock["x"], self.robot._charge_dock["y"]
                        elif self.robot.goal:
                            tx, ty = self.robot.goal["x"], self.robot.goal["y"]
                        else:
                            tx, ty = self.robot.position["x"], self.robot.position["y"]
                    elif intent_type == robot_pb2.INTENT_FOLLOW_PATH:
                        wp = self.robot.path_target
                        tx, ty = wp["x"], wp["y"]
                        path_wps = [
                            robot_pb2.Waypoint(x=w["x"], y=w["y"])
                            for w in self.robot.path_waypoints
                        ]
                        path_idx = self.robot.path_index
                    else:  # IDLE
                        tx, ty = self.robot.position["x"], self.robot.position["y"]
                intent = robot_pb2.IntentUpdate(
                    robot_id=self.robot.robot_id,
                    intent_type=intent_type,
                    target_position=robot_pb2.Position(x=tx, y=ty, z=0),
                    path_waypoints=path_wps,
                    path_waypoint_index=path_idx,
                    timestamp=int(time.time() * 1000)
                )
                yield intent
                time.sleep(1.0)
        finally:
            self.robot._exit_delivery_loop()
    
    def StreamTelemetry(self, request, context):
        """Server-streaming: yield telemetry including gRPC-specific metrics.

        Two of the telemetry fields — ``tcp_connections`` and
        ``active_delivery_loops`` — exist *because* of gRPC's transport
        model and have no meaningful equivalent in DDS.

        ``tcp_connections`` counts ESTABLISHED TCP sockets via
        ``psutil.Process().net_connections()``.  In a 5-robot fleet
        every node maintains 2*(N-1) peer sockets + 1 UI socket = 9.

        ``active_delivery_loops`` counts in-flight server-streaming
        generators; each subscriber of any stream type consumes one
        ``ThreadPoolExecutor`` slot.

        Compare with the DDS approach: telemetry is published with a single
        ``writer.write()``.  ``tcp_connections`` and
        ``active_delivery_loops`` are always zero because DDS uses
        shared-memory or UDP multicast and has no per-subscriber loops.
        """
        self.robot._enter_delivery_loop()
        try:
            while self.robot.running and context.is_active():
                # Count all ESTABLISHED TCP connections involving this robot's gRPC port.
                # This includes both outbound (to peers) and inbound (from peers + UI),
                # giving the true per-node socket count = 2*(N-1) + 1 in a 5-robot fleet.
                # Use Process().net_connections() to stay within macOS permission limits.
                try:
                    # Count all ESTABLISHED TCP sockets in this process.
                    # Inbound:  laddr.port == our gRPC port  (peers + UI connecting to us)
                    # Outbound: raddr.port in fleet range     (our channels to each peer)
                    # Together these give the true per-node total: 2*(N-1) + 1 UI.
                    tcp_conns = sum(
                        1 for c in psutil.Process().net_connections(kind="tcp")
                        if c.status == "ESTABLISHED"
                    )
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    with self.robot.peers_lock:
                        tcp_conns = len(self.robot.peer_connections)  # fallback
                telemetry = robot_pb2.TelemetryUpdate(
                    robot_id=self.robot.robot_id,
                    cpu_usage=random.uniform(20, 80),
                    memory_usage=random.uniform(30, 70),
                    temperature=random.uniform(40, 70),
                    tcp_connections=tcp_conns,
                    active_delivery_loops=self.robot.delivery_loop_count,
                    signal_strength=self.robot.signal_strength,
                    timestamp=int(time.time() * 1000)
                )
                yield telemetry
                time.sleep(1.0)
        finally:
            self.robot._exit_delivery_loop()

    def StreamCoverage(self, request, context):
        """Server-streaming: yield CoveragePoint samples as they are generated.

        Starts from the current end of the robot's coverage list — the
        subscriber only sees points generated *after* the stream opens.
        There is no late-joiner replay in gRPC; a new UI that connects
        after coverage has been accumulating will miss all prior points.

        Compare with the DDS approach: the DDS DataWriter uses TRANSIENT
        durability and KEEP_LAST(1000) history.  A late-joining UI
        receives the full coverage trail automatically — no extra code,
        just a QoS setting.
        """
        self.robot._enter_delivery_loop()
        try:
            idx = len(self.robot._coverage_points)
            while self.robot.running and context.is_active():
                pts = self.robot._coverage_points
                while idx < len(pts):
                    yield pts[idx]
                    idx += 1
                time.sleep(0.05)  # poll at 20 Hz
        finally:
            self.robot._exit_delivery_loop()

    def RequestVideo(self, request, context):
        """Server-streaming: render and yield JPEG video frames at ~5 fps.

        Each open video stream consumes one ``ThreadPoolExecutor`` slot
        for its entire lifetime.  The renderer is shared, but the
        delivery loop — ``yield → sleep(0.2) → yield`` — is unique per
        subscriber.

        Compare with the DDS approach: ``_video_publish_loop()`` writes frames
        to a single DDS DataWriter.  Delivery to one subscriber or ten
        is identical; no additional threads or executor slots are needed.
        Demand is detected via partition-based ``publication_matched``
        events rather than an explicit ``active_video_streams`` counter.
        """
        self.robot.active_video_streams += 1
        self.robot._enter_delivery_loop()
        print(f"[{self.robot.robot_id} {_ts()}] Video stream started "
              f"(active: {self.robot.active_video_streams})")
        try:
            while self.robot.running and context.is_active():
                # Gather peer positions for the renderer
                with self.robot.data_lock:
                    peers = {}
                    for rid, ks in self.robot.other_robots_state.items():
                        if rid != self.robot.robot_id:
                            os_msg = self.robot.other_robots_operational.get(rid)
                            status_name = robot_pb2.RobotStatus.Name(os_msg.status) if os_msg else "STATUS_MOVING"
                            peers[rid] = {
                                "x": ks.position.x,
                                "y": ks.position.y,
                                "heading": ks.heading,
                                "status": status_name,
                            }
                    # Snapshot mutable state under the lock to avoid races
                    snap_pos = dict(self.robot.position)
                    snap_heading = self.robot.heading
                    snap_wps = list(self.robot.path_waypoints)

                    # Build charging station positions for 3D rendering
                    stations = {}
                    for addr, dock in self.robot._charging_station_docks.items():
                        sid = dock.get("station_id", addr)
                        is_active = (self.robot.status == robot_pb2.STATUS_CHARGING
                                     and self.robot._charge_station_address == addr)
                        stations[sid] = {
                            "x": dock["x"],
                            "y": dock["y"],
                            "active": is_active,
                        }

                jpeg_bytes = self.robot.video_renderer.render_frame(
                    my_pos=snap_pos,
                    my_heading=snap_heading,
                    peers=peers,
                    my_waypoints=snap_wps,
                    charging_stations=stations,
                )

                frame = robot_pb2.VideoFrame(
                    frame_data=jpeg_bytes,
                    timestamp=int(time.time() * 1000),
                )
                yield frame
                time.sleep(0.2)  # ~5 fps
        except Exception as e:
            if self.robot.running:
                print(f"[{self.robot.robot_id} {_ts()}] Video stream error: {e}")
        finally:
            self.robot.active_video_streams -= 1
            self.robot._exit_delivery_loop()
            print(f"[{self.robot.robot_id} {_ts()}] Video stream ended "
                  f"(active: {self.robot.active_video_streams})")


def main():
    parser = argparse.ArgumentParser(description='Robot Node - the gRPC approach Full Mesh gRPC')
    parser.add_argument('--id', required=True, help='Robot ID')
    parser.add_argument('--fleet-port', type=int, required=True, help='This robot\'s gRPC port within the fleet')
    parser.add_argument('--fleet-port-list', default="",
                        help='Comma-separated list of all fleet member ports. '
                             'If omitted, peers are discovered automatically via mDNS (Zeroconf).')
    parser.add_argument('--same-host', action='store_true',
                        help='Discovery mode only: all robots run on the same machine. '
                             'Connects via localhost instead of the mDNS-advertised IP.')
    
    args = parser.parse_args()
    
    # Create robot node
    robot = RobotNode(args.id, args.fleet_port, args.fleet_port_list)
    
    # Start the gRPC server first (sets robot.running = True)
    robot.start_server()

    # Station discovery callback (used by both static and mDNS modes)
    use_localhost = args.same_host

    def _on_station_discovered(address: str):
        host, port_s = address.rsplit(":", 1)
        target = f"localhost:{port_s}" if use_localhost else address
        robot.register_charging_station(target)

    if args.fleet_port_list:
        # ── Static mode: connect to all listed peers ──────────────────────
        for port_str in args.fleet_port_list.split(','):
            port_str = port_str.strip()
            if port_str and int(port_str) != args.fleet_port:
                robot.register_peer(f"localhost:{port_str}")

        # Still use mDNS to discover charging stations automatically
        # Register both service types so the UI can discover us.
        discovery = FleetDiscovery(
            robot_id=args.id,
            grpc_port=args.fleet_port,
            register_types=[CMD_SERVICE_TYPE, STREAM_SERVICE_TYPE],
            browse_types=[],   # static mode — peers already known
            on_station_added=_on_station_discovered,
        )
        discovery.start()
    else:
        # ── Discovery mode: use Zeroconf / mDNS ──────────────────────────
        print(f"[{args.id} {_ts()}] No --fleet-port-list given → using mDNS discovery")

        def _on_peer_discovered(address: str):
            """Connect to a newly discovered peer.

            In --same-host mode, rewrite to localhost (avoids VPN/Tailscale
            interface issues).  In multi-host mode, use the mDNS address
            directly.
            """
            host, port_s = address.rsplit(":", 1)
            port = int(port_s)
            if port == args.fleet_port:
                return                       # that's us
            target = f"localhost:{port}" if use_localhost else address
            robot.register_peer(target)

        discovery = FleetDiscovery(
            robot_id=args.id,
            grpc_port=args.fleet_port,
            register_types=[CMD_SERVICE_TYPE, STREAM_SERVICE_TYPE],
            browse_types=[STREAM_SERVICE_TYPE],  # only need streams from peers
            on_peer_added=_on_peer_discovered,
            on_station_added=_on_station_discovered,
        )
        discovery.start()
    
    # Run robot (background threads + main loop)
    robot.run()


if __name__ == '__main__':
    main()
