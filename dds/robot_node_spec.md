# Robot Fleet Node — Specification (the DDS approach: Pure DDS)

> This document fully specifies the `robot_node.py` application so that an AI
> agent (or developer) can re-create it from scratch.  It assumes the reader has
> access to the XML type definitions (`robot_types.xml`), the auto-generated
> Python types (`robot_types.py`), and the QoS profiles (`robot_qos.xml`).

---

## 1  Purpose

A single robot node that participates in a **peer-to-peer DDS fleet**.  Every
node is a DDS DomainParticipant: it publishes its own state / intent /
telemetry, subscribes to all peers via multicast, handles operator commands via
an `rti.rpc.SimpleReplier`, and negotiates charging slots via
`rti.rpc.Requester` objects that fan out to all stations.

Each node autonomously follows a waypoint route, avoids collisions with nearby
peers, reacts to operator commands (STOP, FOLLOW_PATH, GOTO, RESUME, SET_PATH,
CHARGE), and manages an autonomous charging workflow when battery is low.

**Key difference from the gRPC approach:** there is no gRPC, no Zeroconf, no per-peer
connections, no delivery loops, no reconnect logic.  DDS provides discovery,
presence, fan-out, and QoS as first-class middleware features.

**RPC pattern:** Commands and slot negotiation use `rti.rpc.Requester` /
`rti.rpc.Replier` / `rti.rpc.SimpleReplier` objects — the DDS-native
request-reply API.  These handle correlation automatically (no manual
`request_id` matching), provide `send_request` / `receive_replies` semantics
analogous to gRPC unary RPCs, and support fan-out to multiple repliers.

---

## 2  Technology Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.14.3+ |
| Middleware | RTI Connext DDS 7.6.0+ (`rti.connextdds`, `rti.idl`, `rti.rpc`) |
| Type generation | `rtiddsgen -language python robot_types.xml` → `robot_types.py` |
| QoS configuration | `robot_qos.xml` — Pattern-based profiles (`BuiltinQosLib::Pattern.*`) |
| Concurrency | Python `threading` (daemon threads, `threading.Lock`) |
| CLI | `argparse` |
| Math | `math` (trig, sqrt) |
| Randomness | `random` (seeded per-robot for deterministic routes) |
| Serialisation | `json` (for CommandRequest parameters string) |
| Logging | `print()` with `[robot_id HH:MM:SS]` prefix via `_ts()` helper |

No `uuid` import — `rti.rpc` handles request-reply correlation internally.
No web framework, no gRPC, no Zeroconf, no external libraries beyond the RTI
Connext Python binding.

### Comparison with the gRPC approach

| Concern | the gRPC approach | the DDS approach |
|---------|-------------|-------------|
| RPC framework | gRPC (`grpcio`, `grpcio-tools`, `protobuf`) | None — DDS provides pub-sub + request-reply |
| Code generation | `protoc` → `robot_pb2.py` + `robot_pb2_grpc.py` | `rtiddsgen` → `robot_types.py` |
| Discovery | Zeroconf mDNS (`zeroconf`, `ifaddr`) | DDS SPDP — zero code |
| Thread pool | `concurrent.futures.ThreadPoolExecutor(max_workers=50)` | Not needed — DDS manages internal threads |
| Network stats | `psutil` (TCP socket counting) | Not applicable (no TCP connections) |

---

## 3  Command-Line Interface

```
python robot_node.py --id NAME [--domain DOMAIN_ID]
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--id` | str (required) | — | Robot ID (e.g. `tractor1`) |
| `--domain` | int | `0` | DDS Domain ID |

At startup `main()`:

1. Parses arguments.
2. Creates a `RobotNode(robot_id, domain_id)`.
3. Calls `run()` which initialises DDS, starts threads, and blocks.

**Comparison with the gRPC approach:** No `--fleet-port`, `--fleet-port-list`, or
`--same-host` flags — DDS discovery is automatic via SPDP multicast.  All
participants on the same domain ID find each other.

---

## 4  `RobotNode` Class — State & Attributes

### 4.1  Identity

| Attribute | Type | Description |
|-----------|------|-------------|
| `robot_id` | `str` | Robot name (from `--id`) |
| `domain_id` | `int` | DDS domain (from `--domain`) |
| `running` | `bool` | Master flag; `False` → all threads exit |

### 4.2  Motion State

| Attribute | Type | Initial Value | Description |
|-----------|------|---------------|-------------|
| `position` | `dict {x,y,z}` | Random (0–100, 0–100, 0) | Current position in the arena |
| `status` | `RobotStatus` enum | `STATUS_MOVING` | One of `STATUS_MOVING`, `STATUS_HOLDING`, `STATUS_IDLE`, `STATUS_CHARGING` |
| `intent` | `RobotIntentType` enum | `INTENT_FOLLOW_PATH` | One of `INTENT_FOLLOW_PATH`, `INTENT_GOTO`, `INTENT_IDLE`, `INTENT_CHARGE_QUEUE`, `INTENT_CHARGE_DOCK` |
| `battery_level` | `float` | `100.0` | Drains per tick; refills at `CHARGE_RATE` while docked |
| `goal` | `dict {x,y}` or `None` | `None` | GOTO / charge dock target |
| `hold_position` | `dict {x,y}` or `None` | `None` | Position to spring-back to while HOLDING |
| `speed` | `float` | `2.0` | Movement speed in arena units / second |
| `heading` | `float` | `0.0` | Last computed heading angle in radians (`atan2(vy, vx)`) |
| `velocity_x` | `float` | `0.0` | Last computed X velocity |
| `velocity_y` | `float` | `0.0` | Last computed Y velocity |
| `velocity_z` | `float` | `0.0` | Last computed Z velocity (always 0 in 2-D) |
| `signal_strength` | `float` | `95.0` | Simulated signal strength (weaker near arena edges) |

### 4.3  Path State

| Attribute | Type | Description |
|-----------|------|-------------|
| `path_waypoints` | `list[dict {x,y}]` | 5–8 waypoints in a roughly circular loop |
| `path_index` | `int` | Index of the waypoint currently being navigated toward |

