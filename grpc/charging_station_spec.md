# Charging Station — Specification

> This document fully specifies the `charging_station.py` application so that an
> AI agent (or developer) can re-create it from scratch.  It assumes the reader
> has access to the `robot.proto` service/message definitions and the generated
> Python stubs (`robot_pb2.py`, `robot_pb2_grpc.py`).

---

## 1  Purpose

A standalone gRPC service that manages a **single charging dock** for the robot
fleet.  Only one robot can charge at a time; additional robots queue up in FIFO
order and wait near the station until the dock becomes free.

The charging station is a separate process from the robot nodes.  Multiple
stations can run simultaneously (e.g. one at each corner of the arena) to
provide redundancy and reduce wait times.

---

## 2  Technology Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.14.3+ |
| RPC framework | gRPC (`grpcio`, `grpcio-tools`, `protobuf`) |
| Concurrency | Python `threading` (`threading.Lock`, `threading.Condition`) |
| Thread pool | `concurrent.futures.ThreadPoolExecutor` (`max_workers=10`) |
| CLI | `argparse` |
| Discovery | `FleetDiscovery.register_station()` — Zeroconf mDNS/DNS-SD |
| Data structures | `collections.OrderedDict` (preserves FIFO insertion order) |
| IDs | `uuid.uuid4()` (truncated to 8 chars for slot IDs) |
| Logging | `print()` with `[station_id HH:MM:SS]` prefix via `_ts()` helper |

No web framework, no external libraries beyond the gRPC/protobuf stack and
Zeroconf.

---

## 3  Command-Line Interface

```
python charging_station.py --port PORT --station-id ID --dock-x X --dock-y Y
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--port` | int | `50060` | gRPC listen port |
| `--station-id` | str | `"station1"` | Human-readable station identifier |
| `--dock-x` | float | `50.0` | Dock X coordinate in the arena (0–100) |
| `--dock-y` | float | `5.0` | Dock Y coordinate in the arena (0–100) |

At startup `main()`:

1. Parses arguments.
2. Creates a `grpc.server` with `ThreadPoolExecutor(max_workers=10)`.
3. Creates a `ChargingStationServicer(station_id, dock_x, dock_y)`.
4. Registers the servicer on the gRPC server.
5. Binds to `[::]:<port>` (insecure).
6. Starts the server.
7. Registers on mDNS via `FleetDiscovery.register_station(station_id, port)` so
   robots and the UI can discover this station automatically.
8. Blocks on `server.wait_for_termination()`.

---

## 4  `ChargingStationServicer` Class — State

### 4.1  Identity

| Attribute | Type | Description |
|-----------|------|-------------|
| `station_id` | `str` | Station name (from `--station-id`) |
| `dock_x` | `float` | Dock X coordinate |
| `dock_y` | `float` | Dock Y coordinate |

### 4.2  Queue State (protected by `self.lock`)

| Attribute | Type | Description |
|-----------|------|-------------|
| `_queue` | `OrderedDict[str, dict]` | `slot_id → {"robot_id": str, "confirmed": bool, "ts": float}`.  Insertion order = FIFO rank. |
| `_charging` | `str` or `None` | `slot_id` of the robot currently on the dock.  `None` when dock is free. |
| `lock` | `threading.Lock` | Guards all queue state |
| `_status_cv` | `threading.Condition(lock)` | Notified on every state mutation; wakes `StreamStatus` subscribers |

### 4.3  Constants

| Name | Value | Description |
|------|-------|-------------|
| `CHARGE_DURATION_ESTIMATE` | `45` s | Conservative estimate for wait-time calculations |

---

## 5  Queue Model

The queue is an `OrderedDict` where:

- **Keys** are `slot_id` strings (8-char UUID fragments).
- **Values** are dicts `{"robot_id": str, "confirmed": bool, "ts": float}`.
- **Insertion order** determines rank: first entry = rank 0, second = rank 1, etc.

A slot goes through these states:

1. **Offered** (unconfirmed): Created by `RequestSlot`.  `confirmed = False`.
   Expires after 15 s if not confirmed.
2. **Confirmed**: Set by `ConfirmSlot`.  `confirmed = True`.  Robot is committed
   to this station.
3. **Docking**: When a confirmed slot reaches rank 0 and the dock is free,
   `_charging` is set to this slot ID.
