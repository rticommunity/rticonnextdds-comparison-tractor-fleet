# Pure gRPC — Full Mesh Streams

> System-level specification for the pure gRPC approach of the robot fleet demo.  This
> document maps the high-level scenario requirements (see top-level [README](../README.md)) to the
> concrete programs, protocol, and data flows implemented in this directory.
> Per-file implementation details are delegated to the component specs listed
> in §3.

### Component Specs

[`robot_proto_spec.md`](robot_proto_spec.md) ·
[`robot_node_spec.md`](robot_node_spec.md) ·
[`robot_ui_spec.md`](robot_ui_spec.md) ·
[`charging_station_spec.md`](charging_station_spec.md)

### Quick Start

```bash
./run_protoc.sh            # generate protobuf stubs (first time / after proto changes)
./run_demo.sh all          # launch stations + robots + UI → http://localhost:5000
./stop_demo.sh             # stop everything
```

Individual components:

```bash
./run_demo.sh stations     # all charging stations
./run_demo.sh robots       # all robots (mDNS discovery)
./run_demo.sh ui           # dashboard UI
./run_demo.sh robot tractor1   # single robot (port auto-derived from name)
./run_demo.sh station station1 # single station (dock coords auto-derived)
```

The CLI is **uniform** across all three approaches — same commands, same
arguments.  Robot/station names and dock coordinates come from
`../shared/fleet_common.sh`; ports are computed automatically from the
approach-specific `fleet_config.sh`.

---

## 1  Scope

This approach implements the **Full Mesh gRPC** architecture described in the
top-level README:

- Pure Python, gRPC for **all** communication.
- gRPC **server-streaming** RPCs for state, intent, and telemetry.
- gRPC **unary** RPCs for commands.
- gRPC **server-streaming** RPC for on-demand video.
- Every process communicates **directly** with every other — no broker, no
  central hub.
- **Peer discovery** via Zeroconf mDNS/DNS-SD (`fleet_discovery.py`) — robots
  and charging stations register themselves on the local network and are found
  automatically.  A static port-list fallback is still supported.

### Scenario Requirements Addressed

| Requirement | How the gRPC approach handles it |
|-------------|---------------------------|
| Late joiners converge quickly | Each server-streaming RPC begins yielding the robot's current data immediately on subscription — the new subscriber converges within one publish period (≤ 0.1 s for kinematic state). |
| Presence detection ≤ 10 ms | **Not met.** gRPC/TCP detection relies on channel connectivity callbacks and timeouts measured in seconds, not milliseconds. |
| Intent delivered reliably | gRPC streams over TCP — ordered, reliable delivery.  Stream breakage triggers automatic reconnection. |
| State is periodic | `StreamKinematicState` publishes at 10 Hz; `StreamOperationalState` at 1 Hz. |
| Telemetry loss tolerable | `StreamTelemetry` publishes at 1 Hz; simulated values (CPU, memory, temperature, signal strength) — loss of individual updates is harmless. |
| Commands are reliable | Unary gRPC request/response with explicit success/failure acknowledgement. |
| Robots come and go | Per-peer reconnect loops with 3 s retry.  Channel connectivity monitoring detects `TRANSIENT_FAILURE` / `SHUTDOWN`.  In discovery mode, Zeroconf `ServiceBrowser` fires add/remove callbacks as robots appear and disappear. |
| IP addresses change | In **discovery mode**, Zeroconf re-advertises the current IP.  In static mode this is **not met** — peer addresses are fixed at launch. |
| Video streaming | `RequestVideo` server-streaming RPC renders real-time 3D first-person video via PyBullet at ~5 fps. |

### Known Limitations (trade-offs motivating other approaches)

- **N² connections** — each robot maintains a gRPC channel to every other robot.
  With *N* robots the peer mesh alone is *N × (N−1)* TCP connections and
  *4 × N × (N−1)* streaming RPCs (kinematic + operational + intent + telemetry).
  The UI adds *N* channels with 5 streams each (the four above + coverage).
  For the demo fleet of 5 robots: 25 channels, 105 streams;
  at 100 robots it becomes 10 000 channels and 40 500 streams.
