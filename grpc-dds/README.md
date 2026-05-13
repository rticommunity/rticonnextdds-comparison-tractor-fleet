# Hybrid — DDS Pub-Sub + gRPC Request-Reply

> System-level specification for the hybrid approach of the robot fleet demo.
> This document maps the high-level scenario requirements (see top-level
> [README](../README.md)) to an implementation that uses **DDS (Connext)** for
> all publish-subscribe data flows and **gRPC** for request-reply interactions
> (operator commands and charging-station slot negotiations).

### Quick Start

```bash
source ../setup.sourceme grpc-dds        # set NDDSHOME, LD_LIBRARY_PATH, etc.
./types_generate.sh                      # generate protobuf stubs (first time / after proto changes)
./demo_start.sh all                      # launch stations + robots + UI → http://localhost:5000
./demo_stop.sh                           # stop everything
```

Individual components:

```bash
./demo_start.sh stations     # all charging stations
./demo_start.sh robots       # all robots
./demo_start.sh ui           # dashboard UI
./demo_start.sh persistence  # RTI Persistence Service (coverage history)
./demo_start.sh robot tractor1   # single robot (port auto-derived from name)
./demo_start.sh station station1 # single station (dock coords auto-derived)
```

The CLI is **uniform** across all three approaches — same commands, same
arguments.  Robot/station names and dock coordinates come from
`../shared/fleet_common.sh`; gRPC ports are computed automatically from the
approach-specific `fleet_config.sh`.  DDS discovery requires no port
configuration.

---

## 1  Scope

This approach implements the **Hybrid DDS + gRPC** architecture described in
the top-level README:

- Pure Python.  **DDS (Connext)** for all pub-sub data flows, **gRPC** for all
  request-reply interactions.
- DDS **topics** for KinematicState, OperationalState, Intent, Telemetry,
  VideoFrame, CoveragePoint, and StationStatus.
- gRPC **unary** RPCs for operator commands and charging-station slot
  negotiation.
- **Dual discovery**: DDS SPDP (automatic multicast) for pub-sub endpoints;
  Zeroconf mDNS for gRPC endpoints.

### Why this split?

| Pattern | DDS | gRPC |
|---------|-----|------|
| 1-to-N pub-sub | Native multicast, QoS profiles, auto-discovery | Must emulate with per-peer streaming RPCs |
| Request-reply | Works (`rti.rpc`) but adds complexity | Native, simple, well-tooled |
| Service discovery | Built-in SPDP | Needs Zeroconf / consul / etc. |
| Video streaming | Partition-based writer matching | Server-streaming RPC per client |

This approach uses each technology where it excels:
- **DDS** for high-frequency telemetry and state (10 Hz kinematic, 1 Hz
  telemetry, ~5 fps video, coverage tracking, station status)
- **gRPC** for low-frequency commands and transactional operations (charging
  slot negotiation)

### Scenario Requirements Addressed

| Requirement | How the hybrid approach handles it |
|-------------|---------------------------|
| Late joiners converge quickly | `TRANSIENT_LOCAL` durability on OperationalState, Intent, and StationStatus — a new participant receives the last published value for every key immediately on DDS discovery (< 100 ms). |
| Presence detection ≤ 100 ms | **Met** (pub-sub plane).  DDS `AUTOMATIC_LIVELINESS_QOS` with a 100 ms lease duration.  **Not met** (command plane) — gRPC/TCP detection relies on channel callbacks measured in seconds. |
| Intent delivered reliably | DDS `RELIABLE` / `TRANSIENT_LOCAL` topic — ordered, guaranteed delivery with late-joiner convergence. |
| State is periodic | `KinematicState` at 10 Hz; `OperationalState` on change; `Telemetry` at 1 Hz — all via DDS pub-sub. |
| Telemetry loss tolerable | DDS `BEST_EFFORT` / `VOLATILE` — no retransmissions, next sample in 1 s. |
| Commands are reliable | gRPC unary request/response with explicit success/failure acknowledgement. |
| Robots come and go | DDS handles pub-sub reconnection natively (liveliness callbacks).  gRPC endpoints re-discovered via Zeroconf add/remove callbacks. |
| Video streaming | `VideoFrame` DDS topic — partition-based matching.  UI switches partition to the selected robot; no gRPC streaming needed. |
| Coverage tracking | `CoveragePoint` DDS topic with `PERSISTENT` durability via RTI Persistence Service — late-joining UIs see full historical coverage. |
| Charging queue monitoring | `StationStatus` DDS topic — event-driven listener callbacks replace the gRPC approach's polling thread. |

