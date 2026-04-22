# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0

# Fleet configuration – the DDS approach (pure DDS).
# Sourced by demo_start.sh and demo_stop.sh.
#
# DDS discovers everything — no port assignments needed.

# ── Scenario (robot names, station positions, UI port) ──────────────────
_FLEET_CFG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"
source "$_FLEET_CFG_DIR/../shared/fleet_common.sh"

# ── Transport: DDS domain ────────────────────────────────────────────────
DOMAIN_ID=0
