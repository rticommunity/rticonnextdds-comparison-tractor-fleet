# Robot Fleet Dashboard UI — Specification

> This document fully specifies the `robot_ui.py` application so that an AI agent
> (or developer) can re-create it from scratch.  It assumes the reader has access
> to the `robot.proto` service/message definitions and the generated Python stubs
> (`robot_pb2.py`, `robot_pb2_grpc.py`).

---

## 1  Purpose

A single-page live web dashboard that connects to a fleet of robot nodes and
charging stations via gRPC, displays real-time Kinematic State, Operational
State, Intent, Telemetry, and Charging Station status, and lets the operator
send commands to individual robots — all through an interactive browser UI.

---

## 2  Technology Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.14.3+ |
| Web framework | Flask (single-file, `render_template_string`) |
| Live updates | Server-Sent Events (SSE) at 5 Hz |
| Frontend | Inline HTML + CSS + vanilla JavaScript in a single template string |
| Canvas | HTML5 `<canvas>` for the 2-D position map |
| gRPC | `grpcio` / `grpcio-tools` / `protobuf` |
| Threading | Python `threading` (daemon threads, `threading.Event`) |

No external JS libraries.  Everything is self-contained in one Python file.

---

## 3  Command-Line Interface

```
python robot_ui.py [--fleet-port-list PORTS] --ui-port PORT [--same-host]
```

| Argument | Example | Description |
|----------|---------|-------------|
| `--fleet-port-list` | `50051,50052,50053` | Comma-separated list of all fleet member gRPC port numbers (all on `localhost`). If omitted, mDNS discovery is used instead. |
| `--ui-port` | `5000` | HTTP port for the web dashboard |
| `--same-host` | — | When set, discovered peer addresses are rewritten to `localhost:<port>` instead of using the mDNS-advertised IP. Useful as a fallback if LAN connections fail on a single machine. |

**If `--fleet-port-list` is provided** (static mode): calls
`client.connect_to_port(port)` for each port immediately at startup.  Also
starts a `FleetDiscovery` for charging station discovery.

**If `--fleet-port-list` is omitted** (discovery mode): creates a
`FleetDiscovery(robot_id="ui", grpc_port=None, on_peer_added=callback,
on_station_added=station_callback)` and calls `discovery.start()`.  The
peer callback:
- If `--same-host`: calls `client.connect_to_port(port)` → connects via `localhost`.
- Otherwise: calls `client.connect_to_address(host, port)` → connects via the
  mDNS-advertised IP.

The station callback connects to newly discovered charging stations via
`client.connect_to_station(address)`.

Robot names are **not** supplied on the command line.  They are **discovered at
runtime** from the `robot_id` field of incoming stream messages.

---

## 4  Backend Architecture

The backend is split into two classes that enforce a clean separation between
**data storage** and **transport**:

- **`FleetStateStore`** — transport-agnostic, thread-safe data store.
  No knowledge of gRPC or protobuf.
- **`GrpcFleetClient`** — owns all gRPC connections and protobuf logic.
  Pushes plain Python dicts into the store; sends commands on behalf of the UI.

### 4.1  `FleetStateStore` class

A thread-safe data store with **no gRPC or protobuf knowledge**.

#### Data stores (all protected by a single `threading.Lock`)

| Dict | Key | Value |
|------|-----|-------|
| `kinematic` | robot name (str) | `{x, y, z, vx, vy, vz, heading, ts}` |
| `operational` | robot name | `{status, battery, ts}` |
| `intent` | robot name | `{type, target_x, target_y, ts, waypoints?, waypoint_index?}` |
| `telemetry` | robot name | `{cpu, memory, temperature, tcp_connections, delivery_loops, ts}` |
| `connections` | port key (str) | `"connecting"` / `"connected"` / `"disconnected"` |
| `addresses` | port key | `"host:port"` |
| `port_to_name` | port key | discovered robot name |
| `charging` | station\_id (str) | `{station_id, dock_robot, queue, dock_x, dock_y}` |

#### Read methods

