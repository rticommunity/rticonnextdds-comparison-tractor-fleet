# Robot Fleet Dashboard UI — Specification (the DDS approach: Pure DDS)

> This document fully specifies the `robot_ui.py` application so that an AI agent
> (or developer) can re-create it from scratch.  It assumes the reader has access
> to the IDL type definitions (`robot_types.idl`), the auto-generated Python types
> (`robot_types.py`), and the QoS profiles (`robot_qos.xml`).

---

## 1  Purpose

A single-page live web dashboard that subscribes to a fleet of robot nodes and
charging stations via DDS, displays real-time Kinematic State, Operational
State, Intent, Telemetry, and Charging Station status, and lets the operator
send commands to individual robots — all through an interactive browser UI.

**Key difference from the gRPC approach:** There is no gRPC, no Zeroconf, no mDNS, no
per-robot connection management.  The UI is a DDS DomainParticipant that
subscribes to the pub-sub topics for all robots simultaneously.  Robots and
stations are discovered automatically via SPDP.  Commands are sent via
`rti.rpc.Requester` — a single `send_request()` reaches all robots' `SimpleReplier`
instances; the target `robot_id` inside the `CommandRequest` selects which robot
acts on it.

**Data reception model:** In the gRPC approach, the UI opens N separate gRPC channels
(one per robot) and N more for charging stations.  In the DDS approach, the UI has one
`DomainParticipant` with a handful of `DataReader` objects — DDS delivers
samples from every robot through a single reader per topic type.  The `robot_id`
key field in each sample identifies the source.

---

## 2  Technology Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.14.3+ |
| Middleware | RTI Connext DDS 7.7.0+ (`rti.connextdds`, `rti.idl`, `rti.rpc`) |
| Type generation | `rtiddsgen -language python robot_types.idl` → `robot_types.py` |
| QoS configuration | `robot_qos.xml` — Pattern-based profiles (`BuiltinQosLib::Pattern.*`) |
| Web framework | Flask (single-file, `render_template_string`) |
| Live updates | Server-Sent Events (SSE) at 5 Hz |
| Frontend | Inline HTML + CSS + vanilla JavaScript in a single template string |
| Canvas | HTML5 `<canvas>` for the 2-D position map |
| Concurrency | Python `threading` (daemon threads) |

No external JS libraries.  No `grpcio`, `zeroconf`, or `ifaddr`.

### Comparison with the gRPC approach

| Concern | the gRPC approach | the DDS approach |
|---------|-------------|-------------|
| Robot data streams | 4 gRPC server-streaming RPCs per robot (N robots × 4 channels) | 5 DDS DataReaders (one per topic type — **flat** regardless of fleet size) |
| Charging station stream | 1 gRPC `StreamStatus` per station | 1 DDS DataReader on `StationStatus` topic (all stations) |
| Discovery | Zeroconf mDNS + `--fleet-port-list` fallback | DDS SPDP — zero code, zero CLI flags |
| Reconnection | Per-robot reconnect loops with 3 s backoff | Not needed — DDS handles it at the transport layer |
| Commands | gRPC unary RPC per robot (new channel per call) | `rti.rpc.Requester` — `send_request()` + `receive_replies()` |
| Video | gRPC `RequestVideo` server-streaming | DDS `VideoFrame` DataReader (BEST_EFFORT / VOLATILE) |
| Connection tracking | `connections` dict with `connecting/connected/disconnected` | DDS liveliness — robot appears when writer is alive, disappears when lost |
| External dependencies | `grpcio`, `zeroconf`, `ifaddr`, `flask` | `rti.connextdds`, `rti.rpc`, `flask` |

---

## 3  Command-Line Interface

```
python robot_ui.py [--domain DOMAIN_ID] --ui-port PORT
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--domain` | int | `0` | DDS Domain ID (must match robot nodes and charging stations) |
| `--ui-port` | int | `5000` | HTTP port for the web dashboard |

