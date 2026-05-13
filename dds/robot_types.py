
# WARNING: THIS FILE IS AUTO-GENERATED. DO NOT MODIFY.

# This file was generated from robot_types.idl
# using RTI Code Generator (rtiddsgen) version 4.7.0.
# The rtiddsgen tool is part of the RTI Connext DDS distribution.
# For more information, type 'rtiddsgen -help' at a command shell
# or consult the Code Generator User's Manual.

from dataclasses import field
from typing import Union, Sequence, Optional
import rti.idl as idl
import rti.rpc as rpc
from enum import IntEnum
import sys
import os
from abc import ABC



@idl.enum
class RobotStatus(IntEnum):
    STATUS_UNKNOWN = 0
    STATUS_MOVING = 1
    STATUS_IDLE = 2
    STATUS_HOLDING = 3
    STATUS_CHARGING = 4

@idl.enum
class RobotIntentType(IntEnum):
    INTENT_UNKNOWN = 0
    INTENT_FOLLOW_PATH = 1
    INTENT_GOTO = 2
    INTENT_IDLE = 3
    INTENT_CHARGE_QUEUE = 4
    INTENT_CHARGE_DOCK = 5

@idl.enum
class Command(IntEnum):
    COMMAND_UNKNOWN = 0
    CMD_STOP = 1
    CMD_FOLLOW_PATH = 2
    CMD_GOTO = 3
    CMD_RESUME = 4
    CMD_SET_PATH = 5
    CMD_CHARGE = 6

@idl.struct
class Position:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

@idl.struct
class Velocity3:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

@idl.struct
class Waypoint:
    x: float = 0.0
    y: float = 0.0

@idl.struct(

    member_annotations = {
        'robot_id': [idl.key, idl.bound(64),],
    }
)
class KinematicState:
    robot_id: str = ""
    position: Position = field(default_factory = Position)
    velocity: Velocity3 = field(default_factory = Velocity3)
    heading: float = 0.0

@idl.struct(

    member_annotations = {
        'robot_id': [idl.key, idl.bound(64),],
        'status': [idl.default(0),],
    }
)
class OperationalState:
    robot_id: str = ""
    status: RobotStatus = RobotStatus.STATUS_UNKNOWN
    battery_level: float = 0.0

@idl.struct(

    member_annotations = {
        'robot_id': [idl.key, idl.bound(64),],
        'intent_type': [idl.default(0),],
        'path_waypoints': [idl.bound(64),],
    }
)
class Intent:
    robot_id: str = ""
    intent_type: RobotIntentType = RobotIntentType.INTENT_UNKNOWN
    target_position: Position = field(default_factory = Position)
    path_waypoints: Sequence[Waypoint] = field(default_factory = list)
    path_index: idl.int32 = 0

@idl.struct(

    member_annotations = {
        'robot_id': [idl.key, idl.bound(64),],
    }
)
class Telemetry:
    robot_id: str = ""
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    temperature: float = 0.0
    signal_strength: float = 0.0

@idl.struct(

    member_annotations = {
        'robot_id': [idl.key, idl.bound(64),],
        'frame_data': [idl.bound(1000000),],
    }
)
class VideoFrame:
    robot_id: str = ""
    frame_data: Sequence[idl.octet] = field(default_factory = idl.array_factory(idl.octet))

@idl.struct
class GotoParameters:
    x: float = 0.0
    y: float = 0.0
    speed: float = 0.0

@idl.struct(

    member_annotations = {
        'waypoints': [idl.bound(64),],
    }
)
class SetPathParameters:
    waypoints: Sequence[Waypoint] = field(default_factory = list)

@idl.struct(

    member_annotations = {
        'robot_id': [idl.bound(64),],
        'command': [idl.default(0),],
    }
)
class CommandRequest:
    robot_id: str = ""
    command: Command = Command.COMMAND_UNKNOWN
    goto_params: Optional[GotoParameters] = None
    set_path_params: Optional[SetPathParameters] = None

@idl.struct(

    member_annotations = {
        'robot_id': [idl.bound(64),],
        'description': [idl.bound(256),],
        'resulting_status': [idl.default(0),],
        'resulting_intent': [idl.default(0),],
    }
)
class CommandResponse:
    robot_id: str = ""
    success: bool = False
    description: str = ""
    resulting_status: RobotStatus = RobotStatus.STATUS_UNKNOWN
    resulting_intent: RobotIntentType = RobotIntentType.INTENT_UNKNOWN

@idl.struct(

    member_annotations = {
        'robot_id': [idl.bound(64),],
        'station_id': [idl.bound(64),],
    }
)
class SlotReserve:
    robot_id: str = ""
    station_id: str = ""
    battery_level: float = 0.0

@idl.struct(

    member_annotations = {
        'station_id': [idl.bound(64),],
        'slot_id': [idl.bound(64),],
    }
)
class SlotAssignment:
    station_id: str = ""
    slot_id: str = ""
    granted: bool = False
    queue_rank: idl.int32 = 0
    dock_x: float = 0.0
    dock_y: float = 0.0

@idl.struct(

    member_annotations = {
        'robot_id': [idl.bound(64),],
        'slot_id': [idl.bound(64),],
        'station_id': [idl.bound(64),],
    }
)
class SlotRelease:
    robot_id: str = ""
    slot_id: str = ""
    station_id: str = ""

@idl.struct(

    member_annotations = {
        'station_id': [idl.bound(64),],
        'slot_id': [idl.bound(64),],
    }
)
class SlotReleaseAck:
    station_id: str = ""
    slot_id: str = ""
    success: bool = False

@idl.struct(

    member_annotations = {
        'robot_id': [idl.bound(64),],
    }
)
class QueueEntry:
    robot_id: str = ""
    rank: idl.int32 = 0
    confirmed: bool = False

@idl.struct(

    member_annotations = {
        'station_id': [idl.key, idl.bound(64),],
        'docked_robot_id': [idl.bound(64),],
        'queue': [idl.bound(16),],
    }
)
class StationStatus:
    station_id: str = ""
    dock_x: float = 0.0
    dock_y: float = 0.0
    is_occupied: bool = False
    docked_robot_id: str = ""
    queue: Sequence[QueueEntry] = field(default_factory = list)

@idl.struct(

    member_annotations = {
        'robot_id': [idl.key, idl.bound(64),],
    }
)
class CoveragePoint:
    robot_id: str = ""
    x: float = 0.0
    y: float = 0.0