- **`snapshot()` → dict** — Returns a JSON-serialisable copy of all data stores.
  Builds a merged `state` dict combining kinematic and operational data for
  each robot.  Includes `port_names`, `charging` dicts.  This is what the SSE
  endpoint sends every 200 ms.
- **`resolve_address(robot_id)` → str | None** — Reverse-lookup a robot name to
  its `host:port` address via `port_to_name`.

#### Write methods (called by the communication layer)

- **`register_port(port_key, address)`** — Record a new port as `"connecting"`.
- **`set_connection_status(port_key, status)`** — Update connection status.
- **`clear_port_data(port_key)`** — Remove stale data for a disconnected port
  (pops `port_to_name`, `kinematic`, `operational`, `intent`, `telemetry`
  for the old robot name).
- **`update_kinematic(port_key, robot_id, data)`** — Store kinematic state.
  Handles name discovery / name changes via `_ensure_name()`.
- **`update_operational(port_key, robot_id, data)`** — Store operational state.
- **`update_intent(robot_id, data)`** — Store an intent dict.
- **`update_telemetry(robot_id, data)`** — Store a telemetry dict.
- **`update_charging(station_id, data)`** — Store a charging station status dict.

### 4.2  `GrpcFleetClient` class

Owns all gRPC / protobuf logic.  Holds a reference to a `FleetStateStore`.

#### `connect_to_port(port: int)`

Calls `store.register_port()`, then spawns a daemon thread running
`_robot_loop(port_key, address)` where `address = "localhost:<port>"`.

#### `connect_to_address(host: str, port: int)`

Same as `connect_to_port` but uses `address = "<host>:<port>"` (real IP).
Used by the discovery-mode callback when `--same-host` is **not** set,
so robots on other machines are reached at their mDNS-advertised address.

#### `connect_to_station(address: str)`

Spawns a daemon thread running `_station_loop(address)` that subscribes to
the charging station's `StreamStatus` server-streaming RPC.

#### `_robot_loop(port_key, address)`

Reconnect loop:

1. Open `grpc.insecure_channel(address)`.
2. Wait for channel ready (5 s timeout).
3. Call `store.set_connection_status(port_key, "connected")`.
4. Create a `RobotStreamingStub` and spawn four reader threads sharing a
   `threading.Event` called `stream_failed`.
5. Block until `stream_failed` is set or `self.running` is False.
6. On failure → call `store.set_connection_status(port_key, "disconnected")`
   and `store.clear_port_data(port_key)`, sleep 3 s, retry.

#### `_station_loop(address)`

Reconnect loop for a charging station:

1. Open `grpc.insecure_channel(address)`.
2. Wait for channel ready (5 s timeout).
3. Create a `ChargingStationStub`, call `StreamStatus(StationStatusRequest())`.
4. For each `StationStatusResponse`, convert queue entries to plain dicts and
   call `store.update_charging(msg.station_id, data)`.
5. On stream loss → log, sleep 3 s, retry.

#### Reader threads

Each reader opens a **server-streaming** subscription by sending a
`SubscribeRequest(subscriber_id="ui")` and iterating over the response stream.

- **`_read_kinematic(port_key, stub, stream_failed)`**
  - Converts each `KinematicState` to a plain dict (position, velocity, heading).
  - Calls `store.update_kinematic(port_key, msg.robot_id, data)`.

- **`_read_operational(port_key, stub, stream_failed)`**
  - Converts each `OperationalState` to a plain dict (status name, battery).
  - Calls `store.update_operational(port_key, msg.robot_id, data)`.

- **`_read_intent(port_key, stub, stream_failed)`**
  - Converts each `IntentUpdate` to a plain dict with path waypoints.
  - Calls `store.update_intent(msg.robot_id, entry)`.

- **`_read_telemetry(port_key, stub, stream_failed)`**
  - Converts each `TelemetryUpdate` to a plain dict.
  - Calls `store.update_telemetry(msg.robot_id, data)`.

#### `send_command(robot_id, command, *, goto_x, goto_y, goto_speed, waypoints)`