- **No multicast / broadcast** — every state update is serialised and
  transmitted individually to each subscriber.
- **No configurable QoS** — all streams are best-effort-reliable (TCP).  There
  is no way to express "loss-tolerant" or "deadline" semantics per-stream.
- **Discovery requires a bolted-on protocol** — Zeroconf mDNS (RFC 6762/6763)
  works but adds ~200 lines of glue code and a separate multicast stack that
  gRPC knows nothing about.  DDS does this natively.
- **Reconnection storm** — a brief network interruption causes all peers to
  reconnect simultaneously, creating a CPU/memory spike.

---

## 2  Architecture Overview

```
┌─────────────┐     gRPC (full mesh)     ┌─────────────┐
│ robot_node  │◄────────────────────────►│ robot_node  │
│ (port 50051)│                          │ (port 50052)│
└──────┬───┬──┘                          └──┬───┬──────┘
       │   │         ┌─────────────┐        │   │
       │   └────────►│ robot_node  │◄───────┘   │
       │             │ (port 50053)│            │
       │             └──────┬──────┘            │
       │                    │                   │
       └────────────┬───────┴───────────────────┘
                    │
                    │  gRPC (subscribe + command + video)
                    ▼
             ┌──────────────┐       gRPC (slot mgmt)     ┌──────────────────┐
             │ robot_ui     │◄──────────────────────────►│ charging_station │
             │ (Flask :5000)│   StreamStatus             │  (port 50060/61) │
             └──────────────┘                            └──────────────────┘
                    │                                            ▲
                    │  HTTP / SSE / MJPEG                        │ RequestSlot
                    ▼                                            │ ConfirmSlot
             ┌──────────────┐                                    │ ReleaseSlot
             │   Browser    │                    robot_node ─────┘
             └──────────────┘
```

All participants discover each other via **Zeroconf mDNS/DNS-SD**:
- Robots register on `_robot-fleet._tcp.local.`
- Charging stations register on `_chg-station._tcp.local.`

### Participants

| Program | Count | Role |
|---------|-------|------|
| `robot_node.py` | 1 per robot (5 in default config) | Autonomous robot: follows its path, avoids collisions, serves state/intent/telemetry streams, accepts commands, renders onboard video, negotiates charging slots |
| `charging_station.py` | 1 per station (2 in default config) | Charging dock manager: FIFO queue, slot negotiation, status streaming |
| `robot_ui.py` | 1 | Fleet dashboard: subscribes to all robots' streams + station status, serves a web UI with live map + tables + command panel + video feed + charging panel |

Every `robot_node` is both a **gRPC server** (serving its own data to peers and
the UI) and a **gRPC client** (subscribing to every other robot's streams for
collision avoidance, and calling charging station RPCs).

The `robot_ui` is a **gRPC client only** — it subscribes to streams and sends
commands, but does not serve gRPC to other participants.

Each `charging_station` is a **gRPC server only** — it serves slot management
RPCs to robots and a status stream to the UI.

### Topology

Full mesh among robot nodes.  The UI and charging stations are leaves — no
robot subscribes to them.  Robots call station RPCs when they need to charge.

---

## 3  Component Inventory