**Property**: `path_target` → `self.path_waypoints[self.path_index]`.

### 4.4  Charging State

| Attribute | Type | Description |
|-----------|------|-------------|
| `_station_docks` | `dict[str, dict]` | Station dock positions learned from StationStatus topic — `station_id → {"x","y"}` |
| `_charge_slot_id` | `str` or `None` | Unique slot token from the station |
| `_charge_station_id` | `str` or `None` | Station name (e.g. "station1") |
| `_charge_dock` | `dict {x,y}` or `None` | Dock coordinates |
| `_charge_wait_pos` | `dict {x,y}` or `None` | Waiting position while queued |
| `_charge_queue_rank` | `int` | Queue position (-1 = not queued) |
| `_pre_charge_intent` | enum or `None` | Stashed intent before charging |
| `_pre_charge_status` | enum or `None` | Stashed status before charging |
| `_pre_charge_speed` | `float` | Stashed speed (default 2.0) |
| `_pre_charge_goal` | `dict {x,y}` or `None` | Stashed GOTO goal |
| `_pre_charge_hold` | `dict {x,y}` or `None` | Stashed hold position (for GOTO arrival) |
| `_charge_requested` | `bool` | Mutex flag — only one charge attempt at a time |
| `CHARGE_THRESHOLD` | `float` | `20.0` — auto-charge below this % |
| `CHARGE_FULL` | `float` | `95.0` — stop charging at this % |
| `CHARGE_RATE` | `float` | `2.0` — %/second while docked |

**Helper tuple**: `_CHARGE_INTENTS = (INTENT_CHARGE_QUEUE, INTENT_CHARGE_DOCK)` —
module-level constant used to test whether the robot is in any charge state.

### 4.5  Peer Tracking (from DDS readers, protected by `data_lock`)

| Dict | Key | Value |
|------|-----|-------|
| `other_robots_state` | robot\_id | `KinematicState` (DDS sample) |
| `other_robots_operational` | robot\_id | `OperationalState` (DDS sample) |
| `other_robots_intent` | robot\_id | `Intent` (DDS sample) |

`_nearby_peers`: `set` of peer robot IDs currently within `AVOIDANCE_RADIUS`.

**Comparison with the gRPC approach:** No `peers` dict, no `peer_connections`, no
`peer_streams`, no `peers_lock`.  DDS readers automatically receive data from
all matched writers — no connection management.

### 4.6  Change Tracking (for on-change publishing)

| Attribute | Type | Description |
|-----------|------|-------------|
| `_last_published_status` | `RobotStatus` or `None` | Last OperationalState status written |
| `_last_published_battery` | `float` or `None` | Last OperationalState battery (rounded to 1 decimal) |
| `_last_published_intent` | `RobotIntentType` or `None` | Last Intent type written |
| `_last_published_target` | `tuple` or `None` | Last Intent target (rounded to 1 decimal) |
| `_last_published_path_index` | `int` or `None` | Last Intent path index written |

### 4.7  DDS Entities

| Attribute | Type | Description |
|-----------|------|-------------|
| `participant` | `dds.DomainParticipant` or `None` | The single DDS participant for this robot |
| `writers` | `dict[str, dds.DataWriter]` | Pub-sub writers keyed by logical name (see §6) |
| `readers` | `dict[str, dds.DataReader]` | Pub-sub readers keyed by logical name (see §6) |
| `command_replier` | `rti.rpc.SimpleReplier` or `None` | Receives `CommandRequest`, dispatches to `_handle_command()`, sends `CommandResponse` |
| `slot_requester` | `rti.rpc.Requester` or `None` | Sends `SlotRequest`, receives `SlotOffer` (fan-out to all stations) |
| `confirm_requester` | `rti.rpc.Requester` or `None` | Sends `SlotConfirm`, receives `SlotAssignment` |
| `release_requester` | `rti.rpc.Requester` or `None` | Sends `SlotRelease`, receives `SlotReleaseAck` |

### 4.8  Locks

| Lock | Protects |
|------|----------|
| `data_lock` | `other_robots_state`, `other_robots_operational`, `other_robots_intent` |

**Comparison with the gRPC approach:** Only **one** lock.  The gRPC approach needs both
`peers_lock` (connection management) and `data_lock` (received data).  In DDS
there is no connection state to protect.

---

## 5  Path Route Generation

`_generate_path_route()` produces a deterministic set of waypoints seeded by
the robot's name so each robot gets a unique path pattern.

1. Derive a 32-bit seed: `hash(robot_id) & 0xFFFFFFFF`.
2. Create a private `random.Random(seed)`.
3. Pick a random "home" centre `(cx, cy)` ∈ `[20, 80]`.
4. Pick a random radius ∈ `[15, 30]`.
5. Choose `n` ∈ `[5, 8]` waypoints.
6. Place each waypoint at angle `2π·i/n` from the centre, plus jitter ∈ `[-5, 5]`.
7. Clamp each coordinate to `[5, 95]`.

The result is a roughly circular loop that keeps robots spread across the arena.

Identical algorithm to the gRPC approach — same function, same output for the same
robot ID.

---

## 6  DDS Initialisation (`initialize_dds`)

This method replaces the gRPC approach's `start_server()`, `register_peer()`, and
`_connect_to_peer()` — all three are collapsed into a single function.

### 6.1  QoS Provider

1. Resolve the path to `robot_qos.xml` relative to the script.
2. Create a `dds.QosProvider(qos_xml)`.
3. All writer/reader QoS is loaded from named profiles (e.g.
   `RobotQosLibrary::KinematicStateProfile`).

### 6.2  Participant and Infrastructure

1. Create a `dds.DomainParticipant(domain_id)`.
2. Create one `dds.Publisher` and one `dds.Subscriber`.

### 6.3  Topics (5 pub-sub topics)

| Topic Name | Type | Direction |
|------------|------|-----------|
| `KinematicState` | `KinematicState` | Write + Read |
| `OperationalState` | `OperationalState` | Write + Read |
| `Intent` | `Intent` | Write + Read |
| `Telemetry` | `Telemetry` | Write only |
| `VideoFrame` | `VideoFrame` | Write only |

