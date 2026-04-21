#!/bin/bash
# demo_stop.sh — Kill DDS applications.
# No ports to free — DDS uses multicast, not explicit listen ports.
#
# Usage:
#   ./demo_stop.sh                 # stop everything
#   ./demo_stop.sh robots          # stop robot nodes only
#   ./demo_stop.sh ui              # stop the dashboard UI only
#   ./demo_stop.sh persistence     # stop RTI Persistence Service only

case "${1:-all}" in
    robots)
        echo "Stopping robot nodes..."
        pkill -f robot_node.py
        echo "Robot nodes stopped."
        ;;
    ui)
        echo "Stopping dashboard UI..."
        pkill -f robot_ui.py
        echo "UI stopped."
        ;;
    persistence)
        echo "Stopping RTI Persistence Service..."
        pkill -f rtipersistenceservice
        echo "Persistence Service stopped."
        ;;
    all)
        echo "Stopping all DDS processes..."
        pkill -f robot_node.py
        pkill -f charging_station.py
        pkill -f robot_ui.py
        pkill -f rtipersistenceservice
        echo "All processes stopped."
        ;;
    *)
        echo "Usage: $0 [all|robots|ui|persistence]"
        exit 1
        ;;
esac
