#!/bin/bash

# Script to run the Pure DDS (Connext) robot demo.
#
# DDS discovery is automatic — no port lists, no Zeroconf, no mDNS.
# All participants on the same domain ID find each other via SPDP.
#
# Usage:
#   ./demo_start.sh all              - Run stations + robots + UI
#   ./demo_start.sh robots           - Run all robots in background
#   ./demo_start.sh stations         - Run all stations in background
#   ./demo_start.sh ui               - Launch the fleet dashboard UI
#   ./demo_start.sh persistence      - Run RTI Persistence Service
#   ./demo_start.sh robot <name>     - Run a single robot
#   ./demo_start.sh station <name>   - Run a single station
#
# Examples (separate VS Code terminals):
#   Terminal 1:  ./demo_start.sh stations
#   Terminal 2:  ./demo_start.sh robots
#   Terminal 3:  ./demo_start.sh ui

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

# NDDSHOME is exported by setup.sourceme — source it first if not set.
if [ -z "${NDDSHOME:-}" ]; then
    echo "ERROR: NDDSHOME is not set. Source setup.sourceme first:"
    echo "  source ../setup.sourceme dds"
    exit 1
fi

# Generate type support if needed
if [ ! -f "robot_types.py" ]; then
    ./types_generate.sh
fi

case "${1:---help}" in
    ui)
        echo "Starting Fleet Dashboard UI on port $UI_PORT..."
        echo "DDS domain $DOMAIN_ID — robots discovered automatically"
        echo ""
        python robot_ui.py --domain "$DOMAIN_ID" --ui-port "$UI_PORT"
        ;;
    persistence)
        echo "RTI Persistence Service"
        echo "====================================="
        echo "NDDSHOME:  $NDDSHOME"
        STORAGE_DIR="$SCRIPT_DIR/persistent_data"
        mkdir -p "$STORAGE_DIR"
        export STORAGE_DIRECTORY="$STORAGE_DIR"
        echo "Storage:   $STORAGE_DIR"
        echo "Config:    defaultDisk (auto-discovers TRANSIENT topics)"
        echo ""
        echo "Press Ctrl+C to stop"
        echo ""
        exec "$NDDSHOME/bin/rtipersistenceservice" -cfgName defaultDisk
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
        echo "  ./demo_start.sh robot <name>"
        echo "  ./demo_start.sh station <name> --dock-x X --dock-y Y"
        echo "  ./demo_start.sh ui"
        echo ""

        PIDS=()

        # Launch Persistence Service first so TRANSIENT data is captured from the start
        STORAGE_DIR="$SCRIPT_DIR/persistent_data"
        mkdir -p "$STORAGE_DIR"
        export STORAGE_DIRECTORY="$STORAGE_DIR"
        echo "  Starting RTI Persistence Service (storage: $STORAGE_DIR)"
        "$NDDSHOME/bin/rtipersistenceservice" -cfgName defaultDisk &
        PIDS+=($!)
        sleep 1

        # Launch charging stations
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
            echo "Stopping all robots, stations, and persistence service..."
            kill "${PIDS[@]}" 2>/dev/null
            exit 0
        }
        trap cleanup SIGINT SIGTERM

        echo ""
        echo "All participants started.  PIDs: ${PIDS[*]}"
        echo "Press Ctrl+C to stop everything"
        echo ""
        echo "To launch the dashboard in another terminal:"
        echo "  ./demo_start.sh ui"
        wait
        ;;
    -h|--help|*)
        echo "Usage:"
        echo "  $0 all              Run stations + robots + UI"
        echo "  $0 robots           Run all robots in background"
        echo "  $0 stations         Run all stations in background"
        echo "  $0 ui               Launch the dashboard UI"
        echo "  $0 persistence      Run RTI Persistence Service"
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