| File | Purpose | Detailed Spec |
|------|---------|---------------|
| `robot.proto` | Protobuf / gRPC service definitions (services, messages, enums) | [`robot_proto_spec.md`](robot_proto_spec.md) |
| `robot_pb2.py` | Generated Python protobuf code (message classes) | — (auto-generated, gitignored) |
| `robot_pb2_grpc.py` | Generated Python gRPC stubs and servicers | — (auto-generated, gitignored) |
| `robot_node.py` | Robot node — full mesh networking, path following, collision avoidance, commands, charging, video rendering, coverage tracking | [`robot_node_spec.md`](robot_node_spec.md) |
| `charging_station.py` | Charging station — FIFO dock queue, slot negotiation, status streaming | [`charging_station_spec.md`](charging_station_spec.md) |
| `robot_ui.py` | Flask web dashboard — gRPC subscriber, SSE publisher, command proxy, video proxy, charging panel, coverage map overlay | [`robot_ui_spec.md`](robot_ui_spec.md) |
| `fleet_discovery.py` | Zeroconf mDNS/DNS-SD — advertise & discover robots and charging stations | — |
| `fleet_config.sh` | Transport config for this approach (gRPC base ports); sources `../shared/fleet_common.sh` for scenario data | — |
| `run_demo.sh` | Uniform launch script: `all`, `robots`, `stations`, `ui`, `robot <name>`, `station <name>` | — |
| `run_protoc.sh` | Regenerate `robot_pb2*.py` from `robot.proto` | — |
| `stop_demo.sh` | Kill all running robot, station, and UI processes, free ports | — |

**Shared assets** (in `../shared/`):

| File | Purpose |
|------|---------|
| `fleet_common.sh` | Single source of truth for robot names, station positions, UI port — shared by all approaches |
| `video_renderer.py` | PyBullet headless 3D renderer — compound tractor bodies, waypoint beacons, text overlay |
| `arena_ground.urdf` | URDF arena ground plane for PyBullet |
| `field1_map.jpg` / `field1_texture.jpg` | Ground textures for canvas map and 3D renderer |

---

## 4  Data Flows

### 4.1  State / Intent / Telemetry (robot → peers + UI)

```
robot_node (server)                             robot_node / robot_ui (client)
───────────────────                             ────────────────────────────────
RobotStreaming.StreamKinematicState   ──10 Hz──►  position, velocity, heading
RobotStreaming.StreamOperationalState ── 1 Hz──►  status, battery level
RobotStreaming.StreamIntent          ── 1 Hz──►  mission type, target, path waypoints
RobotStreaming.StreamTelemetry       ── 1 Hz──►  CPU, memory, temperature, signal strength, TCP count
RobotStreaming.StreamCoverage        ──on move─►  coverage trail points (robot_id, x, y, seq)
```

All five are **server-streaming** RPCs.  The client sends a single
`SubscribeRequest(subscriber_id=…)` and the server yields updates in a loop.

### 4.2  Coverage (robot → UI)

Each robot publishes `CoveragePoint` messages as it moves along its path.
The UI accumulates these into per-robot polylines drawn on the map canvas
(lineWidth 4, alpha 0.45, rounded joins).  A toggle button and "Clear Coverage"
button control display.

### 4.2  Commands (UI → robot)

```
Browser  ──POST /command──►  Flask  ──gRPC SendCommand──►  robot_node
         ◄── JSON result ──         ◄── CommandResponse ──
```

Unary RPC.  The UI opens a **new** gRPC channel per command, sends
`SendCommand`, and closes it.

### 4.3  Video (UI → robot → browser)

```
Browser  ──GET /video_feed/<rid>──►  Flask  ──gRPC RequestVideo──►  robot_node
         ◄── MJPEG stream ────────          ◄── stream VideoFrame ──
```

The robot renders real-time 3D frames via PyBullet at ~5 fps.  Flask wraps each
JPEG in a MIME multipart chunk (`multipart/x-mixed-replace`) so the browser
displays it as a live-updating `<img>`.

### 4.4  Charging (robot ↔ station, UI ← station)

```
robot_node  ──RequestSlot──►  charging_station   (get estimated wait)
            ──ConfirmSlot──►                      (commit to queue)
            ──ReleaseSlot──►                      (done / cancel)

robot_ui    ──StreamStatus──►  charging_station   (live queue + dock state)
```

Robots discover stations via Zeroconf, compare offers from all stations, then
confirm the best slot.  The UI subscribes to each station's `StreamStatus` for
the charging panel and map icons.

