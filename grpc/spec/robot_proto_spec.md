# Robot Protocol — Requirements Specification

> This document describes the **requirements and design rationale** behind
> `robot.proto`.  It is not a line-by-line copy of the `.proto` file; rather it
> captures the domain concepts, QoS needs, and design decisions so that an AI
> agent (or developer) can re-derive a compatible protocol definition from
> scratch.

---

## 1  Domain Overview

A fleet of up to 10 mobile robots (or drones) operate in a shared 2-D arena
(0–100 × 0–100).  Each robot is an independent process.  In addition there are
non-robot participants: a dashboard UI, a command console, a recorder, a safety
monitor, a cloud ingest service, and one or more **charging stations**.

Every participant needs some combination of:

- **Observing** what each robot is doing (state, intent, telemetry).
- **Commanding** individual robots (navigate, stop, follow path, charge, etc.).
- **Streaming video** from one or more robots on demand.
- **Managing charging** — robots request charge slots from stations, queue,
  dock, charge, and release.

---

## 2  Information Produced by Each Robot

Each robot publishes four independent streams of information about itself:

| Stream | What it carries | Update pattern |
|--------|----------------|----------------|
| **Kinematic State** | Position (x, y, z), velocity (vx, vy, vz), heading | Periodic (high frequency, 10 Hz) |
| **Operational State** | Physical status (moving / holding / idle / charging), battery level | Periodic (1 Hz) |
| **Intent** | Current mission type (follow_path / goto / idle / charge_queue / charge_dock), current navigation target, path route waypoints | Periodic (1 Hz); changes when mission changes |
| **Telemetry** | CPU usage, memory usage, temperature, TCP connections, active delivery loops | Periodic (1 Hz); loss of a few updates is tolerable |

Every stream message must include the **robot's identity** and a **timestamp**.

---

## 3  Communication Patterns

### 3.1  Streams (one-to-many, continuous)

Robots broadcast their Kinematic State, Operational State, Intent, and
Telemetry to all interested parties.  In the gRPC approach (full mesh gRPC) this is
realised as **server-streaming RPCs** — each subscriber sends a
`SubscribeRequest` containing its identity and receives a continuous stream of
updates from the server.  Data flows in one direction only (server → client).

Key requirements:
- Late joiners must converge on current truth quickly.
- Kinematic state is periodic — missing one update is acceptable because the
  next one is imminent.
- Operational state and intent must be delivered **reliably** — they carry
  mission-critical information (where robots plan to go, whether they're
  charging).
- Periodic telemetry may be lost without harm.

### 3.2  Commands (point-to-point, request/response)

An operator (or automated system) sends a command to a specific robot and
receives a synchronous response indicating success/failure and the robot's
resulting state.

Key requirements:
- Commands must be **reliable** (acknowledged, not fire-and-forget).
- The response must echo the robot's new status and intent so the sender has
  immediate confirmation.

### 3.3  Video (point-to-point, server-streaming)

A consumer requests a live video feed from a specific robot.  The robot streams
back a sequence of video frames.  This is a one-way server stream (consumer →
request, robot → stream of frames).

The request message (`VideoRequest`) contains only a `robot_id` (string)
identifying which robot's camera to stream.  Each response message
(`VideoFrame`) carries:

- **`frame_data`** (bytes) — a single JPEG-encoded image.
- **`timestamp`** (int64) — milliseconds since epoch when the frame was
  rendered.

### 3.4  Charging Station Interaction (point-to-point RPCs)

Each charging station exposes a gRPC service that robots call individually.
The protocol models a three-phase handshake:

1. **RequestSlot** (unary) — non-committing inquiry.  Returns an offer with
   estimated wait time and queue rank so the robot can compare multiple
   stations.
2. **ConfirmSlot** (unary) — commits to the offered slot.  Returns a waiting
   position (near the station) or the dock coordinates if the robot is first
   in line.
