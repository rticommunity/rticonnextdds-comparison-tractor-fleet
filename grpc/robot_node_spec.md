# Robot Fleet Node — Specification

> This document fully specifies the `robot_node.py` application so that an AI
> agent (or developer) can re-create it from scratch.  It assumes the reader has
> access to the `robot.proto` service/message definitions and the generated
> Python stubs (`robot_pb2.py`, `robot_pb2_grpc.py`).

---

## 1  Purpose

A single robot node that participates in a **full-mesh gRPC fleet**.  Every node
runs its own gRPC server, connects to every other node as a client, publishes
its own state / intent / telemetry via server-streaming RPCs, and accepts unary
commands from any peer or external controller (e.g. the dashboard UI).

Each node autonomously follows a waypoint route, avoids collisions with nearby
peers, reacts to operator commands (STOP, FOLLOW_PATH, GOTO, RESUME, SET_PATH,
CHARGE), and manages an autonomous charging workflow when battery is low.

---

## 2  Technology Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.14.3+ |
| RPC framework | gRPC (`grpcio`, `grpcio-tools`, `protobuf`) |
| Concurrency | Python `threading` (daemon threads, `threading.Event`, `threading.Lock`) |
| Thread pool | `concurrent.futures.ThreadPoolExecutor` (`max_workers=50`) |
| CLI | `argparse` |
| Math | `math` (trig, sqrt) |
| Randomness | `random` (seeded per-robot for deterministic routes) |
| Logging | `print()` with `[robot_id HH:MM:SS]` prefix via `_ts()` helper |

No web framework, no external libraries beyond the gRPC/protobuf stack.

---

## 3  Command-Line Interface

```
python robot_node.py --id NAME --fleet-port PORT [--fleet-port-list PORTS] [--same-host]
```

| Argument | Type | Example | Description |
|----------|------|---------|-------------|
| `--id` | str (required) | `alpha` | Human-readable robot name |
| `--fleet-port` | int (required) | `50051` | This robot's gRPC listen port |
| `--fleet-port-list` | str (optional) | `50051,50052,50053` | Comma-separated list of **all** fleet member ports. If omitted, mDNS discovery is used instead. |
| `--same-host` | flag (optional) | — | When set, discovered peer addresses are rewritten to `localhost:<port>` instead of using the mDNS-advertised IP. Useful as a fallback if LAN connections fail on a single machine (e.g. firewall, VPN). |

At startup `main()`:

1. Parses arguments.
2. Creates a `RobotNode(id, fleet_port)`.
3. Calls `start_server()` (sets `self.running = True`).
4. **If `--fleet-port-list` is provided** (static mode):
   - Iterates the port list, calls `register_peer("localhost:<port>")` for
     every port **except** this robot's own.
5. **If `--fleet-port-list` is omitted** (discovery mode):
   - Prints `"No --fleet-port-list given → using mDNS discovery"`.
   - Creates a `FleetDiscovery(robot_id, grpc_port, on_peer_added=callback,
     on_station_added=station_callback)`.
   - The peer callback receives `"host:port"` strings from mDNS:
     - If `--same-host`: rewrites to `localhost:<port>`.
     - Otherwise: uses the mDNS-advertised address directly.
     - Skips self by comparing port number against `--fleet-port`.
   - The station callback registers newly discovered charging stations via
     `register_charging_station()`.
   - Calls `discovery.start()`.
6. Calls `run()` which starts background threads and blocks.

---

## 4  `RobotNode` Class — State & Attributes

### 4.1  Identity

| Attribute | Type | Description |
|-----------|------|-------------|
| `robot_id` | `str` | Robot name (from `--id`) |
| `port` | `int` | gRPC listen port |
| `address` | `str` | `"localhost:<port>"` |
| `_fleet_robot_ids` | `list[str]` | Ordered list of all fleet robot IDs (derived from `fleet_port_list`); used by `VideoRenderer` for deterministic colour assignment |

### 4.2  Motion State

