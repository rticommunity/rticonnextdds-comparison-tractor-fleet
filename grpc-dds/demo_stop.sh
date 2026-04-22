#!/bin/bash
# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0

# demo_stop.sh — Kill hybrid gRPC+DDS applications.
#
# Usage:
#   ./demo_stop.sh                 # stop everything
#   ./demo_stop.sh robots          # stop robot nodes only
#   ./demo_stop.sh ui              # stop the dashboard UI only
#   ./demo_stop.sh persistence     # stop RTI Persistence Service only

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/fleet_config.sh"

# Free up gRPC ports held by a given pattern
free_ports() {
    for port in "$@"; do
        pid=$(lsof -ti:$port 2>/dev/null)
        if [ -n "$pid" ]; then
            echo "Killing process $pid using port $port"
            kill -9 $pid 2>/dev/null
        fi
    done
}

case "${1:-all}" in
    robots)
        echo "Stopping robot nodes..."
        pkill -f "grpc-dds/robot_node.py"
        ROBOT_PORTS=()
        for (( i=0; i<${#ROBOTS[@]}; i++ )); do
            ROBOT_PORTS+=( $(( ROBOT_BASE_PORT + i )) )
        done
        free_ports "${ROBOT_PORTS[@]}"
        echo "Robot nodes stopped."
        ;;
    ui)
        echo "Stopping dashboard UI..."
        pkill -f "grpc-dds/robot_ui.py"
        free_ports "$UI_PORT"
        echo "UI stopped."
        ;;
    persistence)
        echo "Stopping RTI Persistence Service..."
        pkill -f rtipersistenceservice
        echo "Persistence Service stopped."
        ;;
    all)
        echo "Stopping all hybrid (grpc-dds) processes..."
        pkill -f "grpc-dds/robot_node.py"
        pkill -f "grpc-dds/robot_ui.py"
        pkill -f "grpc-dds/charging_station.py"
        pkill -f rtipersistenceservice
        ALL_PORTS=("$UI_PORT")
        for (( i=0; i<${#ROBOTS[@]}; i++ )); do
            ALL_PORTS+=( $(( ROBOT_BASE_PORT + i )) )
        done
        for (( si=0; si<${#STATIONS[@]}; si++ )); do
            ALL_PORTS+=( $(( STATION_BASE_PORT + si )) )
        done
        free_ports "${ALL_PORTS[@]}"
        echo "All processes stopped."
        ;;
    *)
        echo "Usage: $0 [all|robots|ui|persistence]"
        exit 1
        ;;
esac