### Known Limitations (honest trade-offs)

- **Dual runtime** — requires both RTI Connext DDS 7.7.0+ and gRPC.  More
  dependencies than either pure approach.
- **Dual discovery** — DDS SPDP handles pub-sub, but gRPC endpoints still need
  Zeroconf mDNS.  Two discovery stacks to configure and troubleshoot.
- **Command plane detection** — gRPC/TCP keepalive still takes seconds, not
  the 100 ms that DDS liveliness provides for pub-sub.
- **Learning curve** — operators must understand both DDS QoS concepts and gRPC
  service definitions.

---

## 2  Architecture Overview

```
┌─────────────┐                            ┌─────────────┐
│ robot_node  │      DDS Domain 0          │ robot_node  │
│  (tractor1) │◄══════════════════════════►│  (tractor2) │
│             │    multicast pub-sub       │             │
│  gRPC :50051│                            │  gRPC :50052│
└──────┬──┬───┘                            └───┬──┬──────┘
       ║  │           ┌─────────────┐          │  ║
       ║  │           │ robot_node  │          │  ║
       ║  │           │  (tractor3) │          │  ║
       ║  │           │  gRPC :50053│          │  ║
       ║  │           └──────┬──────┘          │  ║
       ║  │                  │                 │  ║
       ║  │    DDS pub-sub   ║   gRPC unary    │  ║
       ║  │    (═══════)     ║   (──────)      │  ║
       ║  │                  ║                 │  ║
       ║  └──────────┬───────┼─────────────────┘  ║
       ║             │       ║                    ║
┌──────────────┐     │  ┌──────────────────┐  ┌──────────────────┐
│  robot_ui    │◄════╪══│ charging_station │══│ charging_station │
│  (Flask UI)  │     │  │   (station1)     │  │   (station2)     │
│              │─────┘  │   gRPC :50060    │  │   gRPC :50061    │
└──────────────┘ gRPC   └──────────────────┘  └──────────────────┘
       │         cmds           ▲
       │  HTTP / SSE / MJPEG   │ gRPC RequestSlot
       ▼                       │ gRPC ConfirmSlot
┌──────────────┐               │ gRPC ReleaseSlot
│   Browser    │  robot_node ──┘
└──────────────┘
```

**Key difference from the gRPC approach:** robot-to-robot state exchange uses
DDS multicast pub-sub (double lines ═), not point-to-point TCP streams.
`writer.write()` once; the middleware delivers to all matched readers.

**Key difference from the pure DDS approach:** commands and charging-slot
negotiation use gRPC unary RPCs (single lines ─), not DDS request-reply.
Simpler API for transactional request/response patterns.

### Data Plane (DDS)

```
┌─────────────────────────────────────────────────────────────────┐
│                        Data Plane (DDS)                         │
│  KinematicState · OperationalState · Intent · Telemetry         │
│  VideoFrame (partition-based) · StationStatus · CoveragePoint   │
│  Discovery: DDS SPDP (automatic UDP multicast)                 │
└─────────────────────────────────────────────────────────────────┘
```

### Control Plane (gRPC)

```
┌─────────────────────────────────────────────────────────────────┐
│                    Control Plane (gRPC)                          │
│  RobotCommand.SendCommand (UI → robot, unary)                   │
│  ChargingStation.{RequestSlot, ConfirmSlot, ReleaseSlot}        │
│  Discovery: Zeroconf mDNS (_robot-fleet._tcp / _chg-station._tcp)│
└─────────────────────────────────────────────────────────────────┘
```

### Participants