| Attribute | Type | Initial Value | Description |
|-----------|------|---------------|-------------|
| `position` | `dict {x,y,z}` | Random (0–100, 0–100, 0) | Current position in the arena |
| `status` | `RobotStatus` enum | `STATUS_MOVING` | One of `STATUS_MOVING`, `STATUS_HOLDING`, `STATUS_IDLE`, `STATUS_CHARGING` |
| `intent` | `RobotIntent` enum | `INTENT_FOLLOW_PATH` | One of `INTENT_FOLLOW_PATH`, `INTENT_GOTO`, `INTENT_IDLE`, `INTENT_CHARGE_QUEUE`, `INTENT_CHARGE_DOCK` |
| `battery_level` | `float` | `100.0` | Drains by 0.01 per tick (clamped ≥ 0); refills at CHARGE_RATE while docked |
| `goal` | `dict {x,y}` or `None` | `None` | GOTO / charge dock target (set by `CMD_GOTO` or charge workflow) |
| `hold_position` | `dict {x,y}` or `None` | `None` | Position to spring-back to while HOLDING |
| `speed` | `float` | `2.0` | Movement speed in arena units / second |
| `velocity_x` | `float` | `0.0` | Last computed X velocity (published in kinematic state) |
| `velocity_y` | `float` | `0.0` | Last computed Y velocity (published in kinematic state) |
| `velocity_z` | `float` | `0.0` | Last computed Z velocity (always 0 in 2-D) |
| `heading` | `float` | `0.0` | Last computed heading angle in radians (`atan2(vy, vx)`) |

### 4.3  Path State

| Attribute | Type | Description |
|-----------|------|-------------|
| `path_waypoints` | `list[dict {x,y}]` | 5–8 waypoints in a roughly circular loop |
| `path_index` | `int` | Index of the waypoint currently being navigated toward |

**Property**: `path_target` → `self.path_waypoints[self.path_index]`.

### 4.4  Charging State

| Attribute | Type | Description |
|-----------|------|-------------|
| `_charging_station_addresses` | `list[str]` | Known station `"host:port"` addresses |
| `_charge_slot_id` | `str` or `None` | Unique slot token from the station |
| `_charge_station_id` | `str` or `None` | Station name (e.g. "station1") |
| `_charge_station_address` | `str` or `None` | gRPC address for release |
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

### 4.5  Peer Tracking (all protected by `peers_lock`)

| Dict | Key | Value |
|------|-----|-------|
| `peers` | robot\_id (or placeholder `"pending-<addr>"`) | `str` — address |
| `peer_connections` | same key | `grpc.Channel` |
| `peer_streams` | same key | `dict` (unused but maintained for bookkeeping) |

### 4.6  Received Peer Data (protected by `data_lock`)

| Dict | Key | Value |
|------|-----|-------|
| `other_robots_state` | robot\_id | `KinematicState` protobuf |
| `other_robots_operational` | robot\_id | `OperationalState` protobuf |
| `other_robots_intent` | robot\_id | `IntentUpdate` protobuf |

`_nearby_peers`: `set` of peer robot IDs currently within `AVOIDANCE_RADIUS`.

### 4.7  Infrastructure

| Attribute | Type | Description |
|-----------|------|-------------|
| `server` | `grpc.Server` or `None` | The gRPC server instance |
| `running` | `bool` | Master flag; `False` → all threads exit |
| `peers_lock` | `threading.Lock` | Guards `peers`, `peer_connections`, `peer_streams` |
| `data_lock` | `threading.Lock` | Guards `other_robots_state`, `other_robots_intent` |
| `video_renderer` | `VideoRenderer` | PyBullet-based headless 3D renderer (from `video_renderer.py`) |
| `active_video_streams` | `int` | `0` — count of currently active `RequestVideo` streams |
| `_delivery_loop_count` | `int` | Count of concurrent server-side streaming handlers |
| `_delivery_loop_lock` | `threading.Lock` | Guards `_delivery_loop_count` |

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

---

## 6  gRPC Server

### `start_server()`

1. Creates a `grpc.server` with `ThreadPoolExecutor(max_workers=50)`.
   - The pool must accommodate `(N-1)×4` concurrent server-streaming handlers
     (one per peer per stream type: kinematic, operational, intent, telemetry)
     plus headroom for unary RPCs.
2. Registers both servicers:
   - `RobotCommandServicer(self)` — handles unary commands.
   - `RobotStreamingServicer(self)` — handles server-streaming subscriptions.
