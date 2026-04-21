#!/usr/bin/env python3
"""
Robot Fleet UI – Pure gRPC (Full Mesh Streams)

A live web dashboard that connects to each robot's gRPC streaming service
and displays State, Intent, and Telemetry for every robot in the fleet.

Architecture:
    FleetStateStore   – transport-agnostic data store (state/intent/telemetry)
    GrpcFleetClient   – gRPC communication layer; reads streams, sends commands
    Flask / HTML / JS – presentation layer; reads store via SSE, sends commands
                        through the client

Usage:
    python robot_ui.py --fleet-port-list 50051,50052,50053 --ui-port 5000
"""

import grpc
import os
import threading
import time
import json
import argparse
from typing import Dict, Optional, List

from flask import Flask, Response, render_template_string, request, jsonify, send_from_directory

# -- protobuf / gRPC stubs (same directory) --
import robot_pb2
import robot_pb2_grpc

# Zeroconf peer discovery (mDNS / DNS-SD)
from fleet_discovery import FleetDiscovery, CMD_SERVICE_TYPE, STREAM_SERVICE_TYPE


# ─────────────────────────────────────────────────────────────────────────────
# Fleet State Store – transport-agnostic, thread-safe data store
# ─────────────────────────────────────────────────────────────────────────────
class FleetStateStore:
    """Thread-safe store for fleet state, intent, telemetry, and connection info.

    This class has **no knowledge** of gRPC, protobuf, or any transport.
    It holds plain Python dicts and provides a snapshot for the UI layer.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.kinematic: Dict[str, dict] = {}      # robot_id → {x, y, z, vx, vy, vz, heading, ts}
        self.operational: Dict[str, dict] = {}     # robot_id → {status, battery, ts}
        self.intent: Dict[str, dict] = {}
        self.telemetry: Dict[str, dict] = {}
        self.connections: Dict[str, str] = {}     # port_key → "connecting"/"connected"/"disconnected"
        self.addresses: Dict[str, str] = {}       # port_key → "host:port"
        self.port_to_name: Dict[str, str] = {}    # port_key → discovered robot name
        self.charging: Dict[str, dict] = {}       # station_id → {dock_robot, queue, dock_x, dock_y}
        self.coverage: Dict[str, list] = {}        # robot_id → [{x, y, seq}, …]

    # ── reads ──────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Return a JSON-serialisable copy of all data stores."""
        with self.lock:
            # Build merged state for the map (needs position + status + velocity)
            merged: Dict[str, dict] = {}
            for rid, kd in self.kinematic.items():
                merged[rid] = dict(kd)
            for rid, od in self.operational.items():
                if rid in merged:
                    merged[rid].update(od)
                else:
                    merged[rid] = dict(od)
            return {
                "state": merged,
                "kinematic": {k: dict(v) for k, v in self.kinematic.items()},
                "operational": {k: dict(v) for k, v in self.operational.items()},
                "intent": dict(self.intent),
                "telemetry": dict(self.telemetry),
                "connections": dict(self.connections),
                "port_names": dict(self.port_to_name),
                "charging": {k: dict(v) for k, v in self.charging.items()},
                "coverage": {k: list(v) for k, v in self.coverage.items()},
            }

    def resolve_address(self, robot_id: str) -> Optional[str]:
        """Look up the gRPC address for a robot by its discovered name."""
        with self.lock:
            for port_key, name in self.port_to_name.items():
                if name == robot_id:
                    return self.addresses.get(port_key)
        return None

    # ── writes (called by the communication layer) ─────────────────────────

    def register_port(self, port_key: str, address: str):
        """Record a new port as 'connecting'."""
        with self.lock:
            self.connections[port_key] = "connecting"
            self.addresses[port_key] = address

    def set_connection_status(self, port_key: str, status: str):
        """Update the connection status for a port."""
        with self.lock:
            self.connections[port_key] = status

    def clear_port_data(self, port_key: str):
        """Remove all stale data for a disconnected port."""
        with self.lock:
            old_name = self.port_to_name.pop(port_key, None)
            if old_name:
                self.kinematic.pop(old_name, None)
                self.operational.pop(old_name, None)
                self.intent.pop(old_name, None)
                self.telemetry.pop(old_name, None)

    def _ensure_name(self, port_key: str, robot_id: str):
        """Track port_key → robot_id mapping; clean up on name change."""
        if robot_id and robot_id != "ui":
            old_name = self.port_to_name.get(port_key)
            if old_name != robot_id:
                if old_name:
                    self.kinematic.pop(old_name, None)
                    self.operational.pop(old_name, None)
                    self.intent.pop(old_name, None)
                    self.telemetry.pop(old_name, None)
                self.port_to_name[port_key] = robot_id

    def update_kinematic(self, port_key: str, robot_id: str, data: dict):
        """Store a kinematic state update (position, velocity, heading)."""
        with self.lock:
            self._ensure_name(port_key, robot_id)
            self.kinematic[robot_id] = data

    def update_operational(self, port_key: str, robot_id: str, data: dict):
        """Store an operational state update (status, battery)."""
        with self.lock:
            self._ensure_name(port_key, robot_id)
            self.operational[robot_id] = data

    def update_intent(self, robot_id: str, data: dict):
        """Store an intent update."""
        with self.lock:
            self.intent[robot_id] = data

    def update_telemetry(self, robot_id: str, data: dict):
        """Store a telemetry update."""
        with self.lock:
            self.telemetry[robot_id] = data

    def update_charging(self, station_id: str, data: dict):
        """Store a charging station status update."""
        with self.lock:
            self.charging[station_id] = data

    def update_coverage(self, robot_id: str, points: list):
        """Append coverage points for a robot."""
        with self.lock:
            if robot_id not in self.coverage:
                self.coverage[robot_id] = []
            self.coverage[robot_id].extend(points)

    def clear_coverage(self):
        """Clear all coverage data."""
        with self.lock:
            self.coverage.clear()