3. **ReleaseSlot** (unary) — robot has finished charging or wants to cancel.
   Frees the dock and advances the queue.

The station also provides **StreamStatus** (server-streaming) so the dashboard
UI can display the queue in real time without polling.

---

## 4  Robot Physical Status vs Mission Intent

The protocol distinguishes two orthogonal concepts:

| Concept | What it describes | Values |
|---------|------------------|--------|
| **Status** | Physical motion state | `MOVING`, `IDLE`, `HOLDING`, `CHARGING` (plus `UNKNOWN` zero-value) |
| **Intent** | Current mission / objective | `FOLLOW_PATH`, `GOTO`, `IDLE`, `CHARGE_QUEUE`, `CHARGE_DOCK` (plus `UNKNOWN` zero-value) |

These are independent axes.  The valid combinations are:

| State | Intent | Status | Meaning |
|-------|--------|--------|---------|
| Following path | `INTENT_FOLLOW_PATH` | `STATUS_MOVING` | Cycling through waypoints |
| Navigating to target | `INTENT_GOTO` | `STATUS_MOVING` | En route to commanded position |
| Arrived at target | `INTENT_GOTO` | `STATUS_HOLDING` | At destination, holding position (avoidance active) |
| Parked (no mission) | `INTENT_IDLE` | `STATUS_IDLE` | CMD_STOP — fully parked, no avoidance |
| Queued for charge | `INTENT_CHARGE_QUEUE` | `STATUS_MOVING` / `STATUS_HOLDING` | Heading to or holding at wait position |
| Docking to charge | `INTENT_CHARGE_DOCK` | `STATUS_MOVING` | Navigating to dock |
| Charging | `INTENT_CHARGE_DOCK` | `STATUS_CHARGING` | Battery refilling at dock |

**Key design choice**: `INTENT_GOTO` persists through arrival.  When a robot
reaches its GOTO target, the intent stays `INTENT_GOTO` and only the status
changes to `STATUS_HOLDING`.  This preserves the "I was sent somewhere" context.
`INTENT_IDLE` is used exclusively for CMD_STOP (truly parked, no mission).

The four physical states are:
- **MOVING** — actively navigating toward a goal.
- **IDLE** — parked / powered down; no movement, no collision avoidance.
- **HOLDING** — arrived at a destination and holding position; collision
  avoidance remains active, and a gentle spring pulls the robot back to its
  hold point if displaced.
- **CHARGING** — docked at a charging station; battery refilling.

All enum values are **prefixed** to avoid C++ scoping clashes (e.g.
`STATUS_MOVING`, `STATUS_IDLE`, `STATUS_HOLDING`, `STATUS_CHARGING`,
`INTENT_FOLLOW_PATH`, `INTENT_GOTO`, `INTENT_IDLE`, `INTENT_CHARGE_QUEUE`,
`INTENT_CHARGE_DOCK`, `CMD_STOP`, `CMD_CHARGE`, etc.).

---

## 5  Command Vocabulary

The protocol must support the following operator commands:

| Command | Purpose | Parameters | Resulting behaviour |
|---------|---------|-----------|---------------------|
| **STOP** | Halt the robot immediately | None | Status → IDLE; intent unchanged |
| **GOTO** | Navigate to a specific point | Target (x, y), optional speed | Status → MOVING, intent → GOTO; on arrival → HOLDING (intent stays GOTO) |
| **FOLLOW_PATH** | Begin (or restart) path route | Optional speed | Status → MOVING, intent → FOLLOW_PATH; clears any GOTO goal |
| **RESUME** | Resume movement after IDLE or HOLDING | Optional speed | Behaviour depends on current intent — see §5.2 |
| **SET_PATH** | Replace the robot's path route | Ordered list of waypoints (≥ 2) | Path waypoints updated; path index clamped; status/intent unchanged |
| **CHARGE** | Send robot to nearest charging station | None | Stashes current state, requests slot, navigates to station |