3. Binds to `[::]:<port>` (insecure).
4. Starts the server, sets `self.running = True`.

---

## 7  Full-Mesh Networking (Client Side)

### 7.1  `register_peer(address)`

- Skips if `address == self.address` (own port).
- Skips if address is already known.
- Creates a placeholder key `"pending-<address>"` in `self.peers`.
- Spawns a **daemon thread** running `_connect_to_peer(placeholder, address)`.

### 7.2  `_connect_to_peer(peer_key, address)`

Reconnect loop (runs while `self.running`):

1. Open `grpc.insecure_channel(address)`.
2. Wait for channel ready via `grpc.channel_ready_future(channel).result(timeout=5)`.
   - On timeout → log, sleep 3 s, retry.
3. Create a `RobotStreamingStub(channel)`.
4. Store channel in `peer_connections[peer_key]`.
5. Start four reader threads (kinematic, operational, intent, telemetry — see §8).
6. **Monitor connectivity**: subscribe to the channel with a callback that sets a
   `threading.Event` on `TRANSIENT_FAILURE` or `SHUTDOWN`.
7. Block (polling every 2 s) until the connectivity event fires or `self.running`
   becomes False.
8. On any exception → clean up `peer_connections` and `peer_streams` for this key,
   log, sleep 3 s, retry.

---

## 8  Server-Streaming Subscriptions (Client Reader Threads)

All four readers follow the same pattern:

1. Build a `SubscribeRequest(subscriber_id=self.robot_id)`.
2. Call the stub's server-streaming method.
3. Iterate the response stream; on each message, store it in the appropriate
   `data_lock`-protected dict keyed by `msg.robot_id`.
4. On `grpc.RpcError` → silently exit (expected on shutdown).
5. On other exceptions → log if `self.running`.

### 8.1  `_start_kinematic_stream(robot_id, stub)`

- Subscribes to `stub.StreamKinematicState(req)`.
- Stores each `KinematicState` in `self.other_robots_state[msg.robot_id]`.
- Server publishes at **10 Hz** (0.1 s sleep).

### 8.2  `_start_operational_stream(robot_id, stub)`

- Subscribes to `stub.StreamOperationalState(req)`.
- Stores each `OperationalState` in `self.other_robots_operational[msg.robot_id]`.
- Server publishes at **1 Hz** (1.0 s sleep).

### 8.3  `_start_intent_stream(robot_id, stub)`

- Subscribes to `stub.StreamIntent(req)`.
- Stores each `IntentUpdate` in `self.other_robots_intent[msg.robot_id]`.
- Server publishes at **1 Hz** (1.0 s sleep).

### 8.4  `_start_telemetry_stream(robot_id, stub)`

- Subscribes to `stub.StreamTelemetry(req)`.
- Currently **silently consumed** (no local storage); exists for completeness
  and is logged in status reports.
- Server publishes at **1 Hz** (1.0 s sleep).

---

## 9  Server-Streaming Servicers (Server Side)

### 9.1  `RobotStreamingServicer`

Accepts a `SubscribeRequest` and yields data in a loop while
`self.robot.running and context.is_active()`.  No input stream is read — each
RPC is purely server-streaming.  Each handler increments `_delivery_loop_count`
on entry and decrements it on exit.

#### `StreamKinematicState` (10 Hz)

Yields a `KinematicState` every 0.1 s containing:

| Field | Source |
|-------|--------|
| `robot_id` | `self.robot.robot_id` |
| `position` | `Position(x, y, z)` |
| `velocity` | `Velocity3(x, y, z)` |
| `heading` | `self.robot.heading` |
| `timestamp` | `time.time() * 1000` (ms) |

#### `StreamOperationalState` (1 Hz)

Yields an `OperationalState` every 1.0 s containing:

| Field | Source |
|-------|--------|
| `robot_id` | `self.robot.robot_id` |
| `status` | `self.robot.status` |
| `battery_level` | `self.robot.battery_level` |
| `timestamp` | `time.time() * 1000` (ms) |

#### `StreamIntent` (1 Hz)

Yields an `IntentUpdate` every 1.0 s.  Target position depends on intent:

| Intent | `target_position` | `path_waypoints` | `path_waypoint_index` |
|--------|-------------------|---------------------|--------------------------|
| `INTENT_GOTO` (en route) | `goal` (x, y) | empty | 0 |
| `INTENT_GOTO` (arrived) | `hold_position` (x, y) | empty | 0 |
| `INTENT_GOTO` (no goal or hold) | current `position` | empty | 0 |
| `INTENT_FOLLOW_PATH` | current `path_target` | full waypoint list | `path_index` |
| `INTENT_IDLE` | current `position` | empty | 0 |
| `INTENT_CHARGE_*` | `goal` or current `position` | empty | 0 |

All field reads are protected by `self.robot.data_lock`.

#### `StreamTelemetry` (1 Hz)

Yields a `TelemetryUpdate` every 1.0 s with **random simulated** readings:

| Field | Range |
|-------|-------|
| `cpu_usage` | 20–80 % |
| `memory_usage` | 30–70 % |
| `temperature` | 40–70 °C |
| `tcp_connections` | Live count via `psutil` |
| `active_delivery_loops` | `delivery_loop_count` property |

---

## 10  Movement Model (`update_position`)

Runs in a daemon thread.  Each tick `dt = 0.1 s`.

### 10.1  Four-State Behaviour

#### `STATUS_MOVING`

Compute a desired velocity `(vx, vy)` toward the current goal:

- **`INTENT_GOTO`** (goal ≠ None): steer toward `goal`.
  - If within `speed × dt` of goal → **arrive**: snap to goal, set
    `hold_position = goal`, clear `goal`, set `status = STATUS_HOLDING`.
    **Intent stays `INTENT_GOTO`** — the status alone tells us the robot
    has arrived.
    **Velocity preservation**: on arrival, keep `velocity_x` / `velocity_y`
    at their last moving values so the UI retains the approach heading.
  - Otherwise → `(vx, vy)` = unit direction × speed.

- **`INTENT_FOLLOW_PATH`**: steer toward `path_target`.
  - If within `speed × dt` → snap, advance `path_index` (wrapping).
    **Path look-ahead**: immediately compute the direction toward the
    *next* waypoint and set `(vx, vy)` accordingly, so the heading smoothly
    transitions instead of briefly zeroing.
  - Otherwise → `(vx, vy)` = unit direction × speed.

- **`INTENT_CHARGE_DOCK`** / **`INTENT_CHARGE_QUEUE`**: steer toward `goal`
  (dock position or wait position).  On arrival at the dock (rank 0), set
  `status = STATUS_CHARGING` and begin battery refill.  On arrival at wait
  position, set `status = STATUS_HOLDING` and hold.

After computing goal velocity, blend in collision avoidance (§11):

- If `hard_override` → replace `(vx, vy)` entirely with avoidance vector.
- Otherwise → add avoidance to goal velocity.
- Apply: `position += (vx, vy) × dt`.

**Velocity / heading update** (while `STATUS_MOVING`):

- Unconditionally write `velocity_x = vx`, `velocity_y = vy`.
- Compute `heading = math.atan2(vy, vx)`.

#### `STATUS_HOLDING`

No goal velocity.  A gentle **spring** pulls the robot back toward
`hold_position`:

- `spring_speed = min(0.5, dist_to_hold)` → velocity toward hold point.

Avoidance is still active (same blend logic: hard\_override replaces spring,
otherwise adds to it).  Position updated by `(vx + avoid) × dt`.

**Velocity update** (while `STATUS_HOLDING`): only update `velocity_x` /
`velocity_y` when the total velocity magnitude exceeds 0.01.  This avoids
overwriting the heading with near-zero jitter while the robot is nearly
stationary.

#### `STATUS_IDLE`

No movement at all.  No avoidance.  The robot is completely parked.

**Velocity**: `velocity_x` / `velocity_y` are **not** zeroed — the last
non-trivial values are retained so the UI can preserve the robot's heading
even while idle.

#### `STATUS_CHARGING`

No movement.  No avoidance.  Battery refills at `CHARGE_RATE` %/s (2.0).
When `battery_level >= CHARGE_FULL` (95%), the robot calls
`_release_charge_slot()` to finish charging and resume its prior task.

### 10.2  Post-Move Housekeeping (every tick)

