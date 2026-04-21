# Common fleet configuration — shared across all approaches.
# Sourced by each approach's fleet_config.sh.
#
# This file describes the SCENARIO (what to run),
# not the TRANSPORT (how to communicate).
#
# Change the fleet here once and every approach picks it up.

# ── Robots ──────────────────────────────────────────────────────────────
ROBOTS=("tractor1" "tractor2" "tractor3" "tractor4" "tractor5")

# ── Charging stations: "name dock_x dock_y" ─────────────────────────────
STATIONS=(
  "station1 8 92"
  "station2 92 8"
)

# ── Flask UI ────────────────────────────────────────────────────────────
UI_PORT=5000
