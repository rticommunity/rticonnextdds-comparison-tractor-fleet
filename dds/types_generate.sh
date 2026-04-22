#!/bin/bash
# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0

# Generate Python type support for the DDS approach.
# Runs rtiddsgen to produce robot_types.py from robot_types.idl.
#
# Usage:
#   ./types_generate.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# NDDSHOME is exported by setup.sourceme — source it first if not set.
if [ -z "${NDDSHOME:-}" ]; then
    echo "ERROR: NDDSHOME is not set. Source setup.sourceme first:"
    echo "  source ../setup.sourceme dds"
    exit 1
fi

RTIDDSGEN="$NDDSHOME/bin/rtiddsgen"
if command -v rtiddsgen &>/dev/null; then
    RTIDDSGEN=rtiddsgen
elif [ ! -x "$RTIDDSGEN" ]; then
    echo "ERROR: rtiddsgen not found at $RTIDDSGEN or on PATH."
    exit 1
fi

echo "Generating DDS type support from robot_types.idl (using $RTIDDSGEN)..."
"$RTIDDSGEN" -update typefiles -language python -d . robot_types.idl
echo "  → robot_types.py"
