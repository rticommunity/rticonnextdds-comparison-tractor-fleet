#!/usr/bin/env python3
"""
PyBullet-based 3D video renderer for robot onboard camera simulation.

Each robot gets a ``VideoRenderer`` instance that maintains a headless
PyBullet physics client (DIRECT mode — no GPU, no display required).
The scene is a richly decorated arena with:

  • Farmland aerial-photo ground texture (``field1_texture.jpg``)
  • Peer robots rendered as tractor bodies (chassis + cabin + 4 wheels + beacon)
  • Charging stations (ground pad + post + glowing cap)
  • Waypoint beacons — tall translucent pillars with bright caps
  • First-person onboard camera with a wide FOV

Calling ``render_frame()`` returns JPEG bytes ready to be stuffed into a
``VideoFrame`` protobuf message.
"""

import io
import math
import os
import re
import time
import threading
from typing import Dict, List, Optional, Tuple

import numpy as np
import pybullet as p
import pybullet_data
from PIL import Image, ImageDraw, ImageFont

_DIR = os.path.dirname(os.path.abspath(__file__))


# ─── colour palette (Catppuccin-ish, same order as the UI) ───────────────────
_PALETTE = [
    (0.537, 0.706, 0.980),  # blue
    (0.651, 0.890, 0.631),  # green
    (0.980, 0.702, 0.529),  # peach
    (0.976, 0.886, 0.686),  # yellow
    (0.953, 0.545, 0.659),  # red
    (0.796, 0.651, 0.969),  # mauve
    (0.580, 0.886, 0.835),  # teal
    (0.961, 0.761, 0.906),  # pink
    (0.455, 0.780, 0.925),  # sapphire
    (0.706, 0.745, 0.996),  # lavender
]

# Status → body colour
_STATUS_COLOUR = {
    "STATUS_MOVING":  (0.13, 0.87, 0.13),   # vivid green
    "STATUS_HOLDING": (0.93, 0.80, 0.00),    # vivid yellow
    "STATUS_IDLE":    (0.75, 0.35, 0.35),    # muted red
}

# Status → antenna beacon colour (brighter / emissive-looking)
_STATUS_BEACON = {
    "STATUS_MOVING":  (0.2, 1.0, 0.2),
    "STATUS_HOLDING": (1.0, 0.95, 0.1),
    "STATUS_IDLE":    (1.0, 0.3, 0.3),
}

_WAYPOINT_PILLAR = (0.35, 0.35, 0.90, 0.45)   # translucent blue pillar
_WAYPOINT_CAP    = (0.55, 0.55, 1.00, 0.95)   # bright blue cap
_GRID_DARK       = [0.12, 0.12, 0.16, 1.0]
_GRID_LIGHT      = [0.18, 0.18, 0.24, 1.0]
_SKY_RGB         = [0.53, 0.72, 0.90]          # soft blue sky background
_SURROUND_COLOUR = [0.42, 0.36, 0.26, 1.0]     # bare earth beyond the field

# Charging station colours (warm amber / orange matching the 2D ⚡ UI)
_STATION_PAD_IDLE      = [0.98, 0.70, 0.53, 0.25]   # peach glow pad (idle)
_STATION_PAD_ACTIVE    = [0.65, 0.89, 0.63, 0.30]   # green glow pad (charging)
_STATION_POST_COLOUR   = [0.82, 0.58, 0.35, 0.90]   # warm amber post
_STATION_CAP_IDLE      = [0.98, 0.70, 0.53, 1.00]   # peach cap
_STATION_CAP_ACTIVE    = [0.65, 0.89, 0.63, 1.00]   # green cap