| Program | Count | Role |
|---------|-------|------|
| `robot_node.py` | 1 per tractor (5 in default config: tractor1, tractor2, tractor3, tractor4, tractor5) | Autonomous tractor: DDS publishers (6 topics) + DDS readers (4 topics) + gRPC command server + gRPC charging client |
| `charging_station.py` | 1 per station (2 in default config) | Charging dock manager: gRPC server for slot negotiation + DDS StationStatus publisher + Zeroconf registration |
| `robot_ui.py` | 1 | Fleet dashboard: DDS readers (7 topics) + gRPC command client (Zeroconf-discovered) + Flask web UI |
| `fleet_discovery.py` | (library) | Zeroconf mDNS helper — registers and discovers gRPC endpoints |

Each `robot_node` is:
- A **DDS DomainParticipant** — discovers all other participants via SPDP
  multicast.  Publishes own state; subscribes to peer state for collision
  avoidance; subscribes to StationStatus for event-driven queue monitoring.
- A **gRPC server** — serves `RobotCommand.SendCommand` for operator commands.
- A **gRPC client** — calls `ChargingStation.{RequestSlot, ConfirmSlot,
  ReleaseSlot}` when negotiating a charging slot.

Each `charging_station` is:
- A **gRPC server** — serves slot-management RPCs to robots.
- A **DDS publisher** — publishes `StationStatus` so the UI and robots receive
  live queue state via pub-sub (replacing the gRPC approach's `StreamStatus`
  server-streaming RPC).

The `robot_ui` is:
- A **DDS subscriber** — reads all 7 topics for the fleet dashboard.
- A **gRPC client** — sends commands to robots (discovered via Zeroconf).

### Topology

Pub-sub data flows through the **DDS shared data space** — every participant
reads and writes the topics it cares about.  The middleware handles fan-out
via multicast.  There are no per-peer connections for state exchange.

Commands and charging RPCs flow through **point-to-point gRPC channels** —
the UI opens a channel to the target robot for each command; robots open
channels to charging stations for slot negotiation.

---

## 3  Component Inventory

| File | Purpose | Detailed Spec |
|------|---------|---------------|
| `robot.proto` | gRPC service & message definitions (commands + charging) | — |
| `robot_pb2.py` | Generated Python protobuf code (message classes) | — (auto-generated, gitignored) |
| `robot_pb2_grpc.py` | Generated Python gRPC stubs and servicers | — (auto-generated, gitignored) |
| `robot_types.idl` | DDS type definitions (IDL) — single source of truth for pub-sub types | — |
| `robot_types.py` | Python DDS types generated by `rtiddsgen` — do not edit by hand | — (auto-generated) |
| `robot_qos.xml` | DDS QoS profiles (Pattern-based) incl. CoverageProfile with PERSISTENT durability | — |
| `robot_node.py` | Robot node — DDS pub-sub + gRPC command server + gRPC charging client + path following + collision avoidance + video rendering + coverage tracking | — |
| `charging_station.py` | Charging station — gRPC slot negotiation server + DDS StationStatus publisher | — |
| `robot_ui.py` | Flask web dashboard — DDS subscriber (7 readers) + gRPC command client + SSE + MJPEG video | — |
| `fleet_discovery.py` | Zeroconf mDNS/DNS-SD — advertise & discover gRPC endpoints | — |
| `fleet_config.sh` | Transport config (DDS domain ID + gRPC base ports); sources `../shared/fleet_common.sh` | — |
| `demo_start.sh` | Uniform launch script: `all`, `robots`, `stations`, `ui`, `persistence`, `robot <name>`, `station <name>` | — |
| `demo_stop.sh` | Kill all running processes and free ports | — |
| `types_generate.sh` | Regenerate `robot_pb2*.py` from `robot.proto` | — |

**Shared assets** (in `../shared/`):

| File | Purpose |
|------|---------|
| `fleet_common.sh` | Single source of truth for robot names, station positions, UI port — shared by all approaches |
| `video_renderer.py` | PyBullet headless 3D renderer — compound tractor bodies, waypoint beacons, text overlay |
| `arena_ground.urdf` | URDF arena ground plane for PyBullet |
| `field1_map.jpg` / `field1_texture.jpg` | Ground textures for canvas map and 3D renderer |

---

## 4  Data Flows

