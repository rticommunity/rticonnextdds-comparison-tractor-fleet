# Charging Station — Specification (the DDS approach: Pure DDS)

> This document fully specifies the `charging_station.py` application so that an
> AI agent (or developer) can re-create it from scratch.  It assumes the reader
> has access to the IDL type definitions (`robot_types.idl`), the auto-generated
> Python types (`robot_types.py`), and the QoS profiles (`robot_qos.xml`).

---

## 1  Purpose

A standalone DDS participant that manages a **single charging dock** for the
robot fleet.  Only one robot can charge at a time; additional robots queue up in
FIFO order and wait near the station until the dock becomes free.

The charging station is a separate process from the robot nodes.  Multiple
stations can run simultaneously (e.g. one at each corner of the arena) to
provide redundancy and reduce wait times.

**Key difference from the gRPC approach:** there is no gRPC server, no Zeroconf
advertisement, no mDNS service type.  The station is a DDS DomainParticipant —
robots discover it automatically via SPDP, and all slot negotiation uses
`rti.rpc.Replier` objects (`SlotRequest → SlotOffer`, `SlotConfirm →
SlotAssignment`, `SlotRelease → SlotReleaseAck`).  The station also publishes its live
queue state on a `StationStatus` topic for the UI.

**RPC pattern:** The robot's `Requester.send_request()` fans out to **all**
matching `Replier` instances.  Each station independently receives the request,
evaluates its queue, and sends a reply.  The robot collects all offers and
picks the best — a natural 1:N pattern that gRPC cannot do in a single call.

---

## 2  Technology Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.14.3+ |
| Middleware | RTI Connext DDS 7.7.0+ (`rti.connextdds`, `rti.idl`, `rti.rpc`) |
| Type generation | `rtiddsgen -language python robot_types.idl` → `robot_types.py` |
| QoS configuration | `robot_qos.xml` — Pattern-based profiles (`BuiltinQosLib::Pattern.*`) |
| Concurrency | Python `threading` (`threading.Lock`) |
| CLI | `argparse` |
| Data structures | `collections.OrderedDict` (preserves FIFO insertion order) |
| IDs | `uuid.uuid4()` (truncated to 8 chars for slot IDs) |
| Logging | `print()` with `[station_id HH:MM:SS]` prefix via `_ts()` helper |

No web framework, no gRPC, no Zeroconf, no external libraries beyond the RTI
Connext Python binding.

### Comparison with the gRPC approach

| Concern | the gRPC approach | the DDS approach |
|---------|-------------|-------------|
| RPC framework | gRPC (`grpcio`) | `rti.rpc` — `Replier` objects for request-reply |
| Discovery | Zeroconf mDNS (`zeroconf`, `ifaddr`) via `FleetDiscovery.register_station()` | DDS SPDP — zero code |
| Thread pool | `ThreadPoolExecutor(max_workers=10)` | Not needed — DDS manages internal threads |
| Status streaming | gRPC server-streaming RPC (`StreamStatus`) | DDS `StationStatus` topic (RELIABLE / TRANSIENT_LOCAL) |
| Status subscriber wake | `threading.Condition` variable + notify | DDS late-joiner convergence via `TRANSIENT_LOCAL` durability |
| Code generation | `protoc` → `robot_pb2.py` + `robot_pb2_grpc.py` | `rtiddsgen` → `robot_types.py` |

---

## 3  Command-Line Interface

```
python charging_station.py --id ID [--domain DOMAIN_ID] [--dock-x X] [--dock-y Y]
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--id` | str (required) | — | Station identifier (e.g. `station1`) |
| `--domain` | int | `0` | DDS Domain ID (must match robot nodes) |
| `--dock-x` | float | `50.0` | Dock X coordinate in the arena (0–100) |
| `--dock-y` | float | `5.0` | Dock Y coordinate in the arena (0–100) |

At startup `main()`:

1. Parses arguments.
2. Creates a `ChargingStation(station_id, domain_id, dock_x, dock_y)`.
3. Calls `run()` which initialises DDS, starts threads, and blocks.

**Comparison with the gRPC approach:** No `--port` flag — DDS doesn't need an explicit
listen port.  No Zeroconf registration — DDS SPDP discovery is automatic.

---

## 4  `ChargingStation` Class — State

### 4.1  Identity

| Attribute | Type | Description |
|-----------|------|-------------|
| `station_id` | `str` | Station name (from `--id`) |
| `domain_id` | `int` | DDS domain (from `--domain`) |
| `dock_x` | `float` | Dock X coordinate |
| `dock_y` | `float` | Dock Y coordinate |
| `running` | `bool` | Master flag; `False` → all threads exit |