### 4.5  SSE (Flask → browser)

```
Flask /stream  ──SSE 5 Hz──►  Browser EventSource
```

The Flask backend snapshots the `FleetStateStore` every 200 ms and pushes it as
a JSON SSE event.  The browser JS updates all tables and redraws the canvas map.

### 4.6  Discovery (all participants)

```
                          mDNS multicast (224.0.0.251:5353)
robot_node  ──register──►  _robot-fleet._tcp.local.    ◄──browse── robot_node / robot_ui
station     ──register──►  _chg-station._tcp.local.    ◄──browse── robot_node / robot_ui
```

`FleetDiscovery` (in `fleet_discovery.py`) wraps the `zeroconf` library.  Each
participant registers its gRPC endpoint and browses for peers.  Callbacks fire
when services appear or disappear, triggering connection or cleanup.

---

## 5  Protocol Summary

Defined in `robot.proto` (see [`robot_proto_spec.md`](robot_proto_spec.md) for
design rationale).

### Services

| Service | RPC | Type | Purpose |
|---------|-----|------|---------|
| `RobotStreaming` | `StreamKinematicState` | server-streaming | Position, velocity, heading at 10 Hz |
| `RobotStreaming` | `StreamOperationalState` | server-streaming | Status, battery level at 1 Hz |
| `RobotStreaming` | `StreamIntent` | server-streaming | Mission type, target, path waypoints at 1 Hz |
| `RobotStreaming` | `StreamTelemetry` | server-streaming | CPU, memory, temperature, signal strength, TCP count at 1 Hz |
| `RobotCommand` | `SendCommand` | unary | Operator commands (STOP, GOTO, FOLLOW_PATH, RESUME, SET_PATH, CHARGE) |
| `RobotCommand` | `RequestVideo` | server-streaming | On-demand first-person video frames |
| `ChargingStation` | `RequestSlot` | unary | Non-committing: get estimated wait time |
| `ChargingStation` | `ConfirmSlot` | unary | Commit to a queue slot |
| `ChargingStation` | `ReleaseSlot` | unary | Release dock or cancel queue position |
| `ChargingStation` | `StreamStatus` | server-streaming | Live station status (UI subscribes) |

### Key Enums

- `RobotStatus`: UNKNOWN / MOVING / IDLE / HOLDING / CHARGING
- `RobotIntent`: UNKNOWN / FOLLOW_PATH / GOTO / IDLE / CHARGE_QUEUE / CHARGE_DOCK
- `Command`: UNKNOWN / CMD_STOP / CMD_FOLLOW_PATH / CMD_GOTO / CMD_RESUME / CMD_SET_PATH / CMD_CHARGE

---

## 6  Build & Run

### Prerequisites

- Python 3.14.3+
- Virtual environment with packages: `grpcio`, `grpcio-tools`, `protobuf`,
  `flask`, `pybullet`, `Pillow`, `numpy`, `zeroconf`, `ifaddr`

### Generate Protobuf Stubs

```bash
./run_protoc.sh
# or: python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. robot.proto
```

This produces `robot_pb2.py` and `robot_pb2_grpc.py`.  These files are
gitignored — re-run after any change to `robot.proto`.

### Fleet Configuration

`fleet_config.sh` defines the fleet ports, charging station ports, and UI port:

```bash
FLEET_PORT_LIST="50051,50052,50053,50054,50055"   # 5 robots
UI_PORT=5000

CHARGING_STATION1_PORT=50060    # dock at (8, 92) — top-left
CHARGING_STATION2_PORT=50061    # dock at (92, 8) — bottom-right
```

Edit this file to add or remove robots or stations.  All scripts source it.

### Launch (Discovery Mode — Recommended)

```bash
# Charging stations + all 5 robots (peers found via mDNS):
./run_demo.sh discover

# Dashboard UI (discovers robots + stations via mDNS):
./run_demo.sh ui-discover    # http://localhost:5000
```

