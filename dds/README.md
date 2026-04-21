# Pure DDS — Data-Centric Everything

> System-level specification for the pure DDS approach of the robot fleet demo.  This
> document maps the high-level scenario requirements (see top-level [README](../README.md)) to a
> **pure-DDS** implementation — pub-sub for all data flows, DDS request-reply
> for commands and charging-station slot negotiation.  No gRPC, no Zeroconf,
> no bolted-on discovery.

### Component Specs

[`robot_node_spec.md`](spec/robot_node_spec.md) ·
[`robot_ui_spec.md`](spec/robot_ui_spec.md) ·
[`charging_station_spec.md`](spec/charging_station_spec.md)

### Quick Start

```bash
source ../setup.sourceme dds            # set NDDSHOME, LD_LIBRARY_PATH, etc.
./demo_start.sh all                # launch stations + robots + UI → http://localhost:5000
./demo_stop.sh                   # stop everything
```

Individual components:

```bash
./demo_start.sh stations     # all charging stations
./demo_start.sh robots       # all robots
./demo_start.sh ui           # dashboard UI
./demo_start.sh robot tractor1   # single robot
./demo_start.sh station station1 # single station (dock coords auto-derived)
```

The CLI is **uniform** across all three approaches — same commands, same
arguments.  Robot/station names and dock coordinates come from
`../shared/fleet_common.sh`.  No port assignments needed — DDS SPDP
handles discovery.

---

## 1  Scope

This approach implements the **Data-Centric Pub-Sub and Services** architecture
described in the top-level README:

- Pure Python, **RTI Connext DDS** for **all** communication.
- DDS **pub-sub topics** for KinematicState, OperationalState, Intent,
  Telemetry, and Video.
- DDS **request-reply** for commands and charging-station slot management.
- **No gRPC, no Zeroconf** — DDS provides discovery, presence, QoS, and
  reliable delivery as first-class features.
- Every participant joins the same **DDS domain** — no manual peer addresses,
  no port lists, no connection management.

### Scenario Requirements Addressed

| Requirement | How the DDS approach handles it |
|-------------|---------------------------|
| Late joiners converge quickly | `TRANSIENT_LOCAL` durability on OperationalState and Intent — a new participant receives the last published value for every key immediately on discovery (sub-second, typically < 100 ms). |
| Presence detection ≤ 100 ms | **Met.** DDS `AUTOMATIC_LIVELINESS_QOS` with a 100 ms lease duration.  The middleware fires a `on_liveliness_changed` callback — zero application code. |
| Robots appear dynamically | **Met.** DDS Simple Participant Discovery Protocol (SPDP) uses UDP multicast — no configuration, no registry, no mDNS. |
| KinematicState known within tolerance | `KinematicState` topic published at 10 Hz with `BEST_EFFORT` / `VOLATILE` QoS.  Each robot reads the latest value per key — always fresh, never queued. |
| OperationalState delivered reliably | `OperationalState` topic with `RELIABLE` / `TRANSIENT_LOCAL` QoS — guaranteed delivery plus late-joiner convergence. Published on change. |
| Intent delivered reliably | `Intent` topic with `RELIABLE` / `TRANSIENT_LOCAL` QoS.  Includes path waypoints and path index. Published on change. |
| Telemetry loss tolerable | `Telemetry` topic with `BEST_EFFORT` / `VOLATILE` QoS — 1 Hz, no retransmissions. |
| Commands are reliable | DDS request-reply over `RELIABLE` / `KEEP_ALL` topics with correlation IDs — guaranteed delivery with explicit success/failure response. |
| Robots come and go | DDS handles this natively.  Participant departure triggers liveliness callbacks on all peers.  No reconnect loops, no cleanup code. |
| IP addresses change | DDS discovery is transport-agnostic — participants re-discover each other automatically after IP change (built-in `SPDP` re-announcement). |
| Video streaming | `VideoFrame` topic — keyed by `robot_id`, `BEST_EFFORT` / `VOLATILE`.  Each robot publishes JPEG frames; the UI subscribes with a content filter for the selected robot. |
| Charging stations | DDS request-reply for slot negotiation (RequestSlot / ConfirmSlot / ReleaseSlot).  Station status published as a `StationStatus` topic — the UI subscribes to see live queue state. |

### Known Limitations (honest trade-offs)

- **Runtime dependency** — requires RTI Connext DDS 7.6.0+ installed and
  licensed.  gRPC is pip-installable with zero system dependencies.