**No `--fleet-port-list`, no `--same-host`.** DDS SPDP discovers all
participants on the domain automatically.  Robot names are discovered at
runtime from the `robot_id` key field of incoming samples.

At startup `main()`:

1. Parses arguments.
2. Creates a `FleetStateStore()`.
3. Creates a `DdsFleetClient(store, domain_id)`.
4. Calls `client.start()` → creates the DDS participant, readers, listeners.
5. Creates and starts the Flask app on `--ui-port`.

**Comparison with the gRPC approach:** No `connect_to_port()` calls, no discovery
callbacks, no `connect_to_station()`.  The DDS client just joins the domain
and data flows in automatically.

---

## 4  Backend Architecture

The backend is split into two classes that enforce a clean separation between
**data storage** and **transport**:

- **`FleetStateStore`** — transport-agnostic, thread-safe data store.
  Identical in purpose and API to the gRPC approach's `FleetStateStore`.
- **`DdsFleetClient`** — owns the DDS DomainParticipant, DataReaders, and
  Requester.  Pushes plain Python dicts into the store; sends commands on
  behalf of the UI.

### 4.1  `FleetStateStore` class

A thread-safe data store with **no DDS or middleware knowledge**.

#### Data stores (all protected by a single `threading.Lock`)

| Dict | Key | Value |
|------|-----|-------|
| `kinematic` | robot_id (str) | `{x, y, z, vx, vy, vz, heading, ts}` |
| `operational` | robot_id | `{status, battery, ts}` |
| `intent` | robot_id | `{type, target_x, target_y, ts, waypoints?, waypoint_index?}` |
| `telemetry` | robot_id | `{cpu, memory, temperature, signal_strength, ts}` |
| `charging` | station_id (str) | `{station_id, dock_robot, queue, dock_x, dock_y}` |

Note: No `connections`, `addresses`, or `port_to_name` dicts — DDS doesn't use
per-robot connections.  Robot presence is inferred from having data in the
`kinematic` dict (robots publish at 10 Hz, so absence of data means offline).

#### Read methods

- **`snapshot()` → dict** — Returns a JSON-serialisable copy of all data stores.
  Builds a merged `state` dict combining kinematic and operational data for
  each robot.  Includes `charging` dicts.  This is what the SSE endpoint sends
  every 200 ms.
- **`known_robots()` → list[str]** — Returns sorted list of robot_ids that have
  kinematic data.

#### Write methods (called by the DDS client)

- **`update_kinematic(robot_id, data)`** — Store kinematic state dict.
- **`update_operational(robot_id, data)`** — Store operational state dict.
- **`update_intent(robot_id, data)`** — Store an intent dict with path waypoints.
- **`update_telemetry(robot_id, data)`** — Store a telemetry dict.
- **`update_charging(station_id, data)`** — Store a charging station status dict.
- **`remove_robot(robot_id)`** — Remove all data for a robot (called when DDS
  liveliness detects the robot is gone).

**Comparison with the gRPC approach:** Same conceptual API but simpler — no
`register_port`, `set_connection_status`, `clear_port_data`, `resolve_address`,
`_ensure_name`.  DDS identifies robots by `robot_id` directly, not by port.

### 4.2  `DdsFleetClient` class

Owns the DDS DomainParticipant and all DDS entities.  Holds a reference to a
`FleetStateStore`.

#### DDS Entities

| Entity | Type | QoS Profile | Purpose |
|--------|------|-------------|---------|
| `participant` | `DomainParticipant` | default | Single participant for the UI |
| Kinematic reader | `DataReader[KinematicState]` | `KinematicStateProfile` | 10 Hz position data from all robots |
| Operational reader | `DataReader[OperationalState]` | `OperationalStateProfile` | Status/battery changes from all robots |
| Intent reader | `DataReader[Intent]` | `IntentProfile` | Intent/path changes from all robots |
| Telemetry reader | `DataReader[Telemetry]` | `TelemetryProfile` | 1 Hz telemetry from all robots |
| Video reader | `DataReader[VideoFrame]` | `VideoProfile` | ~5 fps JPEG frames (filtered by `robot_id` content filter or taken for the active video robot) |
| StationStatus reader | `DataReader[StationStatus]` | `StationStatusProfile` | Charging station queue state |
| `command_requester` | `rti.rpc.Requester[CommandRequest, CommandResponse]` | default | Sends commands, receives responses |