### 4.1  KinematicState (robot → all peers + UI) — DDS

```
robot_node                                all other robot_nodes + robot_ui
──────────                                ─────────────────────────────────
writer.write(KinematicState)  ──10 Hz──►  reader takes: position, velocity, heading
  BEST_EFFORT / VOLATILE                    (multicast — single write, N deliveries)
  KEEP_LAST depth=1
```

**DDS advantage over gRPC:** the robot calls `write()` once.  The middleware
delivers to all matched readers via multicast.  No per-subscriber threads,
no delivery loops.

### 4.2  OperationalState (robot → all peers + UI) — DDS

```
robot_node                                all other robot_nodes + robot_ui
──────────                                ─────────────────────────────────
writer.write(OperationalState) ─on change─►  reader takes: status, battery_level
  RELIABLE / TRANSIENT_LOCAL                   (late joiners get last value)
  KEEP_LAST depth=1
```

### 4.3  Intent (robot → all peers + UI) — DDS

```
robot_node                                all other robot_nodes + robot_ui
──────────                                ─────────────────────────────────
writer.write(Intent)         ─on change──►  reader takes: intent_type, target, waypoints
  RELIABLE / TRANSIENT_LOCAL                   (late joiners get last value)
  KEEP_LAST depth=1
```

### 4.4  Telemetry (robot → UI) — DDS

```
robot_node                                robot_ui
──────────                                ────────
writer.write(Telemetry)       ── 1 Hz ──►  reader takes: cpu, memory, temperature, signal strength
  BEST_EFFORT / VOLATILE                     (loss tolerable)
```

### 4.5  Video (robot → UI) — DDS partition matching

```
robot_node                                robot_ui
──────────                                ────────
writer.write(VideoFrame)      ── ~5 fps──►  reader takes: frame_data (JPEG bytes)
  BEST_EFFORT / VOLATILE                     partition-matched by robot_id
  KEEP_LAST depth=1
  publisher partition=[robot_id]             subscriber partition=["__NONE__" or robot_id]
```

The UI selects a robot for video by changing its subscriber partition to match
that robot's publisher partition.  When deselected, the partition reverts to
`"__NONE__"` — no frames are delivered.  No gRPC streaming needed.

### 4.6  CoveragePoint (robot → UI) — DDS

```
robot_node                                robot_ui
──────────                                ────────
writer.write(CoveragePoint)   ──on move──►  reader takes: robot_id, x, y
  RELIABLE / PERSISTENT (via Persistence Service)
  KEEP_LAST depth=1000
```

Each robot publishes `CoveragePoint` as it moves along its path.  The UI
accumulates these into per-robot polylines drawn on the map canvas (lineWidth 4,
alpha 0.45, rounded joins).  A toggle button and "Clear Coverage" button control
display.  PERSISTENT durability (via RTI Persistence Service) allows
late-joining UIs to see historical coverage.

### 4.7  StationStatus (station → robots + UI) — DDS

```
charging_station                          robot_node + robot_ui
────────────────                          ───────────────────────
writer.write(StationStatus)   ─on change─►  reader takes: dock, queue[], is_occupied
  RELIABLE / TRANSIENT_LOCAL                 (event-driven listener callbacks)
```

**Hybrid advantage:** in the gRPC approach, station status is a server-streaming
RPC (`StreamStatus`) requiring a persistent gRPC channel per subscriber.  Here,
it's a DDS topic — the station calls `write()` once after each queue mutation
and all readers (robots + UI) receive the update via multicast.

Robots use a `StationStatusListener` callback to monitor their queue rank
without a polling thread — when the status changes, the listener fires and
the robot updates its wait position or navigates to the dock.

### 4.8  Commands (UI → robot) — gRPC

```
Browser  ──POST /command──►  Flask  ──gRPC SendCommand──►  robot_node
         ◄── JSON result ──         ◄── CommandResponse ──
```

gRPC unary RPC.  The UI discovers the robot's gRPC address via Zeroconf
and sends the command directly.

### 4.9  Charging (robot ↔ station) — gRPC