Design decisions:
- **Speed** is an optional parameter.  Zero means "keep current speed" or use a
  sensible default (2.0 units/s).  Rather than creating separate parameter
  messages for every command, speed is carried via a reusable "goto parameters"
  structure that is also used by FOLLOW_PATH and RESUME.
- **SET_PATH** carries an ordered list of (x, y) waypoints.  The robot treats
  this as a closed loop and cycles through them.
- **CHARGE** initiates the charging workflow: the robot fans out `RequestSlot`
  to all known stations, picks the best offer, confirms the slot, and navigates
  autonomously.  If it was charging, any override command (GOTO, FOLLOW_PATH, STOP)
  aborts the charge and releases the slot.

### 5.1  Typed Parameters via `oneof`

To avoid stringly-typed parameter passing, the command request uses a protobuf
`oneof` to carry typed parameter blocks:

- **GotoParameters** — used by GOTO (target + speed), and optionally by FOLLOW_PATH
  and RESUME (speed-only).
- **SetPathParameters** — used by SET_PATH (list of waypoints).

At most one parameter block is present per request.  Commands that need no
parameters omit the oneof entirely.

### 5.2  RESUME Logic Detail

The RESUME command's behaviour depends on the robot's current intent:

| Current intent | RESUME behaviour |
|----------------|-----------------|
| `INTENT_FOLLOW_PATH` | Set `STATUS_MOVING` — robot continues path route |
| `INTENT_GOTO` with goal | Set `STATUS_MOVING` — robot resumes navigation to GOTO target |
| `INTENT_GOTO` with hold_position (arrived) | Re-navigate to the hold position (issue new GOTO) |
| `INTENT_IDLE` | Set `STATUS_HOLDING` at current position (avoidance active, nothing to move toward) |

---

## 6  Command Response

Every command returns a response containing:

- **success** (bool) — whether the command was accepted.
- **description** (string) — human-readable result message.
- **resulting status** — the robot's physical status after processing.
- **resulting intent** — the robot's mission intent after processing.

This gives the sender immediate feedback without needing to wait for the next
state stream update.

---

## 7  Intent Path Data

The `IntentUpdate` message carries structured path information directly as
protobuf fields (not serialised as JSON):

- **`path_waypoints`** — a repeated list of `Waypoint` messages representing
  the robot's current ordered path route.  Empty when the robot is not
  following a path.
- **`path_waypoint_index`** — an integer indicating which waypoint in the list
  the robot is currently navigating toward.

When the intent is `INTENT_GOTO` and the robot has arrived (goal is None but
hold_position is set), the `target_position` reports the hold position so
the UI can draw the intent arrow correctly.

This allows consumers (UI, safety monitor, etc.) to render and reason about
path routes without JSON parsing or string conventions.

---

## 8  Position Model

Position is a 3-D coordinate (x, y, z) attached to a robot identity and
timestamp.  In the current demo the arena is flat (z = 0), but the schema
supports future 3-D extension (e.g. drones at altitude).

The robot's state is split into two streams at different frequencies:

- **KinematicState** (10 Hz) — position, velocity (3-D), heading.  High-frequency
  for smooth rendering.
- **OperationalState** (1 Hz) — status enum, battery level.  Changes slowly.

Each stream also carries the robot's **velocity** (x, y, z) and **heading**
(radians, `atan2(vy, vx)`).  These allow consumers (particularly the UI) to
orient icons in the direction of travel and smooth rendering between updates.

---

## 9  Identity Model

Every message includes a `robot_id` (string).  Robot IDs are human-readable
identifiers (e.g. "tractor1", "tractor2") chosen at startup.  They should be
unique.

The UI discovers robot names at runtime from the `robot_id` field of incoming
stream messages.  It does not require a registry or configuration file listing
all robot names.

Charging stations have a `station_id` (string, e.g. "station1", "station2")
that is set at startup via `--station-id`.

---

## 10  Streaming Architecture (the gRPC approach context)