- **Larger footprint** — the Connext shared libraries add ~80 MB to the
  deployment.  gRPC + protobuf is ~20 MB.
- **Learning curve** — QoS policy combinations are powerful but non-trivial to
  get right (e.g. a `RELIABLE` reader against a `BEST_EFFORT` writer silently
  fails to match).  gRPC "just works" with TCP semantics.
- **Debugging opacity** — DDS multicast traffic is harder to inspect than
  HTTP/2-framed gRPC on a TCP connection.  RTI Admin Console or Wireshark with
  the RTPS dissector helps, but the tooling barrier is higher.
- **No native browser transport** — DDS cannot push data directly to a browser.
  The Flask/SSE bridge is still required (same as the gRPC approach).
- **Multicast not always available** — some cloud VPCs and corporate networks
  block UDP multicast.  DDS can fall back to unicast peer lists, but this
  requires configuration (similar to the gRPC approach's static mode).

### Design Philosophy: DDS-Natural

The goal is **not** to port the gRPC approach line-for-line, but to solve the
same scenario requirements in the most natural way for DDS:

- **No delivery loops** — each robot calls `writer.write()` once; the
  middleware delivers to all matched readers via multicast.  There are no
  per-subscriber streaming threads.
- **No reconnect logic** — DDS discovery and liveliness are built-in.  There is
  no `_connect_to_peer` retry loop.
- **No Zeroconf / mDNS** — DDS SPDP replaces the bolted-on `fleet_discovery.py`
  from the gRPC approach.
- **QoS per topic** — each data flow gets the reliability, durability, and
  history policy that matches its semantics (see §5).
- **Separate topics for separate concerns** — `KinematicState` (10 Hz,
  best-effort) and `OperationalState` (on change, reliable) are distinct
  topics with distinct QoS, not a single `RobotState` blob.
- **Keyed topics** — `robot_id` is the DDS key on every topic.  The middleware
  manages per-key state automatically: one instance per robot, last value
  cached, liveliness tracked per instance.
- **Content filtering** — the UI can subscribe to video from a single robot
  without receiving (and discarding) frames from all others.  The filter
  expression is evaluated in the middleware, not in application code.

---

## 2  Architecture Overview

```
┌─────────────┐                           ┌─────────────┐
│ robot_node  │     DDS Domain 0          │ robot_node  │
│  (robot1)   │◄═══════════════════════►  │  (robot2)   │
└──────┬──────┘   multicast pub-sub       └──────┬──────┘
       ║              ┌─────────────┐            ║
       ╚══════════════│ robot_node  │════════════╝
                      │  (robot3)   │
                      └──────┬──────┘
                             ║
       ══════════════════════╩═══════════════════════
       ║                     ║                      ║
┌──────────────┐     ┌──────────────────┐    ┌──────────────────┐
│  robot_ui    │     │ charging_station │    │ charging_station │
│  (Flask UI)  │     │   (station1)     │    │   (station2)     │
└──────────────┘     └──────────────────┘    └──────────────────┘
       │
       │  HTTP / SSE / MJPEG
       ▼
┌──────────────┐
│   Browser    │
└──────────────┘
```

**Key difference from the gRPC approach:** there are **no point-to-point connections**.
All participants join the same DDS domain.  The middleware handles discovery,
matching, and delivery.  The double lines (═) represent **logical topic
subscriptions**, not TCP connections.

### Participants

| Program | Count | Role |
|---------|-------|------|
| `robot_node.py` | 1 per tractor (5 in default config: fred, alice, bob, carol, dave) | Autonomous tractor: publishes KinematicState / OperationalState / Intent / Telemetry / Video; subscribes to peer state for collision avoidance; handles command requests; negotiates charging slots |
| `charging_station.py` | 1 per station (2 in default config) | Charging dock manager: FIFO queue, slot negotiation via DDS request-reply, publishes StationStatus |
| `robot_ui.py` | 1 | Fleet dashboard: subscribes to all robot topics + station status, serves web UI with live map + tables + command panel + video feed + charging panel |
| `command_console.py` | 1 (optional) | CLI tool for sending commands via DDS request-reply |

Every participant is a **DDS DomainParticipant** — it discovers all others
automatically via SPDP multicast.  There is no client/server distinction.

### Topology

**Shared data space** — every participant reads and writes the topics it cares
about.  The middleware handles fan-out.  Charging stations and the UI are not
special — they are just participants that subscribe to different topics and
serve different request-reply endpoints.

---

## 3  Component Inventory

| File | Purpose | Status |
|------|---------|--------|
| `robot_types.idl` | DDS type definitions (IDL) — **single source of truth** for all topic types, enums, and structs | ✅ Complete (3 enums, 24 structs incl. CoveragePoint) |
| `robot_types.py` | Python dataclasses generated by `rtiddsgen` — **do not edit by hand** | ✅ Auto-generated |
| `robot_qos.xml` | QoS profiles for each topic type (Pattern-based), incl. CoverageProfile (PERSISTENT durability) | ✅ Complete (9 profiles) |
| `robot_node.py` | Robot node — publishes own state, subscribes to peers, handles commands, path following, collision avoidance, charging, video rendering, coverage tracking | ✅ Complete |
| `charging_station.py` | Charging station — FIFO dock queue, slot negotiation via DDS request-reply, status publishing | ✅ Complete |
| `robot_ui.py` | Flask web dashboard — DDS subscriber (7 readers incl. CoveragePoint), SSE publisher, command proxy, video proxy, charging panel, coverage map overlay | ✅ Complete |
| `fleet_config.sh` | Transport config for this approach (DDS domain ID); sources `../shared/fleet_common.sh` for scenario data | ✅ Complete |
| `demo_start.sh` | Uniform launch script: `all`, `robots`, `stations`, `ui`, `persistence`, `robot <name>`, `station <name>` | ✅ Complete |
| `demo_stop.sh` | Kill all running robot, station, and UI processes | ✅ Complete |
| `../setup.sourceme` | Environment setup — run `source ../setup.sourceme dds` from repo root | ✅ |

**Shared assets** (in `../shared/`):

| File | Purpose |
|------|---------|
| `fleet_common.sh` | Single source of truth for robot names, station positions, UI port — shared by all approaches |
| `video_renderer.py` | PyBullet headless 3D renderer — compound tractor bodies, waypoint beacons, text overlay |
| `arena_ground.urdf` | URDF arena ground plane for PyBullet |
| `field1_map.jpg` / `field1_texture.jpg` | Ground textures for canvas map and 3D renderer |

---

## 4  Data Flows

### 4.1  KinematicState (robot → all peers + UI)

```
robot_node                                all other robot_nodes + robot_ui
──────────                                ─────────────────────────────────
writer.write(KinematicState)  ──10 Hz──►  reader takes: position, velocity, heading
  BEST_EFFORT / VOLATILE                    (multicast — single write, N deliveries)
  KEEP_LAST depth=1
```

**DDS advantage:** the robot calls `write()` once.  The middleware delivers to
all matched readers via multicast.  No per-subscriber threads, no delivery
loops.

### 4.2  OperationalState (robot → all peers + UI)

```
robot_node                                all other robot_nodes + robot_ui
──────────                                ─────────────────────────────────
writer.write(OperationalState) ─on change─►  reader takes: status, battery_level
  RELIABLE / TRANSIENT_LOCAL                   (late joiners get last value)
  KEEP_LAST depth=1
```

**DDS advantage:** `TRANSIENT_LOCAL` durability means a newly discovered robot
immediately receives the current OperationalState of every live peer — no
manual sync, no "catch-up" protocol.

### 4.3  Intent (robot → all peers + UI)

```
robot_node                                all other robot_nodes + robot_ui
──────────                                ─────────────────────────────────
writer.write(Intent)         ─on change──►  reader takes: intent_type, target, waypoints
  RELIABLE / TRANSIENT_LOCAL                   (late joiners get last value)
  KEEP_LAST depth=1
```

### 4.4  Telemetry (robot → UI)

```
robot_node                                robot_ui
──────────                                ────────
writer.write(Telemetry)       ── 1 Hz ──►  reader takes: cpu, memory, temperature, signal strength
  BEST_EFFORT / VOLATILE                     (loss tolerable)
```

### 4.5  Video (robot → UI)

```
robot_node                                robot_ui
──────────                                ────────
writer.write(VideoFrame)      ── ~5 fps──►  reader takes: frame_data (JPEG bytes)
  BEST_EFFORT / VOLATILE                     content-filtered by robot_id
  KEEP_LAST depth=1
```

**DDS advantage:** the UI uses a `ContentFilteredTopic` to subscribe to video
from only the selected robot.  The filter is evaluated in the middleware — the
publisher doesn't need to know who's watching.

### 4.6  CoveragePoint (robot → UI)

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
display.  PERSISTENT durability (via RTI Persistence Service) allows late-joining
UIs to see historical coverage.

### 4.7  Commands (UI / console → robot)

```
command_console / robot_ui                robot_node
──────────────────────────                ──────────
writer.write(CommandRequest)  ──────────►  reader takes: command, params
  RELIABLE / VOLATILE                       (keyed by request_id)
  KEEP_ALL
                              ◄──────────  writer.write(CommandResponse)
                                             (correlated by request_id)
```

**DDS advantage:** reliable delivery is a QoS policy, not a transport
guarantee.  If the target robot is temporarily unreachable, the middleware
retransmits automatically — no application-level retry logic.

### 4.7  Charging (robot ↔ station)

```
robot_node                                charging_station
──────────                                ────────────────
writer.write(SlotRequest)     ──────────►  reader takes: robot_id, battery, info_only
  RELIABLE / VOLATILE                       (keyed by request_id)
                              ◄──────────  writer.write(SlotOffer)
                                             dock position, wait time, slot_id

writer.write(SlotConfirm)     ──────────►  reader: commit to queue
                              ◄──────────  writer.write(SlotAssignment)
                                             granted, dock/wait position, rank

writer.write(SlotRelease)     ──────────►  reader: release dock or cancel
```

Station status (live queue state) is published as a topic:

```
charging_station                          robot_ui
────────────────                          ────────
writer.write(StationStatus)   ── 1 Hz ──►  reader takes: queue entries, dock state
  RELIABLE / TRANSIENT_LOCAL                 (late-joining UI gets current state)
```

### 4.8  SSE (Flask → browser)

```
Flask /stream  ──SSE 5 Hz──►  Browser EventSource
```

Same as the gRPC approach — the Flask backend snapshots the DDS reader caches and
pushes JSON SSE events.  This is the only non-DDS data flow.

### 4.9  Discovery (all participants)

```
                          UDP multicast (239.255.0.1)
                   ┌──────────────────────────────────┐
robot_node  ◄──────┤  DDS Simple Participant           ├──────► robot_node
station     ◄──────┤  Discovery Protocol (SPDP)        ├──────► robot_ui
                   └──────────────────────────────────┘
```

**No application code required.**  Every `DomainParticipant` on the same domain
ID discovers all others automatically.  Topic matching (which reader gets which
writer's data) is handled by the Simple Endpoint Discovery Protocol (SEDP).

---

## 5  QoS Strategy

The central advantage of DDS over gRPC is **per-topic QoS**.  Each data flow
gets exactly the guarantees it needs — no more, no less.

All profiles inherit from RTI **Pattern** profiles (`BuiltinQosLib::Pattern.*`)
rather than raw Generic profiles.  Each Pattern already encodes the right
reliability / durability / history for its communication pattern — our overrides
are minimal (liveliness, deadline tuning).

| Topic | QoS Profile | Base Pattern | Overrides | Rationale |
|-------|-------------|--------------|-----------|-----------|
| `KinematicState` | `KinematicStateProfile` | `Pattern.PeriodicData` | deadline 200 ms writer / 500 ms reader, liveliness 100 ms | High rate, latest-value-only.  Loss of one sample is harmless — the next arrives in 100 ms. |
| `OperationalState` | `OperationalStateProfile` | `Pattern.Status` | liveliness 100 ms | Must not be lost.  Late joiners need current status + battery. |
| `Intent` | `IntentProfile` | `Pattern.LastValueCache` | liveliness 100 ms | Must not be lost.  Late joiners need current intent + waypoints. |
| `Telemetry` | `TelemetryProfile` | `Pattern.Streaming` | deadline 2 s writer / 5 s reader | Loss tolerable.  No liveliness needed (covered by KinematicState). |
| `VideoFrame` | `VideoProfile` | `Pattern.Streaming` | deadline 1 s writer / 2 s reader | High bandwidth.  Dropped frames are replaced by the next one. |
| `CommandRequest` | `CommandProfile` | `Pattern.RPC` | *(none — pattern defaults)* | Every command must be delivered.  Tuned heartbeat/NACK for low-latency reply. |
| `CommandResponse` | `CommandProfile` | `Pattern.RPC` | *(none)* | Every response must be delivered. |
| `SlotRequest/Confirm/Release` | `SlotNegotiationProfile` | `Pattern.RPC` | *(none)* | Charging negotiation is correlated request/reply. |
| `StationStatus` | `StationStatusProfile` | `Pattern.Status` | *(none)* | UI late joiners need current station state. |
| `CoveragePoint` | `CoverageProfile` | `Pattern.Status` | PERSISTENT durability, KEEP_LAST 1000, max_samples 10000, max_samples_per_instance 2000 | Full coverage trail history survives restarts via RTI Persistence Service. |

### Liveliness — Presence Detection

DDS liveliness replaces the manual heartbeat / reconnect logic from the gRPC approach:

```python
# QoS configuration (in robot_qos.xml):
#   liveliness.kind = AUTOMATIC_LIVELINESS_QOS
#   liveliness.lease_duration = 100 ms
#
# Application code:
class MyListener(dds.DataReaderListener):
    def on_liveliness_changed(self, reader, status):
        if status.alive_count_change < 0:
            print(f"Robot DEAD — {status.last_publication_handle}")
        elif status.alive_count_change > 0:
            print(f"Robot ALIVE — {status.last_publication_handle}")
```

**Zero application polling.**  The middleware asserts liveliness automatically
based on write activity.  If a writer stops (crash, network loss), the lease
expires and all readers are notified within 100 ms.

---

## 6  Data Types

### Current types (in `robot_types.idl`)

The types are fully implemented in `robot_types.idl`.  Key data types,
separated by concern and keyed by `robot_id`:

> **No `timestamp` fields.**  DDS provides `source_timestamp` and
> `reception_timestamp` in the `SampleInfo` metadata of every sample —
> nanosecond precision, zero application code.  The gRPC approach must put an explicit
> `int64 timestamp` in every protobuf message; the DDS approach gets it for free.

| Type | Key | Fields | Notes |
|------|-----|--------|-------|
| `KinematicState` | `robot_id` | `position{x,y,z}, velocity{x,y,z}, heading` | 10 Hz, matches the gRPC approach |
| `OperationalState` | `robot_id` | `status (enum), battery_level` | On change; status = MOVING / IDLE / HOLDING / CHARGING |
| `Intent` | `robot_id` | `intent_type (enum), target_x, target_y, waypoints[], path_index` | On change; intent = FOLLOW_PATH / GOTO / IDLE / CHARGE_QUEUE / CHARGE_DOCK |
| `Telemetry` | `robot_id` | `cpu_usage, memory_usage, temperature, signal_strength` | 1 Hz; no `tcp_connections` / `delivery_loops` (those are gRPC artifacts). |
| `VideoFrame` | `robot_id` | `frame_data (octet sequence)` | ~5 fps JPEG |
| `CoveragePoint` | `robot_id` | `x, y` | Published on move; PERSISTENT durability via Persistence Service |
| `CommandRequest` | `request_id` | `robot_id, command (enum), parameters (JSON string)` | Reliable request |
| `CommandResponse` | `request_id` | `robot_id, success, description, resulting_status, resulting_intent` | Reliable response |
| `SlotRequest` | `request_id` | `robot_id, battery_level, info_only` | Charging negotiation |
| `SlotOffer` | `request_id` | `station_id, slot_id, queue_rank, wait_time_s, dock_x, dock_y` | Station response |
| `SlotConfirm` | `request_id` | `robot_id, slot_id` | Robot commits |
| `SlotAssignment` | `request_id` | `granted, queue_rank, wait_x, wait_y, dock_x, dock_y` | Station assignment |
| `SlotRelease` | `request_id` | `robot_id, slot_id` | Release / cancel |
| `SlotReleaseAck` | `request_id` | `success` | Release acknowledgement |
| `StationStatus` | `station_id` | `dock_x, dock_y, is_occupied, docked_robot_id, queue[]` | Live station state for UI |

**Key design difference from the gRPC approach:** `Telemetry` does **not** include
`tcp_connections` or `active_delivery_loops` — those metrics are gRPC-specific
artifacts.  In DDS there are no per-subscriber delivery loops and the
"connection" concept doesn't apply (multicast pub-sub).

### Key Enums (defined in `robot_types.idl`)

- `RobotStatus`: UNKNOWN / MOVING / IDLE / HOLDING / CHARGING
- `RobotIntent`: UNKNOWN / FOLLOW_PATH / GOTO / IDLE / CHARGE_QUEUE / CHARGE_DOCK
- `Command`: UNKNOWN / CMD_STOP / CMD_FOLLOW_PATH / CMD_GOTO / CMD_RESUME / CMD_SET_PATH / CMD_CHARGE

These mirror the gRPC approach's protobuf enums exactly — same values, same semantics —
expressed as IDL enumerations in `robot_types.idl`.

### Code Generation with `rtiddsgen`

`robot_types.idl` is the **single source of truth** for all data types.  Python
dataclasses are generated — never hand-written:

```bash
$NDDSHOME/bin/rtiddsgen -language python robot_types.idl
```

This produces `robot_types.py` (~300 lines, 23 types) with all the correct
`@idl.struct` decorators, `field(default_factory=...)` for mutable defaults,
`Sequence[T]` with `idl.bound()` for bounded sequences, `idl.key` annotations,
and `idl.octet` mappings.  The generated file carries a **DO NOT MODIFY**
header.

**Workflow when types change:**

1. Edit `robot_types.idl` (add/remove/rename fields, types, or enums)
2. Re-run `./types_generate.sh` (or `rtiddsgen -language python robot_types.idl`)
3. All Python code that imports from `robot_types` picks up the change — no
   manual struct editing, no missed fields.

> **Parallel to the gRPC approach:** The gRPC approach uses `protoc` to generate `robot_pb2.py`
> from `robot.proto`.  The DDS approach uses `rtiddsgen` to generate `robot_types.py`
> from `robot_types.idl`.  Same workflow — different IDL, different middleware,
> same single-source-of-truth principle.

---

## 7  Build & Run

### Prerequisites

- Python 3.14.3+
- RTI Connext DDS 7.6.0+ (Professional or Community edition)
- RTI Code Generator (`rtiddsgen`) — included in the Connext installation
- Virtual environment with packages: `rti.connext`, `flask`, `pybullet`,
  `Pillow`, `numpy`

### Environment Setup

```bash
source ../setup.sourceme dds
# or manually:
export NDDSHOME=/path/to/rti_connext_dds-7.6.0
export PATH=$NDDSHOME/bin:$PATH
export LD_LIBRARY_PATH=$NDDSHOME/lib/x64Darwin17clang9.0:$LD_LIBRARY_PATH  # macOS
pip install -r requirements.txt
```

### Launch

```bash
# (Re-)generate Python types from IDL (only needed after editing robot_types.idl):
$NDDSHOME/bin/rtiddsgen -language python robot_types.idl

# All 5 tractors + 2 charging stations:
./demo_start.sh

# Individual tractor:
python robot_node.py --id tractor1 --name fred

# Or specific tractors in separate terminals:
python robot_node.py --id tractor1 --name fred
python robot_node.py --id tractor2 --name alice
python robot_node.py --id tractor3 --name bob
python robot_node.py --id tractor4 --name carol
python robot_node.py --id tractor5 --name dave

# Charging stations:
python charging_station.py --id station1 --dock-x 8 --dock-y 92
python charging_station.py --id station2 --dock-x 92 --dock-y 8

# Command console:
python command_console.py

# Dashboard UI:
python robot_ui.py         # http://localhost:5000
```

> **No static mode needed.** DDS discovery is automatic — all participants on
> the same domain ID find each other via SPDP multicast.  No port lists, no
> peer addresses.

### Stop

```bash
./stop_robots.sh           # kills all robot_node, charging_station, and robot_ui processes
```

---

## 8  Thread Model (per robot node)

DDS eliminates the N² thread explosion of the gRPC approach.  Each robot node needs
approximately **6 threads** regardless of fleet size:

| Thread | Count | Purpose |
|--------|-------|---------|
| Main | 1 | Blocks on `sleep(1)` loop |
| `update_position` | 1 | Movement tick at 10 Hz (path following, collision avoidance) |
| `publish_state` | 1 | Publishes KinematicState at 10 Hz |
| `publish_intent` | 1 | Publishes Intent on change (or 1 Hz heartbeat) |
| `publish_telemetry` | 1 | Publishes Telemetry at 1 Hz |
| `print_status` | 1 | Log line every 5 s |
| DDS internal threads | ~3 | Receive, event, database (managed by middleware) |

**Compare with the gRPC approach:** a 5-robot fleet needs ~19 threads per robot (4
reader threads × 4 peers + connector threads).  A 100-robot fleet would need
~400 threads per robot.  In DDS it's still ~9.

**No delivery loops** — in the gRPC approach, each streaming RPC occupies a thread that
runs `while: yield; sleep` for every subscriber.  In DDS, `writer.write()`
returns immediately; the middleware handles serialisation and multicast
delivery internally.

**No reconnect threads** — in the gRPC approach, each peer gets a `_connect_to_peer`
thread that retries every 3 s on failure.  In DDS, discovery and reconnection
are handled by the middleware's internal SPDP/SEDP threads.

---

## 9  Relationship to Other Approaches

| Aspect | #1 Full Mesh gRPC | #2 Central Hub | #3 DDS + gRPC | #4 Pure DDS (this) |
|--------|--------------------|----------------|---------------|---------------------|
| Topology | Full mesh (N²) | Star (N) | Peer-to-peer multicast | Peer-to-peer multicast |
| Transport | gRPC / TCP | gRPC / TCP | DDS / UDP + gRPC / TCP | DDS / UDP multicast |
| Discovery | Zeroconf mDNS (bolted on) | Central server | DDS built-in | DDS built-in |
| QoS | None (TCP reliable) | None | Per-topic (pub-sub only) | Per-topic (everything) |
| Commands | gRPC unary | gRPC unary | gRPC unary | DDS request-reply |
| Charging | gRPC unary RPCs | gRPC unary RPCs | gRPC unary RPCs | DDS request-reply |
| Scalability | Poor (N²) | Better (N) | Good (multicast) | Good (multicast) |
| Presence detection | Seconds (TCP) | Seconds (TCP) | Milliseconds (DDS liveliness) | Milliseconds (DDS liveliness) |
| Delivery loops per robot | 4 × (N−1) | 4 × N | 0 (pub-sub) + gRPC loops | 0 |
| Threads per robot (N=100) | ~400 | ~10 | ~10 | ~9 |
| Extra protocols | Zeroconf, mDNS | — | Zeroconf (for gRPC peers) | None |

---

## 10  What DDS Eliminates (vs the gRPC approach)

These are concrete pieces of the gRPC approach code that **do not exist** in the DDS approach
because the middleware handles them:

| the gRPC approach Code | Lines | the DDS approach Equivalent |
|------------------|-------|------------------------|
| `fleet_discovery.py` (Zeroconf mDNS wrapper) | ~200 | DDS SPDP — zero code |
| `_connect_to_peer` reconnect loop | ~60 | DDS automatic reconnection — zero code |
| `_start_kinematic_stream` + reader thread | ~20 × 4 topics | `reader.take()` in listener callback — 5 lines |
| Per-subscriber `while: yield; sleep` delivery loops | ~15 × 4 topics | `writer.write()` — 1 line |
| `_delivery_loop_count` / `_enter/_exit_delivery_loop` | ~20 | Does not exist (no concept of delivery loops) |
| Channel connectivity monitoring | ~30 | `on_liveliness_changed` callback — 5 lines |
| `active_video_streams` counter | ~10 | Content-filtered subscription — middleware manages |
| Manual `robot_liveliness` timestamp polling | ~25 | DDS liveliness QoS — zero code |
| `int64 timestamp` field in every protobuf message | 8 B × 13 types | `SampleInfo.source_timestamp` / `reception_timestamp` — zero payload overhead, nanosecond precision |
| `protoc` → `robot_pb2.py` + `robot_pb2_grpc.py` | 2 generated files | `rtiddsgen` → `robot_types.py` — 1 generated file (no separate service stubs; DDS topics are declared at runtime) |
| Thread pool sizing (`max_workers=50`) | — | Middleware manages its own threads |

**Estimated code reduction:** ~400+ lines of networking / discovery / reconnect
/ threading / timestamping plumbing eliminated.

---

## 11  Failure Modes

These are the observable failure scenarios under DDS — contrast with the gRPC approach's
§9.

| Trigger | What happens | Why it's better than gRPC |
|---------|-------------|--------------------------|
| **WiFi blip** | DDS internally buffers reliable samples during the outage and retransmits on reconnect.  Best-effort samples are lost (harmless — next one arrives in 100 ms). | No reconnection storm.  No per-peer retry loops.  No CPU spike. |
| **Robot crash / kill** | Liveliness lease expires within 100 ms.  All readers receive `on_liveliness_changed` callback. | Detection in 100 ms vs seconds with TCP keepalive. |
| **Slow peer** | DDS flow control and writer history depth prevent unbounded buffering.  The slow reader loses best-effort samples gracefully. | No cascading failures.  No thread pool starvation. |
| **Startup burst** | All robots discover each other via multicast in parallel.  No TCP handshakes, no connection storms. | O(1) discovery messages vs O(N²) TCP connections. |
| **Port exhaustion** | Does not apply — DDS uses a small fixed number of UDP ports per participant, not one TCP connection per peer. | No `ulimit` tuning required. |
| **IP address change** | DDS SPDP re-announces with new address.  Peers re-match automatically. | No stale peer addresses.  No manual re-registration. |

---

## 12  Metrics to Watch

| Metric | Expected (DDS) | the gRPC approach (gRPC) for comparison |
|--------|---------------|-----------------------------------|
| **Connections per robot** | ~3 UDP sockets (fixed) | N−1 TCP connections (grows with fleet) |
| **Threads per robot** | ~9 (fixed) | ~4N (grows with fleet) |
| **Delivery loops** | 0 | 4 × (N−1) per robot |
| **Startup time to full mesh** | < 2 s (SPDP discovery) | 3–15 s (sequential TCP connects) |
| **Presence detection latency** | ≤ 100 ms (liveliness QoS) | 3–10 s (TCP keepalive) |
| **Late-joiner convergence** | < 100 ms (TRANSIENT_LOCAL) | < 100 ms (first stream yield) |
| **CPU during reconnection** | Negligible | Spike (N² TCP handshakes) |
| **Memory per robot** | ~50 MB (fixed) | ~50 MB + buffers (grows with N) |

---

## 13  Advanced DDS Features (available but not required for demo)

### Content Filtering

Subscribe to state from robots in a specific area only:

```python
cft = dds.ContentFilteredTopic(
    participant, "NearbyRobots", state_topic,
    dds.Filter("x > 40.0 AND x < 60.0 AND y > 40.0 AND y < 60.0")
)
reader = dds.DataReader(subscriber, cft)
```

### Time-Based Filtering

Receive telemetry at most once per second (even if published faster):

```python
reader_qos.time_based_filter.minimum_separation = dds.Duration(sec=1)
```

### Partitions

Separate robot fleets on the same network:

```python
publisher_qos.partition.name = ["FleetA"]   # Fleet A robots
publisher_qos.partition.name = ["FleetB"]   # Fleet B robots
```

---

## 14  Troubleshooting

### Robots not discovering each other

- Check firewall allows UDP multicast (port 7400+ for SPDP)
- Ensure all robots use the same domain ID (`--domain` flag)
- Verify `NDDSHOME` is set and `rti.connext` is installed
- On macOS: check that the loopback interface allows multicast
  (`sudo route add -net 239.0.0.0/8 -interface lo0`)

### High CPU usage

- Check QoS: `RELIABLE` + `KEEP_ALL` can cause backpressure if a reader
  falls behind
- Use `BEST_EFFORT` for high-rate data (KinematicState, Telemetry, Video)
- Check listener callbacks for blocking operations

### Missing data

- Verify QoS compatibility: a `RELIABLE` reader requires a `RELIABLE` writer
- Check durability: `TRANSIENT_LOCAL` reader needs `TRANSIENT_LOCAL` writer
- Verify topic names and type names match exactly

---

## 15  Ad-Hoc CLI Testing

Commands can be sent directly from the shell without the dashboard UI, using
the command console:

### Interactive Mode

```bash
python command_console.py
# console> send tractor1 STOP
# console> send tractor2 GOTO x=50,y=50,speed=3.0
# console> send tractor3 FOLLOW_PATH
# console> send tractor1 CHARGE
# console> send tractor4 SET_PATH waypoints=20,20;80,20;80,80;20,80
# console> send tractor1 RESUME
# console> list
# console> quit
```

### One-Shot Mode

```bash
# Send STOP
python command_console.py --robot tractor1 --command STOP

# Send GOTO
python command_console.py --robot tractor1 --command GOTO --params "x=50,y=50,speed=3.0"

# Send SET_PATH
python command_console.py --robot tractor1 --command SET_PATH \
  --params "waypoints=20,20;80,20;80,80;20,80"

# Send CHARGE
python command_console.py --robot tractor1 --command CHARGE
```

### DDS Spy (monitor all traffic)

```bash
# If $NDDSHOME/bin is on PATH:
rtiddsspy -domainId 0
```

This shows every DDS sample published on the domain — useful for verifying
that topics, types, and QoS are matching correctly.

---

## 16  Further Reading

- [RTI Connext Documentation](https://community.rti.com/documentation)
- [DDS Specification (OMG)](https://www.omg.org/spec/DDS/)
- [QoS Best Practices](https://community.rti.com/best-practices/qos-configuration)
- [DDS-RPC Specification](https://www.omg.org/spec/DDS-RPC/)
- [RTI Connext Python API](https://community.rti.com/static/documentation/connext-dds/7.6.0/doc/api/connext_dds/api_python/index.html)
