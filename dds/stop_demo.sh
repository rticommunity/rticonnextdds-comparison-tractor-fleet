#!/bin/bash
# stop_demo.sh — Kill all DDS applications.
# No ports to free — DDS uses multicast, not explicit listen ports.

echo "Stopping all DDS processes..."

pkill -f robot_node.py
pkill -f charging_station.py
pkill -f command_console.py
pkill -f robot_ui.py

echo "All processes stopped."