```
robot_node  ──RequestSlot──►  charging_station   (get estimated wait)
            ──ConfirmSlot──►                      (commit to queue)
            ──ReleaseSlot──►                      (done / cancel)
```

Robots discover stations via Zeroconf, fan out `RequestSlot` to all stations,
compare offers (shortest wait, then closest distance), confirm the best
slot, and release rejected offers.  All three RPCs are gRPC unary calls.

### 4.10  SSE (Flask → browser)

```
Flask /stream  ──SSE 5 Hz──►  Browser EventSource
```

The Flask backend snapshots the DDS reader caches every 200 ms and pushes
JSON SSE events.  This is the only non-DDS, non-gRPC data flow.

### 4.11  Discovery (dual stack)

```
DDS pub-sub discovery (automatic):
                          UDP multicast (239.255.0.1)
                   ┌──────────────────────────────────┐
robot_node  ◄──────┤  DDS Simple Participant           ├──────► robot_node
station     ◄──────┤  Discovery Protocol (SPDP)        ├──────► robot_ui
                   └──────────────────────────────────┘

gRPC endpoint discovery (Zeroconf):
                          mDNS multicast (224.0.0.251:5353)
robot_node  ──register──►  _robot-fleet._tcp.local.    ◄──browse── robot_ui
station     ──register──►  _chg-station._tcp.local.    ◄──browse── robot_node / robot_ui
```

DDS topics are discovered automatically — zero code.  gRPC endpoints are
discovered via Zeroconf mDNS callbacks using `fleet_discovery.py`.

---

## 5  gRPC Protocol Summary

Defined in `robot.proto`.  Only request-reply interactions use gRPC — all
pub-sub data flows use DDS (see §4).

### Services

| Service | RPC | Type | Purpose |
|---------|-----|------|---------|
| `RobotCommand` | `SendCommand` | unary | Operator commands (STOP, GOTO, FOLLOW_PATH, RESUME, SET_PATH, CHARGE) |
| `ChargingStation` | `RequestSlot` | unary | Non-committing: get estimated wait time |
| `ChargingStation` | `ConfirmSlot` | unary | Commit to a queue slot |
| `ChargingStation` | `ReleaseSlot` | unary | Release dock or cancel queue position |

**No streaming RPCs** — the gRPC approach's five `Stream*` RPCs (KinematicState,
OperationalState, Intent, Telemetry, Coverage) and `StreamStatus` are all
replaced by DDS topics.  `RequestVideo` is replaced by DDS partition matching.

### Key Enums

- `RobotStatus`: STATUS_UNKNOWN / STATUS_MOVING / STATUS_IDLE / STATUS_HOLDING / STATUS_CHARGING
- `RobotIntent`: INTENT_UNKNOWN / INTENT_FOLLOW_PATH / INTENT_GOTO / INTENT_IDLE / INTENT_CHARGE_QUEUE / INTENT_CHARGE_DOCK
- `Command`: CMD_STOP / CMD_FOLLOW_PATH / CMD_GOTO / CMD_RESUME / CMD_SET_PATH / CMD_CHARGE

---

## 6  DDS QoS Strategy

All profiles inherit from RTI **Pattern** profiles (`BuiltinQosLib::Pattern.*`)
— each Pattern already encodes the right reliability / durability / history
for its communication pattern.

| Topic | QoS Profile | Base Pattern | Key Settings | Rationale |
|-------|-------------|--------------|--------------|-----------|
| `KinematicState` | `KinematicStateProfile` | `Pattern.PeriodicData` | BEST_EFFORT, deadline 200 ms, liveliness 100 ms | High rate, latest-value-only.  Loss harmless — next sample in 100 ms. |
| `OperationalState` | `OperationalStateProfile` | `Pattern.Status` | RELIABLE, TRANSIENT_LOCAL, liveliness 100 ms | Must not be lost.  Late joiners need current status + battery. |
| `Intent` | `IntentProfile` | `Pattern.LastValueCache` | RELIABLE, TRANSIENT_LOCAL, liveliness 100 ms | Must not be lost.  Late joiners need current intent + waypoints. |
| `Telemetry` | `TelemetryProfile` | `Pattern.Streaming` | BEST_EFFORT, deadline 2 s | Loss tolerable.  No liveliness needed (covered by KinematicState). |
| `VideoFrame` | `VideoProfile` | `Pattern.Streaming` | BEST_EFFORT, deadline 1 s | High bandwidth.  Dropped frames replaced by next one. |
| `StationStatus` | `StationStatusProfile` | `Pattern.Status` | RELIABLE, TRANSIENT_LOCAL | UI late joiners need current station queue state. |
| `CoveragePoint` | `CoverageProfile` | `Pattern.Status` | RELIABLE, PERSISTENT, KEEP_LAST 1000 | Full coverage trail history survives restarts via Persistence Service. |