1. **Clamp** position to arena bounds `[0, 100]` on both axes.
2. **Battery drain**: `battery_level -= 0.01` (clamped ≥ 0).
3. **Auto-charge trigger**: If `battery_level < CHARGE_THRESHOLD` (20%) and
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

### `_compute_avoidance()` → `(ax, ay, hard_override)`

1. Snapshot all peer positions from `other_robots_state` (under `data_lock`).
2. Also include `INTENT_CHARGE_QUEUE` peers from `other_robots_intent` as
   "soft obstacles" with weakened avoidance (they're parked near a station and
   should be gently avoided, not treated as full obstacles).
3. For each peer within `AVOIDANCE_RADIUS`:
   - **Inverse-square** repulsion:
     ```
     ratio = AVOIDANCE_RADIUS / dist
     strength = AVOIDANCE_STRENGTH × ratio²
     ```
   - Accumulate `(dx/dist × strength, dy/dist × strength)` into `(ax, ay)`.
   - If `dist < MIN_SEPARATION` → set `hard_override = True`.
4. **Proximity logging**:
   - Peer enters radius → `⚠ Peer {rid} nearby ({dist:.1f} units) — avoiding`
   - Peer exits radius → `✓ Peer {rid} clear ({dist:.1f} units)`
   - Tracked via `_nearby_peers` set.

### Caller Behaviour

- `hard_override = True` → **discard** goal/spring velocity, use pure repulsion.
- `hard_override = False` → **add** avoidance to goal/spring velocity.

This makes collisions physically impossible regardless of goal speed, because
the inverse-square repulsion grows without bound as distance shrinks.

---

## 12  Command Handling

### `RobotCommandServicer.SendCommand(request, context)`

Unary RPC.  Processes a `CommandRequest` and returns a `CommandResponse`
containing `{success, description, resulting_status, resulting_intent}`.

If the robot is currently in a charging state (`_CHARGE_INTENTS`) and receives
GOTO, FOLLOW_PATH, or STOP, the charge is **aborted** via `_abort_charge()` before
processing the new command.

| Command | Effect |
|---------|--------|
| `CMD_STOP` | `status → STATUS_IDLE`, `hold_position → None`.  Intent unchanged. |
| `CMD_FOLLOW_PATH` | `goal → None`, `hold_position → None`, `intent → INTENT_FOLLOW_PATH`, `status → STATUS_MOVING`.  If `goto_params.speed > 0` → update speed. |
| `CMD_GOTO` | Read `(x, y, speed)` from `goto_params` (default speed 2.0).  `goal → {x, y}`, `hold_position → None`, `intent → INTENT_GOTO`, `status → STATUS_MOVING`. |
| `CMD_RESUME` | See §12.1 below. |
| `CMD_SET_PATH` | Read waypoints from `set_path_params.waypoints`.  Rejects if < 2 waypoints.  Replaces `path_waypoints`, clamps `path_index` to valid range.  Does **not** change status or intent. |
| `CMD_CHARGE` | Initiates charging workflow (see §13).  Rejected if already charging. |
| Unknown | Returns `success=False`. |

### 12.1  RESUME Logic Detail

When resuming, behaviour depends on the current intent:

| Current Intent | Goal State | Behaviour |
|----------------|-----------|-----------|
| `INTENT_FOLLOW_PATH` | any | Set `STATUS_MOVING` — resume path route |
| `INTENT_GOTO` | goal ≠ None | Set `STATUS_MOVING` — resume navigation to GOTO target |
| `INTENT_GOTO` | goal = None, hold_position set | Re-navigate to hold position (issue new GOTO) |
| `INTENT_GOTO` | goal = None, no hold | Set `STATUS_HOLDING` at current position |
| `INTENT_IDLE` | — | Set `STATUS_HOLDING` at current position (avoidance active) |

If `goto_params.speed > 0`, the speed is updated for all cases.

### `RequestVideo(request, context)`

Server-streaming RPC.  Renders real-time first-person 3D video from the robot's
onboard camera using PyBullet (via `VideoRenderer`).