1. Call `store.resolve_address(robot_id)` to find the address.
2. Map command string → protobuf `Command` enum.
3. Build a `CommandRequest`:
   - `CMD_GOTO` → attach `GotoParameters(x, y, speed)`.
   - `CMD_FOLLOW_PATH` or `CMD_RESUME` with speed > 0 → attach
     `GotoParameters(speed=speed)`.
   - `CMD_SET_PATH` with waypoints → attach `SetPathParameters`.
4. Open a **new** `insecure_channel`, send `SendCommand` (timeout 5 s), close.
5. Return `{success, description, resulting_status, resulting_intent}`.

#### `send_command_all(command, *, goto_x, goto_y, goto_speed, waypoints)`

Broadcast: iterates all known robots and calls `send_command()` for each.
Returns a summary dict with `{broadcast, command, grpc_calls_made, success,
description, results}`.

#### `stream_video(robot_id)` → generator of JPEG bytes

Opens a `RequestVideo` gRPC server-streaming call to the robot's
`RobotCommand` service.  Yields raw JPEG `frame_data` bytes from each
`VideoFrame` message.  The gRPC channel is kept alive for the duration of the
generator and closed in a `finally` block when the caller stops iterating.

### 4.3  Flask routes

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Serves the inline HTML dashboard |
| `/stream` | GET | SSE endpoint — yields `data: <json>\n\n` every 200 ms (reads from `store.snapshot()`) |
| `/command` | POST | Accepts JSON `{robot_id, command, goto_x?, goto_y?, goto_speed?, waypoints?}`, calls `client.send_command()` or `client.send_command_all()` (when `robot_id == "*"`), returns JSON result |
| `/video_feed/<robot_id>` | GET | MJPEG stream — iterates `client.stream_video(robot_id)`, wrapping each JPEG frame in a MIME multipart chunk (`multipart/x-mixed-replace; boundary=frame`). |
| `/ground_texture.jpg` | GET | Serves `field1_map.jpg` as the 2D canvas background texture |

---

## 5  Frontend Layout

Single-page, dark theme (Catppuccin Mocha palette).  Font: `Menlo` / `Consolas`
monospace, 13 px base.

### Overall structure

```
┌──────────────────────────────────────────────────────────┐
│  🤖 Robot Fleet Dashboard — the gRPC approach: Full Mesh gRPC  │  ← header + stats badge
├────────────────────────────────┬─────────────────────────┤
│                                │  ▸ Commands             │
│                                │  ▸ Onboard Camera       │
│         Position Map           │  ▸ Kinematic State      │
│        (canvas, flex:1)        │  ▸ Operational State    │
│                                │  ▸ Intent               │
│                                │  ▸ Telemetry            │
│                                │  ▸ ⚡ Charging Stations │
│                                │  ▸ Connections          │
└────────────────────────────────┴─────────────────────────┘
```

- Left: `.map-panel` — flex:1, contains a `<canvas>` that fills available space.
- Right: `.data-panel` — fixed width 480 px, scrollable, stacked sections.
- Header: Stats badge shows `N robots · M TCP sockets · L delivery loops · C UI channels`.
- All sections are **collapsible** (click heading to toggle).

### 5.1  Color Palette (CSS custom properties)

```
--bg: #1e1e2e    --panel: #2a2a3d   --header: #3b3b55
--text: #cdd6f4  --dim: #6c7086     --blue: #89b4fa
--green: #a6e3a1 --yellow: #f9e2af  --red: #f38ba8
--peach: #fab387 --canvas: #181825  --grid: #313244  --border: #45475a
```

### 5.2  Robot Color Assignment

A fixed palette of 10 colours is cycled through in order of first appearance:

```
#89b4fa  #a6e3a1  #fab387  #f9e2af  #f38ba8
#cba6f7  #94e2d5  #f5c2e7  #74c7ec  #b4befe
```

Each robot gets a stable colour assigned on first sight (not re-assigned).

---

## 6  Data Tables (Right Panel)

All tables update every SSE frame (5 Hz).  Rows are sorted alphabetically by
robot name.

### 6.1  Command Panel

Position: **top** of the right panel.