### Liveliness — Presence Detection

DDS liveliness replaces manual heartbeat / reconnect logic for the pub-sub
plane:

```
liveliness.kind = AUTOMATIC_LIVELINESS_QOS
liveliness.lease_duration = 100 ms
```

If a writer stops (crash, network loss), the lease expires and all readers
are notified within 100 ms via `on_liveliness_changed` callbacks.  Zero
application polling.

---

## 7  DDS Data Types

Defined in `robot_types.idl` — single source of truth for all pub-sub types.

| Type | Key | Fields | Notes |
|------|-----|--------|-------|
| `KinematicState` | `robot_id` | `position{x,y,z}, velocity{x,y,z}, heading` | 10 Hz |
| `OperationalState` | `robot_id` | `status (enum), battery_level` | On change |
| `Intent` | `robot_id` | `intent_type (enum), target_position, path_waypoints[], path_index` | On change |
| `Telemetry` | `robot_id` | `cpu_usage, memory_usage, temperature, signal_strength` | 1 Hz |
| `VideoFrame` | `robot_id` | `frame_data (octet sequence, 1 MB max)` | ~5 fps, partitioned |
| `CoveragePoint` | `robot_id` | `x, y` | On move, PERSISTENT |
| `StationStatus` | `station_id` | `dock_x, dock_y, is_occupied, docked_robot_id, queue[]` | On change |
| `QueueEntry` | — | `robot_id, rank, confirmed` | Nested in StationStatus |

> **No `timestamp` fields.**  DDS provides `source_timestamp` and
> `reception_timestamp` in the `SampleInfo` metadata — nanosecond precision,
> zero payload overhead.

---

## 8  Build & Run

### Prerequisites

- Python 3.14.3+
- RTI Connext DDS 7.7.0+ (Professional or Community edition)
- RTI Code Generator (`rtiddsgen`) — included in the Connext installation
- Virtual environment with packages: `rti.connext`, `grpcio`, `grpcio-tools`,
  `protobuf`, `flask`, `pybullet`, `Pillow`, `numpy`, `zeroconf`, `ifaddr`

### Environment Setup

```bash
source ../setup.sourceme grpc-dds
# or manually:
export NDDSHOME=/path/to/rti_connext_dds-7.7.0
export PATH=$NDDSHOME/bin:$PATH
pip install -r requirements.txt
```

### Generate Code

```bash
# Protobuf / gRPC stubs:
./types_generate.sh
# or: python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. robot.proto

# DDS types (auto-generated on first run, or manually):
$NDDSHOME/bin/rtiddsgen -language python robot_types.idl
```

### Fleet Configuration

`fleet_config.sh` defines the DDS domain and gRPC ports:

```bash
DOMAIN_ID=0                   # DDS domain (all participants)
ROBOT_BASE_PORT=50051         # Robot gRPC ports: 50051, 50052, ...
STATION_BASE_PORT=50060       # Station gRPC ports: 50060, 50061, ...
UI_PORT=5000                  # Flask web UI port
```

### Launch

```bash
./demo_start.sh all           # persistence + stations + robots + UI
```

Individual components in separate terminals:

```bash
./demo_start.sh persistence   # RTI Persistence Service (coverage history)
./demo_start.sh stations      # all charging stations
./demo_start.sh robots        # all robots
./demo_start.sh ui            # dashboard UI → http://localhost:5000
```

### Stop

```bash
./demo_stop.sh                # kills all robot_node, charging_station, and robot_ui processes
```

---