4. **Released**: Removed by `ReleaseSlot`.  Dock is freed and the next confirmed
   robot advances.

---

## 6  RPCs

### 6.1  `RequestSlot(SlotRequest) → SlotOffer`

Non-committing inquiry.  The robot asks "how long would I wait?"

1. Expire stale (unconfirmed, > 15 s old) offers.
2. Generate a unique `slot_id` (8-char UUID).
3. Add to `_queue` as unconfirmed: `{robot_id, confirmed=False, ts=now}`.
4. Compute `queue_rank` = current position in the queue (0-based).
5. Compute `wait_time_s` = `rank × CHARGE_DURATION_ESTIMATE`.
6. Notify `StreamStatus` subscribers.
7. Return `SlotOffer(station_id, slot_id, queue_rank, wait_time_s, dock_x, dock_y)`.

The robot can compare offers from multiple stations and choose the best.
Un-chosen offers expire or are explicitly released.

### 6.2  `ConfirmSlot(SlotConfirm) → SlotAssignment`

Commits the robot to the queue.

1. Look up `slot_id` in `_queue`.  Verify `robot_id` matches.
2. Set `confirmed = True`.
3. Compute `queue_rank`.
4. **If rank 0 and dock is free** (`_charging is None`):
   - Set `_charging = slot_id`.
   - Return `SlotAssignment(granted=True, queue_rank=0, wait_x=dock_x,
     wait_y=dock_y, dock_x=dock_x, dock_y=dock_y)`.
   - The robot should navigate directly to the dock.
5. **Otherwise** (waiting):
   - Compute waiting position via `_wait_position(rank)`.
   - Return `SlotAssignment(granted=True, queue_rank=rank, wait_x=wx,
     wait_y=wy, dock_x=dock_x, dock_y=dock_y)`.

`ConfirmSlot` is **idempotent** for already-confirmed slots — calling it again
simply returns the current rank and coordinates.  The robot's poll thread uses
this to check for rank changes.

### 6.3  `ReleaseSlot(SlotRelease) → SlotReleaseAck`

Robot finished charging or wants to cancel.

1. Look up `slot_id` in `_queue`.
2. Remove the entry from `_queue`.
3. If `_charging == slot_id`:
   - Set `_charging = None`.
   - Call `_advance_queue()` to promote the next confirmed robot.
4. Notify `StreamStatus` subscribers.
5. Return `SlotReleaseAck(success=True)`.

### 6.4  `StreamStatus(StationStatusRequest) → stream of StationStatusResponse`

Server-streaming RPC.  The UI subscribes once and receives push updates.

1. Send an **initial snapshot** immediately on connect.
2. Loop while `context.is_active()`:
   a. Expire stale offers.
   b. Build and yield a `StationStatusResponse`.
   c. Wait on the condition variable (timeout 5 s as heartbeat).

Each `StationStatusResponse` contains:

| Field | Description |
|-------|-------------|
| `station_id` | Station identifier |
| `dock_x`, `dock_y` | Dock coordinates |
| `dock_robot` | Robot ID currently on the dock (empty string if free) |
| `queue` | Repeated `QueueEntry(robot_id, rank, confirmed)` |

---

## 7  Queue Waiting Positions

`_wait_position(rank)` computes where a queued robot should hold while waiting.

**Algorithm**: Robots line up in a column extending **toward the centre of the
arena** (50, 50), so the queue never extends off the map edge.

1. `spacing = 8` arena units between positions.
2. `offset = 10 + rank × spacing` — distance from dock.
3. Determine direction on each axis: `dx = 1 if dock_x < 50 else -1`,
   `dy = 1 if dock_y < 50 else -1`.
4. Choose the axis with **more room** toward the centre:
   - If `|50 - dock_x| ≥ |50 - dock_y|` → queue along X:
     `(dock_x + dx × offset, dock_y)`.
   - Otherwise → queue along Y: `(dock_x, dock_y + dy × offset)`.

**Examples**:
- Station at (8, 92) — top-left: queue extends rightward (+X) →
  positions at (18, 92), (26, 92), (34, 92), …
- Station at (92, 8) — bottom-right: queue extends leftward (−X) →
  positions at (82, 8), (74, 8), (66, 8), …

---

## 8  Stale Offer Expiration