### Launch (Static Mode — No Zeroconf)

```bash
# All 5 robots in one terminal (port list passed on CLI):
./run_demo.sh all

# Or individual robots in separate terminals:
./run_demo.sh fred 1       # port 50051
./run_demo.sh alice 2      # port 50052
./run_demo.sh bob 3        # port 50053
./run_demo.sh carol 4      # port 50054
./run_demo.sh dave 5       # port 50055

# Dashboard UI (static):
./run_demo.sh ui           # http://localhost:5000
```

### Stop

```bash
./stop_demo.sh             # kills all robot_node, charging_station, and robot_ui processes
```

---

## 7  Thread Model (per robot node)

Each `robot_node` runs approximately **19 threads** for a 5-robot fleet:

| Thread | Count | Purpose |
|--------|-------|---------|
| Main | 1 | Blocks on `sleep(1)` loop |
| `update_position` | 1 | Movement tick at 10 Hz (path following, collision avoidance) |
| `print_status` | 1 | Log line every 5 s |
| Per-peer connector | N−1 | `_connect_to_peer` reconnect loops |
| Per-peer stream readers | 4 × (N−1) | Kinematic / operational / intent / telemetry iterators |
| Charge poll thread | 0–1 | Polls `ConfirmSlot` while queued at a station |
| gRPC server pool | 50 (max) | Handles incoming RPCs (streams + commands + video) |

The UI has a similar structure: 1 main thread, 1 thread per robot port
(reconnect loop), 4 reader threads per connected robot, 1 thread per charging
station (`StreamStatus`), plus Flask's HTTP server.

---

## 8  Relationship to Other Approaches

| Aspect | gRPC (this) | Hybrid (grpc-dds/) | DDS pub-sub (dds/) | DDS + RPC (dds/) |
|--------|--------------------|----------------|----------------|--------------|
| Topology | Full mesh (N²) | Star (N) | Peer-to-peer multicast | Peer-to-peer multicast |
| Transport | gRPC / TCP | gRPC / TCP | DDS / UDP multicast | DDS / UDP multicast |
| Discovery | Zeroconf mDNS (bolted on) | Central server | Automatic (DDS built-in) | Automatic (DDS built-in) |
| QoS | None (TCP reliable) | None | Per-topic QoS policies | Per-topic QoS policies |
| Commands | gRPC unary | gRPC unary | gRPC unary | DDS-RPC |
| Scalability | Poor (N²) | Better (N) | Good (multicast) | Good (multicast) |
| Presence detection | Seconds (TCP) | Seconds (TCP) | Milliseconds (liveliness) | Milliseconds (liveliness) |

This comparison is the central thesis of the webinar: The gRPC approach is the
simplest to build and reason about, but it hits fundamental scaling and
operational walls that the data-centric approaches solve by design.

Even with Zeroconf bolted on, discovery remains a separate protocol stack with
its own failure modes — DDS provides discovery, presence, and QoS as first-class
features with zero extra code.

---

## 9  Failure Modes

These are the observable failure scenarios inherent to the full-mesh TCP/gRPC
architecture — useful for live-demo narration and comparison with DDS.

| Trigger | What happens | Why it hurts |
|---------|-------------|--------------|
| **WiFi blip** (brief network interruption) | All *N × (N−1)* streams break simultaneously.  Every node enters its 3 s reconnect loop. | Burst of TCP handshakes; state goes stale for several seconds across the entire fleet. |
| **Robot crash / kill** | Channel connectivity callbacks fire `TRANSIENT_FAILURE` on every peer connected to it.  Each peer cleans up and retries every 3 s. | Detection takes seconds (TCP keepalive / callback lag), not the 10 ms the scenario requires. |
| **Slow peer** | One robot with high CPU stalls its gRPC server thread pool.  Peers' streaming iterators block or time out. | Can cascade: the connectivity event fires, triggering reconnects on all peers of the slow robot. |
| **Connection storm at startup** | All *N* robots start near-simultaneously.  Each opens *N−1* channels, each with a 5 s ready timeout. | CPU spike from concurrent TLS-less handshakes; memory spike from *4 × N × (N−1)* stream buffers being allocated at once. |
| **Port exhaustion** | At high *N*, the number of TCP connections per host the OS limits. | Requires tuning `ulimit` / `sysctl` — operational burden that doesn't exist with UDP multicast. |
| **mDNS failure** | Zeroconf multicast blocked by firewall or network config. | Robots cannot discover peers — fall back to static port list or troubleshoot the network. DDS uses its own discovery protocol and doesn't depend on mDNS. |