1. Increments `active_video_streams`.
2. Loops at ~5 fps while `context.is_active()`:
   a. Takes a `data_lock` snapshot of own position / status / intent /
      `path_waypoints` and all peer positions from `other_robots_state`.
   b. Calls `video_renderer.render_frame(...)` with the snapshot data.
   c. Yields a `VideoFrame(frame_data=jpeg_bytes, timestamp=ms)`.
3. On exit (client disconnects or error) → decrements `active_video_streams`
   in a `finally` block.

---

## 13  Charging Workflow

Charging is triggered in two ways:

1. **Operator command**: `CMD_CHARGE` — spawns a thread to call `_initiate_charge()`.
2. **Auto-charge**: When `battery_level < CHARGE_THRESHOLD` (20%) during
   `update_position`, and no charge is already in progress.

In both cases, `_charge_requested` is set to `True` as a simple mutex so only
one charge attempt can be in flight at a time.

### 13.1  `_initiate_charge()` Flow

1. Check if already in a charge state (`_CHARGE_INTENTS`).  If so, return.
2. **Stash** current state: intent, status, speed, goal, hold_position.
3. **Fan out** `RequestSlot` to all known stations.  Collect offers.
4. If no stations reachable → log, reset `_charge_requested`, return.
5. **Rank offers** by `(wait_time_s, distance_to_dock)`.  Pick the best.
6. **Release rejected** offers (best-effort `ReleaseSlot` to non-chosen stations).
7. **Confirm** the best slot via `ConfirmSlot`.
8. Store charge state (`_charge_slot_id`, `_charge_dock`, `_charge_wait_pos`, etc.).
9. **Navigate**:
   - If `queue_rank == 0` → goal = dock, intent = `INTENT_CHARGE_DOCK`.
   - Otherwise → goal = wait position, intent = `INTENT_CHARGE_QUEUE`.
10. Set `status = STATUS_MOVING`, `speed = 3.0` (move briskly to station).
11. If `INTENT_CHARGE_QUEUE` → **start poll thread immediately** (don't wait for
    arrival — the dock may free while the robot is still en route).

### 13.2  `_poll_charge_queue()` — Background Rank Poller

Runs in a daemon thread while the robot is in the charge workflow.  Every 3 s:

1. Guard: exit if intent is no longer in `_CHARGE_INTENTS`, or if status is not
   `STATUS_HOLDING` or `STATUS_MOVING`, or if charge state has been cleared.
2. Re-call `ConfirmSlot` (idempotent for already-confirmed slots) to get
   updated rank.
3. If `granted == False` → slot lost; abort charge and restore prior state.
4. If `queue_rank == 0` → navigate to dock, set `INTENT_CHARGE_DOCK`, exit thread.
5. If rank changed → navigate to updated wait position.

### 13.3  Dock Arrival and Charging

When the robot arrives at the dock (goal reached with `INTENT_CHARGE_DOCK`):

1. Set `status = STATUS_CHARGING`.
2. The `update_position` tick loop refills battery at `CHARGE_RATE` %/s.
3. When `battery_level >= CHARGE_FULL` (95%) → call `_release_charge_slot()`.

### 13.4  `_release_charge_slot()` — Post-Charge Resume

1. Call `ReleaseSlot` to free the dock for the next robot.
2. **Restore pre-charge state**:
   - If the stashed state was `INTENT_GOTO` + arrived (goal=None, hold set):
     re-issue GOTO to the original hold position so the robot navigates back.
   - Otherwise: restore intent, status, speed, goal directly.
3. Clear all charge state attributes.

### 13.5  `_abort_charge()` — Operator Override

Called when GOTO / FOLLOW_PATH / STOP overrides an in-progress charge:

1. Call `ReleaseSlot` (best-effort).
2. Clear all charge state.
3. Does **not** restore intent/status — the caller sets those for the new command.

---

## 14  Status Reporting (`print_status`)

Runs in a daemon thread, prints every **5 seconds**.

Format:
```
[robot_id HH:MM:SS] pos=(x,y) STATUS/INTENT batt=N% peers=C/T streams=S
```

| Token | Meaning |
|-------|---------|
| `pos=(x,y)` | Current position (1 decimal) |
| `STATUS/INTENT` | Enum name strings (e.g. `STATUS_MOVING/INTENT_FOLLOW_PATH`) |
| `batt=N%` | Battery percentage (0 decimal) |
| `peers=C/T` | Connected channels / total registered peers |
| `streams=S` | `C × 4` (kinematic + operational + intent + telemetry per connected peer) |