#### `start()`

1. Resolve `robot_qos.xml` path relative to the script.
2. Create `QosProvider` and `DomainParticipant`.
3. Create topics: `KinematicState`, `OperationalState`, `Intent`, `Telemetry`,
   `VideoFrame`, `StationStatus`.
4. Create `Subscriber` and 6 `DataReader` objects with QoS from profiles.
5. Attach `DataReaderListener` to each reader (see §4.3).
6. Create `rti.rpc.Requester` for commands:
   ```python
   self.command_requester = rpc.Requester(
       request_type=CommandRequest,
       reply_type=CommandResponse,
       participant=self.participant,
       service_name="RobotCommand",
   )
   ```

#### Listeners

Each DataReader gets a listener with `on_data_available`.  The listener
callback calls `reader.take()`, iterates valid samples, converts each to a
plain Python dict, and calls the corresponding `store.update_*()` method.

- **KinematicListener** — extracts `robot_id`, `position.x/y/z`,
  `velocity.x/y/z`, `heading`, `source_timestamp` → `store.update_kinematic()`.
- **OperationalListener** — extracts `robot_id`, `status` (enum name string),
  `battery_level` → `store.update_operational()`.
- **IntentListener** — extracts `robot_id`, `intent_type` (enum name string),
  `target_x/y`, `path_waypoints` (as list of `{x,y}` dicts), `path_index`
  → `store.update_intent()`.
- **TelemetryListener** — extracts `robot_id`, `cpu_usage`, `memory_usage`,
  `temperature`, `signal_strength` → `store.update_telemetry()`.
- **StationStatusListener** — extracts `station_id`, `dock_x/y`, `is_occupied`,
  `docked_robot_id`, `queue` entries → `store.update_charging()`.
- **VideoListener** — stores the latest `frame_data` (JPEG bytes) keyed by
  `robot_id` in an internal dict.  The `/video_feed/<robot_id>` route reads
  from this dict.

**Comparison with the gRPC approach:** The gRPC approach has 4 reader threads per robot, each
maintaining its own gRPC stream.  The DDS approach has 6 listeners total (one per
topic type), each receiving data from **all** robots.  No per-robot threads, no
reconnect loops.

#### `send_command(robot_id, command, *, goto_x, goto_y, goto_speed, waypoints)`

1. Map command string → `Command` enum value.
2. Build `parameters` JSON string:
   - `CMD_GOTO` → `json.dumps({"x": goto_x, "y": goto_y, "speed": goto_speed})`.
   - `CMD_SET_PATH` → `json.dumps({"waypoints": waypoints})`.
   - `CMD_FOLLOW_PATH` / `CMD_RESUME` with speed > 0 →
     `json.dumps({"speed": goto_speed})`.
   - Otherwise → `""`.