## 9  Thread Model (per robot node)

The hybrid approach eliminates the N² thread explosion of the gRPC approach
while adding only a small fixed gRPC server pool.  Each robot node needs
approximately **10 threads** regardless of fleet size:

| Thread | Count | Purpose |
|--------|-------|---------|
| Main | 1 | Blocks on `sleep(1)` loop |
| `update_position` | 1 | Movement tick at 10 Hz (path following, collision avoidance) |
| `publish_kinematic_state` | 1 | Publishes KinematicState at 10 Hz |
| `publish_operational_state` | 1 | Publishes OperationalState on change (10 Hz check) |
| `publish_intent` | 1 | Publishes Intent on change (10 Hz check) |
| `publish_telemetry` | 1 | Publishes Telemetry at 1 Hz |
| `publish_video` | 1 | Publishes VideoFrame at ~5 fps (when selected) |
| `print_status` | 1 | Log line every 5 s |
| gRPC server pool | 4 (max) | Handles incoming command RPCs |
| DDS internal threads | ~3 | Receive, event, database (managed by middleware) |

**Compare with the gRPC approach:** a 5-robot fleet needs ~22 threads per
robot (4 reader threads × 4 peers + connector threads + gRPC pool).  At 100
robots it would need ~400 threads per robot.  The hybrid stays at ~10.

**No delivery loops** — DDS `writer.write()` returns immediately; the
middleware handles serialisation and multicast delivery internally.

**No reconnect threads** — DDS discovery and reconnection are handled by
the middleware's internal SPDP/SEDP threads.  gRPC is only used for
infrequent commands — no persistent streaming connections to maintain.

**No charge-polling thread** — in the gRPC approach, robots poll
`ConfirmSlot` in a dedicated thread while waiting in the charge queue.
Here, the `StationStatusListener` DDS callback fires whenever the station
publishes a queue update — fully event-driven.

---

## 10  What the Hybrid Eliminates (vs the gRPC approach)

| gRPC Approach Code | Lines | Hybrid Equivalent |
|--------------------|-------|-------------------|
| 5 `Stream*` server-streaming RPCs per robot | ~75 | DDS topics — `writer.write()` (1 line each) |
| Per-subscriber `while: yield; sleep` delivery loops | ~60 | Does not exist (DDS multicast) |
| Per-peer `_connect_to_peer` reconnect loops | ~60 | DDS SPDP — zero code |
| 4 reader threads × (N−1) peers | 4×(N−1) threads | DDS listener callbacks — fixed thread count |
| `StreamStatus` server-streaming RPC (station) | ~30 | DDS StationStatus topic — `write()` once |
| `RequestVideo` server-streaming RPC | ~40 | DDS VideoFrame topic — partition matching |
| `StreamCoverage` server-streaming RPC | ~30 | DDS CoveragePoint topic |
| Charge-queue polling thread | ~25 | DDS StationStatusListener callback |
| `_delivery_loop_count` / enter/exit tracking | ~20 | Does not exist |
| Channel connectivity monitoring | ~30 | `on_liveliness_changed` callback (pub-sub) |

**What the hybrid keeps from gRPC:** unary command RPCs (simple, well-tooled)
and charging-slot negotiation RPCs (transactional request-reply pattern).

**What the hybrid keeps from DDS:** all pub-sub data flows, QoS per topic,
automatic discovery, liveliness-based presence detection, partition-based
video routing, PERSISTENT coverage history.

---

## 11  Failure Modes

| Trigger | What happens | Comparison |
|---------|-------------|------------|
| **WiFi blip** | DDS internally buffers reliable samples and retransmits on reconnect.  Best-effort samples are lost (harmless).  gRPC command channels may break but are short-lived (opened per command). | Better than gRPC: no reconnection storm for pub-sub.  gRPC impact limited to in-flight commands. |
| **Robot crash / kill** | DDS liveliness lease expires within 100 ms — all readers notified.  Zeroconf fires `ServiceRemoved` for the gRPC endpoint. | Pub-sub detection in 100 ms (vs seconds with TCP in pure gRPC). |
| **Slow peer** | DDS flow control prevents unbounded buffering; slow readers lose best-effort samples gracefully.  gRPC commands time out individually. | No cascading failures.  No thread pool starvation. |
| **Startup burst** | DDS SPDP discovery via multicast — O(1) messages.  gRPC server pools start independently (no N² handshakes). | Much better than gRPC's N² TCP connection storm. |
| **mDNS failure** | gRPC discovery fails — commands cannot be sent.  DDS pub-sub is **unaffected** (uses its own SPDP protocol). | Partial failure: fleet data keeps flowing; only commands are degraded. |

