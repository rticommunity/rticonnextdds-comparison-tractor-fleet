#!/bin/bash

# Run RTI Persistence Service for the the DDS approach tractor fleet.
#
# Persistence Service backs TRANSIENT-durability topics so that
# coverage data survives even when all robot writers are stopped.
# When the UI (or any late joiner) starts, Persistence Service
# replays the stored CoveragePoint samples automatically.
#
# The "default" configuration auto-discovers all TRANSIENT topics
# on the domain — no custom XML needed.
#
# Prerequisites:
#   - $NDDSHOME set to the RTI Connext DDS installation
#
# Usage:
#   ./run_persistence_service.sh

if [ -z "$NDDSHOME" ]; then
    echo "ERROR: \$NDDSHOME is not set."
    echo "  Export NDDSHOME to your RTI Connext DDS installation directory."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export STORAGE_DIRECTORY="$SCRIPT_DIR/persistent_data"
mkdir -p "$STORAGE_DIRECTORY"

echo "Pure DDS: RTI Persistence Service"
echo "====================================="
echo "NDDSHOME:  $NDDSHOME"
echo "Storage:   $STORAGE_DIRECTORY"
echo "Config:    defaultDisk (auto-discovers TRANSIENT topics)"
echo ""
echo "Press Ctrl+C to stop"
echo ""

exec "$NDDSHOME/bin/rtipersistenceservice" -cfgName defaultDisk
