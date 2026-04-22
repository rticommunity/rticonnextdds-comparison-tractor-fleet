#!/bin/bash
# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/fleet_config.sh"

echo "Stopping all robot processes..."

# Kill any running robot_node.py, robot_ui.py, and charging_station.py processes
pkill -f robot_node.py
pkill -f robot_ui.py
pkill -f charging_station.py

# Also free up the ports if they're still held
_ALL_PORTS=("$UI_PORT")
for (( i=0; i<${#ROBOTS[@]}; i++ )); do
    _ALL_PORTS+=( $(( ROBOT_BASE_PORT + i )) )
done
for (( si=0; si<${#STATIONS[@]}; si++ )); do
    _ALL_PORTS+=( $(( STATION_BASE_PORT + si )) )
done

for port in "${_ALL_PORTS[@]}"; do
    pid=$(lsof -ti:$port 2>/dev/null)
    if [ ! -z "$pid" ]; then
        echo "Killing process $pid using port $port"
        kill -9 $pid 2>/dev/null
    fi
done

echo "All robots stopped."