---

## 12  Metrics to Watch

| Metric | Expected (Hybrid) | gRPC for comparison |
|--------|-------------------|---------------------|
| **Connections per robot** | ~3 UDP sockets (DDS) + 1 gRPC port (commands) | N−1 TCP connections (grows with fleet) |
| **Threads per robot** | ~10 (fixed) | ~4N (grows with fleet) |
| **Delivery loops** | 0 | 4 × (N−1) per robot |
| **Startup time to full mesh** | < 2 s (DDS SPDP) | 3–15 s (sequential TCP connects) |
| **Presence detection latency** | ≤ 100 ms (DDS liveliness) | 3–10 s (TCP keepalive) |
| **Late-joiner convergence** | < 100 ms (TRANSIENT_LOCAL) | < 100 ms (first stream yield) |
| **CPU during reconnection** | Negligible (DDS); no storm | Spike (N² TCP handshakes) |

---

## 13  Relationship to Other Approaches

| Aspect | gRPC (`grpc/`) | Hybrid (this) | DDS (`dds/`) |
|--------|----------------|---------------|--------------|
| Pub-sub transport | gRPC streaming (emulated) | DDS (native) | DDS (native) |
| Command transport | gRPC unary | gRPC unary | DDS request-reply |
| Charging transport | gRPC unary RPCs | gRPC unary RPCs | DDS request-reply |
| Station status | gRPC StreamStatus | DDS StationStatus topic | DDS StationStatus topic |
| Discovery | Zeroconf only | DDS SPDP + Zeroconf | DDS SPDP only |
| Connections (5 robots) | ~25 gRPC channels, 105 streams | 7 DDS readers + 5 gRPC channels | 7 DDS readers + 1 Requester |
| Threads (5 robots) | ~22 | ~10 | ~9 |
| Video delivery | gRPC streaming | DDS partition matching | DDS partition matching |
| Coverage tracking | gRPC StreamCoverage | DDS CoveragePoint topic | DDS CoveragePoint topic |
| Presence detection | Seconds (TCP) | 100 ms (DDS) / seconds (gRPC) | 100 ms (DDS) |
| Extra dependencies | grpc, zeroconf | grpc, zeroconf, rti.connextdds | rti.connextdds |

---

## 14  Troubleshooting

### Robots not discovering each other (DDS)

- Check firewall allows UDP multicast (port 7400+ for SPDP)
- Ensure all robots use the same domain ID (`DOMAIN_ID` in `fleet_config.sh`)
- Verify `NDDSHOME` is set and `rti.connext` is installed
- On macOS: check that the loopback interface allows multicast

### Commands not reaching robots (gRPC)

- Check Zeroconf is working: `dns-sd -B _robot-fleet._tcp`
- Verify gRPC ports are not blocked (50051–50055 for robots)
- Check that the robot's gRPC server started (look for "gRPC server started" in logs)

### DDS Spy (monitor pub-sub traffic)

```bash
# Prints discovery data. Shows samples being published
$NDDSHOME/bin/rtiddsspy -domainId 0
# Prints discovery data and data content
$NDDSHOME/bin/rtiddsspy -domainId 0 -print
```

---

## 15  Further Reading

- [RTI Connext Documentation](https://community.rti.com/documentation)
- [gRPC Python Documentation](https://grpc.io/docs/languages/python/)
- [DDS Specification (OMG)](https://www.omg.org/spec/DDS/)
- [QoS Best Practices](https://community.rti.com/best-practices/qos-configuration)
- [Zeroconf / mDNS (RFC 6762/6763)](https://www.rfc-editor.org/rfc/rfc6762)