| Element | Details |
|---------|---------|
| Robot dropdown | `<select>` populated from `state` keys + `* ALL` broadcast option last.  Only rebuilt when the set of names changes; preserves previous selection. |
| X / Y inputs | `type="number"`, 0–100, step 1, default 50 |
| Speed input | `type="number"`, 0.5–20, step 0.5, default 2.0, wider |
| GOTO button | Blue border/text, calls `sendGoto()` |
| PATH button | Green border/text, calls `sendSimple("FOLLOW_PATH")` |
| STOP button | Red border/text, calls `sendSimple("STOP")` |
| RESUME button | Yellow border/text, calls `sendSimple("RESUME")` |
| CHARGE button | Peach border/text with ⚡ icon, calls `sendSimple("CHARGE")` |
| Result line | Shows `✓`/`✗` + description + `[STATUS/INTENT]`.  Broadcast shows per-robot pass/fail with gRPC call count. |
| Hint line | Describes mouse interactions: Click, Drag, Double-click, ⌥+Click |

`sendSimple` sends `goto_speed` alongside the command (so FOLLOW_PATH/RESUME pick up the speed value).

### 6.2  Onboard Camera Panel

Position: **second** in the right panel (between Commands and Kinematic State).

| Element | Details |
|---------|---------|
| Robot dropdown | `<select id="vid-robot">` populated identically to the command dropdown. |
| Start button | `📹 Start` — calls `startVideo()`: sets `activeVideoRobot`, replaces the container content with `<img src="/video_feed/<rid>">`. |
| Stop button | `⏹ Stop` (red-accented) — calls `stopVideo()`: clears `activeVideoRobot`, replaces container with placeholder text. |
| Video container | `.video-container` — black background, rounded corners, min-height 60 px, flex-centered. |

### 6.3  Kinematic State Table

Columns: robot name (blue bold), `(x, y)` position, speed (u/s), heading (°).

### 6.4  Operational State Table

Columns: robot name, status string (with `⚡ CHARGING` in peach/bold when
charging), battery % (colour-coded: green > 50, yellow > 20, red ≤ 20).

### 6.5  Intent Table

Columns: robot name, intent type string, `(target_x, target_y)`.

### 6.6  Telemetry Table

Columns: robot name, CPU %, MEM %, temperature °C, TCP connections (peach),
delivery loops.

Footer note: *"DDS: 0 delivery loops — middleware delivers from a single
write() call"*.

### 6.7  Charging Stations Panel

Displays each station with its dock coordinates and queue.

| State | Display |
|-------|---------|
| Station idle (no queue) | `"Idle"` in dim text |
| Rank 0, robot `STATUS_CHARGING` | `🔌 Charging` in green; row has green tint background; robot name bold |
| Rank 0, robot not yet charging | `🚜 En route` in blue |
| Confirmed, rank > 0 | `⏳ Waiting` in yellow |
| Not confirmed | `… Pending` in dim |

The panel cross-references the robot's actual `operational` status to
distinguish "assigned to dock but still en route" from "physically at the dock
and charging".

### 6.8  Connections Table

Columns: label, port, status dot.

- **Connected**: label = discovered robot name, dot = green `●`.
- **Connecting**: label = `port <N>`, dot = yellow `◌`.
- **Disconnected**: label = `port <N>`, dot = red `✕`.

---

## 7  Canvas Position Map

### 7.1  Coordinate System

- Arena: 0–100 × 0–100 (both axes).
- Canvas margin `M = 30` px on all sides.
- Conversion: `toPx(arenaX, arenaY)` → pixel `(x, y)` where Y is **inverted**
  (arena Y 0 is at the bottom, Y 100 is at the top).
- Grid lines every 20 units, with axis labels.  Border rectangle drawn with
  border colour.
- When a ground texture image is loaded, it's drawn inside the arena area
  with a slight darkening overlay.

### 7.2  Charging Station Icons

Drawn from `lastChargingData` (stashed each SSE frame).  Each station shows:

- **Glowing pad**: circular fill (radius 18) with shadow blur.  Three states:
  - **Charging** (robot confirmed `STATUS_CHARGING`): green glow + outline
  - **En route** (dock_robot set but not yet charging): blue glow + outline
  - **Idle** (no robot): orange/peach glow + outline
- **Lightning bolt** emoji centred in the circle.
- **Label** below: dock occupant name or "CHARGE".
- **Queue count badge** `[N]` in yellow if queue is non-empty.

