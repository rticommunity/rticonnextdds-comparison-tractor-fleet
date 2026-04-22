# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0

# Fleet configuration – the hybrid approach (hybrid DDS pub-sub + gRPC commands).
# Sourced by demo_start.sh and demo_stop.sh.
#
# DDS handles pub-sub (automatic discovery).
# gRPC handles commands and charging — needs explicit ports.

# ── Scenario (robot names, station positions, UI port) ──────────────────
_FLEET_CFG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"
source "$_FLEET_CFG_DIR/../shared/fleet_common.sh"

# ── Transport: DDS domain ────────────────────────────────────────────────
DOMAIN_ID=0

# ── Transport: gRPC port ranges ─────────────────────────────────────────
ROBOT_BASE_PORT=50051      # tractor1→50051, tractor2→50052, ...
STATION_BASE_PORT=50060    # station1→50060, station2→50061, ...