# ─────────────────────────────────────────────────────────────────────────────
# gRPC Fleet Client – all transport / protobuf logic lives here
# ─────────────────────────────────────────────────────────────────────────────
class GrpcFleetClient:
    """Manages gRPC connections to every robot in the fleet.

    Reads server-streaming State / Intent / Telemetry and pushes plain dicts
    into a FleetStateStore.  Sends commands on behalf of the UI layer.
    """

    def __init__(self, store: FleetStateStore):
        self.store = store
        self.running = True

    def stop(self):
        self.running = False

    # ── outbound: connect to fleet members ─────────────────────────────────

    def connect_to_port(self, port: int):
        """Register a port and spawn a background connection loop."""
        address = f"localhost:{port}"
        port_key = str(port)
        self.store.register_port(port_key, address)
        threading.Thread(
            target=self._robot_loop, args=(port_key, address), daemon=True,
        ).start()

    def connect_to_address(self, host: str, port: int):
        """Register a remote host:port and spawn a background connection loop.

        Used in multi-host discovery mode where robots run on different
        machines and we need the real IP, not localhost.
        """
        address = f"{host}:{port}"
        port_key = address          # use full address as key (ports may collide across hosts)
        self.store.register_port(port_key, address)
        threading.Thread(
            target=self._robot_loop, args=(port_key, address), daemon=True,
        ).start()

    def connect_to_station(self, address: str):
        """Subscribe to a charging station's StreamStatus (server-streaming).

        Spawns a background reconnect loop that keeps the store's
        ``charging`` dict up to date with every state change.
        """
        threading.Thread(
            target=self._station_loop, args=(address,), daemon=True,
        ).start()

    # ── command sending ────────────────────────────────────────────────────

    def send_command_all(self, command: str, *,
                         goto_x: float = 0, goto_y: float = 0,
                         goto_speed: float = 0,
                         waypoints: Optional[List[dict]] = None) -> dict:
        """Send a command to every connected robot, one call at a time.

        The application must issue one SendCommand RPC per robot — there
        is no broadcast primitive in gRPC.  The loop here makes that
        explicit: N robots = N calls.
        """
        with self.store.lock:
            robot_ids = sorted(self.store.kinematic.keys())

        results: dict = {}
        for rid in robot_ids:
            results[rid] = self.send_command(rid, command,
                                             goto_x=goto_x, goto_y=goto_y,
                                             goto_speed=goto_speed,
                                             waypoints=waypoints)

        n = len(robot_ids)
        successes = sum(1 for r in results.values() if r.get("success"))
        return {
            "broadcast": True,
            "command": command,
            "grpc_calls_made": n,   # one RPC per robot — no broadcast primitive in gRPC
            "success": successes == n,
            "description": f"{successes}/{n} robots acknowledged {command}",
            "results": results,
        }

    def send_command(self, robot_id: str, command: str, *,
                     goto_x: float = 0, goto_y: float = 0,
                     goto_speed: float = 0,
                     waypoints: Optional[List[dict]] = None) -> dict:
        """Send a command to a robot.  Returns a plain dict result."""
        address = self.store.resolve_address(robot_id)
        if not address:
            return {"success": False, "description": f"Unknown robot: {robot_id}"}

        # Map command string → protobuf enum
        cmd_map = {
            "STOP":         robot_pb2.CMD_STOP,
            "FOLLOW_PATH":  robot_pb2.CMD_FOLLOW_PATH,
            "GOTO":         robot_pb2.CMD_GOTO,
            "RESUME":       robot_pb2.CMD_RESUME,
            "SET_PATH":     robot_pb2.CMD_SET_PATH,
            "CHARGE":       robot_pb2.CMD_CHARGE,
        }
        cmd_enum = cmd_map.get(command)
        if cmd_enum is None:
            return {"success": False, "description": f"Unknown command: {command}"}

        # Build protobuf request with typed parameters
        kwargs = dict(robot_id=robot_id, command=cmd_enum)
        if cmd_enum == robot_pb2.CMD_GOTO:
            kwargs["goto_params"] = robot_pb2.GotoParameters(
                x=goto_x, y=goto_y, speed=goto_speed,
            )
        elif cmd_enum in (robot_pb2.CMD_FOLLOW_PATH, robot_pb2.CMD_RESUME) and goto_speed > 0:
            kwargs["goto_params"] = robot_pb2.GotoParameters(speed=goto_speed)
        elif cmd_enum == robot_pb2.CMD_SET_PATH and waypoints:
            kwargs["set_path_params"] = robot_pb2.SetPathParameters(
                waypoints=[robot_pb2.Waypoint(x=w["x"], y=w["y"]) for w in waypoints],
            )
        req = robot_pb2.CommandRequest(**kwargs)

        try:
            channel = grpc.insecure_channel(address)
            grpc.channel_ready_future(channel).result(timeout=3)
            stub = robot_pb2_grpc.RobotCommandStub(channel)
            resp = stub.SendCommand(req, timeout=5)
            channel.close()
            return {
                "success": resp.success,
                "description": resp.description,
                "resulting_status": robot_pb2.RobotStatus.Name(resp.resulting_status),
                "resulting_intent": robot_pb2.RobotIntent.Name(resp.resulting_intent),
            }
        except Exception as e:
            return {"success": False, "description": str(e)}

    def stream_video(self, robot_id: str):
        """Open a RequestVideo gRPC stream and yield raw JPEG bytes.

        This is a *generator* — the Flask route iterates it to produce
        an MJPEG response.  The gRPC channel is kept alive for the
        duration of the stream and closed when the generator exits.
        """
        address = self.store.resolve_address(robot_id)
        if not address:
            return

        channel = None
        try:
            channel = grpc.insecure_channel(address)
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = robot_pb2_grpc.RobotStreamingStub(channel)
            req = robot_pb2.VideoRequest(robot_id=robot_id)
            for frame in stub.RequestVideo(req):
                if not self.running:
                    break
                yield frame.frame_data
        except Exception:
            pass
        finally:
            if channel:
                channel.close()

    # ── internal: connection loop & stream readers ─────────────────────────

    def _robot_loop(self, port_key: str, address: str):
        """Reconnect loop for a single robot port."""
        while self.running:
            try:
                channel = grpc.insecure_channel(address)
                grpc.channel_ready_future(channel).result(timeout=5)
                self.store.set_connection_status(port_key, "connected")

                stub = robot_pb2_grpc.RobotStreamingStub(channel)
                stream_failed = threading.Event()
                for fn in (self._read_kinematic, self._read_operational,
                           self._read_intent, self._read_telemetry,
                           self._read_coverage):
                    threading.Thread(
                        target=fn, args=(port_key, stub, stream_failed), daemon=True,
                    ).start()

                while self.running and not stream_failed.is_set():
                    stream_failed.wait(timeout=1)

                channel.close()
                if not self.running:
                    return
                raise Exception("stream lost")

            except Exception:
                self.store.set_connection_status(port_key, "disconnected")
                self.store.clear_port_data(port_key)
                if not self.running:
                    return
                time.sleep(3)

    def _read_kinematic(self, port_key, stub, stream_failed):
        try:
            req = robot_pb2.SubscribeRequest(subscriber_id="ui")
            for msg in stub.StreamKinematicState(req):
                if not self.running:
                    return
                self.store.update_kinematic(port_key, msg.robot_id, {
                    "x": msg.position.x, "y": msg.position.y, "z": msg.position.z,
                    "vx": msg.velocity.x, "vy": msg.velocity.y, "vz": msg.velocity.z,
                    "heading": msg.heading, "ts": msg.timestamp,
                })
        except Exception as exc:
            print(f"[ui] _read_kinematic failed for {port_key}: {exc}")
            stream_failed.set()

    def _read_operational(self, port_key, stub, stream_failed):
        try:
            req = robot_pb2.SubscribeRequest(subscriber_id="ui")
            for msg in stub.StreamOperationalState(req):
                if not self.running:
                    return
                self.store.update_operational(port_key, msg.robot_id, {
                    "status": robot_pb2.RobotStatus.Name(msg.status),
                    "battery": msg.battery_level, "ts": msg.timestamp,
                })
        except Exception as exc:
            print(f"[ui] _read_operational failed for {port_key}: {exc}")
            stream_failed.set()

    def _read_intent(self, port_key, stub, stream_failed):
        try:
            req = robot_pb2.SubscribeRequest(subscriber_id="ui")
            for msg in stub.StreamIntent(req):
                if not self.running:
                    return
                entry = {
                    "type": robot_pb2.RobotIntent.Name(msg.intent_type),
                    "target_x": msg.target_position.x,
                    "target_y": msg.target_position.y,
                    "ts": msg.timestamp,
                }
                if msg.path_waypoints:
                    entry["waypoints"] = [
                        {"x": wp.x, "y": wp.y} for wp in msg.path_waypoints
                    ]
                    entry["waypoint_index"] = msg.path_waypoint_index
                self.store.update_intent(msg.robot_id, entry)
        except Exception as exc:
            print(f"[ui] _read_intent failed for {port_key}: {exc}")
            stream_failed.set()

    def _read_telemetry(self, port_key, stub, stream_failed):
        try:
            req = robot_pb2.SubscribeRequest(subscriber_id="ui")
            for msg in stub.StreamTelemetry(req):
                if not self.running:
                    return
                self.store.update_telemetry(msg.robot_id, {
                    "cpu": msg.cpu_usage, "memory": msg.memory_usage,
                    "temperature": msg.temperature,
                    "tcp_connections": msg.tcp_connections,
                    "delivery_loops": msg.active_delivery_loops,
                    "signal_strength": msg.signal_strength,
                    "ts": msg.timestamp,
                })
        except Exception as exc:
            print(f"[ui] _read_telemetry failed for {port_key}: {exc}")
            stream_failed.set()

    def _read_coverage(self, port_key, stub, stream_failed):
        try:
            req = robot_pb2.SubscribeRequest(subscriber_id="ui")
            for msg in stub.StreamCoverage(req):
                if not self.running:
                    return
                self.store.update_coverage(msg.robot_id,
                    [{"x": msg.x, "y": msg.y, "seq": msg.sequence_number}])
        except Exception as exc:
            print(f"[ui] _read_coverage failed for {port_key}: {exc}")
            stream_failed.set()

    # ── charging station stream ────────────────────────────────────────────

    def _station_loop(self, address: str):
        """Reconnect loop for a charging station's StreamStatus."""
        while self.running:
            try:
                channel = grpc.insecure_channel(address)
                grpc.channel_ready_future(channel).result(timeout=5)
                print(f"[ui] Connected to charging station at {address}")

                stub = robot_pb2_grpc.ChargingStationStub(channel)
                req = robot_pb2.StationStatusRequest()
                for msg in stub.StreamStatus(req):
                    if not self.running:
                        return
                    queue = [
                        {"robot_id": e.robot_id, "rank": e.rank, "confirmed": e.confirmed}
                        for e in msg.queue
                    ]
                    self.store.update_charging(msg.station_id, {
                        "station_id": msg.station_id,
                        "dock_x": msg.dock_x,
                        "dock_y": msg.dock_y,
                        "dock_robot": msg.dock_robot,
                        "queue": queue,
                    })

                channel.close()
                if not self.running:
                    return
                raise Exception("station stream ended")

            except Exception as exc:
                if not self.running:
                    return
                print(f"[ui] Charging station stream lost ({address}): {exc} — reconnecting in 3s")
                time.sleep(3)


