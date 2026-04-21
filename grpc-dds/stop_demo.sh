#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/fleet_config.sh"

echo "Stopping all hybrid (grpc-dds) processes..."

pkill -f "grpc-dds/robot_node.py"
pkill -f "grpc-dds/robot_ui.py"
pkill -f "grpc-dds/charging_station.py"

# Free up ports if still held
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

echo "All processes stopped."
