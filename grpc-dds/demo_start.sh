#!/bin/bash
# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0

# Script to run the Hybrid DDS + gRPC robot demo.
#
# DDS handles pub-sub (automatic discovery via SPDP).
# gRPC handles commands (robot → UI) and charging negotiations.
# Zeroconf discovers gRPC endpoints for the UI command path.
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

echo "Hybrid: DDS + gRPC Demo"
echo "===================================="
echo ""

# NDDSHOME is exported by setup.sourceme — source it first if not set.
if [ -z "${NDDSHOME:-}" ]; then
    echo "ERROR: NDDSHOME is not set. Source setup.sourceme first:"
    echo "  source ../setup.sourceme grpc-dds"
    exit 1
fi

# Generate type support if needed
if [ ! -f "robot_pb2.py" ] || [ ! -f "robot_types.py" ]; then
    ./types_generate.sh
fi

case "${1:---help}" in
    ui)
        echo "Starting Fleet Dashboard UI on port $UI_PORT..."
        echo "DDS domain $DOMAIN_ID — pub-sub data discovered automatically"
        echo "gRPC commands via Zeroconf discovery"
        echo ""
        python robot_ui.py --domain "$DOMAIN_ID" --ui-port "$UI_PORT" --same-host
        ;;
    persistence)
        echo "RTI Persistence Service"
        echo "===================================="
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
            echo "Available: ${ROBOTS[*]}"
            exit 1
        fi
        IDX=$(robot_index "$ROBOT_NAME")
        PORT=$(robot_port "$IDX")
        echo "Starting $ROBOT_NAME (DDS domain $DOMAIN_ID, gRPC port $PORT)..."
        python robot_node.py --id "$ROBOT_NAME" --domain "$DOMAIN_ID" --grpc-port "$PORT"
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
        echo "Starting $STATION_NAME on gRPC port $SPORT (dock at $sx,$sy)..."
        python charging_station.py --station-id "$STATION_NAME" --domain "$DOMAIN_ID" \
            --port "$SPORT" \
            --dock-x "$sx" --dock-y "$sy"
        ;;
    robots)
        echo "Starting all robots in background..."
        echo "DDS domain $DOMAIN_ID, gRPC discovery via Zeroconf"
        echo ""

        PIDS=()
        for (( i=0; i<${#ROBOTS[@]}; i++ )); do
            NAME="${ROBOTS[$i]}"
            PORT=$(robot_port $i)
            echo "  Starting $NAME (gRPC port $PORT)"
            python robot_node.py --id "$NAME" --domain "$DOMAIN_ID" --grpc-port "$PORT" &
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
            sport=$(station_port $si)
            echo "  Starting $sname on gRPC port $sport (dock at $sx,$sy)"
            python charging_station.py --station-id "$sname" --domain "$DOMAIN_ID" \
                --port "$sport" \
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
        echo "DDS domain $DOMAIN_ID — pub-sub auto-discovered via SPDP"
        echo "gRPC command/charging endpoints discovered via Zeroconf mDNS"
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

        # Launch charging stations first
        for (( si=0; si<${#STATIONS[@]}; si++ )); do
            read -r sname sx sy <<< "${STATIONS[$si]}"
            sport=$(station_port $si)
            echo "  Starting $sname on gRPC port $sport (dock at $sx,$sy)"
            python charging_station.py --station-id "$sname" --domain "$DOMAIN_ID" \
                --port "$sport" \
                --dock-x "$sx" --dock-y "$sy" &
            PIDS+=($!)
        done
        sleep 1

        for (( i=0; i<${#ROBOTS[@]}; i++ )); do
            NAME="${ROBOTS[$i]}"
            PORT=$(robot_port $i)
            echo "  Starting $NAME (gRPC port $PORT)"
            python robot_node.py --id "$NAME" --domain "$DOMAIN_ID" --grpc-port "$PORT" &
            PIDS+=($!)
            sleep 2
        done

        echo "Starting Fleet Dashboard UI on port $UI_PORT..."
        echo "DDS domain $DOMAIN_ID — pub-sub data discovered automatically"
        echo "gRPC commands via Zeroconf discovery"
        echo ""
        python robot_ui.py --domain "$DOMAIN_ID" --ui-port "$UI_PORT" --same-host


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
        echo "Robots: ${ROBOTS[*]}"
        echo "Stations: $(printf '%s ' "${STATIONS[@]}")"
        echo ""
        echo "DDS domain: $DOMAIN_ID | gRPC via Zeroconf mDNS"
        exit 0
        ;;
esac