The station cross-references `lastOperationalData` (stashed each SSE frame) to
determine whether the docked robot is actually charging vs still en route.

### 7.3  Per-Robot Rendering (drawn for each robot that has state data)

Drawing order (back to front):

#### 7.3.1  Path Route

Drawn only if intent data contains `waypoints` array with ≥ 2 entries.

- **Route loop**: dashed line (dash `[4,6]`), robot colour, lineWidth 2.5,
  globalAlpha 0.55.  Connects all waypoints in order with `closePath()`.
- **Waypoint dots**: radius 11 (or 14 if being dragged), hitRadius 6.
  - Fill: robot colour at alpha 0.7 (or white at 0.9 if dragged).
  - Stroke: robot colour lineWidth 2 (or red `#f38ba8` lineWidth 3 if dragged).
  - Centred index label: bold 11 px Menlo, white (or dark if dragged).

#### 7.3.2  Intent Arrow

Dashed line (dash `[4,4]`) from robot position to current target position, robot
colour, lineWidth 1.5.  Triangular arrowhead at the target end.

#### 7.3.3  Tractor Icon

The active icon is a stylised **tractor** drawn by `drawTractor()`.  The icon
is rotated to face the robot's direction of travel.

**Heading / rotation**

The heading angle is computed from velocity fields (`vx`, `vy`) in the merged
state data.  When velocity is near zero, falls back to the heading field from
`KinematicState`.  Canvas Y is inverted so `vy` is negated.

The tractor body is drawn inside a `save/translate/rotate/restore` block.

**Tractor parts** (all drawn relative to origin after translate, stroke width
`lw = 2.5`):

| Part | Details |
|------|---------|
| Shadow | Black ellipse (alpha 0.25) |
| Cabin | Two side-by-side windows with tinted fill |
| Cabin roof | Solid bar across top |
| Neck / pillars | Two vertical lines connecting cabin to chassis |
| Body plate | Solid filled rectangle (alpha 0.25) |
| Chassis bar | Horizontal line across wheel span |
| Wheels | Two tall rounded rectangles with hub lines |
| Axle bar | Horizontal line between wheel inner edges |
| Status beacon | Filled glowing circle (radius 6) on top of cabin |
| Label | Robot name below (drawn outside rotation for readability) |

**Status beacon colours**:

| Status | Colour | Hex |
|--------|--------|-----|
| `STATUS_MOVING` | Green | `#22dd22` |
| `STATUS_HOLDING` | Blue | `#89b4fa` |
| `STATUS_IDLE` | Pink/Red | `#f38ba8` |
| `STATUS_CHARGING` | Warm Yellow | `#f9e2af` |

The beacon has a glow effect (`shadowBlur = 12`) for visibility.

#### 7.3.4  GOTO Crosshair

When a GOTO target is pending (`gotoTarget` is set):

- White circle radius 12, lineWidth 2.
- Horizontal and vertical crosshair lines.
- Coordinate label below.
- Cleared after 1.5 s via `setTimeout`.

#### 7.3.5  Flash Marker (waypoint add/remove feedback)

When `flashMarker` is set:

- Expanding ring animation over 600 ms.
- Colour: green for add, red for remove.
- Small solid dot at centre.

---

## 8  Mouse Interactions on the Map

All interactions use a helper `mouseToArena(event, canvas)` that converts mouse
pixel coordinates to arena coordinates (0–100), returning `{ax, ay, inBounds}`.

### 8.1  Waypoint Hit-Testing

`hitTestWaypoint(ax, ay)` iterates all robots' waypoints in `lastIntentData`.
Hit radius: **6 arena units**.  Returns `{rid, wpIndex, waypoints}` (waypoints
is a **deep copy** of the array) or `null`.

### 8.2  Nearest Route Detection

`findNearestRoute(ax, ay)` finds the closest path route segment across all
robots.  Returns `{rid, insertIndex, waypoints}` or `null`.

### 8.3  Interaction Table