### 4.2  Queue State (protected by `self.lock`)

| Attribute | Type | Description |
|-----------|------|-------------|
| `_queue` | `OrderedDict[str, dict]` | `slot_id → {"robot_id": str, "confirmed": bool, "ts": float}`.  Insertion order = FIFO rank. |
| `_charging` | `str` or `None` | `slot_id` of the robot currently on the dock.  `None` when dock is free. |
| `lock` | `threading.Lock` | Guards all queue state |

### 4.3  DDS Entities

| Attribute | Type | Description |
|-----------|------|-------------|
| `participant` | `dds.DomainParticipant` or `None` | The single DDS participant for this station |
| `writers` | `dict[str, dds.DataWriter]` | Pub-sub writers — only `StationStatus` (see §6) |
| `slot_replier` | `rti.rpc.Replier` or `None` | Receives `SlotRequest`, sends `SlotOffer` |
| `confirm_replier` | `rti.rpc.Replier` or `None` | Receives `SlotConfirm`, sends `SlotAssignment` |
| `release_replier` | `rti.rpc.Replier` or `None` | Receives `SlotRelease`, sends `SlotReleaseAck` |

### 4.4  Constants

| Name | Value | Description |
|------|-------|-------------|
| `CHARGE_DURATION_ESTIMATE` | `45` s | Conservative estimate for wait-time calculations |
| `STALE_OFFER_TIMEOUT` | `15` s | Unconfirmed offers expire after this |

**Comparison with the gRPC approach:** No `_status_cv` (`threading.Condition`) — DDS
handles push delivery to UI subscribers.  No gRPC server reference.

---

## 5  Queue Model

The queue is an `OrderedDict` where:

- **Keys** are `slot_id` strings (8-char UUID fragments).
- **Values** are dicts `{"robot_id": str, "confirmed": bool, "ts": float}`.
- **Insertion order** determines rank: first entry = rank 0, second = rank 1, etc.

A slot goes through these states:

1. **Offered** (unconfirmed): Created when `SlotRequest` is received.
   `confirmed = False`.  Expires after 15 s if not confirmed.
2. **Confirmed**: Set when `SlotConfirm` is received.  `confirmed = True`.
   Robot is committed to this station.
3. **Docking**: When a confirmed slot reaches rank 0 and the dock is free,
   `_charging` is set to this slot ID.
4. **Released**: Removed when `SlotRelease` is received.  Dock is freed and
   the next confirmed robot advances.

Identical queue model to the gRPC approach — same states, same semantics.

---

## 6  DDS Initialisation

### 6.1  QoS Provider

1. Resolve the path to `robot_qos.xml` relative to the script.
2. Create a `dds.QosProvider(qos_xml)`.

### 6.2  Participant and Infrastructure

1. Create a `dds.DomainParticipant(domain_id)`.
2. Create one `dds.Publisher` and one `dds.Subscriber`.

### 6.3  Topics (pub-sub only)

| Topic Name | Type | Direction | QoS Profile |
|------------|------|-----------|-------------|
| `StationStatus` | `StationStatus` | Write | `RobotQosLibrary::StationStatusProfile` |

Slot negotiation topics are **not** created manually — the `rti.rpc.Replier`
objects create their own internal DataWriters and DataReaders automatically,
derived from the `service_name`.

### 6.4  Pub-Sub Writers (1 total)

| Dict Key | Topic | QoS Profile |
|----------|-------|-------------|
| `station_status` | `StationStatus` | `RobotQosLibrary::StationStatusProfile` |

### 6.5  RPC Objects — Slot Negotiation Repliers

Three `Replier` objects, one per request-reply pair.  The `service_name` for
each must match the corresponding `Requester` in `robot_node.py`:

```python
self.slot_replier = rti.rpc.Replier(
    request_type=SlotRequest,
    reply_type=SlotOffer,
    participant=self.participant,
    service_name="ChargingSlotRequest",
)

self.confirm_replier = rti.rpc.Replier(
    request_type=SlotConfirm,
    reply_type=SlotAssignment,
    participant=self.participant,
    service_name="ChargingSlotConfirm",
)

self.release_replier = rti.rpc.Replier(
    request_type=SlotRelease,
    reply_type=SlotReleaseAck,
    participant=self.participant,
    service_name="ChargingSlotRelease",
)
```

