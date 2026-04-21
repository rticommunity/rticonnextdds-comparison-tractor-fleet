#!/bin/bash

# Script to run the Pure DDS (Connext) robot demo.
#
# DDS discovery is automatic — no port lists, no Zeroconf, no mDNS.
# All participants on the same domain ID find each other via SPDP.
#
# Usage:
#   ./run_demo.sh all              - Run stations + robots + UI
#   ./run_demo.sh robots           - Run all robots in background
#   ./run_demo.sh stations         - Run all stations in background
#   ./run_demo.sh ui               - Launch the fleet dashboard UI
#   ./run_demo.sh robot <name>     - Run a single robot
#   ./run_demo.sh station <name>   - Run a single station
#
# Examples (separate VS Code terminals):
#   Terminal 1:  ./run_demo.sh stations
#   Terminal 2:  ./run_demo.sh robots
#   Terminal 3:  ./run_demo.sh ui

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Activate the project venv if not already active
VENV_DIR="$SCRIPT_DIR/../../venv"
if [ -z "$VIRTUAL_ENV" ] && [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
fi

# ── Fleet configuration ──────────────────────────────────────────────────
# Sets ROBOTS[], STATIONS[], UI_PORT (from shared/fleet_common.sh)
# and  DOMAIN_ID (approach-specific).
source "$SCRIPT_DIR/fleet_config.sh"
# ─────────────────────────────────────────────────────────────────────────

# Helper: look up station dock coords by name → "x y"
station_lookup() {
    for (( i=0; i<${#STATIONS[@]}; i++ )); do
        read -r sname sx sy <<< "${STATIONS[$i]}"
        [[ "$sname" == "$1" ]] && echo "$sx $sy" && return
    done
    echo "Error: unknown station '$1'. Known: $(printf '%s ' "${STATIONS[@]}")" >&2; exit 1
}

echo "Pure DDS (Connext) Demo"
echo "====================================="
echo ""

# ── Locate RTI Connext DDS installation ──────────────────────────────────
# NDDSHOME must be set so the runtime can find its license file.
if [ -z "${NDDSHOME:-}" ]; then
    for d in /Applications/rti_connext_dds-* \
             /opt/rti_connext_dds-* \
             "$HOME/rti_connext_dds-"*; do
        [ -d "$d/bin" ] && NDDSHOME="$d"   # picks the last (newest) match
    done
    if [ -n "${NDDSHOME:-}" ]; then
        export NDDSHOME
        echo "NOTE: NDDSHOME was not set — auto-detected $NDDSHOME"
    else
        echo "ERROR: NDDSHOME is not set and no Connext installation found."
        echo "  Set NDDSHOME to your RTI Connext DDS installation directory."
        exit 1
    fi
fi
export NDDSHOME
# ─────────────────────────────────────────────────────────────────────────

# Generate DDS type support if needed
if [ ! -f "robot_types.py" ]; then
    RTIDDSGEN="$NDDSHOME/bin/rtiddsgen"
    if command -v rtiddsgen &>/dev/null; then
        RTIDDSGEN=rtiddsgen
    elif [ ! -x "$RTIDDSGEN" ]; then
        echo "ERROR: rtiddsgen not found at $RTIDDSGEN or on PATH."
        exit 1
    fi
    echo "Generating DDS type support (using $RTIDDSGEN)..."
    "$RTIDDSGEN" -language python -d . robot_types.xml
fi

case "${1:---help}" in
    ui)
        echo "Starting Fleet Dashboard UI on port $UI_PORT..."
        echo "DDS domain $DOMAIN_ID — robots discovered automatically"
        echo ""
        python robot_ui.py --domain "$DOMAIN_ID" --ui-port "$UI_PORT"
        ;;
    robot)
        ROBOT_NAME="$2"
        if [ -z "$ROBOT_NAME" ]; then
            echo "Error: missing robot name.  Usage: $0 robot <name>"
            echo "Example: $0 robot tractor1"
            exit 1
        fi
        echo "Starting robot $ROBOT_NAME (domain $DOMAIN_ID)..."
        echo "DDS discovery is automatic — no peer configuration needed"
        echo ""
        python robot_node.py --id "$ROBOT_NAME" --domain "$DOMAIN_ID"
        ;;
    station)
        STATION_NAME="$2"
        if [ -z "$STATION_NAME" ]; then
            echo "Error: missing station name.  Usage: $0 station <name>"
            echo "Available: $(printf '%s ' "${STATIONS[@]}")"
            exit 1
        fi
        read -r sx sy <<< "$(station_lookup "$STATION_NAME")"
        echo "Starting $STATION_NAME (domain $DOMAIN_ID, dock at $sx,$sy)..."
        python charging_station.py --id "$STATION_NAME" --domain "$DOMAIN_ID" \
            --dock-x "$sx" --dock-y "$sy"
        ;;
    robots)
        echo "Starting all robots in background..."
        echo "DDS domain $DOMAIN_ID"
        echo ""

        PIDS=()
        for NAME in "${ROBOTS[@]}"; do
            echo "  Starting $NAME"
            python robot_node.py --id "$NAME" --domain "$DOMAIN_ID" &
            PIDS+=($!)
            sleep 2
        done

        cleanup() {
            echo ""
            echo "Stopping all robots..."
            kill "${PIDS[@]}" 2>/dev/null
            exit 0
        }
        trap cleanup SIGINT SIGTERM

        echo ""
        echo "All robots started.  PIDs: ${PIDS[*]}"
        echo "Press Ctrl+C to stop"
        wait
        ;;
    stations)
        echo "Starting all stations in background..."
        echo "DDS domain $DOMAIN_ID"
        echo ""

        PIDS=()
        for (( si=0; si<${#STATIONS[@]}; si++ )); do
            read -r sname sx sy <<< "${STATIONS[$si]}"
            echo "  Starting $sname (dock at $sx,$sy)"
            python charging_station.py --id "$sname" --domain "$DOMAIN_ID" \
                --dock-x "$sx" --dock-y "$sy" &
            PIDS+=($!)
        done

        cleanup() {
            echo ""
            echo "Stopping all stations..."
            kill "${PIDS[@]}" 2>/dev/null
            exit 0
        }
        trap cleanup SIGINT SIGTERM

        echo ""
        echo "All stations started.  PIDs: ${PIDS[*]}"
        echo "Press Ctrl+C to stop"
        wait
        ;;
    all)
        echo "Starting all robots + stations in background..."
        echo "DDS domain $DOMAIN_ID — all participants discover each other automatically"
        echo ""
        echo "TIP: For separate terminals, run each component individually:"
        echo "  ./run_demo.sh robot <name>"
        echo "  ./run_demo.sh station <name> --dock-x X --dock-y Y"
        echo "  ./run_demo.sh ui"
        echo ""

        PIDS=()

        # Launch charging stations first
        for (( si=0; si<${#STATIONS[@]}; si++ )); do
            read -r sname sx sy <<< "${STATIONS[$si]}"
            echo "  Starting $sname (dock at $sx,$sy)"
            python charging_station.py --id "$sname" --domain "$DOMAIN_ID" \
                --dock-x "$sx" --dock-y "$sy" &
            PIDS+=($!)
        done
        sleep 1

        for NAME in "${ROBOTS[@]}"; do
            echo "  Starting $NAME"
            python robot_node.py --id "$NAME" --domain "$DOMAIN_ID" &
            PIDS+=($!)
            sleep 2
        done

        echo "Starting Fleet Dashboard UI on port $UI_PORT..."
        echo "DDS domain $DOMAIN_ID — robots discovered automatically"
        echo ""
        python robot_ui.py --domain "$DOMAIN_ID" --ui-port "$UI_PORT"

        cleanup() {
            echo ""
            echo "Stopping all robots and stations..."
            kill "${PIDS[@]}" 2>/dev/null
            exit 0
        }
        trap cleanup SIGINT SIGTERM

        echo ""
        echo "All participants started.  PIDs: ${PIDS[*]}"
        echo "Press Ctrl+C to stop everything"
        echo ""
        echo "To launch the dashboard in another terminal:"
        echo "  ./run_demo.sh ui"
        wait
        ;;
    -h|--help|*)
        echo "Usage:"
        echo "  $0 all              Run stations + robots + UI"
        echo "  $0 robots           Run all robots in background"
        echo "  $0 stations         Run all stations in background"
        echo "  $0 ui               Launch the dashboard UI"
        echo "  $0 robot <name>     Run a single robot"
        echo "  $0 station <name>   Run a single station"
        echo ""
        echo "DDS domain: $DOMAIN_ID (all participants auto-discover)"
        echo "Robots: ${ROBOTS[*]}"
        echo "Stations: $(printf '%s ' "${STATIONS[@]}")"
        echo ""
        echo "No port lists needed — DDS SPDP handles discovery."
        exit 0
        ;;
esac