---

## 15  Main Run Loop

`run()`:

1. Calls `start_server()` if not already running.
2. Spawns two daemon threads:
   - `update_position` (movement / avoidance / charging tick loop).
   - `print_status` (periodic log line).
3. Blocks on `while self.running: sleep(1)`.
4. On `KeyboardInterrupt` → sets `running = False`, calls `server.stop(0)`.

---

## 16  Resilience Behaviour

| Scenario | Behaviour |
|----------|-----------|
| Peer not yet started | `channel_ready_future` times out after 5 s → log, sleep 3 s, retry. |
| Peer crashes mid-session | Channel connectivity callback detects `TRANSIENT_FAILURE` or `SHUTDOWN` → raises exception → clean up, sleep 3 s, reconnect. |
| Stream ends unexpectedly | Reader thread catches exception → exits silently.  Connectivity monitor detects the drop and triggers reconnect. |
| Node shutdown (`Ctrl-C`) | `running = False` → all daemon threads and streaming loops exit.  `server.stop(0)` terminates the gRPC server. |
| Charging station unreachable | `_initiate_charge` catches the exception and logs it.  Robot stays on current task. |
| Slot lost during polling | `_poll_charge_queue` detects `granted == False` → aborts charge and restores prior state. |

---

## 17  Thread Summary

| Thread | Daemon | Description | Created by |
|--------|--------|-------------|------------|
| Main | No | `run()` → blocks on `sleep(1)` loop | `main()` |
| `update_position` | Yes | Movement tick at 10 Hz | `run()` |
| `print_status` | Yes | Log line every 5 s | `run()` |
| Per-peer connector | Yes | `_connect_to_peer` reconnect loop | `register_peer()` |
| Per-peer kinematic reader | Yes | `_start_kinematic_stream` iterator | `_connect_to_peer()` |
| Per-peer operational reader | Yes | `_start_operational_stream` iterator | `_connect_to_peer()` |
| Per-peer intent reader | Yes | `_start_intent_stream` iterator | `_connect_to_peer()` |
| Per-peer telemetry reader | Yes | `_start_telemetry_stream` iterator | `_connect_to_peer()` |
| Charge initiator | Yes | `_initiate_charge` (one-shot) | CMD_CHARGE or auto-trigger |
| Charge queue poller | Yes | `_poll_charge_queue` (long-running) | `_initiate_charge()` |
| gRPC server pool (50) | — | Handles incoming RPCs | `start_server()` |

For a 5-robot fleet, each node has: 1 main + 2 background + 4 connector threads
+ 4×4 reader threads = **23 threads** plus the gRPC server pool, plus up to
2 charging threads when a charge cycle is active.

---

## 18  Proto Contract Summary

The node depends on the following from `robot.proto` (see that file or
`robot_proto_spec.md` for full definitions):

### Services implemented (server side)

- `RobotStreaming.StreamKinematicState` (server-streaming: `SubscribeRequest` → stream of `KinematicState`)
- `RobotStreaming.StreamOperationalState` (server-streaming: `SubscribeRequest` → stream of `OperationalState`)
- `RobotStreaming.StreamIntent` (server-streaming: `SubscribeRequest` → stream of `IntentUpdate`)
- `RobotStreaming.StreamTelemetry` (server-streaming: `SubscribeRequest` → stream of `TelemetryUpdate`)
- `RobotCommand.SendCommand` (unary: `CommandRequest` → `CommandResponse`)
- `RobotCommand.RequestVideo` (server-streaming: `VideoRequest` → stream of `VideoFrame`)

### Services consumed (client side — peer robots)

- `RobotStreaming.StreamKinematicState` — subscribe to each peer's kinematic state at 10 Hz.
- `RobotStreaming.StreamOperationalState` — subscribe to each peer's operational state at 1 Hz.
- `RobotStreaming.StreamIntent` — subscribe to each peer's intent at 1 Hz.
- `RobotStreaming.StreamTelemetry` — subscribe to each peer's telemetry at 1 Hz.

### Services consumed (client side — charging stations)