Command and slot-negotiation topics are **not** created manually — the
`rti.rpc.Requester` / `rti.rpc.SimpleReplier` objects create their own
internal DataWriters and DataReaders automatically, derived from the
`service_name`.

### 6.4  Writers (5 pub-sub)

| Dict Key | Topic | QoS Profile |
|----------|-------|-------------|
| `kinematic` | `KinematicState` | `RobotQosLibrary::KinematicStateProfile` |
| `operational` | `OperationalState` | `RobotQosLibrary::OperationalStateProfile` |
| `intent` | `Intent` | `RobotQosLibrary::IntentProfile` |
| `telemetry` | `Telemetry` | `RobotQosLibrary::TelemetryProfile` |
| `video` | `VideoFrame` | `RobotQosLibrary::VideoProfile` |

### 6.5  Readers (3 pub-sub)

| Dict Key | Topic | QoS Profile |
|----------|-------|-------------|
| `kinematic` | `KinematicState` | `RobotQosLibrary::KinematicStateProfile` |
| `operational` | `OperationalState` | `RobotQosLibrary::OperationalStateProfile` |
| `intent` | `Intent` | `RobotQosLibrary::IntentProfile` |

### 6.6  RPC Objects — Command Handling

The robot acts as a **server** for commands (console/UI sends a
`CommandRequest`, robot returns a `CommandResponse`).

```python
self.command_replier = rti.rpc.SimpleReplier(
    request_type=CommandRequest,
    reply_type=CommandResponse,
    participant=self.participant,
    service_name="RobotCommand",
    handler=self._handle_command,
)
```

`SimpleReplier` creates internal topics (named from `service_name`), an
internal DataReader for `CommandRequest`, and an internal DataWriter for
`CommandResponse`.  It calls `handler(request_data)` on each incoming request
and writes the returned `CommandResponse` back, automatically correlating the
reply to the request via `SampleIdentity`.

**Filtering**: `_handle_command` checks `request.robot_id == self.robot_id` to
ignore commands addressed to other robots.  (All robots share the same
`service_name`, so every robot's `SimpleReplier` receives every command — but
only the addressed robot processes it and returns a meaningful response.)

### 6.7  RPC Objects — Slot Negotiation

The robot acts as a **client** for slot negotiation (sends requests to charging
stations, receives replies from one or more station repliers).

Three `Requester` objects, one per request-reply pair:

```python
self.slot_requester = rti.rpc.Requester(
    request_type=SlotRequest,
    reply_type=SlotOffer,
    participant=self.participant,
    service_name="ChargingSlotRequest",
)

self.confirm_requester = rti.rpc.Requester(
    request_type=SlotConfirm,
    reply_type=SlotAssignment,
    participant=self.participant,
    service_name="ChargingSlotConfirm",
)

self.release_requester = rti.rpc.Requester(
    request_type=SlotRelease,
    reply_type=SlotReleaseAck,
    participant=self.participant,
    service_name="ChargingSlotRelease",
)
```

Each `Requester` creates an internal DataWriter (for sending) and an internal
DataReader (for receiving replies).  The DDS middleware manages topic names and
correlation internally.

**Fan-out**: `slot_requester.send_request(SlotRequest(...))` reaches **all**
matched `Replier` objects (i.e. all running charging stations).  The robot then
calls `slot_requester.receive_replies(max_wait)` to collect `SlotOffer`s from
every station and choose the best offer.

### 6.8  Pub-Sub Listeners

After creating writers, readers, and RPC objects, call `_setup_listeners()` (see §7).

**Comparison with the gRPC approach:** The gRPC approach needs `start_server()` to create a
gRPC server + servicers, then `register_peer()` + `_connect_to_peer()` per
peer (with reconnect loops).  Here it's one function: create participant →
create topics → create writers/readers → create RPC objects → attach listeners.
Done.

---

## 7  DDS Listeners (Pub-Sub Data Reception)

`_setup_listeners()` attaches `dds.DataReaderListener` subclasses to the
**pub-sub** readers (kinematic, operational, intent).  These callbacks fire on
the DDS receive thread whenever new data arrives from any matched writer in the
domain.

Command reception is handled by the `SimpleReplier` (§6.6), not by a manual
listener.  Slot negotiation replies are read explicitly by the `Requester`
objects (§6.7) during the charging workflow — no listener needed.

**Comparison with the gRPC approach:** Replaces the four `_start_*_stream()` methods
(§8.1–8.4 in the gRPC approach spec) plus the per-peer reconnect loop.  Zero
application threads for data reception.

### 7.1  `KinematicListener`

Attached to `readers['kinematic']` with
`StatusMask.DATA_AVAILABLE | StatusMask.LIVELINESS_CHANGED`.

#### `on_data_available(reader)`

1. `reader.take()` — returns all queued samples.
2. For each valid sample: if `data.robot_id != self.robot_id`, store in
   `other_robots_state[data.robot_id]` under `data_lock`.

#### `on_liveliness_changed(reader, status)`

Replaces the gRPC approach's manual heartbeat/timestamp polling:

- `alive_count_change < 0` → `"⚠  Robot DEAD (alive=N)"`
- `alive_count_change > 0` → `"✓  Robot ALIVE (alive=N)"`

Detection latency: **≤ 100 ms** (from QoS liveliness lease duration).
Compare with the gRPC approach: 3–10 seconds via TCP keepalive.

### 7.2  `OperationalListener`

Attached to `readers['operational']` with `StatusMask.DATA_AVAILABLE`.

- `on_data_available`: take samples, store non-self in
  `other_robots_operational` under `data_lock`.

### 7.3  `IntentListener`

Attached to `readers['intent']` with `StatusMask.DATA_AVAILABLE`.

- `on_data_available`: take samples, store non-self in
  `other_robots_intent` under `data_lock`.

### 7.4  Command Reception (via SimpleReplier — no listener)

Command reception is **not** handled by a manual DDS listener.  The
`SimpleReplier` created in §6.6 internally listens for `CommandRequest`
samples and invokes `self._handle_command(request_data)` for each one.  The
return value of the handler is automatically written back as the correlated
`CommandResponse`.

