#!/bin/bash
# Generate Python type support for the hybrid gRPC+DDS approach.
# Runs both protoc (for gRPC) and rtiddsgen (for DDS types).
#
# Usage:
#   ./types_generate.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Activate the project venv if not already active
VENV_DIR="$SCRIPT_DIR/../../venv"
if [ -z "$VIRTUAL_ENV" ] && [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
fi

# NDDSHOME is exported by setup.sourceme — source it first if not set.
if [ -z "${NDDSHOME:-}" ]; then
    echo "ERROR: NDDSHOME is not set. Source setup.sourceme first:"
    echo "  source ../setup.sourceme grpc-dds"
    exit 1
fi

# ── protobuf / gRPC ───────────────────────────────────────────────────────
echo "Generating protobuf stubs from robot.proto..."
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. robot.proto
echo "  → robot_pb2.py, robot_pb2_grpc.py"

# ── DDS types ─────────────────────────────────────────────────────────────
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
