# Fleet configuration – the gRPC approach (pure gRPC).
# Sourced by run_demo.sh and stop_demo.sh.
#
# gRPC needs explicit ports for every listener.
# Ports are computed from base + index in run_demo.sh.

# ── Scenario (robot names, station positions, UI port) ──────────────────
_FLEET_CFG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"
source "$_FLEET_CFG_DIR/../shared/fleet_common.sh"

# ── Transport: gRPC port ranges ─────────────────────────────────────────
ROBOT_BASE_PORT=50051      # tractor1→50051, tractor2→50052, ...
STATION_BASE_PORT=50060    # station1→50060, station2→50061, ...