This is analogous to how gRPC calls a servicer method — the framework handles
receive, dispatch, and reply; the application only implements the handler.

---

## 8  Command Handling (`_handle_command`)

Called by the `SimpleReplier` for each incoming `CommandRequest`.  The handler
**returns** a `CommandResponse` — the `SimpleReplier` writes it back
automatically with correct correlation.

**Filtering**: If `cmd.robot_id != self.robot_id`, return a no-op
`CommandResponse(success=True, description="not addressed to me")` — the
command is for a different robot.

If the robot is currently in a charging state (`_CHARGE_INTENTS`) and receives
GOTO, FOLLOW_PATH, or STOP, the charge is **aborted** via `_abort_charge()` before
processing the new command.

| Command | Effect |
|---------|--------|
| `CMD_STOP` | `status → STATUS_IDLE`, `hold_position → None`, velocity zeroed.  Intent unchanged. |
| `CMD_FOLLOW_PATH` | `goal → None`, `hold_position → None`, `intent → INTENT_FOLLOW_PATH`, `status → STATUS_MOVING`.  Apply speed from params if provided. |
| `CMD_GOTO` | Read `{x, y, speed}` from JSON `parameters`.  `goal → {x, y}`, `hold_position → None`, `intent → INTENT_GOTO`, `status → STATUS_MOVING`. |
| `CMD_RESUME` | See §8.1 below. |
| `CMD_SET_PATH` | Read waypoints from JSON `parameters`.  Rejects if < 2 waypoints.  Replaces `path_waypoints`, clamps `path_index`.  Does **not** change status or intent. |
| `CMD_CHARGE` | Initiates charging workflow (see §13).  Rejected if already charging or charge already in progress. |
| Unknown | Returns `success=False`. |

### 8.1  RESUME Logic Detail

| Current Intent | Goal State | Behaviour |
|----------------|-----------|-----------|
| `INTENT_FOLLOW_PATH` | any | Set `STATUS_MOVING` — resume path route |
| `INTENT_GOTO` | goal ≠ None | Set `STATUS_MOVING` — resume navigation to GOTO target |
| `INTENT_GOTO` | goal = None, hold_position set | Re-navigate to hold position (issue new GOTO) |
| `INTENT_GOTO` | goal = None, no hold | Set `STATUS_HOLDING` at current position |
| Other (e.g. IDLE) | — | Set `STATUS_HOLDING` at current position (avoidance active) |

If params include `speed > 0`, the speed is updated for all cases.

### 8.2  Parameter Parsing Helpers

- `_parse_params(params_str)` — JSON-decode the string, return `{}` on failure.
- `_apply_speed_from_params(params_str)` — extract and apply `speed` field if > 0.

**Comparison with the gRPC approach:** The gRPC approach uses `CommandRequest.goto_params` and
`set_path_params` (protobuf `oneof` fields).  The DDS approach uses a single JSON
string field `parameters` in `CommandRequest`.  The XML also defines typed
`GotoParameters` and `SetPathParameters` structs (available for future use),
but the current implementation uses the JSON string for simplicity.

---

## 9  Response Publishing

`_handle_command` **returns** a `CommandResponse`:

```python
return CommandResponse(
    robot_id=self.robot_id,
    success=success,
    description=description,
    resulting_status=self.status,
    resulting_intent=self.intent,
)
```

The `SimpleReplier` automatically writes this reply on its internal
DataWriter, correlating it to the original request via `SampleIdentity`.
Correlation uses the DDS writer GUID + sequence number in the sample
metadata — no application-level `request_id` field is needed.

**Comparison with the gRPC approach:** The gRPC approach returns the response as the gRPC
unary return value.  The DDS approach returns it from the `SimpleReplier` handler —
same one-request-one-response semantics, with DDS handling delivery.

---

## 10  Movement Model (`update_position`)

Runs in a daemon thread.  Each tick `dt = 0.1 s` (10 Hz).

### 10.1  Four-State Behaviour

#### `STATUS_MOVING`

Compute a desired velocity `(vx, vy)` toward the current goal:

- **`INTENT_GOTO` / `INTENT_CHARGE_*`** (goal ≠ None): steer toward `goal`.
  - If within `speed × dt` of goal → **arrive**: snap to goal.
    - **`INTENT_CHARGE_DOCK`**: set `status = STATUS_CHARGING`, clear goal.
    - **`INTENT_CHARGE_QUEUE`**: set `status = STATUS_HOLDING`, set
      `hold_position` to arrival point, clear goal.
    - **`INTENT_GOTO`** (plain): set `status = STATUS_HOLDING`, set
      `hold_position` to arrival point, clear goal.
    - On arrival, preserve heading from approach direction.
    - Zero velocity after arrival.
  - Otherwise → `(vx, vy)` = unit direction × speed.

- **`INTENT_FOLLOW_PATH`**: steer toward `path_target`.
  - If within `speed × dt` → snap, advance `path_index` (wrapping).
    **Path look-ahead**: immediately compute the direction toward the
    *next* waypoint and set `(vx, vy)` accordingly.
  - Otherwise → `(vx, vy)` = unit direction × speed.

After computing goal velocity, blend in collision avoidance (§11):

- If `hard_override` → replace `(vx, vy)` entirely with avoidance vector.
- Otherwise → add avoidance to goal velocity.
- Apply: `position += (vx, vy) × dt`.

**Velocity / heading update** (while `STATUS_MOVING`):

- Write `velocity_x = vx`, `velocity_y = vy`.
- If magnitude > 0.01: compute `heading = math.atan2(vy, vx)`.

#### `STATUS_HOLDING`

No goal velocity.  A gentle **spring** pulls the robot back toward
`hold_position`:

- If distance > 0.05: `spring_speed = min(0.5, dist_to_hold)` → velocity
  toward hold point.

Avoidance is still active (same blend logic: hard\_override replaces spring,
otherwise adds to it).  Position updated by `(vx + avoid) × dt`.

**Velocity update**: write `velocity_x` / `velocity_y`; update heading only
when magnitude > 0.01.

#### `STATUS_IDLE`