Each `Replier` creates an internal DataReader (for receiving requests) and an
internal DataWriter (for sending replies).  Reply correlation is handled
automatically — the station calls `replier.send_reply(reply_data, request_info)`
where `request_info` is the `SampleInfo` from the received request.

### 6.6  Request Processing Threads

Each `Replier` needs a processing loop that takes incoming requests and calls
the handler.  These run in daemon threads:

- **`_process_slot_requests()`** — loops on `slot_replier.receive_requests()`
  → calls `_handle_slot_request(request_data)` → calls
  `slot_replier.send_reply(offer, request_info)`.
- **`_process_slot_confirms()`** — loops on `confirm_replier.receive_requests()`
  → calls `_handle_slot_confirm(request_data)` → calls
  `confirm_replier.send_reply(assignment, request_info)`.
- **`_process_slot_releases()`** — loops on `release_replier.receive_requests()`
  → calls `_handle_slot_release(request_data)` → calls
  `release_replier.send_reply(ack, request_info)`.

Each loop uses `receive_requests(max_wait=Duration.from_seconds(1))` with a
1-second timeout so the thread can check `self.running` and exit cleanly on
shutdown.

**Comparison with the gRPC approach:** The gRPC approach defines a single
`ChargingStationServicer` class with 4 gRPC methods.  The DDS approach uses 3
`rti.rpc.Replier` objects with processing threads — same handler functions,
but the replies are sent via `Replier.send_reply()` instead of being
returned as gRPC return values.  No `StreamStatus` streaming RPC — the
`StationStatus` topic with `TRANSIENT_LOCAL` durability handles late-joiner
convergence automatically.

---

## 7  Slot Request Handling (`_handle_slot_request`)

Called by the `_process_slot_requests()` thread when a robot's `Requester`
sends a `SlotRequest`.  Returns a `SlotOffer` that the processing thread
sends back via `slot_replier.send_reply(offer, request_info)`.

Non-committing inquiry.  The robot asks "how long would I wait?"

1. Acquire `self.lock`.
2. Expire stale (unconfirmed, > 15 s old) offers via `_expire_stale_offers()`.
3. If `info_only == True`: compute rank and wait time without creating a slot.
   Return `SlotOffer` with `slot_id = ""` (informational only).
4. Generate a unique `slot_id` (8-char UUID).
5. Add to `_queue` as unconfirmed: `{robot_id, confirmed=False, ts=now}`.
6. Compute `queue_rank` = current position in the queue (0-based).
7. Compute `wait_time_s` = `rank × CHARGE_DURATION_ESTIMATE`.
8. Release lock.
9. Return `SlotOffer`:
   ```python
   SlotOffer(
       station_id=self.station_id,
       slot_id=slot_id,
       queue_rank=queue_rank,
       wait_time_s=wait_time_s,
       dock_x=self.dock_x,
       dock_y=self.dock_y,
   )
   ```
10. After `send_reply()`, the processing thread publishes updated `StationStatus`.

The robot's `Requester` collects offers from **all** stations (fan-out) and
chooses the best.  Un-chosen offers expire or are explicitly released.

**Comparison with the gRPC approach `RequestSlot`:** Same logic.  The difference is
that the gRPC approach returns a `SlotOffer` as a gRPC return value to a specific
client.  The DDS approach returns it from the handler and `Replier.send_reply()`
delivers it to the requesting `Requester`.  The fan-out to multiple stations is
automatic — each station's `Replier` independently receives the same request
and replies.  The `info_only` flag is new in the DDS approach — the gRPC approach doesn't have
it.

---

## 8  Slot Confirm Handling (`_handle_slot_confirm`)

Called by the `_process_slot_confirms()` thread when a robot's `Requester`
sends a `SlotConfirm`.  Returns a `SlotAssignment` that the processing thread
sends back via `confirm_replier.send_reply(assignment, request_info)`.

Commits the robot to the queue.

1. Acquire `self.lock`.
2. Look up `slot_id` in `_queue`.  Verify `robot_id` matches.
   - If not found: return `SlotAssignment(granted=False)`.
3. Set `confirmed = True`.
4. Compute `queue_rank`.
5. **If rank 0 and dock is free** (`_charging is None`):
   - Set `_charging = slot_id`.
   - Return `SlotAssignment(granted=True, queue_rank=0, wait_x=dock_x,
     wait_y=dock_y, dock_x=dock_x, dock_y=dock_y)`.
   - The robot should navigate directly to the dock.