3. Create `CommandRequest(robot_id=robot_id, command=cmd_enum, parameters=params)`.
4. Call `command_requester.send_request(request)`.
5. Call `command_requester.receive_replies(dds.Duration.from_seconds(5))`.
6. Collect replies.  Find the reply where `robot_id` matches the target
   (since all robots' SimpleRepliers receive the request, multiple replies
   arrive — only the target robot's reply is meaningful).
7. Return `{success, description, resulting_status, resulting_intent}`.

**Comparison with the gRPC approach:** The gRPC approach opens a new gRPC channel per command,
looks up the robot's address, sends `SendCommand`, closes the channel.
The DDS approach uses the persistent `Requester` — no address lookup, no channel
management.  The fan-out to all robots is inherent; the `robot_id` field
inside the request determines which robot actually acts on it.

#### `send_command_all(command, *, goto_x, goto_y, goto_speed, waypoints)`

Broadcast: iterates all known robots (from `store.known_robots()`) and calls
`send_command()` for each.  Returns a summary dict with `{broadcast, command,
calls_made, success, description, results}`.

#### `get_video_frame(robot_id)` → bytes or None

Returns the most recent JPEG `frame_data` for the given robot from the
VideoListener's internal buffer.  Returns `None` if no frame is available.

### 4.3  Flask routes

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Serves the inline HTML dashboard |
| `/stream` | GET | SSE endpoint — yields `data: <json>\n\n` every 200 ms (reads from `store.snapshot()`) |
| `/command` | POST | Accepts JSON `{robot_id, command, goto_x?, goto_y?, goto_speed?, waypoints?}`, calls `client.send_command()` or `client.send_command_all()` (when `robot_id == "*"`), returns JSON result |
| `/video_feed/<robot_id>` | GET | MJPEG stream — polls `client.get_video_frame(robot_id)` at ~5 Hz, wrapping each JPEG frame in a MIME multipart chunk (`multipart/x-mixed-replace; boundary=frame`). |
| `/ground_texture.jpg` | GET | Serves `field1_map.jpg` as the 2D canvas background texture |

---

## 5  Frontend Layout

Single-page, dark theme (Catppuccin Mocha palette).  Font: `Menlo` / `Consolas`
monospace, 13 px base.

**Identical to the gRPC approach** — the frontend is transport-agnostic.  It consumes
the same JSON shape from the SSE `/stream` endpoint and sends commands to the
same `/command` POST endpoint.  The only difference is the header text and the
telemetry footer note.

### Overall structure

```
┌──────────────────────────────────────────────────────────────┐
│  🤖 Robot Fleet Dashboard — the DDS approach: Pure DDS (Connext)   │  ← header + stats badge
├────────────────────────────────┬─────────────────────────────┤
│                                │  ▸ Commands                 │
│                                │  ▸ Onboard Camera           │
│         Position Map           │  ▸ Kinematic State          │
│        (canvas, flex:1)        │  ▸ Operational State        │
│                                │  ▸ Intent                   │
│                                │  ▸ Telemetry                │
│                                │  ▸ ⚡ Charging Stations     │
│                                │  ▸ Connections              │
└────────────────────────────────┴─────────────────────────────┘
```

- Left: `.map-panel` — flex:1, contains a `<canvas>` that fills available space.
- Right: `.data-panel` — fixed width 480 px, scrollable, stacked sections.
- Header: Stats badge shows `N robots · S stations · DDS domain D`.
- All sections are **collapsible** (click heading to toggle).

### 5.1  Color Palette

Identical to the gRPC approach.  Same CSS custom properties, same robot color
assignment logic (fixed palette of 10 colours, cycled by first appearance).

---

## 6  Data Tables (Right Panel)

All tables update every SSE frame (5 Hz).  Rows are sorted alphabetically by
robot name.

### 6.1  Command Panel

Identical to the gRPC approach — same robot dropdown, X/Y/Speed inputs, GOTO/PATH/
STOP/RESUME/CHARGE buttons.  Broadcast to `* ALL` supported.

### 6.2  Onboard Camera Panel

Identical to the gRPC approach — same Start/Stop buttons, same video container.  The
`/video_feed/<rid>` route polls the DDS VideoFrame reader instead of a gRPC
stream, but the browser sees the same MJPEG stream.

### 6.3  Kinematic State Table

Columns: robot name (blue bold), `(x, y)` position, speed (u/s), heading (°).

### 6.4  Operational State Table

Columns: robot name, status string (with `⚡ CHARGING` in peach/bold), battery
% (colour-coded: green > 50, yellow > 20, red ≤ 20).

### 6.5  Intent Table

Columns: robot name, intent type string, `(target_x, target_y)`.

### 6.6  Telemetry Table

Columns: robot name, CPU %, MEM %, temperature °C, signal strength %.

No `tcp_connections` or `delivery_loops` columns — those are gRPC artifacts
that don't exist in DDS.  Replaced by `signal_strength`.

Footer note: *"DDS: zero per-robot connections — middleware delivers via
multicast from a single write() call"*.

### 6.7  Charging Stations Panel

Identical to the gRPC approach — same dock status display, same queue rendering with
rank icons (🔌 Charging, 🚜 En route, ⏳ Waiting, … Pending).  Data comes from
the `StationStatus` DDS topic instead of gRPC `StreamStatus`.

### 6.8  Connections Panel

**Simplified** compared to the gRPC approach.  No per-port connection status tracking.

Instead, shows:
- Number of discovered robots (from kinematic data).
- Number of discovered stations (from charging data).
- DDS domain ID.

**Comparison with the gRPC approach:** The gRPC approach shows per-port connection dots
(green/yellow/red).  The DDS approach has no connection concept — DDS handles
transport internally.  Robot presence is determined by having recent data.

---

## 7  Canvas Position Map

Identical to the gRPC approach — same coordinate system (0–100 arena), same margin,
same Y-inversion, same grid, same ground texture, same robot colour assignment.

### 7.1  Charging Station Icons

Same rendering as the gRPC approach — glowing pad with green/blue/orange state, ⚡
emoji, queue count badge.

### 7.2  Per-Robot Rendering

Same drawing order and visual style as the gRPC approach — path route (dashed loop
with waypoint dots), intent arrow, tractor icon with status beacon, GOTO
crosshair, flash marker.

### 7.3  Mouse Interactions

Identical to the gRPC approach — click for GOTO, drag waypoints, double-click to add
waypoint, ⌥+click to remove.  Same hit-testing, same dblclick/click
discrimination.

---

## 8  SSE Data Flow

```
┌──────────────┐  DDS pub-sub topics    ┌──────────────────┐
│  Robot Nodes  │ ─────────────────────►│ DdsFleetClient    │
│  (N robots)   │  Kin/Op/Intent/Telem  │   (Python)        │
│               │  VideoFrame           │  6 DataReaders    │
└──────────────┘                        └────────┬─────────┘
                                                 │ store.update_*()
┌──────────────┐  DDS StationStatus     ┌────────┤
│  Charging Stn │ ─────────────────────►│  FleetStateStore      │
│  (M stations) │  (RELIABLE/TL)        │  (transport-agnostic) │
└──────────────┘                        └────────┬──────────┘
                                                 │ snapshot() every 200ms
                                     ┌───────────▼──────────┐
                                     │  SSE: /stream         │
                                     │  data: {state,        │
                                     │    kinematic,          │
                                     │    operational,        │
                                     │    intent, telemetry,  │
                                     │    charging}           │
                                     └───────────┬──────────┘
                                                 │ EventSource
                                     ┌───────────▼──────────┐
                                     │  Browser JS           │
                                     │  updates tables +     │
                                     │  redraws canvas       │
                                     └──────────────────────┘
```

**Key difference:** the gRPC approach has N gRPC channels (one per robot) with 4
reader threads each, plus M station channels.  The DDS approach has 6 DDS DataReaders
total — regardless of fleet size.  Data from all robots arrives through the
same reader per topic type, keyed by `robot_id`.

The browser JS stashes:
- `lastIntentData` — not overwritten during drag.
- `lastChargingData` — for station icon rendering on map.
- `lastOperationalData` — cross-referenced by station icons and charging panel.

---

## 9  Command Flow (Browser → Robot)

```
Browser JS                     Flask /command             Robot DDS
──────────                     ──────────────             ─────────
sendCommand(rid, cmd, extra)
  → POST /command {json}
                               → client.send_command()
                                 → build CommandRequest
                                 → requester.send_request()  → all robots receive
                                 → requester.receive_replies()
                                 ← CommandResponse (from     ← target robot responds
                                    matching robot)
                               ← json {success, desc, …}
  ← update #cmd-result
```

For broadcast (`robot_id == "*"`): iterates all known robots, sends one request
per robot (each robot's SimpleReplier filters by `robot_id`).  Returns summary
with `calls_made` count.

**Comparison with the gRPC approach:** No address lookup, no per-command channel
creation.  The `Requester` is persistent.  Fan-out is automatic — every
robot's `SimpleReplier` receives every request, but only the robot whose
`robot_id` matches actually executes the command (others return a no-op
response which the UI ignores).

---

## 10  Resilience Behaviour

| Scenario | Behaviour |
|----------|-----------|
| Robot disconnects | DDS liveliness detects loss.  Robot's data goes stale (no new kinematic samples).  UI can optionally remove stale robots after a timeout. |
| Robot restarts | New participant discovered via SPDP.  First kinematic sample re-adds the robot to the store.  No reconnect logic needed. |
| Charging station disconnects | `StationStatus` topic goes stale.  UI keeps showing last-known state.  TRANSIENT_LOCAL ensures late-joining UI gets current state on reconnect. |
| Command to unknown robot | All SimpleRepliers return no-op responses.  UI reports `{success: false, description: "No matching robot responded"}`. |
| Network blip | DDS internally buffers reliable samples and retransmits.  BEST_EFFORT streams (kinematic, telemetry, video) simply miss a few samples and resume. |
| Late-joining UI | TRANSIENT_LOCAL topics (operational, intent, station status) deliver the current state immediately on join.  VOLATILE topics (kinematic, telemetry) start from the next published sample. |

**Comparison with the gRPC approach:** No per-robot reconnect loops, no 3 s backoff
timers, no `stream_failed` events.  DDS handles all transport-level resilience
internally.

---

## 11  Thread Summary

| Thread | Daemon | Description |
|--------|--------|-------------|
| Main | No | Flask HTTP server (`app.run()`) |
| DDS internal | — | Receive, event, database (~3 threads per participant) |
| DDS listener callbacks | — | Invoked on DDS internal threads when data arrives |

**Total: ~4 threads** (1 Flask + ~3 DDS internal).

**Comparison with the gRPC approach:** The gRPC approach has 1 Flask thread + N × 5 threads
(robot_loop + 4 reader threads per robot) + M station threads + discovery
thread.  For 3 robots + 2 stations, that's ~22 threads.  The DDS approach has ~4
regardless of fleet size.

---

## 12  File Structure

The application is a **single Python file** (`robot_ui.py`) that:

1. Imports DDS types from `robot_types.py` (generated — never hand-edited).
2. Defines `FleetStateStore` (transport-agnostic data store).
3. Defines `DdsFleetClient` (DDS communication layer).
4. Defines `DASHBOARD_HTML` as a raw string containing the full HTML/CSS/JS.
5. Defines `create_app(store, client)` returning a Flask app.
6. Defines `main()` parsing CLI args, creating the store + client, and starting
   everything.

No templates directory, no external JS/CSS.  One static asset (`field1_map.jpg`)
served by the `/ground_texture.jpg` route.

### Companion files

| File | Purpose |
|------|---------|
| `robot_types.idl` | DDS IDL type definitions — single source of truth |
| `robot_types.py` | Auto-generated Python types (`rtiddsgen -language python robot_types.idl`) |
| `robot_qos.xml` | QoS profiles — Pattern-based (`BuiltinQosLib::Pattern.*`) |

### External Dependencies

| Package | Use |
|---------|-----|
| `rti.connextdds` | DDS middleware — participant, topics, readers |
| `rti.rpc` | Request-reply — `Requester` for commands |
| `rti.idl` | DDS type system |
| `flask` | Web server and SSE endpoint |

### What Doesn't Exist (vs the gRPC approach)

| the gRPC approach Code | Purpose | Why it doesn't exist in the DDS approach |
|------------------|---------|-----------------------------------|
| `fleet_discovery.py` import | Zeroconf mDNS discovery | DDS SPDP — automatic |
| `FleetDiscovery` class | mDNS peer/station callbacks | Not needed |
| `GrpcFleetClient` class | gRPC channel management | Replaced by `DdsFleetClient` |
| `connect_to_port()` / `connect_to_address()` | Per-robot connection | DDS DataReaders receive from all writers automatically |
| `_robot_loop()` + reconnect logic | Per-robot reconnect with backoff | DDS handles transport internally |
| `_read_kinematic()` etc. (4 threads per robot) | Per-robot gRPC stream readers | Replaced by 6 DDS DataReaderListeners (total, not per-robot) |
| `_station_loop()` | Per-station gRPC stream reader | StationStatus DataReader handles all stations |
| `connections` / `addresses` / `port_to_name` dicts | Connection tracking | DDS doesn't have per-robot connections |
| `stream_failed` event | Detect gRPC stream loss | DDS liveliness |
| `--fleet-port-list` / `--same-host` CLI flags | Manual robot addressing | DDS automatic discovery |

---

## 13  DDS Type Contract Summary

The UI uses the following types from `robot_types.py`:

### Types Read (via DataReaders)

| Type | Key | QoS Profile | Fields Used |
|------|-----|-------------|-------------|
| `KinematicState` | `robot_id` | `KinematicStateProfile` | `robot_id`, `position.x/y/z`, `velocity.x/y/z`, `heading` |
| `OperationalState` | `robot_id` | `OperationalStateProfile` | `robot_id`, `status` (enum), `battery_level` |
| `Intent` | `robot_id` | `IntentProfile` | `robot_id`, `intent_type` (enum), `target_x/y`, `path_waypoints[]`, `path_index` |
| `Telemetry` | `robot_id` | `TelemetryProfile` | `robot_id`, `cpu_usage`, `memory_usage`, `temperature`, `signal_strength` |
| `VideoFrame` | `robot_id` | `VideoProfile` | `robot_id`, `frame_data` (JPEG bytes) |
| `StationStatus` | `station_id` | `StationStatusProfile` | `station_id`, `dock_x/y`, `is_occupied`, `docked_robot_id`, `queue[]` |
| `QueueEntry` | — | (nested) | `robot_id`, `rank`, `confirmed` |

### Types Written (via `rti.rpc.Requester`)

| Type | Key | Fields Written | Mechanism |
|------|-----|----------------|-----------|
| `CommandRequest` | — | `robot_id`, `command` (enum), `parameters` (JSON string) | `command_requester.send_request()` |

### Types Received (via `rti.rpc.Requester`)

| Type | Key | Fields Read | Mechanism |
|------|-----|-------------|-----------|
| `CommandResponse` | — | `robot_id`, `success`, `description`, `resulting_status`, `resulting_intent` | `command_requester.receive_replies()` |

### Enums

- `RobotStatus`: `STATUS_UNKNOWN=0`, `STATUS_MOVING=1`, `STATUS_IDLE=2`, `STATUS_HOLDING=3`, `STATUS_CHARGING=4`
- `RobotIntentType`: `INTENT_UNKNOWN=0`, `INTENT_FOLLOW_PATH=1`, `INTENT_GOTO=2`, `INTENT_IDLE=3`, `INTENT_CHARGE_QUEUE=4`, `INTENT_CHARGE_DOCK=5`
- `Command`: `COMMAND_UNKNOWN=0`, `CMD_STOP=1`, `CMD_FOLLOW_PATH=2`, `CMD_GOTO=3`, `CMD_RESUME=4`, `CMD_SET_PATH=5`, `CMD_CHARGE=6`

---

## 14  Deployment

The UI is launched by `demo_start.sh` after the robot nodes and charging stations:

```bash
# Start the dashboard
python robot_ui.py --ui-port 5000 &
```

The browser connects to `http://localhost:5000`.  The UI joins DDS domain 0
and begins receiving data from all robots and stations within seconds.

**Comparison with the gRPC approach:** No `--fleet-port-list` needed.  No
`--same-host` workaround.  The dashboard just works on any machine that shares
the DDS domain.