No movement at all.  No avoidance.  The robot is completely parked.
Velocity is zeroed on CMD_STOP.

#### `STATUS_CHARGING`

No movement.  No avoidance.  Battery refills at `CHARGE_RATE` %/s (2.0).
Velocity zeroed.  When `battery_level >= CHARGE_FULL` (95%), the robot calls
`_release_charge_slot()` to finish charging and resume its prior task.

### 10.2  Post-Move Housekeeping (every tick)

1. **Clamp** position to arena bounds `[0, 100]` on both axes.
2. **Battery drain** (when not charging):
   `drain = 0.01 + 0.01 × actual_speed` (clamped ≥ 0).
3. **Signal strength**: weaker near arena edges, stronger near centre.
   `base_signal = max(10, 100 - edge_dist × 1.6)` + random jitter ±3.
4. **Auto-charge trigger**: If `battery_level < CHARGE_THRESHOLD` (20%) and
   not already charging and not already requested, set `_charge_requested = True`
   and spawn a thread to call `_initiate_charge()`.

---

## 11  Collision Avoidance

### Constants

| Name | Value | Description |
|------|-------|-------------|
| `AVOIDANCE_RADIUS` | 12.0 | Begin steering when a peer is within this distance |
| `AVOIDANCE_STRENGTH` | 4.0 | Base repulsion speed at the radius boundary |
| `MIN_SEPARATION` | 4.0 | Hard floor — override goal velocity entirely below this |

### `_is_docking` Property

Returns `True` if `intent == INTENT_CHARGE_DOCK` and `goal is not None`.

### `_compute_avoidance()` → `(ax, ay, hard_override)`

1. Snapshot all peer positions from `other_robots_state` (under `data_lock`).
2. Determine effective radius/strength based on charging proximity:
   - **Docking** (`_is_docking`): `eff_radius = AVOIDANCE_RADIUS × 0.4`,
     `eff_strength = AVOIDANCE_STRENGTH × 0.3`.
   - **Near station** (in `_CHARGE_INTENTS` with `_charge_dock` set, not
     docking): `eff_radius × 0.5`, `eff_strength × 0.25`.
   - **Normal**: full radius and strength.
3. Identify docking peers (when near station but not docking): check
   `other_robots_intent` for peers with `INTENT_CHARGE_DOCK` — these get
   3× avoidance strength (yield to docking robots).
4. For each peer within `eff_radius`:
   - **Inverse-square** repulsion:
     ```
     ratio = eff_radius / dist
     strength = eff_strength × ratio²
     ```
   - If peer is a docking peer: `strength × 3.0`.
   - Accumulate repulsion vector.
   - If `dist < MIN_SEPARATION` and not near station / not docking →
     set `hard_override = True`.
5. **Proximity logging**:
   - Peer enters radius → `⚠ Peer {rid} nearby ({dist:.1f} units) — avoiding`
   - Peer exits radius → `✓ Peer {rid} clear ({dist:.1f} units)`
   - Tracked via `_nearby_peers` set.

### Caller Behaviour

- `hard_override = True` → **discard** goal/spring velocity, use pure repulsion.
- `hard_override = False` → **add** avoidance to goal/spring velocity.

Same algorithm as the gRPC approach, with the addition of docking priority logic.

---

## 12  Publishing Threads

### 12.1  `publish_kinematic_state` (10 Hz)

Constructs a `KinematicState` sample:

| Field | Source |
|-------|--------|
| `robot_id` | `self.robot_id` |
| `position` | `Position(x, y, z)` from `self.position` |
| `velocity` | `Velocity3(x, y, z)` from `self.velocity_*` |
| `heading` | `self.heading` |

Writes via `writers['kinematic'].write(sample)`.  Sleeps 0.1 s.

**No timestamp field.** DDS `SampleInfo.source_timestamp` provides nanosecond-
precision timing automatically.

**Comparison with the gRPC approach:** One `writer.write()` call replaces N per-subscriber
`StreamKinematicState` delivery loops.

### 12.2  `publish_operational_state` (on change, 10 Hz check)

Checks at 10 Hz whether `status` or `battery_level` (rounded to 1 decimal) has
changed since last publish.  Only writes when something changed.

| Field | Source |
|-------|--------|
| `robot_id` | `self.robot_id` |
| `status` | `self.status` (RobotStatus enum) |
| `battery_level` | `self.battery_level` |

**Comparison with the gRPC approach:** The gRPC approach publishes at a fixed 1 Hz regardless
of change.  The DDS approach publishes on-change (more efficient), though the QoS
profile (`Pattern.Status` with `TRANSIENT_LOCAL`) ensures late joiners always
get the last value.

### 12.3  `publish_intent` (1 Hz)

Publishes the current intent every 1 second.  Target position depends on intent:

| Intent | `target_x`, `target_y` | `path_waypoints` | `path_index` |
|--------|------------------------|-------------------|--------------|
| `INTENT_GOTO` (en route) | `goal.x, goal.y` | empty | 0 |
| `INTENT_GOTO` (arrived) | `hold_position.x, hold_position.y` | empty | 0 |
| `INTENT_GOTO` (no goal or hold) | current position | empty | 0 |
| `INTENT_CHARGE_*` | dock or goal or current position | empty | 0 |
| `INTENT_FOLLOW_PATH` | current `path_target` | full waypoint list as `[Waypoint]` | `path_index` |
| Other | current position | empty | 0 |

### 12.4  `publish_telemetry` (1 Hz)

Publishes random simulated readings:

| Field | Range |
|-------|-------|
| `cpu_usage` | 20–80 % |
| `memory_usage` | 30–70 % |
| `temperature` | 40–70 °C |
| `signal_strength` | `self.signal_strength` (simulated from arena position) |

**Comparison with the gRPC approach:** No `tcp_connections` or `active_delivery_loops`
fields — those are gRPC-specific artifacts that don't exist in DDS.

---

## 13  Charging Workflow

Charging is triggered in two ways:

1. **Operator command**: `CMD_CHARGE` — spawns a thread to call `_initiate_charge()`.
2. **Auto-charge**: When `battery_level < CHARGE_THRESHOLD` (20%) during
   `update_position`, and no charge is already in progress.

