#!/bin/bash
# Generate Python type support for the gRPC approach.
# Runs protoc to produce robot_pb2.py and robot_pb2_grpc.py from robot.proto.
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

echo "Generating protobuf stubs from robot.proto..."
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. robot.proto
echo "  → robot_pb2.py, robot_pb2_grpc.py"