6. **Otherwise** (waiting):
   - Compute waiting position via `_wait_position(rank)`.
   - Return `SlotAssignment(granted=True, queue_rank=rank, wait_x=wx,
     wait_y=wy, dock_x=dock_x, dock_y=dock_y)`.
7. Release lock.
8. After `send_reply()`, the processing thread publishes updated `StationStatus`.

The `SlotAssignment` is returned from the handler.  The processing thread
passes it to `confirm_replier.send_reply(assignment, request_info)` — the
`Replier` automatically correlates it to the robot's `Requester` via
`SampleIdentity`.  No application-level correlation field is needed — the DDS
writer GUID and sequence number handle it automatically.

`_handle_slot_confirm` is **idempotent** for already-confirmed slots — calling
it again simply returns the current rank and coordinates.  The robot's poll
thread uses this to check for rank changes.

**Comparison with the gRPC approach `ConfirmSlot`:** Same logic, same idempotency.

---

## 9  Slot Release Handling (`_handle_slot_release`)

Called by the `_process_slot_releases()` thread when a robot's `Requester`
sends a `SlotRelease`.  Returns a `SlotReleaseAck` that the processing thread sends
back via `release_replier.send_reply(ack, request_info)`.

Robot finished charging or wants to cancel.

1. Acquire `self.lock`.
2. Look up `slot_id` in `_queue`.
   - If not found: return `SlotReleaseAck(success=True)` anyway (idempotent).
3. Remove the entry from `_queue`.
4. If `_charging == slot_id`:
   - Set `_charging = None`.
   - Call `_advance_queue()` to promote the next confirmed robot.
5. Release lock.
6. Return `SlotReleaseAck`:
   ```python
   SlotReleaseAck(success=True)
   ```
7. After `send_reply()`, the processing thread publishes updated `StationStatus`.

**Comparison with the gRPC approach `ReleaseSlot`:** Same logic.

---

## 10  StationStatus Publishing

The station publishes its current state on the `StationStatus` topic
periodically (every 5 s) and on every state mutation (slot request, confirm,
release).

The QoS profile `StationStatusProfile` inherits from `Pattern.Status`:
- `RELIABLE` — guaranteed delivery.
- `TRANSIENT_LOCAL` — late-joining UI immediately gets the current state.
- `KEEP_LAST depth=1` — only the latest state matters.

### StationStatus Sample

```python
StationStatus(
    station_id=self.station_id,
    dock_x=self.dock_x,
    dock_y=self.dock_y,
    is_occupied=(self._charging is not None),
    docked_robot_id=docked_robot_id,  # "" if dock is free
    queue=[
        QueueEntry(
            robot_id=entry["robot_id"],
            rank=rank,
            confirmed=entry["confirmed"],
        )
        for rank, (slot_id, entry) in enumerate(self._queue.items())
    ],
)
```

### `publish_station_status` Thread

A daemon thread that publishes `StationStatus` every 5 s as a heartbeat,
ensuring the topic stays fresh even when no mutations occur.

The station also calls `_publish_status()` immediately after every state
mutation (in `_handle_slot_request`, `_handle_slot_confirm`,
`_handle_slot_release`).

**Comparison with the gRPC approach `StreamStatus`:** The gRPC approach uses a gRPC
server-streaming RPC with a `threading.Condition` to wake subscribers on each
state mutation.  The DDS approach publishes on a DDS topic — subscribers receive data
automatically via multicast.  Late joiners get the current state immediately
from `TRANSIENT_LOCAL` durability, with no explicit "initial snapshot" logic.

---

## 11  Queue Waiting Positions

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

Identical algorithm to the gRPC approach.

---

## 12  Stale Offer Expiration

`_expire_stale_offers()` removes unconfirmed offers older than
`STALE_OFFER_TIMEOUT` (15 s).

Called:
- At the start of `_handle_slot_request`.
- On each `publish_station_status` heartbeat.

This prevents abandoned offers from blocking the queue indefinitely.

Same logic as the gRPC approach.

---

## 13  Queue Advancement

`_advance_queue()` is called after a robot releases the dock.