- `ChargingStation.RequestSlot` (unary)
- `ChargingStation.ConfirmSlot` (unary)
- `ChargingStation.ReleaseSlot` (unary)

### Enums

- `RobotStatus`: `STATUS_UNKNOWN=0`, `STATUS_MOVING=1`, `STATUS_IDLE=2`, `STATUS_HOLDING=3`, `STATUS_CHARGING=4`
- `RobotIntent`: `INTENT_UNKNOWN=0`, `INTENT_FOLLOW_PATH=1`, `INTENT_GOTO=2`, `INTENT_IDLE=3`, `INTENT_CHARGE_QUEUE=4`, `INTENT_CHARGE_DOCK=5`
- `Command`: `COMMAND_UNKNOWN=0`, `CMD_STOP=1`, `CMD_FOLLOW_PATH=2`, `CMD_GOTO=3`, `CMD_RESUME=4`, `CMD_SET_PATH=5`, `CMD_CHARGE=6`

### Key Messages

- `SubscribeRequest`: `subscriber_id` (string)
- `KinematicState`: `robot_id`, `position`, `velocity`, `heading`, `timestamp`
- `OperationalState`: `robot_id`, `status`, `battery_level`, `timestamp`
- `IntentUpdate`: `robot_id`, `intent_type`, `target_position`, `path_waypoints`, `path_waypoint_index`, `timestamp`
- `CommandRequest`: `robot_id`, `command`, `oneof params { GotoParameters goto_params = 10; SetPathParameters set_path_params = 11; }`
- `GotoParameters`: `x`, `y`, `speed`
- `SetPathParameters`: `repeated Waypoint waypoints`
- `Waypoint`: `x`, `y`
- `CommandResponse`: `success`, `description`, `resulting_status`, `resulting_intent`
- `VideoRequest`: `robot_id` (string)
- `VideoFrame`: `frame_data` (bytes — JPEG), `timestamp` (int64)
- `SlotRequest`: `robot_id`, `battery_level`
- `SlotOffer`: `station_id`, `slot_id`, `queue_rank`, `wait_time_s`, `dock_x`, `dock_y`
- `SlotConfirm`: `robot_id`, `slot_id`
- `SlotAssignment`: `granted`, `queue_rank`, `wait_x`, `wait_y`, `dock_x`, `dock_y`
- `SlotRelease`: `robot_id`, `slot_id`
- `SlotReleaseAck`: `success`

---

## 19  File Structure

The application is a **single Python file** (`robot_node.py`) that:

1. Defines a `_ts()` timestamp helper.
2. Defines `_CHARGE_INTENTS` tuple at module level.
3. Imports `robot_pb2` and `robot_pb2_grpc` (generated from `robot.proto`).
4. Imports `VideoRenderer` from `video_renderer.py`.
5. Imports `FleetDiscovery` from `fleet_discovery.py`.
6. Defines `RobotNode` (core state, networking, movement, avoidance, video,
   charging workflow).
7. Defines `RobotCommandServicer` (unary command handler + `RequestVideo`
   server-streaming video feed).
8. Defines `RobotStreamingServicer` (server-streaming data publisher).
9. Defines `main()` parsing CLI args, creating the node, and running it.

### Companion files

| File | Purpose |
|------|---------|
| `video_renderer.py` | `VideoRenderer` class — PyBullet headless 3D FPV camera rendering |
| `fleet_discovery.py` | `FleetDiscovery` class — Zeroconf mDNS/DNS-SD peer discovery |
| `charging_station.py` | Standalone charging station process |

### External Dependencies

| Package | Use |
|---------|-----|
| `grpcio` / `grpcio-tools` / `protobuf` | RPC framework and code generation |
| `pybullet` | Headless 3D physics engine for onboard-camera rendering (via `video_renderer.py`) |
| `Pillow` | 2D text overlay on rendered frames (via `video_renderer.py`) |
| `numpy` | Image buffer conversion (via `video_renderer.py`) |
| `zeroconf` | mDNS/DNS-SD peer discovery (discovery mode only) |
| `ifaddr` | LAN IP selection — enumerate interfaces and pick best RFC-1918 address |
| `psutil` | Process-level TCP socket counting for telemetry |