`_expire_stale_offers()` removes unconfirmed offers older than 15 seconds.

Called at the start of `RequestSlot` and on each `StreamStatus` heartbeat.
This prevents abandoned offers from blocking the queue indefinitely.

---

## 9  Queue Advancement

`_advance_queue()` is called after a robot releases the dock.

Iterates `_queue` in insertion order.  The first **confirmed** slot is promoted
to `_charging`.  Unconfirmed slots are skipped (they haven't committed yet).

The promoted robot's poll thread will detect `queue_rank == 0` on its next
`ConfirmSlot` call and navigate to the dock.

---

## 10  mDNS Discovery

The station registers itself on mDNS under the `_chg-station._tcp.local.`
service type using `FleetDiscovery.register_station(station_id, port)`.

This allows:
- **Robots** to discover station addresses and call `RequestSlot`.
- **The UI** to discover station addresses and subscribe to `StreamStatus`.

No hardcoded station addresses are needed anywhere in the fleet.

---

## 11  Thread Summary

| Thread | Description |
|--------|-------------|
| Main | Blocks on `server.wait_for_termination()` |
| gRPC server pool (10) | Handles incoming RPCs and `StreamStatus` subscriptions |

The station is much simpler than a robot node — it has no movement, no peers,
and no background work beyond serving RPCs.  The `StreamStatus` handler blocks
on a condition variable inside the gRPC thread pool, waking on state mutations
or every 5 s for heartbeat.

---

## 12  Proto Contract Summary

The station implements the following from `robot.proto`:

### Service implemented

- `ChargingStation.RequestSlot` (unary: `SlotRequest` → `SlotOffer`)
- `ChargingStation.ConfirmSlot` (unary: `SlotConfirm` → `SlotAssignment`)
- `ChargingStation.ReleaseSlot` (unary: `SlotRelease` → `SlotReleaseAck`)
- `ChargingStation.StreamStatus` (server-streaming: `StationStatusRequest` → stream of `StationStatusResponse`)

### Key Messages

- `SlotRequest`: `robot_id` (string), `battery_level` (double)
- `SlotOffer`: `station_id`, `slot_id`, `queue_rank`, `wait_time_s`, `dock_x`, `dock_y`
- `SlotConfirm`: `robot_id`, `slot_id`
- `SlotAssignment`: `granted` (bool), `queue_rank`, `wait_x`, `wait_y`, `dock_x`, `dock_y`
- `SlotRelease`: `robot_id`, `slot_id`
- `SlotReleaseAck`: `success` (bool)
- `StationStatusRequest`: (empty)
- `StationStatusResponse`: `station_id`, `dock_x`, `dock_y`, `dock_robot`, `queue` (repeated `QueueEntry`)
- `QueueEntry`: `robot_id`, `rank`, `confirmed`

---

## 13  File Structure

The application is a **single Python file** (`charging_station.py`) that:

1. Defines a `_ts()` timestamp helper.
2. Defines `CHARGE_DURATION_ESTIMATE` constant.
3. Imports `robot_pb2` and `robot_pb2_grpc` (generated from `robot.proto`).
4. Imports `FleetDiscovery` from `fleet_discovery.py`.
5. Defines `ChargingStationServicer` (all queue logic and RPCs).
6. Defines `main()` parsing CLI args, creating the server, and running it.

### Companion files

| File | Purpose |
|------|---------|
| `fleet_discovery.py` | `FleetDiscovery` class — Zeroconf mDNS/DNS-SD station registration |

### External Dependencies

| Package | Use |
|---------|-----|
| `grpcio` / `grpcio-tools` / `protobuf` | RPC framework and code generation |
| `zeroconf` | mDNS/DNS-SD station advertisement |
| `ifaddr` | LAN IP selection inside `fleet_discovery.py` |

---

## 14  Deployment

Charging stations are launched by `fleet_config.sh` (or `run_demo.sh`) before
the robot nodes.  Typical configuration:

```bash
# Station 1 — top-left corner
python charging_station.py --port 50060 --station-id station1 --dock-x 8 --dock-y 92 &

# Station 2 — bottom-right corner
python charging_station.py --port 50061 --station-id station2 --dock-x 92 --dock-y 8 &
```

Each station advertises on mDNS immediately.  Robots and the UI discover them
within seconds of starting.