In both cases, `_charge_requested` is set to `True` as a simple mutex so only
one charge attempt can be in flight at a time.

### 13.1  `_initiate_charge()` Flow

1. Check if already in a charge state (`_CHARGE_INTENTS`).  If so, reset
   `_charge_requested` and return.
2. **Stash** current state: intent, status, speed, goal, hold_position.
3. **Send SlotRequest** via the `Requester`:
   ```python
   request_id = self.slot_requester.send_request(
       SlotRequest(
           robot_id=self.robot_id,
           battery_level=self.battery_level,
           info_only=False,
       )
   )
   ```
   `send_request()` returns a `SampleIdentity` used to correlate replies.
4. **Collect offers** from all stations (fan-out):
   ```python
   self.slot_requester.wait_for_replies(
       max_wait=Duration.from_seconds(5),
       min_count=1,
       related_request_id=request_id,
   )
   replies = self.slot_requester.take_replies(related_request_id=request_id)
   ```
   `replies` is a list of `(data, info)` tuples — one from each responding
   station.  Multiple stations may respond.
5. If no replies → log, reset `_charge_requested`, return.
6. **Select best offer**: choose the `SlotOffer` with lowest `queue_rank`
   (tie-break: lowest `wait_time_s`).
7. **Confirm** the chosen slot via `confirm_requester`:
   ```python
   confirm_id = self.confirm_requester.send_request(
       SlotConfirm(robot_id=self.robot_id, slot_id=best.slot_id)
   )
   ```
8. **Wait** for `SlotAssignment`:
   ```python
   self.confirm_requester.wait_for_replies(
       max_wait=Duration.from_seconds(5),
       min_count=1,
       related_request_id=confirm_id,
   )
   assignments = self.confirm_requester.take_replies(related_request_id=confirm_id)
   ```
9. If not granted → log, reset `_charge_requested`, return.
10. Store charge state (`_charge_slot_id`, `_charge_dock`, `_charge_wait_pos`, etc.).
11. **Navigate**:
    - If `queue_rank == 0` → goal = dock, intent = `INTENT_CHARGE_DOCK`.
    - Otherwise → goal = wait position, intent = `INTENT_CHARGE_QUEUE`.
12. Set `status = STATUS_MOVING`, `speed = 3.0` (move briskly to station).
13. If `INTENT_CHARGE_QUEUE` → start `_poll_charge_queue()` thread immediately.

**Comparison with the gRPC approach:** The gRPC approach uses gRPC unary RPCs (`RequestSlot`,
`ConfirmSlot`, `ReleaseSlot`) to specific station addresses discovered via
Zeroconf.  The DDS approach uses `rti.rpc.Requester` objects — same request-reply
semantics, but the `send_request()` fans out to **all** stations automatically
(no station addresses, no channel management).  The robot collects all offers
and picks the best — something that would require N separate gRPC calls in
the gRPC approach.

### 13.2  Requester Reply Collection

Replaces the old `_wait_for_sample()` polling loop.  `rti.rpc.Requester`
provides blocking `wait_for_replies()` and `take_replies()` methods that:

- **Block** until at least `min_count` replies arrive or `max_wait` expires.
- **Correlate** replies automatically via `SampleIdentity` (writer GUID +
  sequence number) — no application-level `request_id` field needed.
- **Fan-out**: a single `send_request()` reaches all matched `Replier`s;
  `take_replies()` returns all responses.

This eliminates the 50 ms polling sleep, reduces latency, and simplifies the
code significantly.

### 13.3  `_poll_charge_queue()` — Background Rank Poller

Runs in a daemon thread while the robot is in the charge workflow.  Every 3 s:

1. Guard: exit if intent is no longer in `_CHARGE_INTENTS`, or if
   `_charge_slot_id` has been cleared.
2. Re-send `SlotConfirm` (idempotent) via `confirm_requester.send_request()`
   to get updated rank.
3. Collect `SlotAssignment` replies via `confirm_requester.wait_for_replies()`
   + `confirm_requester.take_replies()` (5 s timeout).
4. If no replies → skip this cycle, try again in 3 s.
5. If `granted == False` → slot lost; restore pre-charge state, exit thread.
6. If `queue_rank == 0` → navigate to dock, set `INTENT_CHARGE_DOCK`, exit thread.
7. If rank changed → navigate to updated wait position.

### 13.4  Dock Arrival and Charging

When the robot arrives at the dock (goal reached with `INTENT_CHARGE_DOCK`):

1. Set `status = STATUS_CHARGING`.
2. The `update_position` tick loop refills battery at `CHARGE_RATE` %/s.
3. When `battery_level >= CHARGE_FULL` (95%) → call `_release_charge_slot()`.

### 13.5  `_release_charge_slot()` — Post-Charge Resume

1. Send `SlotRelease` via `release_requester.send_request()` to free the dock.
2. Optionally collect `SlotReleaseAck` via `release_requester.wait_for_replies()` +
   `release_requester.take_replies()` (best-effort, short timeout).
3. **Restore pre-charge state**:
   - If the stashed state was `INTENT_GOTO` + arrived (goal=None, hold set):
     re-issue GOTO to the original hold position so the robot navigates back.
   - Otherwise: restore intent, status, speed, goal directly.
4. Call `_clear_charge_state()`.

### 13.6  `_abort_charge()` — Operator Override

Called when GOTO / FOLLOW_PATH / STOP overrides an in-progress charge:

1. Send `SlotRelease` via `release_requester.send_request()` (best-effort —
   don't wait for ack).
2. Call `_clear_charge_state()`.
3. Does **not** restore intent/status — the caller sets those for the new command.

### 13.7  `_clear_charge_state()` — Reset Helper

Resets all charge-related fields to defaults:
`_charge_slot_id`, `_charge_station_id`, `_charge_dock`, `_charge_wait_pos`,
`_charge_queue_rank`, `_charge_requested`, and all `_pre_charge_*` fields.

---

## 14  Status Reporting (`print_status`)

Runs in a daemon thread, prints every **5 seconds**.

Format:
```
[robot_id HH:MM:SS] pos=(x,y) STATUS/INTENT batt=N% sig=N% peers=P
```

| Token | Meaning |
|-------|---------|
| `pos=(x,y)` | Current position (1 decimal) |
| `STATUS/INTENT` | Enum name strings (e.g. `STATUS_MOVING/INTENT_FOLLOW_PATH`) |
| `batt=N%` | Battery percentage (0 decimal) |
| `sig=N%` | Signal strength (0 decimal) |
| `peers=P` | Count of peer robot IDs in `other_robots_state` |

**Comparison with the gRPC approach:** No `peers=C/T` (connected/total) or `streams=S`
— there are no connections or streams to count.  Instead, `peers=P` is just the
number of discovered peers (peer count from the DDS reader cache).  `sig=N%`
replaces the gRPC approach's stream count.

---

## 15  Main Run Loop

`run()`:

1. Calls `initialize_dds()`.
2. Sets `self.running = True`.
3. Spawns six daemon threads:
   - `update_position` — movement / avoidance / charging tick loop (10 Hz).
   - `publish_kinematic_state` — writes KinematicState (10 Hz).
   - `publish_operational_state` — writes OperationalState (on change).
   - `publish_intent` — writes Intent (1 Hz).
   - `publish_telemetry` — writes Telemetry (1 Hz).
   - `print_status` — periodic log line (every 5 s).
4. Logs: `"Robot is running (6 app threads + DDS internal threads)."`.
5. Blocks on `while self.running: sleep(1)`.
6. On `KeyboardInterrupt` → sets `running = False`, joins threads (1 s timeout),
   calls `participant.close()`.

---

## 16  Resilience Behaviour

| Scenario | Behaviour |
|----------|-----------|
| Peer not yet started | DDS discovery finds it automatically when it joins.  No explicit wait, no retry loop, no timeout. |
| Peer crashes mid-session | DDS liveliness fires `on_liveliness_changed` within 100 ms.  No channel monitoring, no reconnect. |
| Peer rejoins after crash | DDS re-discovers automatically via SPDP.  `TRANSIENT_LOCAL` topics deliver the last value immediately. |
| Network blip | DDS internally buffers reliable samples and retransmits.  Best-effort samples lost (harmless — next arrives in 100 ms). |
| Node shutdown (`Ctrl-C`) | `running = False` → all daemon threads exit.  `participant.close()` cleanly disposes DDS entities. |
| Charging station unreachable | `Requester.wait_for_replies()` times out after 5 s.  Robot stays on current task. |
| Slot lost during polling | `_poll_charge_queue` detects `granted == False` → restores prior state. |
| IP address change | DDS SPDP re-announces with new address.  Peers re-match automatically. |

**Comparison with the gRPC approach:** the gRPC approach's §16 (Resilience) needs explicit code
for every scenario (reconnect loops, channel monitoring, connectivity callbacks,
retry timers).  In DDS every row above is handled by the middleware — zero
application code for resilience.

---

## 17  Thread Summary

| Thread | Daemon | Description | Created by |
|--------|--------|-------------|------------|
| Main | No | `run()` → blocks on `sleep(1)` loop | `main()` |
| `update_position` | Yes | Movement tick at 10 Hz | `run()` |
| `publish_kinematic_state` | Yes | Writes KinematicState at 10 Hz | `run()` |
| `publish_operational_state` | Yes | Writes OperationalState on change | `run()` |
| `publish_intent` | Yes | Writes Intent at 1 Hz | `run()` |
| `publish_telemetry` | Yes | Writes Telemetry at 1 Hz | `run()` |
| `print_status` | Yes | Log line every 5 s | `run()` |
| Charge initiator | Yes | `_initiate_charge` (one-shot) | CMD_CHARGE or auto-trigger |
| Charge queue poller | Yes | `_poll_charge_queue` (long-running) | `_initiate_charge()` |
| SimpleReplier internal | — | Dispatches `CommandRequest` → `_handle_command()` | `SimpleReplier()` |
| DDS internal threads | — | Receive, event, database (~3 threads, managed by middleware) | `DomainParticipant()` |

**Total: ~9 threads** (6 app + ~3 DDS) + up to 2 charging threads when a charge
cycle is active.  This count is **fixed regardless of fleet size**.

**Comparison with the gRPC approach:** a 5-robot fleet needs ~23 threads per node.  A
100-robot fleet would need ~400+ threads per node (4 reader threads × N peers +
connector threads + server pool).  In DDS it's still ~9.

---

## 18  Type Contract Summary

The node depends on the following types from `robot_types.py` (generated by
`rtiddsgen -language python robot_types.xml`):

### Enums

- `RobotStatus`: `STATUS_UNKNOWN=0`, `STATUS_MOVING=1`, `STATUS_IDLE=2`, `STATUS_HOLDING=3`, `STATUS_CHARGING=4`
- `RobotIntentType`: `INTENT_UNKNOWN=0`, `INTENT_FOLLOW_PATH=1`, `INTENT_GOTO=2`, `INTENT_IDLE=3`, `INTENT_CHARGE_QUEUE=4`, `INTENT_CHARGE_DOCK=5`
- `Command`: `COMMAND_UNKNOWN=0`, `CMD_STOP=1`, `CMD_FOLLOW_PATH=2`, `CMD_GOTO=3`, `CMD_RESUME=4`, `CMD_SET_PATH=5`, `CMD_CHARGE=6`

### Types Used by robot_node.py

| Type | Key | Fields | Used for |
|------|-----|--------|----------|
| `Position` | — | `x, y, z` | Nested in KinematicState |
| `Velocity3` | — | `x, y, z` | Nested in KinematicState |
| `Waypoint` | — | `x, y` | Nested in Intent.path_waypoints |
| `KinematicState` | `robot_id` | `position, velocity, heading` | 10 Hz publish + peer read |
| `OperationalState` | `robot_id` | `status, battery_level` | On-change publish + peer read |
| `Intent` | `robot_id` | `intent_type, target_x, target_y, path_waypoints[], path_index` | 1 Hz publish + peer read |
| `Telemetry` | `robot_id` | `cpu_usage, memory_usage, temperature, signal_strength` | 1 Hz publish |
| `VideoFrame` | `robot_id` | `frame_data (octet sequence)` | Write only (future: ~5 fps JPEG) |
| `CommandRequest` | — | `robot_id, command, parameters (JSON string)` | Received by `SimpleReplier` handler |
| `CommandResponse` | — | `robot_id, success, description, resulting_status, resulting_intent` | Returned from `SimpleReplier` handler |
| `SlotRequest` | — | `robot_id, battery_level, info_only` | Sent via `slot_requester.send_request()` |
| `SlotOffer` | — | `station_id, slot_id, queue_rank, wait_time_s, dock_x, dock_y` | Received via `slot_requester.take_replies()` |
| `SlotConfirm` | — | `robot_id, slot_id` | Sent via `confirm_requester.send_request()` |
| `SlotAssignment` | — | `granted, queue_rank, wait_x, wait_y, dock_x, dock_y` | Received via `confirm_requester.take_replies()` |
| `SlotRelease` | — | `robot_id, slot_id` | Sent via `release_requester.send_request()` |
| `SlotReleaseAck` | — | `success` | Received via `release_requester.take_replies()` |

### Types Defined But Not Yet Used by robot_node.py

| Type | Purpose |
|------|---------|
| `GotoParameters` | Typed alternative to JSON-encoded GOTO params (future use) |
| `SetPathParameters` | Typed alternative to JSON-encoded waypoint list (future use) |
| `QueueEntry` | Nested in StationStatus — used by `charging_station.py` |
| `StationStatus` | Published by `charging_station.py`, read by `robot_ui.py` |

### Comparison with the gRPC approach proto contract

| Aspect | the gRPC approach (protobuf) | the DDS approach (DDS IDL-XML) |
|--------|------------------------|---------------------------|
| Source file | `robot.proto` | `robot_types.xml` |
| Generated files | `robot_pb2.py` + `robot_pb2_grpc.py` | `robot_types.py` |
| Generator tool | `protoc` | `rtiddsgen` |
| Service definitions | 2 gRPC services (6 RPCs) | Not needed — `rti.rpc` Requester/Replier + pub-sub topics |
| RPC pattern | gRPC stub → servicer (1:1 channel) | `Requester.send_request()` → `Replier`/`SimpleReplier` (1:N fan-out) |
| Timestamp fields | `int64 timestamp` in every message | None — DDS SampleInfo provides automatically |
| Command params | protobuf `oneof { GotoParameters, SetPathParameters }` | JSON string field (typed structs available but unused) |
| Correlation | gRPC manages per-call (implicit) | `rti.rpc` manages via `SampleIdentity` (implicit) |

---

## 19  File Structure

The application is a **single Python file** (`robot_node.py`) that:

1. Imports DDS types from `robot_types.py` (generated — never hand-edited).
2. Defines `_ts()` timestamp helper and `_CHARGE_INTENTS` tuple at module level.
3. Defines `RobotNode`:
   - State attributes (§4)
   - `_generate_path_route()` (§5)
   - `initialize_dds()` (§6) — creates participant, topics, writers, readers,
     `SimpleReplier` (commands), and `Requester` objects (slot negotiation)
   - `_setup_listeners()` (§7) — attaches DDS callbacks to pub-sub readers
   - `_handle_command()` (§8) — processes commands, returns `CommandResponse`
     (called by `SimpleReplier`)
   - `_initiate_charge()`, `_poll_charge_queue()`,
     `_abort_charge()`, `_release_charge_slot()`, `_clear_charge_state()` (§13)
   - `_compute_avoidance()` (§11) — collision avoidance
   - `update_position()` (§10) — movement tick loop
   - `publish_kinematic_state()`, `publish_operational_state()`,
     `publish_intent()`, `publish_telemetry()` (§12)
   - `print_status()` (§14)
   - `run()` (§15) — main loop
4. Defines `main()` — CLI parsing, creates `RobotNode`, calls `run()`.

**No servicer classes.** The gRPC approach needs `RobotCommandServicer` and
`RobotStreamingServicer` to handle incoming gRPC RPCs.  In DDS, incoming data
is handled by listener callbacks attached in `_setup_listeners()` — the
`RobotNode` class is self-contained.

### Companion files

| File | Purpose |
|------|---------|
| `robot_types.xml` | DDS IDL-XML type definitions — single source of truth |
| `robot_types.py` | Auto-generated Python types (`rtiddsgen -language python robot_types.xml`) |
| `robot_qos.xml` | QoS profiles — Pattern-based (`BuiltinQosLib::Pattern.*`) |
| `charging_station.py` | Standalone charging station process (future) |
| `command_console.py` | Interactive CLI for sending commands |

### External Dependencies

| Package | Use |
|---------|-----|
| `rti.connextdds` | DDS middleware — participant, topics, writers, readers, listeners |
| `rti.rpc` | Request-reply — `SimpleReplier` (commands), `Requester` (slot negotiation) |
| `rti.idl` | DDS type system — `@idl.struct`, `@idl.enum`, `idl.key`, etc. |

That's it.  No `grpcio`, no `zeroconf`, no `ifaddr`, no `psutil`, no `pybullet`
(video rendering not yet integrated), no `flask`, no `numpy`, no `Pillow`.

### What Doesn't Exist (vs the gRPC approach)

| the gRPC approach File / Class | Purpose | Why it doesn't exist in the DDS approach |
|--------------------------|---------|-----------------------------------|
| `fleet_discovery.py` | Zeroconf mDNS peer discovery | DDS SPDP — zero code |
| `video_renderer.py` | PyBullet 3D rendering | Not yet integrated (same code will be reused) |
| `RobotCommandServicer` | gRPC unary command handler | DDS listener callback in `_setup_listeners()` |
| `RobotStreamingServicer` | gRPC server-streaming publisher | `writer.write()` in publishing threads |
| `register_peer()` | Manual peer registration | DDS automatic discovery |
| `_connect_to_peer()` | Per-peer reconnect loop | Does not exist |
| `_start_*_stream()` | Per-peer reader threads (×4 per peer) | DDS listener callbacks (4 total, not 4×N) |