Iterates `_queue` in insertion order.  The first **confirmed** slot is promoted
to `_charging`.  Unconfirmed slots are skipped (they haven't committed yet).

The promoted robot's poll thread will detect `queue_rank == 0` on its next
`SlotConfirm` and navigate to the dock.

Same logic as the gRPC approach.

---

## 14  Thread Summary

| Thread | Daemon | Description | Created by |
|--------|--------|-------------|------------|
| Main | No | `run()` → blocks on `sleep(1)` loop | `main()` |
| `publish_station_status` | Yes | Publishes StationStatus every 5 s | `run()` |
| `_process_slot_requests` | Yes | Loops on `slot_replier.receive_requests()` → `_handle_slot_request()` → `send_reply()` | `run()` |
| `_process_slot_confirms` | Yes | Loops on `confirm_replier.receive_requests()` → `_handle_slot_confirm()` → `send_reply()` | `run()` |
| `_process_slot_releases` | Yes | Loops on `release_replier.receive_requests()` → `_handle_slot_release()` → `send_reply()` | `run()` |
| DDS internal threads | — | Receive, event, database (~3 threads) | `DomainParticipant()` |

**Total: ~8 threads** (1 main + 1 status publisher + 3 replier processors +
~3 DDS internal).

The station is still simpler than a robot node — it has no movement, no peers,
no publishing threads beyond status.  The 3 replier processing threads each
block on `receive_requests()`, consuming near-zero CPU when idle.

**Comparison with the gRPC approach:** The gRPC approach uses a gRPC thread pool of 10 threads,
with `StreamStatus` handlers blocking on a condition variable inside the pool.
The DDS approach has 3 replier processing threads + 1 status publisher + DDS
internals — no thread pool, no condition variable.

---

## 15  Resilience Behaviour

| Scenario | Behaviour |
|----------|-----------|
| Robot crashes mid-charge | The robot's `SlotRelease` never arrives.  The station can detect the loss via DDS liveliness (if a KinematicState reader is added) or rely on the next robot's `RequestSlot` to trigger stale-offer expiration.  If the crashed robot held `_charging`, a manual cleanup or timeout mechanism is needed (future enhancement). |
| Robot disappears from queue | Stale-offer expiration removes unconfirmed slots after 15 s.  Confirmed slots persist until explicitly released or the station is restarted. |
| Station restart | All queue state is lost.  Robots with in-flight charge workflows will time out on `Requester.wait_for_replies()` (5 s) and stay on their current task. |
| Network blip | DDS internally buffers reliable samples and retransmits.  Slot negotiation messages are RELIABLE — no message loss. |
| Multiple stations | Each station manages its own queue independently.  Robots compare `SlotOffer`s from all responding stations and choose the best (lowest wait time / closest). |
| Late-joining UI | `StationStatus` uses `TRANSIENT_LOCAL` durability — the UI receives the current state immediately on discovery. |

---

## 16  Type Contract Summary

The station uses the following types from `robot_types.py` (generated by
`rtiddsgen -language python robot_types.idl`):

### Types Read (via `rti.rpc.Replier` objects)

| Type | Key | Fields Used |
|------|-----|-------------|
| `SlotRequest` | — | `robot_id`, `battery_level`, `info_only` |
| `SlotConfirm` | — | `robot_id`, `slot_id` |
| `SlotRelease` | — | `robot_id`, `slot_id` |

### Types Written (via `rti.rpc.Replier` objects + pub-sub)

| Type | Key | Fields Written | Mechanism |
|------|-----|----------------|-----------|
| `SlotOffer` | — | `station_id`, `slot_id`, `queue_rank`, `wait_time_s`, `dock_x`, `dock_y` | `slot_replier.send_reply()` |
| `SlotAssignment` | — | `granted`, `queue_rank`, `wait_x`, `wait_y`, `dock_x`, `dock_y` | `confirm_replier.send_reply()` |
| `SlotReleaseAck` | — | `success` | `release_replier.send_reply()` |
| `StationStatus` | `station_id` | `dock_x`, `dock_y`, `is_occupied`, `docked_robot_id`, `queue[]` | `writers['station_status'].write()` |
| `QueueEntry` | — | `robot_id`, `rank`, `confirmed` (nested in StationStatus.queue) | (nested) |

### Comparison with the gRPC approach proto contract

| Aspect | the gRPC approach (protobuf) | the DDS approach (DDS IDL-XML) |
|--------|------------------------|---------------------------|
| Service definition | `ChargingStation` service with 4 RPCs | Not needed — 3 `rti.rpc.Replier` objects + 1 status topic |
| Request/response pattern | gRPC unary RPC (synchronous return) | `Replier.receive_requests()` → handler → `Replier.send_reply()` (asynchronous) |
| Fan-out | Robot must call each station individually (N gRPC calls) | Robot's `Requester.send_request()` reaches all stations; each `Replier` responds independently |
| Correlation | gRPC manages per-call (implicit) | `rti.rpc` manages via `SampleIdentity` (implicit) |
| Status streaming | `StreamStatus` server-streaming RPC | `StationStatus` topic (RELIABLE / TRANSIENT_LOCAL) |
| `StationStatusRequest` | Empty message (gRPC parameter) | Does not exist — subscribers just subscribe to the topic |
| `QueueEntry` | Nested in `StationStatusResponse` | Nested in `StationStatus` (same semantics) |
| `info_only` on SlotRequest | Not present | Present — allows robots to query without creating a slot |

---

## 17  File Structure

The application is a **single Python file** (`charging_station.py`) that:

1. Imports DDS types from `robot_types.py` (generated — never hand-edited).
2. Defines a `_ts()` timestamp helper.
3. Defines `CHARGE_DURATION_ESTIMATE` and `STALE_OFFER_TIMEOUT` constants.
4. Defines `ChargingStation`:
   - State attributes (§4)
   - `initialize_dds()` (§6) — creates participant, `StationStatus` writer,
     and 3 `rti.rpc.Replier` objects
   - `_process_slot_requests()`, `_process_slot_confirms()`,
     `_process_slot_releases()` (§6.6) — replier processing loops
   - `_handle_slot_request()` (§7) — processes slot requests, returns offers
   - `_handle_slot_confirm()` (§8) — confirms slots, returns assignments
   - `_handle_slot_release()` (§9) — releases slots, returns acks
   - `_publish_status()` (§10) — publishes StationStatus sample
   - `publish_station_status()` (§10) — periodic heartbeat thread
   - `_wait_position()` (§11) — computes queue waiting positions
   - `_expire_stale_offers()` (§12) — removes old unconfirmed offers
   - `_advance_queue()` (§13) — promotes next robot after dock release
   - `run()` — main loop
5. Defines `main()` — CLI parsing, creates `ChargingStation`, calls `run()`.

**No servicer class.** The gRPC approach defines a `ChargingStationServicer` class
registered on a gRPC server.  In DDS, incoming requests are handled by listener
callbacks — the `ChargingStation` class is self-contained.

### Companion files

| File | Purpose |
|------|---------|
| `robot_types.idl` | DDS IDL type definitions — single source of truth |
| `robot_types.py` | Auto-generated Python types (`rtiddsgen -language python robot_types.idl`) |
| `robot_qos.xml` | QoS profiles — Pattern-based (`BuiltinQosLib::Pattern.*`) |

### External Dependencies

| Package | Use |
|---------|-----|
| `rti.connextdds` | DDS middleware — participant, topics, writers, readers |
| `rti.rpc` | Request-reply — `Replier` objects for slot negotiation |
| `rti.idl` | DDS type system — `@idl.struct`, `@idl.enum`, `idl.key`, etc. |

That's it.  No `grpcio`, no `zeroconf`, no `ifaddr`.

### What Doesn't Exist (vs the gRPC approach)

| the gRPC approach Code | Purpose | Why it doesn't exist in the DDS approach |
|------------------|---------|-----------------------------------|
| `fleet_discovery.py` import | Zeroconf mDNS station advertisement | DDS SPDP — zero code |
| `FleetDiscovery.register_station()` | mDNS service registration | DDS automatic discovery |
| `grpc.server(ThreadPoolExecutor)` | gRPC server + thread pool | `rti.rpc.Replier` objects handle request-reply |
| `ChargingStationServicer` class | gRPC servicer with 4 RPC methods | Handler functions called by Replier processing threads |
| `_status_cv` (Condition variable) | Wake `StreamStatus` subscribers on mutation | DDS TRANSIENT_LOCAL delivers latest value automatically |
| `StreamStatus` RPC | Server-streaming status to UI | `StationStatus` topic publish |
| Manual `request_id` field | Correlate request with reply | `rti.rpc` correlates via DDS `SampleIdentity` (writer GUID + sequence number) |

---

## 18  Deployment

Charging stations are launched by `demo_start.sh` before the robot nodes.
Typical configuration:

```bash
# Station 1 — top-left corner
python charging_station.py --id station1 --dock-x 8 --dock-y 92 &

# Station 2 — bottom-right corner
python charging_station.py --id station2 --dock-x 92 --dock-y 8 &
```

Each station joins the DDS domain immediately.  Robots discover them via SPDP
within seconds of starting — no registration, no hardcoded addresses.

**Comparison with the gRPC approach:** The gRPC approach needs explicit `--port` flags (50060,
50061) and Zeroconf advertisement.  The DDS approach needs no port — DDS handles
transport automatically.