---

## 10  Metrics to Watch

When running the demo (especially at higher fleet sizes), these are the key
observables for comparing approaches:

- **Active connections per robot** — should be *N−1* (plus UI).
- **CPU usage during connection storms** — spikes on startup and after network
  interruptions.
- **Memory usage per robot** — grows with connection count (buffers, thread
  stacks).
- **State update latency** — time from `robot_node` publish to `robot_ui`
  canvas render (should be < 200 ms in steady state).
- **Presence detection time** — how long after a robot dies before peers notice
  (typically 3–10 s with TCP; compare to < 10 ms with DDS liveliness).
- **Charging queue convergence** — time from slot request to dock arrival;
  compare station-hopping overhead (per-station gRPC calls) vs DDS multicast.

---

## 11  Ad-Hoc CLI Testing

Commands can be sent directly from the shell without the dashboard UI.  These
snippets use the current typed protobuf API:

### Send STOP

```bash
python -c "
import grpc, robot_pb2, robot_pb2_grpc
ch = grpc.insecure_channel('localhost:50051')
stub = robot_pb2_grpc.RobotCommandStub(ch)
resp = stub.SendCommand(robot_pb2.CommandRequest(
    robot_id='tractor1',
    command=robot_pb2.CMD_STOP))
print(f'{resp.success}: {resp.description}  [{resp.resulting_status}/{resp.resulting_intent}]')
"
```

### Send GOTO

```bash
python -c "
import grpc, robot_pb2, robot_pb2_grpc
ch = grpc.insecure_channel('localhost:50051')
stub = robot_pb2_grpc.RobotCommandStub(ch)
resp = stub.SendCommand(robot_pb2.CommandRequest(
    robot_id='tractor1',
    command=robot_pb2.CMD_GOTO,
    goto_params=robot_pb2.GotoParameters(x=50, y=50, speed=3.0)))
print(f'{resp.success}: {resp.description}  [{resp.resulting_status}/{resp.resulting_intent}]')
"
```

### Send SET_PATH

```bash
python -c "
import grpc, robot_pb2, robot_pb2_grpc
ch = grpc.insecure_channel('localhost:50051')
stub = robot_pb2_grpc.RobotCommandStub(ch)
wps = [robot_pb2.Waypoint(x=20,y=20), robot_pb2.Waypoint(x=80,y=20),
       robot_pb2.Waypoint(x=80,y=80), robot_pb2.Waypoint(x=20,y=80)]
resp = stub.SendCommand(robot_pb2.CommandRequest(
    robot_id='tractor1',
    command=robot_pb2.CMD_SET_PATH,
    set_path_params=robot_pb2.SetPathParameters(waypoints=wps)))
print(f'{resp.success}: {resp.description}')
"
```

### Send CHARGE

```bash
python -c "
import grpc, robot_pb2, robot_pb2_grpc
ch = grpc.insecure_channel('localhost:50051')
stub = robot_pb2_grpc.RobotCommandStub(ch)
resp = stub.SendCommand(robot_pb2.CommandRequest(
    robot_id='tractor1',
    command=robot_pb2.CMD_CHARGE))
print(f'{resp.success}: {resp.description}  [{resp.resulting_status}/{resp.resulting_intent}]')
"
```