# ─────────────────────────────────────────────────────────────────────────────
# Presentation layer – Flask web UI (reads FleetStateStore, commands via
# GrpcFleetClient)
# ─────────────────────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Robot Fleet Dashboard</title>
<style>
  :root {
    --bg: #1e1e2e; --panel: #2a2a3d; --header: #3b3b55;
    --text: #cdd6f4; --dim: #6c7086; --blue: #89b4fa;
    --green: #a6e3a1; --yellow: #f9e2af; --red: #f38ba8;
    --peach: #fab387; --canvas: #181825; --grid: #313244; --border: #45475a;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family: "Menlo","Consolas",monospace; font-size:13px;
         background:var(--bg); color:var(--text); }
  header { background:var(--header); padding:12px 20px; font-size:15px;
           font-weight:bold; color:var(--blue); }
  .wrap { display:flex; gap:8px; padding:8px; height:calc(100vh - 50px); }

  /* --- map --- */
  .map-panel { flex:1; background:var(--panel); border-radius:6px; padding:8px;
               display:flex; flex-direction:column; }
  .map-panel h3 { color:var(--blue); margin-bottom:6px; font-size:12px; }
  canvas { flex:1; border-radius:4px; }

  /* --- right panel --- */
  .data-panel { width:480px; display:flex; flex-direction:column; gap:8px;
                overflow-y:auto; }
  .section { background:var(--panel); border-radius:6px; padding:10px 14px; }
  .section h3 { color:var(--blue); font-size:12px; margin-bottom:6px; }
  table { width:100%; border-collapse:collapse; }
  td { padding:2px 6px; white-space:nowrap; }
  .rid { color:var(--blue); font-weight:bold; }
  .ok  { color:var(--green); } .warn { color:var(--yellow); } .crit { color:var(--red); }
  .conn-dot { font-weight:bold; }

  /* --- collapsible sections --- */
  .section h3, .cmd-panel h3, .video-panel h3 {
    cursor:pointer; user-select:none; transition:color 0.15s;
  }
  .section h3:hover, .cmd-panel h3:hover, .video-panel h3:hover { color:var(--text); }
  .section h3::before, .cmd-panel h3::before, .video-panel h3::before {
    content:""; display:inline-block; width:0; height:0;
    border-left:5px solid transparent; border-right:5px solid transparent;
    border-top:6px solid var(--blue); margin-right:6px;
    transition:transform 0.2s; vertical-align:middle;
  }
  .collapsed h3::before { transform:rotate(-90deg); }
  .collapsed .panel-body { display:none; }

  /* --- command panel --- */
  .cmd-panel { background:var(--panel); border-radius:6px; padding:10px 14px; }
  .cmd-panel h3 { color:var(--blue); font-size:12px; margin-bottom:8px; }
  .cmd-row { display:flex; gap:6px; align-items:center; margin-bottom:6px; flex-wrap:wrap; }
  .cmd-row label { color:var(--dim); font-size:11px; min-width:28px; }
  .cmd-row select, .cmd-row input {
    background:var(--bg); color:var(--text); border:1px solid var(--border);
    border-radius:4px; padding:4px 8px; font-family:inherit; font-size:12px;
  }
  .cmd-row input { width:60px; }
  .cmd-row input.wide { width:80px; }
  .cmd-btns { display:flex; gap:6px; margin-top:8px; }
  .cmd-btns button {
    background:var(--header); color:var(--text); border:1px solid var(--border);
    border-radius:4px; padding:5px 12px; font-family:inherit; font-size:12px;
    cursor:pointer; transition: background 0.15s;
  }
  .cmd-btns button:hover { background:var(--blue); color:var(--bg); }
  .cmd-btns button.goto-btn { border-color:var(--blue); color:var(--blue); }
  .cmd-btns button.goto-btn:hover { background:var(--blue); color:var(--bg); }
  .cmd-btns button.stop-btn { border-color:var(--red); color:var(--red); }
  .cmd-btns button.stop-btn:hover { background:var(--red); color:var(--bg); }
  .cmd-btns button.path-btn { border-color:var(--green); color:var(--green); }
  .cmd-btns button.path-btn:hover { background:var(--green); color:var(--bg); }
  .cmd-btns button.resume-btn { border-color:var(--yellow); color:var(--yellow); }
  .cmd-btns button.resume-btn:hover { background:var(--yellow); color:var(--bg); }
  .cmd-btns button.charge-btn { border-color:#fab387; color:#fab387; }
  .cmd-btns button.charge-btn:hover { background:#fab387; color:var(--bg); }
  .cmd-result { color:var(--dim); font-size:11px; margin-top:6px; min-height:14px; }
  .cmd-hint { color:var(--text); font-size:10px; margin-top:6px; font-style:normal;
              line-height:1.6; opacity:0.85; }
  .cmd-hint kbd { background:var(--header); color:var(--blue); border:1px solid var(--border);
                  border-radius:3px; padding:1px 5px; font-family:inherit; font-size:10px; }

  /* --- video panel --- */
  .video-panel { background:var(--panel); border-radius:6px; padding:10px 14px; }
  .video-panel h3 { color:var(--blue); font-size:12px; margin-bottom:8px; }
  .video-row { display:flex; gap:6px; align-items:center; margin-bottom:6px; }
  .video-row select { background:var(--bg); color:var(--text); border:1px solid var(--border);
                      border-radius:4px; padding:4px 8px; font-family:inherit; font-size:12px; }
  .video-btns { display:flex; gap:6px; margin-bottom:8px; }
  .video-btns button { background:var(--header); color:var(--text); border:1px solid var(--border);
                       border-radius:4px; padding:5px 12px; font-family:inherit; font-size:12px;
                       cursor:pointer; transition:background 0.15s; }
  .video-btns button:hover { background:var(--blue); color:var(--bg); }
  .video-btns button.vid-stop { border-color:var(--red); color:var(--red); }
  .video-btns button.vid-stop:hover { background:var(--red); color:var(--bg); }
  .video-container { background:var(--canvas); border-radius:4px; overflow:hidden;
                     min-height:60px; display:flex; align-items:center; justify-content:center; }
  .video-container img { width:100%; display:block; }
  .video-container .placeholder { color:var(--dim); font-size:11px; padding:20px; text-align:center; }
</style>
</head>
<body>
<header>🤖 Robot Fleet Dashboard — Pure gRPC: Full Mesh Streams <span id="hdr-stats" style="font-size:12px;font-weight:normal;color:var(--dim);margin-left:16px;"></span></header>
<div class="wrap">
  <div class="map-panel">
    <h3>Position Map (0–100 × 0–100)</h3>
    <canvas id="map"></canvas>
  </div>
  <div class="data-panel">
    <div class="cmd-panel">
      <h3>Commands</h3>
      <div class="panel-body">
      <div class="cmd-row">
        <label>Robot</label>
        <select id="cmd-robot"></select>
      </div>
      <div class="cmd-row">
        <label>X</label><input id="cmd-x" type="number" min="0" max="100" step="1" value="50">
        <label>Y</label><input id="cmd-y" type="number" min="0" max="100" step="1" value="50">
        <label>Speed</label><input id="cmd-speed" type="number" min="0.5" max="20" step="0.5" value="2.0" class="wide">
      </div>
      <div class="cmd-btns">
        <button class="goto-btn" onclick="sendGoto()">GOTO</button>
        <button class="path-btn" onclick="sendSimple('FOLLOW_PATH')">PATH</button>
        <button class="stop-btn" onclick="sendSimple('STOP')">STOP</button>
        <button class="resume-btn" onclick="sendSimple('RESUME')">RESUME</button>
        <button class="charge-btn" onclick="sendSimple('CHARGE')">⚡ CHARGE</button>
      </div>
      <div class="cmd-result" id="cmd-result"></div>
      <div class="cmd-hint">
        <kbd>Click</kbd> map → set GOTO target &nbsp;·&nbsp;
        <kbd>Drag</kbd> waypoint → move &nbsp;·&nbsp;
        <kbd>Double-click</kbd> → add waypoint &nbsp;·&nbsp;
        <kbd>⌥+Click</kbd> waypoint → remove &nbsp;·&nbsp;
        <button id="toggle-cov-btn" onclick="toggleCoverage()" style="font-size:11px;padding:3px 10px;border:1px solid #6c7086;background:#313244;color:#cdd6f4;border-radius:4px;cursor:pointer;">Coverage ✓</button>
        <button onclick="clearCoverage()" style="font-size:11px;padding:3px 10px;border:1px solid #6c7086;background:transparent;color:#cdd6f4;border-radius:4px;cursor:pointer;">Clear Coverage</button>
      </div>
      </div>
    </div>
    <div class="video-panel">
      <h3>Onboard Camera</h3>
      <div class="panel-body">
      <div class="video-row">
        <label style="color:var(--dim);font-size:11px;min-width:28px;">Robot</label>
        <select id="vid-robot"></select>
      </div>
      <div class="video-btns">
        <button onclick="startVideo()">📹 Start</button>
        <button class="vid-stop" onclick="stopVideo()">⏹ Stop</button>
      </div>
      <div class="video-container" id="vid-container">
        <div class="placeholder">Select a robot and click Start</div>
      </div>
      </div>
    </div>
    <div class="section"><h3>Kinematic State <span style="color:var(--dim);font-weight:normal;font-size:10px">10 Hz</span></h3><div class="panel-body"><table id="t-kinematic"><tbody></tbody></table></div></div>
    <div class="section"><h3>Operational State <span style="color:var(--dim);font-weight:normal;font-size:10px">1 Hz</span></h3><div class="panel-body"><table id="t-operational"><tbody></tbody></table></div></div>
    <div class="section"><h3>Intent <span style="color:var(--dim);font-weight:normal;font-size:10px">1 Hz</span></h3><div class="panel-body"><table id="t-intent"><tbody></tbody></table></div></div>
    <div class="section"><h3>Telemetry <span style="color:var(--dim);font-weight:normal;font-size:10px">1 Hz</span></h3><div class="panel-body"><table id="t-telemetry"><tbody></tbody></table><div style="margin-top:6px;font-size:10px;color:var(--dim);font-style:italic">gRPC: each subscriber opens a separate server-streaming RPC — delivery loops = active subscriber count</div></div></div>
    <div class="section"><h3>⚡ Charging Stations <span style="color:var(--dim);font-weight:normal;font-size:10px">stream</span></h3><div class="panel-body"><div id="charging-panel" style="font-size:12px;color:var(--dim);">Waiting for station data…</div></div></div>
    <div class="section"><h3>Connections</h3><div class="panel-body"><table id="t-conn"><tbody></tbody></table></div></div>
  </div>
</div>

<script>
// --- Collapsible panels ---
document.querySelectorAll(".section h3, .cmd-panel h3, .video-panel h3").forEach(h3 => {
  h3.addEventListener("click", () => {
    h3.parentElement.classList.toggle("collapsed");
  });
});

const COLORS = ["#89b4fa","#a6e3a1","#fab387","#f9e2af","#f38ba8",
                "#cba6f7","#94e2d5","#f5c2e7","#74c7ec","#b4befe"];
let colorMap = {};
let colorIdx = 0;
function robotColor(rid) {
  if (!colorMap[rid]) colorMap[rid] = COLORS[colorIdx++ % COLORS.length];
  return colorMap[rid];
}
function cls(v, lo, hi) { return v > hi ? "crit" : v > lo ? "warn" : "ok"; }

// --- Coverage data from gRPC StreamCoverage (server-provided) ---
let coverageData = {};  // rid → [{x, y, seq}, …] from SSE
let showCoverage = true;
function toggleCoverage() {
  showCoverage = !showCoverage;
  const btn = document.getElementById('toggle-cov-btn');
  btn.textContent = showCoverage ? 'Coverage ✓' : 'Coverage ✗';
  btn.style.background = showCoverage ? '#313244' : 'transparent';
}
function clearCoverage() {
  fetch('/clear_coverage', {method: 'POST'});
  coverageData = {};
}

// --- Preload ground texture for canvas map ---
let groundImg = null;
{
  const img = new Image();
  img.onload = () => { groundImg = img; };
  img.src = "/ground_texture.jpg";
}

// --- SSE stream ---
const src = new EventSource("/stream");
src.onmessage = (e) => {
  const d = JSON.parse(e.data);
  refreshRobotDropdown(d.state);
  updateKinematic(d.kinematic);
  updateOperational(d.operational);
  updateIntent(d.intent);
  const totalTcp = updateTelemetry(d.telemetry);
  updateConn(d.connections, d.port_names || {});
  updateCharging(d.charging || {}, d.operational || {});
  // Update header stats badge
  const robotCount = Object.keys(d.state).length;
  const connCount = Object.values(d.connections || {}).filter(s => s === "connected").length;
  if (robotCount > 0) {
    // Each robot reports its outbound TCP count; sum gives total robot↔robot connections
    // (each physical TCP link counted once per endpoint = 2×edges, which is correct:
    //  it reflects the per-robot management burden N-1 outbound + N-1 inbound).
    document.getElementById("hdr-stats").textContent =
      `${robotCount} robots · ${totalTcp.totalTcp} TCP sockets · ${totalTcp.totalLoops} delivery loops · ${connCount} UI channels`;
  }
  // Stash intent data for waypoint hit-testing (but don't overwrite during drag)
  if (!dragging) {
    lastIntentData = d.intent || {};
  }
  lastChargingData = d.charging || {};
  lastOperationalData = d.operational || {};
  coverageData = d.coverage || {};
  drawMap(d.state, dragging ? lastIntentData : d.intent);
};

function updateKinematic(kin) {
  const tb = document.querySelector("#t-kinematic tbody");
  tb.innerHTML = "";
  for (const rid of Object.keys(kin).sort()) {
    const k = kin[rid];
    const spd = Math.sqrt((k.vx||0)**2 + (k.vy||0)**2);
    const hdg = ((k.heading||0) * 180 / Math.PI).toFixed(0);
    tb.innerHTML += `<tr>
      <td class="rid">${rid}</td>
      <td>(${(k.x??0).toFixed(1)}, ${(k.y??0).toFixed(1)})</td>
      <td>${spd.toFixed(1)} u/s</td>
      <td>${hdg}°</td></tr>`;
  }
}
function updateOperational(ops) {
  const tb = document.querySelector("#t-operational tbody");
  tb.innerHTML = "";
  for (const rid of Object.keys(ops).sort()) {
    const o = ops[rid];
    const batt = o.battery ?? 0;
    const bc = batt > 50 ? "ok" : batt > 20 ? "warn" : "crit";
    const st = o.status ?? "…";
    const isCharging = st === "STATUS_CHARGING";
    const stStyle = isCharging ? ' style="color:#fab387;font-weight:bold"' : '';
    const stLabel = isCharging ? "⚡ CHARGING" : st;
    tb.innerHTML += `<tr>
      <td class="rid">${rid}</td>
      <td${stStyle}>${stLabel}</td>
      <td class="${bc}">${batt.toFixed(1)}%</td></tr>`;
  }
}
function updateIntent(intent) {
  const tb = document.querySelector("#t-intent tbody");
  tb.innerHTML = "";
  for (const rid of Object.keys(intent).sort()) {
    const i = intent[rid];
    tb.innerHTML += `<tr>
      <td class="rid">${rid}</td>
      <td>${i.type}</td>
      <td>(${i.target_x.toFixed(1)}, ${i.target_y.toFixed(1)})</td></tr>`;
  }
}
function updateTelemetry(tel) {
  const tb = document.querySelector("#t-telemetry tbody");
  tb.innerHTML = "";
  let totalTcp = 0;
  let totalLoops = 0;
  for (const rid of Object.keys(tel).sort()) {
    const t = tel[rid];
    const tcp = t.tcp_connections ?? 0;
    const loops = t.delivery_loops ?? 0;
    const sig = t.signal_strength ?? 100;
    totalTcp += tcp;
    totalLoops += loops;
    tb.innerHTML += `<tr>
      <td class="rid">${rid}</td>
      <td class="${cls(t.cpu,50,75)}">CPU ${t.cpu.toFixed(1)}%</td>
      <td>MEM ${t.memory.toFixed(1)}%</td>
      <td class="${cls(t.temperature,55,65)}">${t.temperature.toFixed(1)}°C</td>
      <td class="${cls(100-sig,50,80)}">LNK ${sig.toFixed(0)}%</td>
      <td style="color:var(--peach)">${tcp} TCP</td>
      <td style="color:var(--mauve)">${loops} Loops</td></tr>`;
  }
  return {totalTcp, totalLoops};
}
function updateConn(conns, portNames) {
  const tb = document.querySelector("#t-conn tbody");
  tb.innerHTML = "";
  for (const port of Object.keys(conns).sort()) {
    const s = conns[port];
    const name = portNames[port] || "…";
    const [sym, c] = s === "connected" ? ["●","ok"]
                   : s === "connecting" ? ["◌","warn"] : ["✕","crit"];
    const label = s === "connected" ? `${name}` : `port ${port}`;
    tb.innerHTML += `<tr>
      <td class="rid">${label}</td>
      <td>:${port}</td>
      <td class="conn-dot ${c}">${sym} ${s}</td></tr>`;
  }
}
function updateCharging(charging, operational) {
  const panel = document.getElementById("charging-panel");
  const ids = Object.keys(charging).sort();
  if (ids.length === 0) { panel.innerHTML = '<span style="color:var(--dim)">No stations connected</span>'; return; }
  let html = '';
  for (const sid of ids) {
    const st = charging[sid];
    html += `<div style="margin-bottom:8px;">`;
    html += `<div style="font-weight:bold;color:#fab387;margin-bottom:4px;">⚡ ${sid} <span style="font-weight:normal;color:var(--dim);font-size:10px">(${st.dock_x}, ${st.dock_y})</span></div>`;
    if (st.queue && st.queue.length > 0) {
      html += `<table style="width:100%;font-size:11px;">`;
      html += `<tr style="color:var(--dim)"><td style="width:30px">#</td><td>Robot</td><td style="width:70px">Status</td></tr>`;
      for (const e of st.queue) {
        const isOnDock = (e.robot_id === st.dock_robot);
        // Cross-reference the robot's actual operational status to
        // distinguish "assigned to dock but still en route" from
        // "physically at the dock and charging".
        const robotOps = operational[e.robot_id];
        const actuallyCharging = robotOps && robotOps.status === "STATUS_CHARGING";
        let statusTxt, statusCol;
        if (isOnDock && actuallyCharging) {
          statusTxt = '🔌 Charging';
          statusCol = '#a6e3a1';
        } else if (isOnDock) {
          statusTxt = '🚜 En route';
          statusCol = '#89b4fa';
        } else if (e.confirmed) {
          statusTxt = '⏳ Waiting';
          statusCol = '#f9e2af';
        } else {
          statusTxt = '… Pending';
          statusCol = 'var(--dim)';
        }
        const rowBg = (isOnDock && actuallyCharging) ? 'background:rgba(166,227,161,0.1);' : '';
        html += `<tr style="${rowBg}"><td style="color:var(--dim)">${e.rank}</td>`;
        html += `<td class="rid" style="${(isOnDock && actuallyCharging) ? 'font-weight:bold' : ''}">${e.robot_id}</td>`;
        html += `<td style="color:${statusCol}">${statusTxt}</td></tr>`;
      }
      html += `</table>`;
    } else {
      html += `<div style="color:var(--dim);font-size:11px">Idle</div>`;
    }
    html += `</div>`;
  }
  panel.innerHTML = html;
}

// --- Robot icon renderer ---
function drawTractor(ctx, sx, sy, col, statusStr, label, heading) {
  // Tractor icon inspired by icon_tractor.png
  // Drawn centered at (sx, sy) with the robot's identity colour.
  // heading is in radians (0 = right, pi/2 = up). The icon is drawn pointing UP
  // so we rotate by (heading - pi/2) to orient the "nose" toward the velocity direction.
  // Overall size ≈ 48 wide × 56 tall (comparable to the old drawRobot)
  const S = 0.44;  // scale factor from icon pixels to canvas pixels

  // Status beacon color: green=moving, blue=holding, red=idle, yellow=charging
  const antCol = statusStr === "STATUS_HOLDING"  ? "#89b4fa"
               : statusStr === "STATUS_IDLE"     ? "#f38ba8"
               : statusStr === "STATUS_CHARGING" ? "#f9e2af"
               : "#22dd22";

  // Rotate the entire tractor around its center
  ctx.save();
  ctx.translate(sx, sy);
  // heading=0 means +X (right); icon faces up (-Y), so rotate by heading + pi/2
  // In canvas, +Y is down, so atan2 gives angle where right=0, down=pi/2.
  // The icon nose points up (canvas -Y), so we need to add pi/2 to swing it.
  ctx.rotate(heading + Math.PI/2);

  // From here, draw relative to origin (0,0) instead of (sx,sy)
  const ox = 0, oy = 0;

  // -- shadow --
  ctx.fillStyle = "rgba(0,0,0,0.25)";
  ctx.beginPath();
  ctx.ellipse(2, 26, 26, 5, 0, 0, Math.PI*2);
  ctx.fill();

  const lw = 2.5;
  ctx.strokeStyle = col;
  ctx.lineWidth = lw;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";

  // -- Cabin (two windows side by side) --
  // Left window
  const cabY = -28, cabH = 18, cabGap = 2;
  const lwW = 12, rwW = 14;  // left/right window widths
  const cabTotalW = lwW + cabGap + rwW;
  const cabX = -cabTotalW/2;

  // Left window (solid tinted fill)
  ctx.fillStyle = col; ctx.globalAlpha = 0.35;
  ctx.fillRect(ox + cabX, oy + cabY, lwW, cabH);
  ctx.globalAlpha = 1.0;
  ctx.strokeStyle = col; ctx.lineWidth = lw;
  ctx.strokeRect(ox + cabX, oy + cabY, lwW, cabH);

  // Right window (solid tinted fill)
  ctx.fillStyle = col; ctx.globalAlpha = 0.35;
  ctx.fillRect(ox + cabX + lwW + cabGap, oy + cabY, rwW, cabH);
  ctx.globalAlpha = 1.0;
  ctx.strokeStyle = col; ctx.lineWidth = lw;
  ctx.strokeRect(ox + cabX + lwW + cabGap, oy + cabY, rwW, cabH);

  // Cabin roof (solid bar across top)
  ctx.fillStyle = col; ctx.globalAlpha = 0.5;
  ctx.fillRect(ox + cabX - 2, oy + cabY - 3, cabTotalW + 4, 3);
  ctx.globalAlpha = 1.0;
  ctx.strokeStyle = col; ctx.lineWidth = lw + 0.5;
  ctx.beginPath();
  ctx.moveTo(ox + cabX - 2, oy + cabY);
  ctx.lineTo(ox + cabX + cabTotalW + 2, oy + cabY);
  ctx.stroke();

  // -- Neck / pillar connecting cabin to chassis --
  const neckX1 = cabX + lwW + cabGap/2 - 1;
  const neckX2 = neckX1 + 2;
  const neckTop = cabY + cabH;
  const neckBot = cabY + cabH + 8;
  ctx.strokeStyle = col; ctx.lineWidth = lw;
  ctx.beginPath();
  ctx.moveTo(ox + neckX1, oy + neckTop);
  ctx.lineTo(ox + neckX1, oy + neckBot);
  ctx.moveTo(ox + neckX2, oy + neckTop);
  ctx.lineTo(ox + neckX2, oy + neckBot);
  ctx.stroke();

  // -- Chassis bar (horizontal bar between wheels) --
  const chY = neckBot;
  const chHalfW = 22;

  // -- Solid body plate (filled rectangle: cabin bottom to axle area) --
  const plateTop = neckTop;
  const plateBot = chY + 18;  // just above axle
  const plateHalfW = 10;
  ctx.fillStyle = col; ctx.globalAlpha = 0.25;
  ctx.fillRect(ox - plateHalfW, oy + plateTop, plateHalfW * 2, plateBot - plateTop);
  ctx.globalAlpha = 1.0;

  ctx.strokeStyle = col; ctx.lineWidth = lw + 0.5;
  ctx.beginPath();
  ctx.moveTo(ox - chHalfW, oy + chY);
  ctx.lineTo(ox + chHalfW, oy + chY);
  ctx.stroke();

  // -- Wheels (two tall rectangles) --
  const wW = 10, wH = 24;
  const wY = chY - 2;
  const wLeftX = -chHalfW;
  const wRightX = chHalfW - wW;

  // Left wheel (solid filled)
  const wr = 3; // corner radius
  ctx.beginPath();
  ctx.roundRect(ox + wLeftX, oy + wY, wW, wH, wr);
  ctx.fillStyle = col; ctx.globalAlpha = 0.7; ctx.fill();
  ctx.globalAlpha = 1.0;
  ctx.strokeStyle = col; ctx.lineWidth = lw; ctx.stroke();

  // Right wheel (solid filled)
  ctx.beginPath();
  ctx.roundRect(ox + wRightX, oy + wY, wW, wH, wr);
  ctx.fillStyle = col; ctx.globalAlpha = 0.7; ctx.fill();
  ctx.globalAlpha = 1.0;
  ctx.strokeStyle = col; ctx.lineWidth = lw; ctx.stroke();

  // Wheel hub lines (bright against solid wheels)
  ctx.strokeStyle = "#1e1e2e"; ctx.lineWidth = 1; ctx.globalAlpha = 0.5;
  for (const dx of [wLeftX, wRightX]) {
    for (let i = 1; i <= 3; i++) {
      const ly = wY + i * (wH / 4);
      ctx.beginPath();
      ctx.moveTo(ox + dx + 2, oy + ly);
      ctx.lineTo(ox + dx + wW - 2, oy + ly);
      ctx.stroke();
    }
  }
  ctx.globalAlpha = 1.0;

  // -- Axle bar (bottom connector between wheels) --
  const axleY = wY + wH - 3;
  ctx.strokeStyle = col; ctx.lineWidth = lw;
  ctx.beginPath();
  ctx.moveTo(ox + wLeftX + wW, oy + axleY);
  ctx.lineTo(ox + wRightX, oy + axleY);
  ctx.stroke();

  // -- Status beacon (glowing circle on the cabin ROOF, centred left-right) --
  // Place it at the vertical mid-point of the cabin so it reads as "on top"
  // from any heading, not at the nose/front of the tractor.
  const beaconR = 6;
  const beaconX = ox;                          // centred left-right
  const beaconY = cabY + cabH / 2;             // mid-height of cabin = roof centre in top-down view
  // Glow ring — makes the status color visible at a glance
  ctx.save();
  ctx.shadowColor = antCol;
  ctx.shadowBlur = 12;
  ctx.beginPath();
  ctx.arc(beaconX, oy + beaconY, beaconR, 0, Math.PI*2);
  ctx.fillStyle = antCol; ctx.fill();
  ctx.restore();
  ctx.strokeStyle = col; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.arc(beaconX, oy + beaconY, beaconR, 0, Math.PI*2); ctx.stroke();

  // -- Label (below tractor, drawn outside rotation so text stays upright) --
  ctx.restore();  // undo rotation+translation
  ctx.fillStyle = col; ctx.font = "bold 12px Menlo"; ctx.textAlign = "center";
  ctx.fillText(label, sx, sy + wY + wH + 16);
  ctx.lineCap = "butt";
}

function drawRobot(ctx, sx, sy, col, statusStr, label) {
  const R = 20;   // half-height of body
  const RW = 24;  // half-width of body (wider than tall)
  const lw = 3;   // main stroke width

  // Status beacon color: green=moving, blue=holding, red=idle, yellow=charging
  const antCol = statusStr === "STATUS_HOLDING"  ? "#89b4fa"
               : statusStr === "STATUS_IDLE"     ? "#f38ba8"
               : statusStr === "STATUS_CHARGING" ? "#f9e2af"
               : "#22dd22";

  // shadow
  ctx.fillStyle = "rgba(0,0,0,0.3)";
  ctx.beginPath();
  ctx.ellipse(sx+2, sy+R+4, RW+2, 5, 0, 0, Math.PI*2);
  ctx.fill();

  // antenna stalk
  ctx.strokeStyle = col; ctx.lineWidth = lw;
  ctx.beginPath(); ctx.moveTo(sx, sy - R); ctx.lineTo(sx, sy - R - 16); ctx.stroke();
  // antenna ball (bigger, solid filled)
  const antR = 9;
  ctx.beginPath(); ctx.arc(sx, sy - R - 16 - antR, antR, 0, Math.PI*2);
  ctx.fillStyle = antCol; ctx.fill();
  ctx.strokeStyle = col; ctx.lineWidth = lw; ctx.stroke();

  // body (rounded rectangle, wider than tall)
  const bx = sx - RW, by = sy - R, bw = RW*2, bh = R*2, cr = 8;
  ctx.beginPath();
  ctx.moveTo(bx+cr, by);
  ctx.lineTo(bx+bw-cr, by); ctx.quadraticCurveTo(bx+bw, by, bx+bw, by+cr);
  ctx.lineTo(bx+bw, by+bh-cr); ctx.quadraticCurveTo(bx+bw, by+bh, bx+bw-cr, by+bh);
  ctx.lineTo(bx+cr, by+bh); ctx.quadraticCurveTo(bx, by+bh, bx, by+bh-cr);
  ctx.lineTo(bx, by+cr); ctx.quadraticCurveTo(bx, by, bx+cr, by);
  ctx.closePath();
  ctx.fillStyle = col; ctx.globalAlpha = 0.2; ctx.fill();
  ctx.globalAlpha = 1.0;
  ctx.strokeStyle = col; ctx.lineWidth = lw; ctx.stroke();

  // eyes (simple hollow circles, no eyeballs)
  const eyeY = sy - 4, eyeOff = 9, eyeR = 6;
  ctx.beginPath(); ctx.arc(sx - eyeOff, eyeY, eyeR, 0, Math.PI*2);
  ctx.strokeStyle = col; ctx.lineWidth = lw; ctx.stroke();
  ctx.beginPath(); ctx.arc(sx + eyeOff, eyeY, eyeR, 0, Math.PI*2);
  ctx.strokeStyle = col; ctx.lineWidth = lw; ctx.stroke();

  // mouth (horizontal line with rounded ends)
  ctx.strokeStyle = col; ctx.lineWidth = 2.5; ctx.lineCap = "round";
  ctx.beginPath(); ctx.moveTo(sx - 7, sy + 10); ctx.lineTo(sx + 7, sy + 10); ctx.stroke();
  ctx.lineCap = "butt";

  // label (below robot)
  ctx.fillStyle = col; ctx.font = "bold 12px Menlo"; ctx.textAlign = "center";
  ctx.fillText(label, sx, sy + R + 22);
}

// --- Canvas map ---
function drawMap(state, intent) {
  const cv = document.getElementById("map");
  const ctx = cv.getContext("2d");
  cv.width = cv.clientWidth;
  cv.height = cv.clientHeight;
  const W = cv.width, H = cv.height, M = 30;

  ctx.fillStyle = "#181825"; ctx.fillRect(0,0,W,H);

  // Draw ground texture inside the arena area
  if (groundImg) {
    ctx.drawImage(groundImg, M, M, W - 2*M, H - 2*M);
    // Darken slightly so grid / icons pop over the photo
    ctx.fillStyle = "rgba(24,24,37,0.35)"; ctx.fillRect(M,M,W-2*M,H-2*M);
  }

  // grid
  ctx.font = "10px Menlo"; ctx.textAlign = "center";
  const gridCol = groundImg ? "rgba(255,255,255,0.12)" : "#313244";
  const borderCol = groundImg ? "rgba(255,255,255,0.30)" : "#45475a";
  for (let i = 0; i <= 100; i += 20) {
    const x = M + i/100*(W-2*M), y = M + (1-i/100)*(H-2*M);
    ctx.strokeStyle = gridCol; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x,M); ctx.lineTo(x,H-M); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(M,y); ctx.lineTo(W-M,y); ctx.stroke();
    ctx.fillStyle = "#6c7086";
    ctx.fillText(i, x, H-M+14);
    ctx.textAlign = "right"; ctx.fillText(i, M-6, y+4); ctx.textAlign = "center";
  }
  ctx.strokeStyle = borderCol; ctx.strokeRect(M,M,W-2*M,H-2*M);

  function toPx(px,py) { return [M+px/100*(W-2*M), M+(1-py/100)*(H-2*M)]; }

  // --- Draw charging station icon(s) from live stream data ---
  let stationsToDraw = [];
  const chargingIds = Object.keys(lastChargingData);
  if (chargingIds.length > 0) {
    for (const sid of chargingIds) {
      const s = lastChargingData[sid];
      stationsToDraw.push({x: s.dock_x, y: s.dock_y, id: sid, dock_robot: s.dock_robot, queue: s.queue || []});
    }
  }
  for (const st of stationsToDraw) {
    const [dx, dy] = toPx(st.x, st.y);
    // Cross-reference: dock_robot is set as soon as rank 0 is confirmed,
    // but the robot may still be en route.  Only show green "active" once
    // the robot's operational status flips to STATUS_CHARGING.
    const dockOps = st.dock_robot ? lastOperationalData[st.dock_robot] : null;
    const isCharging = dockOps && dockOps.status === "STATUS_CHARGING";
    const isEnRoute = !!st.dock_robot && !isCharging;
    // Glowing pad — brighter when charging
    ctx.save();
    ctx.shadowColor = "#fab387";
    ctx.shadowBlur = isCharging ? 20 : 10;
    ctx.fillStyle = isCharging ? "rgba(166,227,161,0.25)"
                  : isEnRoute  ? "rgba(137,180,250,0.20)"
                  :              "rgba(250,179,135,0.15)";
    ctx.beginPath(); ctx.arc(dx, dy, 18, 0, Math.PI*2); ctx.fill();
    ctx.restore();
    // Station outline
    ctx.strokeStyle = isCharging ? "#a6e3a1"
                    : isEnRoute  ? "#89b4fa"
                    :              "#fab387";
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(dx, dy, 14, 0, Math.PI*2); ctx.stroke();
    // Lightning bolt
    ctx.font = "bold 16px sans-serif";
    ctx.fillStyle = isCharging ? "#a6e3a1"
                  : isEnRoute  ? "#89b4fa"
                  :              "#fab387";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("⚡", dx, dy);
    // Label: show dock occupant or "CHARGE"
    ctx.font = "9px Menlo";
    ctx.fillStyle = isCharging ? "#a6e3a1"
                  : isEnRoute  ? "#89b4fa"
                  :              "#fab387";
    ctx.textBaseline = "top";
    const label = st.dock_robot ? st.dock_robot : "CHARGE";
    ctx.fillText(label, dx, dy + 18);
    // Queue count badge
    const qLen = st.queue.length;
    if (qLen > 0) {
      ctx.font = "bold 9px Menlo";
      ctx.fillStyle = "#f9e2af";
      ctx.textBaseline = "top";
      ctx.fillText(`[${qLen}]`, dx, dy + 28);
    }
    ctx.textBaseline = "alphabetic";
  }

  // --- Coverage trail lines (from gRPC StreamCoverage) ---
  if (showCoverage)
  for (const rid of Object.keys(coverageData)) {
    const trail = coverageData[rid];
    if (!trail || trail.length < 2) continue;
    const col = robotColor(rid);
    ctx.save();
    ctx.strokeStyle = col;
    ctx.globalAlpha = 0.45;
    ctx.lineWidth = 4;
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    ctx.beginPath();
    const [fx, fy] = toPx(trail[0].x, trail[0].y);
    ctx.moveTo(fx, fy);
    for (let i = 1; i < trail.length; i++) {
      const [px, py] = toPx(trail[i].x, trail[i].y);
      ctx.lineTo(px, py);
    }
    ctx.stroke();
    ctx.restore();
  }

  const allIds = [...new Set([...Object.keys(state),...Object.keys(intent)])].sort();
  for (const rid of allIds) {
    const col = robotColor(rid);
    if (!state[rid]) continue;
    const s = state[rid];
    const [sx,sy] = toPx(s.x, s.y);

    // intent arrow
    if (intent[rid]) {
      const idata = intent[rid];
      const [tx,ty] = toPx(idata.target_x, idata.target_y);

      // If path route is available, draw the full loop
      if (idata.waypoints && idata.waypoints.length > 1) {
        const wps = idata.waypoints;
        // Draw route loop
        ctx.setLineDash([4,6]); ctx.strokeStyle = col; ctx.lineWidth = 2.5;
        ctx.globalAlpha = 0.55;
        ctx.beginPath();
        const [fx,fy] = toPx(wps[0].x, wps[0].y);
        ctx.moveTo(fx,fy);
        for (let w=1; w<wps.length; w++) {
          const [wx,wy] = toPx(wps[w].x, wps[w].y);
          ctx.lineTo(wx,wy);
        }
        ctx.closePath(); ctx.stroke();
        ctx.globalAlpha = 1.0; ctx.setLineDash([]);

        // Draw waypoint dots (bigger, draggable)
        for (let w=0; w<wps.length; w++) {
          const [wx,wy] = toPx(wps[w].x, wps[w].y);
          const isDragged = dragging && dragging.rid === rid && dragging.wpIndex === w;
          const r = isDragged ? 14 : 11;
          ctx.beginPath(); ctx.arc(wx,wy,r,0,Math.PI*2);
          ctx.fillStyle = isDragged ? "#fff" : col;
          ctx.globalAlpha = isDragged ? 0.9 : 0.7; ctx.fill();
          ctx.globalAlpha = 1.0;
          ctx.strokeStyle = isDragged ? "#f38ba8" : col;
          ctx.lineWidth = isDragged ? 3 : 2; ctx.stroke();
          // waypoint index label
          ctx.fillStyle = isDragged ? "#1e1e2e" : "#fff";
          ctx.font = "bold 11px Menlo"; ctx.textAlign = "center";
          ctx.fillText(w, wx, wy+4);
        }
      }

      // Active intent arrow (robot → current target)
      ctx.setLineDash([4,4]); ctx.strokeStyle = col; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(sx,sy); ctx.lineTo(tx,ty); ctx.stroke();
      ctx.setLineDash([]);
      // arrowhead
      const a = Math.atan2(ty-sy, tx-sx), al = 10;
      ctx.fillStyle = col; ctx.beginPath();
      ctx.moveTo(tx,ty);
      ctx.lineTo(tx-al*Math.cos(a-0.4), ty-al*Math.sin(a-0.4));
      ctx.lineTo(tx-al*Math.cos(a+0.4), ty-al*Math.sin(a+0.4));
      ctx.fill();
    }

    // robot icon – compute heading from velocity
    const statusStr = (state[rid] && state[rid].status) || "STATUS_MOVING";
    const vx = (state[rid] && state[rid].vx) || 0;
    const vy = (state[rid] && state[rid].vy) || 0;
    // Use velocity direction when moving; fall back to the heading field
    // streamed in KinematicState (which the node keeps even when parked).
    // heading from proto is math convention (atan2(vy,vx)); canvas Y is
    // inverted, so we negate vy / negate the raw heading angle.
    const heading = (Math.abs(vx) + Math.abs(vy) > 0.01)
      ? Math.atan2(-vy, vx)
      : -(s.heading || 0);  // proto heading → canvas heading (negate for Y-flip)
    drawTractor(ctx, sx, sy, col, statusStr, rid, heading);
  }

  // --- Draw pending GOTO target crosshair ---
  if (gotoTarget) {
    const [gx, gy] = toPx(gotoTarget.x, gotoTarget.y);
    const r = 12;
    ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 2;
    // crosshair circle
    ctx.beginPath(); ctx.arc(gx, gy, r, 0, Math.PI*2); ctx.stroke();
    // crosshair lines
    ctx.beginPath(); ctx.moveTo(gx-r-4, gy); ctx.lineTo(gx+r+4, gy); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(gx, gy-r-4); ctx.lineTo(gx, gy+r+4); ctx.stroke();
    // label
    ctx.fillStyle = "#ffffff"; ctx.font = "10px Menlo"; ctx.textAlign = "center";
    ctx.fillText(`(${gotoTarget.x.toFixed(0)},${gotoTarget.y.toFixed(0)})`, gx, gy+r+14);
  }

  // --- Draw flash marker for waypoint add/remove feedback ---
  if (flashMarker && Date.now() < flashMarker.until) {
    const [fx, fy] = toPx(flashMarker.x, flashMarker.y);
    const elapsed = Date.now() - (flashMarker.until - 600);
    const progress = elapsed / 600;
    const radius = 8 + progress * 14;
    ctx.globalAlpha = 1.0 - progress;
    ctx.strokeStyle = flashMarker.color; ctx.lineWidth = 3;
    ctx.beginPath(); ctx.arc(fx, fy, radius, 0, Math.PI*2); ctx.stroke();
    ctx.fillStyle = flashMarker.color;
    ctx.beginPath(); ctx.arc(fx, fy, 4, 0, Math.PI*2); ctx.fill();
    ctx.globalAlpha = 1.0;
  } else {
    flashMarker = null;
  }
}

// resize canvas on window resize
window.addEventListener("resize", () => {
  const cv = document.getElementById("map");
  cv.width = cv.clientWidth; cv.height = cv.clientHeight;
});

// --- Command panel ---
let knownRobots = new Set();

function refreshRobotDropdown(state) {
  const ids = Object.keys(state).sort();
  const key = ids.join(",");

  // Command dropdown — "* ALL" is always the last option
  const sel = document.getElementById("cmd-robot");
  if (sel.dataset.key !== key) {
    const prev = sel.value;
    sel.dataset.key = key;
    sel.innerHTML = "";
    for (const rid of ids) {
      const opt = document.createElement("option");
      opt.value = rid; opt.textContent = rid;
      sel.appendChild(opt);
    }
    // Broadcast option last
    const allOpt = document.createElement("option");
    allOpt.value = "*"; allOpt.textContent = "* ALL";
    sel.appendChild(allOpt);
    if (prev === "*" || ids.includes(prev)) sel.value = prev;
  }

  // Video dropdown
  const vsel = document.getElementById("vid-robot");
  if (vsel.dataset.key !== key) {
    const vprev = vsel.value;
    vsel.dataset.key = key;
    vsel.innerHTML = "";
    for (const rid of ids) {
      const opt = document.createElement("option");
      opt.value = rid; opt.textContent = rid;
      vsel.appendChild(opt);
    }
    if (ids.includes(vprev)) vsel.value = vprev;
  }
}

// --- Video panel ---
let activeVideoRobot = null;

function startVideo() {
  const rid = document.getElementById("vid-robot").value;
  if (!rid) return;
  activeVideoRobot = rid;
  const container = document.getElementById("vid-container");
  container.innerHTML = `<img src="/video_feed/${rid}" alt="Onboard camera: ${rid}">`;
}

function stopVideo() {
  activeVideoRobot = null;
  const container = document.getElementById("vid-container");
  container.innerHTML = '<div class="placeholder">Select a robot and click Start</div>';
}

async function sendCommand(robot_id, command, extra) {
  const el = document.getElementById("cmd-result");
  el.textContent = `Sending ${command}…`;
  el.style.color = "var(--dim)";
  try {
    const payload = Object.assign({robot_id, command}, extra || {});
    const resp = await fetch("/command", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const data = await resp.json();

    if (data.broadcast) {
      // Broadcast response — show call count and per-robot pass/fail
      const rows = Object.entries(data.results || {})
        .sort(([a],[b]) => a.localeCompare(b))
        .map(([rid, r]) => {
          const sym = r.success ? "✓" : "✗";
          const col = r.success ? "var(--green)" : "var(--red)";
          return `<span style="color:${col}">${sym} ${rid}</span>`;
        }).join("  ");
      el.innerHTML =
        `<span style="color:var(--yellow)">⬡ ${data.command} → all robots</span>  ` +
        `<span style="color:var(--peach)">(${data.grpc_calls_made} gRPC calls)</span><br>` +
        rows;
    } else {
      // Single-robot response
      const ok = data.success;
      let msg = `${ok ? "✓" : "✗"} ${data.description}`;
      if (ok && data.resulting_status) msg += ` [${data.resulting_status}/${data.resulting_intent}]`;
      el.textContent = msg;
      el.style.color = ok ? "var(--green)" : "var(--red)";
    }
  } catch (e) {
    el.textContent = `✗ ${e}`;
    el.style.color = "var(--red)";
  }
}

function sendGoto() {
  const rid = document.getElementById("cmd-robot").value;
  const x = parseFloat(document.getElementById("cmd-x").value);
  const y = parseFloat(document.getElementById("cmd-y").value);
  const spd = parseFloat(document.getElementById("cmd-speed").value) || 0;
  if (!rid) return;
  setTimeout(() => { gotoTarget = null; }, 1500);  // keep crosshair visible briefly
  sendCommand(rid, "GOTO", {goto_x: x, goto_y: y, goto_speed: spd});
}

function sendSimple(cmd) {
  const rid = document.getElementById("cmd-robot").value;
  if (!rid) return;
  const spd = parseFloat(document.getElementById("cmd-speed").value) || 0;
  sendCommand(rid, cmd, {goto_speed: spd});
}

// --- Click on map to set GOTO target / drag waypoints ---
let gotoTarget = null;  // {x, y} in arena coords

// Drag state for path waypoints
let dragging = null;     // {rid, wpIndex, waypoints} while dragging
let lastIntentData = {}; // stash latest intent data for hit-testing
let lastChargingData = {}; // stash latest charging station status
let lastOperationalData = {}; // stash latest operational state per robot
let dblClickPending = false;  // suppress GOTO on double-click

// Store intent data each frame so mouse handlers can access it
const _origDrawMap = drawMap;
// We patch drawMap inline — intent data is captured in the SSE handler below.

// Helper: convert mouse event → arena coords
function mouseToArena(e, cv) {
  const rect = cv.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  const W = cv.width, H = cv.height, M = 30;
  const ax = (mx - M) / (W - 2*M) * 100;
  const ay = (1 - (my - M) / (H - 2*M)) * 100;
  return {ax, ay, inBounds: ax >= 0 && ax <= 100 && ay >= 0 && ay <= 100};
}

// Helper: find waypoint under mouse (returns {rid, wpIndex} or null)
function hitTestWaypoint(ax, ay) {
  const hitRadius = 6; // arena-coord units
  for (const rid of Object.keys(lastIntentData)) {
    const idata = lastIntentData[rid];
    if (!idata || !idata.waypoints) continue;
    for (let i = 0; i < idata.waypoints.length; i++) {
      const wp = idata.waypoints[i];
      const dx = wp.x - ax, dy = wp.y - ay;
      if (Math.sqrt(dx*dx + dy*dy) < hitRadius) {
        return {rid, wpIndex: i, waypoints: idata.waypoints.map(w => ({x:w.x, y:w.y}))};
      }
    }
  }
  return null;
}

// Helper: find the nearest robot's path route to a point.
// Returns {rid, insertIndex, waypoints} or null if no robot has a path route.
function findNearestRoute(ax, ay) {
  let best = null, bestDist = Infinity;
  for (const rid of Object.keys(lastIntentData)) {
    const idata = lastIntentData[rid];
    if (!idata || !idata.waypoints || idata.waypoints.length < 2) continue;
    const wps = idata.waypoints;
    // Check distance to each route segment; find closest segment for insertion
    for (let i = 0; i < wps.length; i++) {
      const a = wps[i], b = wps[(i+1) % wps.length];
      // Distance from point to segment midpoint (simple heuristic)
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      const d = Math.sqrt((ax-mx)*(ax-mx) + (ay-my)*(ay-my));
      // Also check distance to each waypoint
      const da = Math.sqrt((ax-a.x)*(ax-a.x) + (ay-a.y)*(ay-a.y));
      const segDist = Math.min(d, da);
      if (segDist < bestDist) {
        bestDist = segDist;
        best = {rid, insertIndex: (i+1) % wps.length, waypoints: wps.map(w => ({x:w.x, y:w.y}))};
      }
    }
  }
  return best;
}

// Brief visual flash for add/remove feedback
let flashMarker = null;  // {x, y, color, until}

const mapCanvas = document.getElementById("map");

mapCanvas.addEventListener("mousedown", (e) => {
  const {ax, ay, inBounds} = mouseToArena(e, mapCanvas);
  if (!inBounds) return;

  const hit = hitTestWaypoint(ax, ay);
  if (hit) {
    // Alt+click (⌥+click) on a waypoint → remove it
    if (e.altKey) {
      const el = document.getElementById("cmd-result");
      if (hit.waypoints.length <= 2) {
        // Don't allow removing below 2 waypoints (need at least 2 for a route)
        el.textContent = "✗ Path needs at least 2 waypoints";
        el.style.color = "var(--red)";
        return;
      }
      hit.waypoints.splice(hit.wpIndex, 1);
      // Flash red at the removed location
      const removedWp = lastIntentData[hit.rid].waypoints[hit.wpIndex];
      flashMarker = {x: removedWp.x, y: removedWp.y, color: "#f38ba8", until: Date.now() + 600};
      // Update local data immediately for visual feedback
      lastIntentData[hit.rid].waypoints = hit.waypoints;
      el.textContent = `Removing waypoint from ${hit.rid}'s path…`;
      el.style.color = "var(--peach)";
      sendCommand(hit.rid, "SET_PATH", {waypoints: hit.waypoints});
      e.preventDefault();
      return;
    }

    // Normal click on waypoint → start drag
    dragging = hit;
    mapCanvas.style.cursor = "grabbing";
    e.preventDefault();
  }
});

mapCanvas.addEventListener("mousemove", (e) => {
  const {ax, ay, inBounds} = mouseToArena(e, mapCanvas);

  if (dragging) {
    // Update the dragged waypoint position
    if (inBounds) {
      dragging.waypoints[dragging.wpIndex] = {x: ax, y: ay};
      // Temporarily override the intent data so drawMap shows the dragged position
      if (lastIntentData[dragging.rid]) {
        lastIntentData[dragging.rid].waypoints = dragging.waypoints;
      }
    }
    e.preventDefault();
    return;
  }

  // Hover cursor hint
  if (inBounds) {
    const hit = hitTestWaypoint(ax, ay);
    if (hit) {
      mapCanvas.style.cursor = e.altKey ? "not-allowed" : "grab";
    } else {
      mapCanvas.style.cursor = "crosshair";
    }
  }
});

mapCanvas.addEventListener("mouseup", (e) => {
  if (dragging) {
    const {rid, waypoints} = dragging;
    dragging = null;
    mapCanvas.style.cursor = "crosshair";
    // Send updated path route to the robot
    sendCommand(rid, "SET_PATH", {waypoints});
    return;
  }

  // Alt+click on empty space → ignore (only remove waypoints)
  if (e.altKey) return;

  // Suppress GOTO when user is double-clicking (to add a waypoint instead)
  if (dblClickPending) return;

  // Regular click → set GOTO target (with short delay to detect double-click)
  const {ax, ay, inBounds} = mouseToArena(e, mapCanvas);
  if (!inBounds) return;
  // Don't set GOTO target if we clicked on an existing waypoint
  if (hitTestWaypoint(ax, ay)) return;
  dblClickPending = true;
  setTimeout(() => {
    if (dblClickPending) {
      document.getElementById("cmd-x").value = ax.toFixed(1);
      document.getElementById("cmd-y").value = ay.toFixed(1);
      gotoTarget = {x: ax, y: ay};
    }
    dblClickPending = false;
  }, 250);
});

// Double-click → add a waypoint to the nearest robot's path route
mapCanvas.addEventListener("dblclick", (e) => {
  dblClickPending = false;  // cancel any pending GOTO from the mouseup
  gotoTarget = null;        // clear any crosshair that snuck in
  const {ax, ay, inBounds} = mouseToArena(e, mapCanvas);
  if (!inBounds) return;

  // Don't add if we clicked on an existing waypoint
  if (hitTestWaypoint(ax, ay)) return;

  const nearest = findNearestRoute(ax, ay);
  if (!nearest) {
    const el = document.getElementById("cmd-result");
    el.textContent = "✗ No path route to add waypoint to";
    el.style.color = "var(--red)";
    return;
  }

  // Insert new waypoint at the best position in the route
  nearest.waypoints.splice(nearest.insertIndex, 0, {x: ax, y: ay});
  // Flash green at the new location
  flashMarker = {x: ax, y: ay, color: "#a6e3a1", until: Date.now() + 600};
  // Update local data immediately for visual feedback
  if (lastIntentData[nearest.rid]) {
    lastIntentData[nearest.rid].waypoints = nearest.waypoints;
  }
  const el = document.getElementById("cmd-result");
  el.textContent = `Adding waypoint to ${nearest.rid}'s path…`;
  el.style.color = "var(--green)";
  sendCommand(nearest.rid, "SET_PATH", {waypoints: nearest.waypoints});
  e.preventDefault();
});

mapCanvas.addEventListener("mouseleave", () => {
  if (dragging) {
    // Cancel drag if mouse leaves canvas
    dragging = null;
    mapCanvas.style.cursor = "crosshair";
  }
});
</script>
</body>
</html>"""


def create_app(store: FleetStateStore, client: GrpcFleetClient) -> Flask:
    """Create the Flask app.

    *store* is the read-only data source for SSE snapshots.
    *client* is used exclusively for sending commands.
    """
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template_string(DASHBOARD_HTML)

    @app.route("/stream")
    def stream():
        def generate():
            while client.running:
                data = json.dumps(store.snapshot())
                yield f"data: {data}\n\n"
                time.sleep(0.2)  # 5 Hz
        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.route("/command", methods=["POST"])
    def command():
        body = request.get_json(force=True)
        robot_id = body.get("robot_id", "")
        cmd = body.get("command", "")
        goto_x = float(body.get("goto_x", 0))
        goto_y = float(body.get("goto_y", 0))
        goto_speed = float(body.get("goto_speed", 0))
        waypoints = body.get("waypoints", None)
        if robot_id == "*":
            result = client.send_command_all(cmd,
                                             goto_x=goto_x, goto_y=goto_y,
                                             goto_speed=goto_speed,
                                             waypoints=waypoints)
        else:
            result = client.send_command(robot_id, cmd,
                                         goto_x=goto_x, goto_y=goto_y,
                                         goto_speed=goto_speed,
                                         waypoints=waypoints)
        return jsonify(result)

    @app.route("/clear_coverage", methods=["POST"])
    def clear_coverage():
        store.clear_coverage()
        return jsonify({"success": True})

    @app.route("/video_feed/<robot_id>")
    def video_feed(robot_id):
        """MJPEG stream from a robot's onboard camera.

        The browser renders this with a simple ``<img src="…">``.
        Each JPEG frame is delivered as a MIME multipart chunk.
        """
        def generate():
            for jpeg in client.stream_video(robot_id):
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n"
                       + jpeg + b"\r\n")

        return Response(generate(),
                        mimetype="multipart/x-mixed-replace; boundary=frame",
                        headers={"Cache-Control": "no-cache"})

    @app.route("/ground_texture.jpg")
    def ground_texture():
        """Serve the farmland aerial-photo used as the 2D canvas background."""
        return send_from_directory(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared'),
            "field1_map.jpg",
            mimetype="image/jpeg",
        )

    return app


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Robot Fleet UI – connects to robots via gRPC and displays live data"
    )
    parser.add_argument(
        "--fleet-port-list", default="",
        help="Comma-separated list of all fleet member gRPC ports  (e.g. 50051,50052,50053). "
             "If omitted, robots are discovered automatically via mDNS (Zeroconf).",
    )
    parser.add_argument(
        "--ui-port", type=int, required=True,
        help="HTTP port for the dashboard UI  (e.g. 5000)",
    )
    parser.add_argument(
        "--same-host", action="store_true",
        help="Discovery mode only: all robots run on the same machine. "
             "Connects via localhost instead of the mDNS-advertised IP.",
    )
    args = parser.parse_args()

    # Create the state store (transport-agnostic) and gRPC client
    store = FleetStateStore()
    client = GrpcFleetClient(store)

    use_localhost = args.same_host or bool(args.fleet_port_list)

    def _on_station_discovered(address: str):
        """Connect to a newly discovered charging station."""
        host, port_s = address.rsplit(":", 1)
        target = f"localhost:{port_s}" if use_localhost else address
        client.connect_to_station(target)

    if args.fleet_port_list:
        # ── Static mode: connect to listed ports ──────────────────────────
        for p in args.fleet_port_list.split(","):
            client.connect_to_port(int(p.strip()))
    else:
        # ── Discovery mode: use Zeroconf / mDNS ──────────────────────────
        print(f"[ui] No --fleet-port-list given → using mDNS discovery")

        def _on_peer_discovered(address: str):
            """Connect to a newly discovered robot.

            In --same-host mode, connect via localhost.  In multi-host
            mode, connect to the mDNS-advertised address directly.
            """
            host, port_s = address.rsplit(":", 1)
            port = int(port_s)
            if use_localhost:
                client.connect_to_port(port)           # → localhost:{port}
            else:
                client.connect_to_address(host, port)  # → real IP

        discovery = FleetDiscovery(
            robot_id="ui",
            grpc_port=None,   # UI doesn't register — it only browses
            browse_types=[CMD_SERVICE_TYPE, STREAM_SERVICE_TYPE],
            on_peer_added=_on_peer_discovered,
            on_station_added=_on_station_discovered,
        )
        discovery.start()

    # In static mode, still discover charging stations via mDNS
    if args.fleet_port_list:
        station_discovery = FleetDiscovery(
            robot_id="ui",
            browse_types=[],   # no robot browsing in static mode
            on_station_added=_on_station_discovered,
        )
        station_discovery.start()

    app = create_app(store, client)
    print(f"\n  Dashboard → http://localhost:{args.ui_port}\n")
    app.run(host="0.0.0.0", port=args.ui_port, threaded=True)


if __name__ == "__main__":
    main()