| Gesture | Condition | Behaviour |
|---------|-----------|-----------|
| **Click** (mouseup) | Empty map, no waypoint hit | Sets GOTO target X/Y inputs + crosshair.  Delayed 250 ms to allow double-click detection.  Does **not** auto-send the GOTO command. |
| **Click** on waypoint | Normal click (no modifier) | Starts **drag**. |
| **Drag** (mousedown → mousemove → mouseup) | Started on a waypoint | Moves the waypoint in real time.  `lastIntentData` is frozen during drag.  On mouseup, sends `CMD_SET_PATH`. |
| **Double-click** | Empty map (no waypoint hit) | Adds a new waypoint to the **nearest** robot's path route.  Green flash. |
| **⌥+click** (Alt+click on a waypoint) | Waypoint hit with `e.altKey` | Removes the waypoint (min 2 enforced).  Red flash. |
| **⌥+click** on empty map | `e.altKey` without waypoint hit | Ignored. |
| **Hover** over waypoint | No modifier | Cursor: `grab` |
| **Hover** over waypoint | Alt held | Cursor: `not-allowed` |
| **Hover** over empty map | — | Cursor: `crosshair` |
| **Mouse leave** canvas | While dragging | Cancels the drag. |

### 8.4  Double-Click vs Single-Click Discrimination

A `dblClickPending` flag is used with a 250 ms timer.  Double-click sets
`dblClickPending = false` immediately, cancelling the pending GOTO.

---

## 9  SSE Data Flow

```
┌──────────────┐  gRPC server-streaming  ┌──────────────────┐
│  Robot Node   │ ─────────────────────► │ GrpcFleetClient   │
│  (port N)     │  Kin/Op/Intent/Telem  │   (Python)        │
└──────────────┘                        └────────┬─────────┘
                                                 │ store.update_*()
┌──────────────┐  gRPC StreamStatus     ┌────────┤
│  Charging Stn │ ─────────────────────►│  FleetStateStore      │
│  (port M)     │                       │  (transport-agnostic) │
└──────────────┘                        └────────┬──────────┘
                                                 │ snapshot() every 200ms
                                     ┌───────────▼──────────┐
                                     │  SSE: /stream         │
                                     │  data: {state,        │
                                     │    kinematic,          │
                                     │    operational,        │
                                     │    intent, telemetry,  │
                                     │    connections,        │
                                     │    port_names,         │
                                     │    charging}           │
                                     └───────────┬──────────┘
                                                 │ EventSource
                                     ┌───────────▼──────────┐
                                     │  Browser JS           │
                                     │  updates tables +     │
                                     │  redraws canvas       │
                                     └──────────────────────┘
```

The browser JS stashes:
- `lastIntentData` — not overwritten during drag.
- `lastChargingData` — for station icon rendering on map.
- `lastOperationalData` — cross-referenced by station icons and charging panel.

---

## 10  Command Flow (Browser → Robot)

```
Browser JS                       Flask /command              Robot gRPC
──────────                       ──────────────              ──────────
sendCommand(rid, cmd, extra)
  → POST /command {json}
                                 → client.send_command()     (or send_command_all)
                                   → store.resolve_address()
                                   → build CommandRequest
                                   → open grpc channel
                                   → stub.SendCommand()      → robot processes
                                   ← CommandResponse          ← response
                                 ← json {success, desc, …}
  ← update #cmd-result
```

For broadcast (`robot_id == "*"`): iterates all known robots, sends one RPC
per robot, collects results, returns summary with `grpc_calls_made` count.

---

## 11  Resilience Behaviour

| Scenario | Behaviour |
|----------|-----------|
| Robot disconnects | Connection marked `"disconnected"`.  State cleaned up.  Retries every 3 s. |
| Robot restarts with **different name** on same port | `_ensure_name()` detects name change → removes stale data, maps new name. |
| Stream exception | `stream_failed` event set → `_robot_loop` closes channel, reconnects. |
| Command to unknown robot | Returns `{success: false, description: "Unknown robot: …"}`. |
| Charging station disconnects | `_station_loop` catches exception → log, sleep 3 s, retry. |

---

## 12  File Structure

The application is a **single Python file** (`robot_ui.py`) that:

1. Imports `robot_pb2` and `robot_pb2_grpc` (generated from `robot.proto`).
2. Imports `FleetDiscovery` from `fleet_discovery.py`.
3. Defines `FleetStateStore` (transport-agnostic data store).
4. Defines `GrpcFleetClient` (gRPC communication layer).
5. Defines `DASHBOARD_HTML` as a raw string containing the full HTML/CSS/JS.
6. Defines `create_app(store, client)` returning a Flask app.
7. Defines `main()` parsing CLI args, creating the store + client, and starting
   everything.

No templates directory, no external JS/CSS.  One static asset (`field1_map.jpg`)
served by the `/ground_texture.jpg` route.

### External Dependencies

| Package | Use |
|---------|-----|
| `grpcio` / `grpcio-tools` / `protobuf` | RPC framework and code generation |
| `flask` | Web server and SSE endpoint |
| `zeroconf` | mDNS/DNS-SD peer discovery (discovery mode only) |
| `ifaddr` | LAN IP selection inside `fleet_discovery.py` |

---

## 13  Proto Contract Summary

The UI depends on the following from `robot.proto`:

### Services used (robot streams)

- `RobotStreaming.StreamKinematicState` (server-streaming: `SubscribeRequest` → stream of `KinematicState`)
- `RobotStreaming.StreamOperationalState` (server-streaming: `SubscribeRequest` → stream of `OperationalState`)
- `RobotStreaming.StreamIntent` (server-streaming: `SubscribeRequest` → stream of `IntentUpdate`)
- `RobotStreaming.StreamTelemetry` (server-streaming: `SubscribeRequest` → stream of `TelemetryUpdate`)
- `RobotCommand.SendCommand` (unary: `CommandRequest` → `CommandResponse`)
- `RobotCommand.RequestVideo` (server-streaming: `VideoRequest` → stream of `VideoFrame`)

### Services used (charging station)

- `ChargingStation.StreamStatus` (server-streaming: `StationStatusRequest` → stream of `StationStatusResponse`)

### Enums

- `RobotStatus`: `STATUS_UNKNOWN=0`, `STATUS_MOVING=1`, `STATUS_IDLE=2`, `STATUS_HOLDING=3`, `STATUS_CHARGING=4`
- `RobotIntent`: `INTENT_UNKNOWN=0`, `INTENT_FOLLOW_PATH=1`, `INTENT_GOTO=2`, `INTENT_IDLE=3`, `INTENT_CHARGE_QUEUE=4`, `INTENT_CHARGE_DOCK=5`
- `Command`: `COMMAND_UNKNOWN=0`, `CMD_STOP=1`, `CMD_FOLLOW_PATH=2`, `CMD_GOTO=3`,
  `CMD_RESUME=4`, `CMD_SET_PATH=5`, `CMD_CHARGE=6`

### Key messages

- `SubscribeRequest`: `subscriber_id` (string)
- `KinematicState`: `robot_id`, `position`, `velocity`, `heading`, `timestamp`
- `OperationalState`: `robot_id`, `status`, `battery_level`, `timestamp`
- `IntentUpdate`: `robot_id`, `intent_type`, `target_position`, `path_waypoints`, `path_waypoint_index`, `timestamp`
- `TelemetryUpdate`: `robot_id`, `cpu_usage`, `memory_usage`, `temperature`, `tcp_connections`, `active_delivery_loops`, `timestamp`
- `CommandRequest`: `robot_id`, `command`, `oneof params { GotoParameters, SetPathParameters }`
- `CommandResponse`: `success`, `description`, `resulting_status`, `resulting_intent`
- `StationStatusResponse`: `station_id`, `dock_x`, `dock_y`, `dock_robot`, `queue` (repeated `QueueEntry`)
- `QueueEntry`: `robot_id`, `rank`, `confirmed`

### Intent payload convention

The `IntentUpdate` message carries path information as structured protobuf
fields:

- `repeated Waypoint path_waypoints` — the robot's ordered path route
  (empty when not following a path).
- `int32 path_waypoint_index` — which waypoint the robot is navigating toward.

When intent is `INTENT_GOTO` and the robot has arrived, `target_position`
reports the hold position (not the cleared goal).
