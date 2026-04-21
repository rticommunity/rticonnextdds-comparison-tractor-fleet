#!/bin/bash

# Script to run the Full Mesh gRPC robot demo.
#
# gRPC endpoints are discovered automatically via Zeroconf mDNS.
# Each robot gets a port derived from its position in ROBOTS[].
#
# Usage:
#   ./demo_start.sh all              - Run stations + robots + UI
#   ./demo_start.sh robots           - Run all robots in background
#   ./demo_start.sh stations         - Run all stations in background
#   ./demo_start.sh ui               - Launch the fleet dashboard UI
#   ./demo_start.sh robot <name>     - Run a single robot
#   ./demo_start.sh station <name>   - Run a single station

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Activate the project venv if not already active
VENV_DIR="$SCRIPT_DIR/../../venv"
if [ -z "$VIRTUAL_ENV" ] && [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
fi

# ── Fleet configuration ──────────────────────────────────────────────────
# Sets ROBOTS[], STATIONS[], UI_PORT (from shared/fleet_common.sh)
# and  ROBOT_BASE_PORT, STATION_BASE_PORT (approach-specific).
source "$SCRIPT_DIR/fleet_config.sh"
# ─────────────────────────────────────────────────────────────────────────

# Helper: compute gRPC port from base + 0-based index
robot_port()  { echo $(( ROBOT_BASE_PORT  + $1 )); }
station_port(){ echo $(( STATION_BASE_PORT + $1 )); }

# Helper: look up 0-based index for a robot name
robot_index() {
    for (( i=0; i<${#ROBOTS[@]}; i++ )); do
        [[ "${ROBOTS[$i]}" == "$1" ]] && echo $i && return
    done
    echo "Error: unknown robot '$1'. Known: ${ROBOTS[*]}" >&2; exit 1
}

# Helper: look up station index + dock coords by name → "index x y"
station_lookup() {
    for (( i=0; i<${#STATIONS[@]}; i++ )); do
        read -r sname sx sy <<< "${STATIONS[$i]}"
        [[ "$sname" == "$1" ]] && echo "$i $sx $sy" && return
    done
    echo "Error: unknown station '$1'. Known: $(printf '%s ' "${STATIONS[@]}")" >&2; exit 1
}

echo "Pure gRPC: Full Mesh Streams Demo"
echo "================================"
echo ""

# Generate type support if needed
if [ ! -f "robot_pb2.py" ]; then
    ./types_generate.sh
fi

case "${1:---help}" in
    ui)
        echo "Starting Fleet Dashboard UI on port $UI_PORT..."
        echo "Robots discovered automatically via Zeroconf mDNS"
        echo ""
        python robot_ui.py --ui-port "$UI_PORT" --same-host
        ;;
    robot)
        ROBOT_NAME="$2"
        if [ -z "$ROBOT_NAME" ]; then
            echo "Error: missing robot name.  Usage: $0 robot <name>"
            echo "Available: ${ROBOTS[*]}"
            exit 1
        fi
        IDX=$(robot_index "$ROBOT_NAME")
        PORT=$(robot_port "$IDX")
        echo "Starting $ROBOT_NAME (port $PORT, mDNS discovery)..."
        python robot_node.py --id "$ROBOT_NAME" --fleet-port "$PORT"
        ;;
    station)
        STATION_NAME="$2"
        if [ -z "$STATION_NAME" ]; then
            echo "Error: missing station name.  Usage: $0 station <name>"
            echo "Available: $(printf '%s ' "${STATIONS[@]}")"
            exit 1
        fi
        read -r sidx sx sy <<< "$(station_lookup "$STATION_NAME")"
        SPORT=$(station_port "$sidx")
        echo "Starting $STATION_NAME on port $SPORT (dock at $sx,$sy)..."
        python charging_station.py --port "$SPORT" \
            --station-id "$STATION_NAME" \
            --dock-x "$sx" --dock-y "$sy"
        ;;
    robots)
        echo "Starting all robots in background (mDNS discovery)..."
        echo ""

        PIDS=()
        for (( i=0; i<${#ROBOTS[@]}; i++ )); do
            NAME="${ROBOTS[$i]}"
            PORT=$(robot_port $i)
            echo "  Starting $NAME on port $PORT"
            python robot_node.py --id "$NAME" --fleet-port "$PORT" &
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
        echo ""

        PIDS=()
        for (( si=0; si<${#STATIONS[@]}; si++ )); do
            read -r sname sx sy <<< "${STATIONS[$si]}"
            sport=$(station_port $si)
            echo "  Starting $sname on port $sport (dock at $sx,$sy)"
            python charging_station.py --port "$sport" \
                --station-id "$sname" \
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
        echo "Starting all stations + robots + UI..."
        echo "gRPC endpoints discovered via Zeroconf mDNS"
        echo ""

        PIDS=()

        # Launch charging stations first
        for (( si=0; si<${#STATIONS[@]}; si++ )); do
            read -r sname sx sy <<< "${STATIONS[$si]}"
            sport=$(station_port $si)
            echo "  Starting $sname on port $sport (dock at $sx,$sy)"
            python charging_station.py --port "$sport" \
                --station-id "$sname" \
                --dock-x "$sx" --dock-y "$sy" &
            PIDS+=($!)
        done
        sleep 1

        for (( i=0; i<${#ROBOTS[@]}; i++ )); do
            NAME="${ROBOTS[$i]}"
            PORT=$(robot_port $i)
            echo "  Starting $NAME on port $PORT"
            python robot_node.py --id "$NAME" --fleet-port "$PORT" &
            PIDS+=($!)
            sleep 2
        done

        cleanup() {
            echo ""
            echo "Stopping all..."
            kill "${PIDS[@]}" 2>/dev/null
            exit 0
        }
        trap cleanup SIGINT SIGTERM

        echo ""
        echo "All robots + stations started.  PIDs: ${PIDS[*]}"
        echo "Launching UI on port $UI_PORT..."
        echo ""
        python robot_ui.py --ui-port "$UI_PORT" --same-host
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
        echo "Robots: ${ROBOTS[*]}"
        echo "Stations: $(printf '%s ' "${STATIONS[@]}")"
        echo ""
        echo "gRPC discovery via Zeroconf mDNS (no port lists needed)."
        exit 0
        ;;
esac