For the gRPC approach (full mesh gRPC), the four data streams (Kinematic State,
Operational State, Intent, Telemetry) are each a **server-streaming RPC**.
The subscriber sends a `SubscribeRequest` (containing its `subscriber_id`
string) and the server yields a continuous stream of updates.  This means:

- Data flows in **one direction** only (server → subscriber).
- No noop/heartbeat messages are needed from the subscriber side.
- Stream breakage is detected by exception in the reader thread, triggering
  reconnection logic.

The `SubscribeRequest` message contains a single `subscriber_id` field
(string).  This allows the server to log who is subscribing but has no effect
on the data produced — every subscriber receives the same stream.

The Command service is a simple **unary RPC** (request → response).

Video is a **server-streaming RPC** (single request → stream of frames).

All robot service methods are grouped into two gRPC services:
- **RobotCommand** — SendCommand (unary) + RequestVideo (server-stream).
- **RobotStreaming** — StreamKinematicState, StreamOperationalState,
  StreamIntent, StreamTelemetry (all server-streaming).

A third service handles charging:
- **ChargingStation** — RequestSlot, ConfirmSlot, ReleaseSlot (all unary) +
  StreamStatus (server-streaming for UI).

This separation allows a node to subscribe to streams without needing command
authority, and charging stations to run as independent processes.

---

## 11  Charging Station Protocol

### 11.1  Three-Phase Handshake

The charging protocol uses a three-phase handshake to avoid races when multiple
robots compete for a single dock:

1. **RequestSlot** — Robot sends `SlotRequest(robot_id, battery_level)`.
   Station returns `SlotOffer(station_id, slot_id, queue_rank, wait_time_s,
   dock_x, dock_y)`.  The offer is non-committing and expires after 15 s if
   not confirmed.

2. **ConfirmSlot** — Robot sends `SlotConfirm(robot_id, slot_id)`.  Station
   returns `SlotAssignment(granted, queue_rank, wait_x, wait_y, dock_x,
   dock_y)`.  The `wait_x/wait_y` position is where the robot should hold
   while queued; if `queue_rank == 0` the robot goes directly to the dock.

3. **ReleaseSlot** — Robot sends `SlotRelease(robot_id, slot_id)`.  Station
   returns `SlotReleaseAck(success)`.  Frees the dock and advances the queue.

### 11.2  Station Discovery

Charging stations register on mDNS under the `_chg-station._tcp.local.`
service type so robots and the UI discover them automatically (no hardcoded
addresses).

### 11.3  Queue Waiting Positions

The station computes a waiting position for each queued robot.  Robots line
up in a column extending **toward the centre of the arena** (never off the
map edge).  Spacing is ~8 arena units between positions, and the column
direction is chosen based on which axis has more room toward (50, 50).

### 11.4  StreamStatus (UI subscription)

The station provides a `StreamStatus` server-streaming RPC.  The UI subscribes
once and receives push updates on every state mutation (robot joins queue,
confirms, docks, releases).  Each response contains:

- `station_id`, `dock_x`, `dock_y`
- `dock_robot` — the robot currently on the dock (empty string if free)
- `queue` — repeated `QueueEntry(robot_id, rank, confirmed)`

---

## 12  Design Constraints & Conventions

- **Proto3 syntax** — all fields are implicitly optional; zero-values are
  defaults.
- **Enum zero-values** must represent "unknown" / "unset" to avoid ambiguity
  (e.g. `STATUS_UNKNOWN = 0`, `INTENT_UNKNOWN = 0`, `COMMAND_UNKNOWN = 0`).
- **Timestamps** are `int64` milliseconds since epoch.
- **Coordinates** are `double` (floating point), range 0–100 for the demo.
- The protocol should be **language-agnostic** — although the demo uses Python,
  the `.proto` file must compile cleanly for any gRPC-supported language.
- **No authentication or encryption** in the demo.  All channels are
  `insecure_channel` / `insecure_port`.