class VideoRenderer:
    """Headless PyBullet 3D renderer for a single robot's onboard camera."""

    WIDTH  = 400
    HEIGHT = 300
    JPEG_QUALITY = 80

    # FPV camera
    CAMERA_EYE_HEIGHT  = 3.5    # metres above ground
    CAMERA_LOOK_AHEAD  = 20.0   # look-at distance forward
    CAMERA_LOOK_DOWN_Z = 0.8    # target Z (slight down-tilt)
    CAMERA_FOV         = 78     # degrees

    # Robot body dimensions — realistic tractor layout:
    #   Rear half: large wheels + tall cabin (operator cab)
    #   Front half: small wheels + low engine hood
    TRACTOR_CHASSIS_L  = 4.5   # total length (X, travel direction)
    TRACTOR_CHASSIS_W  = 2.8   # width (Y)
    TRACTOR_CHASSIS_H  = 1.0   # chassis/frame height
    TRACTOR_CABIN_L    = 1.8   # cabin length (sits over rear axle)
    TRACTOR_CABIN_W    = 2.4
    TRACTOR_CABIN_H    = 2.2   # tall — operator headroom
    TRACTOR_HOOD_L     = 1.6   # engine hood length (front)
    TRACTOR_HOOD_W     = 2.0   # narrower than cabin
    TRACTOR_HOOD_H     = 0.9   # low — just covers the engine
    TRACTOR_REAR_W_R   = 1.1   # rear wheel radius (large)
    TRACTOR_REAR_W_T   = 0.55  # rear wheel thickness
    TRACTOR_FRONT_W_R  = 0.65  # front wheel radius (small)
    TRACTOR_FRONT_W_T  = 0.30  # front wheel thickness
    TRACTOR_BEACON_R   = 0.40  # glowing sphere on cabin roof

    # Waypoint beacon
    WP_PILLAR_RADIUS = 0.1
    WP_PILLAR_HEIGHT = 4.0
    WP_CAP_RADIUS    = 0.3

    # Charging station — ground pad + upright post + ⚡ cap
    STATION_PAD_RADIUS  = 3.5   # flat circular pad on the ground
    STATION_PAD_HEIGHT  = 0.15
    STATION_POST_RADIUS = 0.20
    STATION_POST_HEIGHT = 6.5
    STATION_CAP_RADIUS  = 0.55

    def __init__(self, robot_id: str, fleet_robot_ids: Optional[List[str]] = None):
        self.robot_id = robot_id
        self._lock = threading.Lock()
        self._frame_counter = 0

        # Build a deterministic robot_id → palette-index mapping so that
        # 3D colours match the 2D dashboard exactly.  The UI assigns
        # colours in the order robots first appear (which follows fleet
        # port order).  *fleet_robot_ids* is that ordered list.
        # If not provided, fall back to extracting the numeric suffix
        # from robot IDs (e.g. "robot3" → index 2).
        self._colour_map: Dict[str, int] = {}
        if fleet_robot_ids:
            for idx, rid in enumerate(fleet_robot_ids):
                self._colour_map[rid] = idx

        # Connect to a headless physics server
        self._cid = p.connect(p.DIRECT)
        p.setAdditionalSearchPath(pybullet_data.getDataPath(),
                                  physicsClientId=self._cid)

        p.setGravity(0, 0, -10, physicsClientId=self._cid)

        # Configure lighting for better depth / shadows
        p.configureDebugVisualizer(p.COV_ENABLE_SHADOWS, 1,
                                   physicsClientId=self._cid)

        # Sky-blue background so the horizon is clearly visible
        p.configureDebugVisualizer(
            p.COV_ENABLE_RGB_BUFFER_PREVIEW, 0,
            physicsClientId=self._cid)
        p.resetDebugVisualizerCamera(
            cameraDistance=10, cameraYaw=0, cameraPitch=-30,
            cameraTargetPosition=[0, 0, 0],
            physicsClientId=self._cid)

        # Build the static scene
        self._build_ground_grid()
        self._build_surrounding_terrain()

        # Track dynamic bodies
        self._peer_bodies: Dict[str, dict] = {}       # rid → {chassis, cabin, wheels, beacon}
        self._waypoint_bodies: Dict[str, list] = {}   # rid → [(pillar, cap), …]
        self._station_bodies: Dict[str, dict] = {}    # sid → {pad, post, cap}

    # ── public API ────────────────────────────────────────────────────────

    def render_frame(self, *,
                     my_pos: Dict[str, float],
                     my_heading: float,
                     peers: Dict[str, dict],
                     my_waypoints: list | None = None,
                     charging_stations: Dict[str, dict] | None = None) -> bytes:
        """Render one frame and return JPEG bytes.

        Parameters
        ----------
        my_pos : dict   {"x": float, "y": float}
        my_heading : float   radians, direction the robot is facing
        peers : dict    {robot_id: {"x", "y", "status"}}
        my_waypoints : list | None  [{"x", "y"}, …]
        charging_stations : dict | None
            {station_id: {"x", "y", "active": bool}} — dock positions
        """
        with self._lock:
            self._frame_counter += 1
            self._overlay_peers = {}      # rid → (bx, by, top_z)
            self._overlay_waypoints = []  # [(bx, by, cap_z, index_1based), …]
            self._overlay_stations = []   # [(bx, by, top_z, label), …]
            self._update_scene(my_pos, peers, my_waypoints,
                               charging_stations or {})
            rgba, view_mat, proj_mat = self._capture(my_pos, my_heading)
            return self._encode_jpeg_with_labels(rgba, view_mat, proj_mat)

    def close(self):
        """Disconnect the physics client."""
        try:
            p.disconnect(physicsClientId=self._cid)
        except Exception:
            pass

    # ── scene construction helpers ────────────────────────────────────────

    def _arena_to_bullet(self, ax: float, ay: float, az: float = 0.0):
        """Convert arena coords (0-100) to PyBullet world coords."""
        return (ax - 50.0, ay - 50.0, az)

    def _build_ground_grid(self):
        """Create a textured ground plane using the farmland aerial photo.

        Uses a custom URDF / OBJ with UV coords 0-1 so the texture is
        stretched once across the 100 × 100 arena instead of tiling.
        """
        urdf_path = os.path.join(_DIR, "arena_ground.urdf")
        self._ground = p.loadURDF(urdf_path, [0, 0, 0],
                                  physicsClientId=self._cid)
        tex_path = os.path.join(_DIR, "field1_texture.jpg")
        if os.path.isfile(tex_path):
            tex_id = p.loadTexture(tex_path, physicsClientId=self._cid)
            p.changeVisualShape(self._ground, -1,
                                textureUniqueId=tex_id,
                                rgbaColor=[1, 1, 1, 1],
                                physicsClientId=self._cid)
        else:
            # Fallback: plain dark floor
            p.changeVisualShape(self._ground, -1,
                                rgbaColor=_GRID_DARK,
                                physicsClientId=self._cid)

    def _build_surrounding_terrain(self):
        """Create a large flat plane around the arena to contrast with the sky.

        This muted-earth plane sits just below z=0 so it doesn't z-fight
        with the textured arena ground.  It extends 500 m in every direction
        — far enough that the camera never sees the edge.
        """
        half = 500.0
        vis = p.createVisualShape(
            p.GEOM_BOX,
            halfExtents=[half, half, 0.05],
            rgbaColor=_SURROUND_COLOUR,
            physicsClientId=self._cid)
        self._surround = p.createMultiBody(
            baseMass=0,
            baseVisualShapeIndex=vis,
            basePosition=[0, 0, -0.06],   # just below ground
            physicsClientId=self._cid)

    # ── peer robot management ─────────────────────────────────────────────

    def _peer_colour(self, rid: str) -> Tuple[float, float, float]:
        """Return the palette colour for *rid*, matching the 2D UI.

        Lookup order:
        1. Explicit fleet_robot_ids mapping (set at construction)
        2. Numeric suffix in the robot id (e.g. "robot3" → index 2)
        3. Stable hash fallback for truly arbitrary names
        """
        # Strip internal prefix used for "self" body
        clean = rid.removeprefix("_self_")

        if clean in self._colour_map:
            idx = self._colour_map[clean]
        else:
            # Try to extract trailing digits: "robot7" → 7 → index 6
            m = re.search(r"(\d+)$", clean)
            if m:
                idx = int(m.group(1)) - 1   # 1-based name → 0-based index
            else:
                idx = hash(clean) & 0x7FFFFFFF

        return _PALETTE[idx % len(_PALETTE)]

    def _create_robot_body(self, rid: str) -> dict:
        """Build a tractor: chassis + cabin (rear) + engine hood (front) + 4 wheels + beacon."""
        r, g, b = self._peer_colour(rid)
        dark = 0.30   # wheel tint multiplier

        # --- chassis (flat frame box) ---
        vis_chassis = p.createVisualShape(
            p.GEOM_BOX,
            halfExtents=[self.TRACTOR_CHASSIS_L / 2,
                         self.TRACTOR_CHASSIS_W / 2,
                         self.TRACTOR_CHASSIS_H / 2],
            rgbaColor=[r * 0.7, g * 0.7, b * 0.7, 0.92],
            physicsClientId=self._cid)
        chassis = p.createMultiBody(baseMass=0, baseVisualShapeIndex=vis_chassis,
                                    basePosition=[0, 0, -100],
                                    physicsClientId=self._cid)

        # --- cabin (tall box, over the rear axle) ---
        vis_cabin = p.createVisualShape(
            p.GEOM_BOX,
            halfExtents=[self.TRACTOR_CABIN_L / 2,
                         self.TRACTOR_CABIN_W / 2,
                         self.TRACTOR_CABIN_H / 2],
            rgbaColor=[r * 0.5, g * 0.5, b * 0.5, 0.90],
            physicsClientId=self._cid)
        cabin = p.createMultiBody(baseMass=0, baseVisualShapeIndex=vis_cabin,
                                  basePosition=[0, 0, -100],
                                  physicsClientId=self._cid)

        # --- engine hood (low box, over the front axle) ---
        vis_hood = p.createVisualShape(
            p.GEOM_BOX,
            halfExtents=[self.TRACTOR_HOOD_L / 2,
                         self.TRACTOR_HOOD_W / 2,
                         self.TRACTOR_HOOD_H / 2],
            rgbaColor=[r * 0.65, g * 0.65, b * 0.65, 0.88],
            physicsClientId=self._cid)
        hood = p.createMultiBody(baseMass=0, baseVisualShapeIndex=vis_hood,
                                 basePosition=[0, 0, -100],
                                 physicsClientId=self._cid)

        # --- rear wheels (large, cylinders turned sideways) ---
        vis_rear_wheel = p.createVisualShape(
            p.GEOM_CYLINDER,
            radius=self.TRACTOR_REAR_W_R,
            length=self.TRACTOR_REAR_W_T,
            rgbaColor=[r * dark, g * dark, b * dark, 0.95],
            physicsClientId=self._cid)
        # --- front wheels (smaller) ---
        vis_front_wheel = p.createVisualShape(
            p.GEOM_CYLINDER,
            radius=self.TRACTOR_FRONT_W_R,
            length=self.TRACTOR_FRONT_W_T,
            rgbaColor=[r * dark, g * dark, b * dark, 0.95],
            physicsClientId=self._cid)

        wheels = []
        for i in range(4):
            vis = vis_rear_wheel if i >= 2 else vis_front_wheel
            w = p.createMultiBody(baseMass=0, baseVisualShapeIndex=vis,
                                  basePosition=[0, 0, -100],
                                  physicsClientId=self._cid)
            wheels.append(w)

        # --- beacon sphere on cabin roof ---
        vis_bcn = p.createVisualShape(
            p.GEOM_SPHERE,
            radius=self.TRACTOR_BEACON_R,
            rgbaColor=[0.2, 1.0, 0.2, 1.0],
            physicsClientId=self._cid)
        beacon = p.createMultiBody(baseMass=0, baseVisualShapeIndex=vis_bcn,
                                   basePosition=[0, 0, -100],
                                   physicsClientId=self._cid)

        parts = {
            "chassis": chassis,
            "cabin": cabin,
            "hood": hood,
            "wheels": wheels,
            "beacon": beacon,
        }
        self._peer_bodies[rid] = parts
        return parts

    def _place_robot(self, rid: str, bx: float, by: float, status: str,
                     heading: float = 0.0):
        """Position all parts of a tractor body at (bx, by) in bullet coords.

        Layout (X = travel direction, +X = forward):
          Rear  (-X) : large wheels + tall cabin
          Front (+X) : small wheels + low engine hood

        *heading* is the robot's travel direction in radians (arena convention:
        0 = +X, pi/2 = +Y).  All parts are rotated around Z to match.
        """
        parts = self._peer_bodies.get(rid) or self._create_robot_body(rid)

        rwr  = self.TRACTOR_REAR_W_R
        rwt  = self.TRACTOR_REAR_W_T
        fwr  = self.TRACTOR_FRONT_W_R
        fwt  = self.TRACTOR_FRONT_W_T
        ch_h = self.TRACTOR_CHASSIS_H
        ch_l = self.TRACTOR_CHASSIS_L
        ch_w = self.TRACTOR_CHASSIS_W
        cab_l = self.TRACTOR_CABIN_L
        cab_h = self.TRACTOR_CABIN_H
        hood_l = self.TRACTOR_HOOD_L
        hood_h = self.TRACTOR_HOOD_H

        # Vertical heights (independent of heading)
        chassis_z = rwr + ch_h / 2
        cabin_z   = rwr + ch_h + cab_h / 2
        hood_z    = rwr + ch_h + hood_h / 2
        bcn_z     = rwr + ch_h + cab_h + self.TRACTOR_BEACON_R

        # Local offsets along the forward axis (+X = front in local frame)
        cabin_local_x = -(ch_l / 2 - cab_l / 2)   # toward rear
        hood_local_x  =  (ch_l / 2 - hood_l / 2)  # toward front

        # Rotate local offsets by heading to get world offsets
        cos_h, sin_h = math.cos(heading), math.sin(heading)
        def rotate(lx, ly):
            return (bx + lx * cos_h - ly * sin_h,
                    by + lx * sin_h + ly * cos_h)

        cabin_wx, cabin_wy = rotate(cabin_local_x, 0)
        hood_wx,  hood_wy  = rotate(hood_local_x,  0)

        # Record label anchor (above the beacon) for the 2D overlay pass
        self._overlay_peers[rid] = (bx, by, bcn_z + 1.2)

        # Body orientation — rotate all boxes around Z by heading
        body_q = p.getQuaternionFromEuler([0, 0, heading])

        id_q = [0, 0, 0, 1]
        p.resetBasePositionAndOrientation(parts["chassis"], [bx,       by,       chassis_z], body_q,
                                          physicsClientId=self._cid)
        p.resetBasePositionAndOrientation(parts["cabin"],   [cabin_wx, cabin_wy, cabin_z],   body_q,
                                          physicsClientId=self._cid)
        p.resetBasePositionAndOrientation(parts["hood"],    [hood_wx,  hood_wy,  hood_z],    body_q,
                                          physicsClientId=self._cid)
        p.resetBasePositionAndOrientation(parts["beacon"],  [cabin_wx, cabin_wy, bcn_z],     id_q,
                                          physicsClientId=self._cid)

        # Wheels: cylinder axis along Y, then rotated by heading around Z
        wheel_q = p.getQuaternionFromEuler([math.pi / 2, 0, heading])
        # Local wheel positions (before heading rotation)
        local_wheel_positions = [
            ( ch_l * 0.33, -(ch_w / 2 + fwt / 2), fwr),   # front-left
            ( ch_l * 0.33,  (ch_w / 2 + fwt / 2), fwr),   # front-right
            (-ch_l * 0.33, -(ch_w / 2 + rwt / 2), rwr),   # rear-left
            (-ch_l * 0.33,  (ch_w / 2 + rwt / 2), rwr),   # rear-right
        ]
        for i, (lx, ly, wz) in enumerate(local_wheel_positions):
            wx, wy = rotate(lx, ly)
            p.resetBasePositionAndOrientation(parts["wheels"][i], [wx, wy, wz], wheel_q,
                                              physicsClientId=self._cid)

        # Identity colour: chassis + cabin + hood
        ir, ig, ib = self._peer_colour(rid)
        p.changeVisualShape(parts["chassis"], -1,
                            rgbaColor=[ir * 0.7, ig * 0.7, ib * 0.7, 0.92],
                            physicsClientId=self._cid)
        p.changeVisualShape(parts["cabin"], -1,
                            rgbaColor=[ir * 0.5, ig * 0.5, ib * 0.5, 0.90],
                            physicsClientId=self._cid)
        p.changeVisualShape(parts["hood"], -1,
                            rgbaColor=[ir * 0.65, ig * 0.65, ib * 0.65, 0.88],
                            physicsClientId=self._cid)

        # Beacon colour reflects status
        br, bg, bb = _STATUS_BEACON.get(status, (0.9, 0.9, 0.9))
        p.changeVisualShape(parts["beacon"], -1,
                            rgbaColor=[br, bg, bb, 1.0],
                            physicsClientId=self._cid)

    def _remove_robot(self, rid: str):
        """Remove all body parts for a peer tractor."""
        parts = self._peer_bodies.pop(rid, {})
        for key, val in parts.items():
            if isinstance(val, list):
                for body in val:
                    p.removeBody(body, physicsClientId=self._cid)
            else:
                p.removeBody(val, physicsClientId=self._cid)

    # ── waypoint beacon management ────────────────────────────────────────

    def _create_waypoint_beacon(self) -> tuple:
        """Create a tall translucent pillar + bright cap sphere."""
        vis_pil = p.createVisualShape(
            p.GEOM_CYLINDER,
            radius=self.WP_PILLAR_RADIUS,
            length=self.WP_PILLAR_HEIGHT,
            rgbaColor=list(_WAYPOINT_PILLAR),
            physicsClientId=self._cid)
        pillar = p.createMultiBody(baseMass=0, baseVisualShapeIndex=vis_pil,
                                   basePosition=[0, 0, -100],
                                   physicsClientId=self._cid)

        vis_cap = p.createVisualShape(
            p.GEOM_SPHERE,
            radius=self.WP_CAP_RADIUS,
            rgbaColor=list(_WAYPOINT_CAP),
            physicsClientId=self._cid)
        cap = p.createMultiBody(baseMass=0, baseVisualShapeIndex=vis_cap,
                                basePosition=[0, 0, -100],
                                physicsClientId=self._cid)
        return (pillar, cap)

    # ── charging station management ───────────────────────────────────────

    def _create_station_body(self, sid: str) -> dict:
        """Build a charging station: ground pad + upright post + glowing cap."""
        # --- flat circular pad on the ground ---
        vis_pad = p.createVisualShape(
            p.GEOM_CYLINDER,
            radius=self.STATION_PAD_RADIUS,
            length=self.STATION_PAD_HEIGHT,
            rgbaColor=_STATION_PAD_IDLE,
            physicsClientId=self._cid)
        pad = p.createMultiBody(baseMass=0, baseVisualShapeIndex=vis_pad,
                                basePosition=[0, 0, -100],
                                physicsClientId=self._cid)

        # --- upright post (slim cylinder) ---
        vis_post = p.createVisualShape(
            p.GEOM_CYLINDER,
            radius=self.STATION_POST_RADIUS,
            length=self.STATION_POST_HEIGHT,
            rgbaColor=_STATION_POST_COLOUR,
            physicsClientId=self._cid)
        post = p.createMultiBody(baseMass=0, baseVisualShapeIndex=vis_post,
                                 basePosition=[0, 0, -100],
                                 physicsClientId=self._cid)

        # --- glowing cap sphere ---
        vis_cap = p.createVisualShape(
            p.GEOM_SPHERE,
            radius=self.STATION_CAP_RADIUS,
            rgbaColor=_STATION_CAP_IDLE,
            physicsClientId=self._cid)
        cap = p.createMultiBody(baseMass=0, baseVisualShapeIndex=vis_cap,
                                basePosition=[0, 0, -100],
                                physicsClientId=self._cid)

        parts = {"pad": pad, "post": post, "cap": cap}
        self._station_bodies[sid] = parts
        return parts

    def _place_station(self, sid: str, bx: float, by: float, active: bool):
        """Position a charging station at (bx, by) and tint by status."""
        parts = self._station_bodies.get(sid) or self._create_station_body(sid)

        pad_z = self.STATION_PAD_HEIGHT / 2
        post_z = self.STATION_PAD_HEIGHT + self.STATION_POST_HEIGHT / 2
        cap_z = self.STATION_PAD_HEIGHT + self.STATION_POST_HEIGHT + self.STATION_CAP_RADIUS

        id_q = [0, 0, 0, 1]
        p.resetBasePositionAndOrientation(parts["pad"],  [bx, by, pad_z],  id_q,
                                          physicsClientId=self._cid)
        p.resetBasePositionAndOrientation(parts["post"], [bx, by, post_z], id_q,
                                          physicsClientId=self._cid)
        p.resetBasePositionAndOrientation(parts["cap"],  [bx, by, cap_z],  id_q,
                                          physicsClientId=self._cid)

        # Colour reflects charging status
        pad_col = _STATION_PAD_ACTIVE if active else _STATION_PAD_IDLE
        cap_col = _STATION_CAP_ACTIVE if active else _STATION_CAP_IDLE
        p.changeVisualShape(parts["pad"], -1, rgbaColor=pad_col,
                            physicsClientId=self._cid)

        # Pulsing glow on cap
        pulse = 0.7 + 0.3 * math.sin(self._frame_counter * 0.4)
        cr, cg, cb, ca = cap_col
        p.changeVisualShape(parts["cap"], -1,
                            rgbaColor=[cr * pulse, cg * pulse, cb * pulse, ca],
                            physicsClientId=self._cid)

        # Record label anchor for 2D text overlay
        label = "⚡ " + sid.replace("station", "stn")
        self._overlay_stations.append((bx, by, cap_z + 1.0, label))

    # ── scene update ──────────────────────────────────────────────────────

    def _update_scene(self, my_pos, peers, my_waypoints, charging_stations):
        """Move peer robots, waypoint beacons, and charging stations."""

        # -- peers --
        seen = set()
        for rid, info in peers.items():
            seen.add(rid)
            bx, by, _ = self._arena_to_bullet(info["x"], info["y"])
            status = info.get("status", "STATUS_MOVING")
            heading = info.get("heading", 0.0)   # radians, from KinematicState
            self._place_robot(rid, bx, by, status, heading)

        # Remove bodies for peers that are gone
        for rid in list(self._peer_bodies):
            if rid not in seen and not rid.startswith("_self_"):
                self._remove_robot(rid)

        # -- hide self-body far below ground --
        self_key = f"_self_{self.robot_id}"
        if self_key not in self._peer_bodies:
            self._create_robot_body(self_key)
        for val in self._peer_bodies[self_key].values():
            bodies = val if isinstance(val, list) else [val]
            for body in bodies:
                p.resetBasePositionAndOrientation(
                    body, [0, 0, -100], [0, 0, 0, 1],
                    physicsClientId=self._cid)

        # -- waypoint beacons --
        old_wps = self._waypoint_bodies.get(self.robot_id, [])
        new_wps = my_waypoints or []

        while len(old_wps) < len(new_wps):
            old_wps.append(self._create_waypoint_beacon())
        while len(old_wps) > len(new_wps):
            pillar, cap = old_wps.pop()
            p.removeBody(pillar, physicsClientId=self._cid)
            p.removeBody(cap, physicsClientId=self._cid)

        for i, wp in enumerate(new_wps):
            wx, wy, _ = self._arena_to_bullet(wp["x"], wp["y"])
            pil_z = self.WP_PILLAR_HEIGHT / 2
            cap_z = self.WP_PILLAR_HEIGHT + self.WP_CAP_RADIUS
            pillar, cap = old_wps[i]
            p.resetBasePositionAndOrientation(
                pillar, [wx, wy, pil_z], [0, 0, 0, 1],
                physicsClientId=self._cid)
            p.resetBasePositionAndOrientation(
                cap, [wx, wy, cap_z], [0, 0, 0, 1],
                physicsClientId=self._cid)

            # Record label anchor (above the cap) for overlay (0-based, matching 2D UI)
            self._overlay_waypoints.append((wx, wy, cap_z + 1.0, i))

            # Tint waypoint pillar + cap with this robot's identity colour
            wr, wg, wb = self._peer_colour(self.robot_id)
            p.changeVisualShape(pillar, -1,
                                rgbaColor=[wr * 0.4, wg * 0.4, wb * 0.4, 0.5],
                                physicsClientId=self._cid)

            # Subtle pulsing glow on the cap — tinted with robot colour
            pulse = 0.7 + 0.3 * math.sin(self._frame_counter * 0.5 + i)
            p.changeVisualShape(cap, -1,
                                rgbaColor=[wr * pulse, wg * pulse, wb * pulse, 0.95],
                                physicsClientId=self._cid)

        self._waypoint_bodies[self.robot_id] = old_wps

        # -- charging stations --
        for sid, info in charging_stations.items():
            bx, by, _ = self._arena_to_bullet(info["x"], info["y"])
            active = info.get("active", False)
            self._place_station(sid, bx, by, active)

    # ── camera ────────────────────────────────────────────────────────────

    def _capture(self, my_pos, heading):
        """Render the scene from first-person perspective."""
        cx, cy = my_pos["x"] - 50.0, my_pos["y"] - 50.0

        # Eye at robot position, slightly elevated
        cam_x = cx
        cam_y = cy
        cam_z = self.CAMERA_EYE_HEIGHT

        # Look-at: far ahead in the travel direction
        look_x = cx + self.CAMERA_LOOK_AHEAD * math.cos(heading)
        look_y = cy + self.CAMERA_LOOK_AHEAD * math.sin(heading)
        look_z = self.CAMERA_LOOK_DOWN_Z

        view_matrix = p.computeViewMatrix(
            cameraEyePosition=[cam_x, cam_y, cam_z],
            cameraTargetPosition=[look_x, look_y, look_z],
            cameraUpVector=[0, 0, 1],
            physicsClientId=self._cid,
        )
        proj_matrix = p.computeProjectionMatrixFOV(
            fov=self.CAMERA_FOV,
            aspect=self.WIDTH / self.HEIGHT,
            nearVal=0.3, farVal=250.0,
            physicsClientId=self._cid,
        )

        # Custom light direction for better depth cues
        _, _, rgba, _, _ = p.getCameraImage(
            width=self.WIDTH, height=self.HEIGHT,
            viewMatrix=view_matrix,
            projectionMatrix=proj_matrix,
            lightDirection=[0.4, 0.3, 1.0],
            lightColor=[1.0, 1.0, 0.95],
            lightDistance=10.0,
            lightAmbientCoeff=0.45,
            lightDiffuseCoeff=0.55,
            lightSpecularCoeff=0.25,
            renderer=p.ER_TINY_RENDERER,
            physicsClientId=self._cid,
        )
        return rgba, view_matrix, proj_matrix

    # ── projection helper ─────────────────────────────────────────────────

    def _project_to_screen(self, wx, wy, wz, view_mat, proj_mat):
        """Project a 3-D world point to 2-D screen pixel coordinates.

        Returns (sx, sy) in pixel space or *None* if the point is behind
        the camera (negative w after projection).
        """
        # Build 4×4 matrices from the flat 16-element tuples (column-major)
        V = np.array(view_mat, dtype=np.float64).reshape(4, 4).T
        P = np.array(proj_mat, dtype=np.float64).reshape(4, 4).T

        pt = np.array([wx, wy, wz, 1.0], dtype=np.float64)
        clip = P @ V @ pt
        if clip[3] <= 0:
            return None                       # behind camera
        ndc = clip[:3] / clip[3]              # normalised device coords

        sx = (ndc[0] * 0.5 + 0.5) * self.WIDTH
        sy = (1.0 - (ndc[1] * 0.5 + 0.5)) * self.HEIGHT   # flip Y
        return int(sx), int(sy)

    # ── encoding ──────────────────────────────────────────────────────────

    @staticmethod
    def _encode_jpeg(rgba_flat) -> bytes:
        """Convert PyBullet's flat RGBA buffer to JPEG bytes."""
        arr = np.array(rgba_flat, dtype=np.uint8).reshape(
            VideoRenderer.HEIGHT, VideoRenderer.WIDTH, 4)
        rgb = arr[:, :, :3]
        img = Image.fromarray(rgb, "RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=VideoRenderer.JPEG_QUALITY)
        return buf.getvalue()

    def _encode_jpeg_with_labels(self, rgba_flat, view_mat, proj_mat) -> bytes:
        """Convert RGBA buffer to JPEG with robot-name / waypoint-number
        text labels overlaid via Pillow ImageDraw."""
        arr = np.array(rgba_flat, dtype=np.uint8).reshape(
            self.HEIGHT, self.WIDTH, 4)
        rgb = arr[:, :, :3].copy()

        # Replace PyBullet TINY_RENDERER's white background with sky blue.
        # Background pixels are pure white (255,255,255).  We detect them
        # by looking for pixels where all three channels equal 255.
        bg_mask = np.all(rgb == 255, axis=2)
        sky = np.array([int(c * 255) for c in _SKY_RGB], dtype=np.uint8)
        rgb[bg_mask] = sky

        img = Image.fromarray(rgb, "RGB")
        draw = ImageDraw.Draw(img)

        # Use default bitmap font (always available)
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
        except Exception:
            font = ImageFont.load_default()

        outline_col = (0, 0, 0)
        text_col = (255, 255, 255)

        # Robot peer labels
        for rid, (wx, wy, wz) in self._overlay_peers.items():
            pt = self._project_to_screen(wx, wy, wz, view_mat, proj_mat)
            if pt is None:
                continue
            sx, sy = pt
            if not (0 <= sx < self.WIDTH and 0 <= sy < self.HEIGHT):
                continue
            label = rid.split("_")[-1] if "_" in rid else rid
            # Outline + fill for readability
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx or dy:
                        draw.text((sx + dx, sy + dy), label,
                                  fill=outline_col, font=font, anchor="mb")
            draw.text((sx, sy), label, fill=text_col, font=font, anchor="mb")

        # Waypoint number labels
        for wx, wy, wz, idx in self._overlay_waypoints:
            pt = self._project_to_screen(wx, wy, wz, view_mat, proj_mat)
            if pt is None:
                continue
            sx, sy = pt
            if not (0 <= sx < self.WIDTH and 0 <= sy < self.HEIGHT):
                continue
            label = str(idx)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx or dy:
                        draw.text((sx + dx, sy + dy), label,
                                  fill=outline_col, font=font, anchor="mb")
            draw.text((sx, sy), label, fill=text_col, font=font, anchor="mb")

        # Charging station labels (amber tint to match 2D UI)
        station_col = (250, 179, 135)
        for wx, wy, wz, label in self._overlay_stations:
            pt = self._project_to_screen(wx, wy, wz, view_mat, proj_mat)
            if pt is None:
                continue
            sx, sy = pt
            if not (0 <= sx < self.WIDTH and 0 <= sy < self.HEIGHT):
                continue
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx or dy:
                        draw.text((sx + dx, sy + dy), label,
                                  fill=outline_col, font=font, anchor="mb")
            draw.text((sx, sy), label, fill=station_col, font=font, anchor="mb")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self.JPEG_QUALITY)
        return buf.getvalue()
