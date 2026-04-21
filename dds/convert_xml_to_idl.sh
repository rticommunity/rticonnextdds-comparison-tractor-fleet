#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

INPUT_XML="${1:-robot_types.xml}"

usage() {
    cat <<EOF
Usage:
    ./convert_xml_to_idl.sh [input.xml]

Examples:
  ./convert_xml_to_idl.sh
    ./convert_xml_to_idl.sh robot_types.xml

Converts an RTI Connext IDL-XML file into plain .idl text using:
  rtiddsgen -convertToIdl -inputXml

Writes the generated .idl next to the input XML file.
EOF
}

if [[ "${INPUT_XML}" == "-h" || "${INPUT_XML}" == "--help" ]]; then
    usage
    exit 0
fi

if [[ $# -gt 1 ]]; then
        echo "Error: this script accepts at most one argument: [input.xml]" >&2
        usage >&2
        exit 1
fi

if [[ ! -f "$INPUT_XML" ]]; then
    echo "Error: input file not found: $INPUT_XML" >&2
    exit 1
fi

find_rtiddsgen() {
    if command -v rtiddsgen >/dev/null 2>&1; then
        command -v rtiddsgen
        return 0
    fi

    if [[ -n "${NDDSHOME:-}" && -x "$NDDSHOME/bin/rtiddsgen" ]]; then
        echo "$NDDSHOME/bin/rtiddsgen"
        return 0
    fi

    for candidate in \
        /Applications/rti_connext_dds-7.7.0/bin/rtiddsgen \
        /Applications/rti_connext_dds-7.6.0/bin/rtiddsgen \
        /Applications/rti_connext_dds-7.5.0/bin/rtiddsgen; do
        if [[ -x "$candidate" ]]; then
            echo "$candidate"
            return 0
        fi
    done

    return 1
}

RTIDDSGEN="$(find_rtiddsgen || true)"
if [[ -z "$RTIDDSGEN" ]]; then
    echo "Error: rtiddsgen not found." >&2
    echo "Set NDDSHOME or add rtiddsgen to PATH." >&2
    exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

INPUT_ABS="$(cd "$(dirname "$INPUT_XML")" && pwd)/$(basename "$INPUT_XML")"
OUTPUT_IDL="${INPUT_ABS%.xml}.idl"
GENERATED_NAME="$(basename "${INPUT_XML%.xml}.idl")"

echo "Using rtiddsgen: $RTIDDSGEN"
echo "Converting $INPUT_XML -> $OUTPUT_IDL"

"$RTIDDSGEN" \
    -convertToIdl \
    -inputXml "$INPUT_ABS" \
    -d "$TMP_DIR"

if [[ ! -f "$TMP_DIR/$GENERATED_NAME" ]]; then
    echo "Error: rtiddsgen did not produce $GENERATED_NAME" >&2
    exit 1
fi

mv -f "$TMP_DIR/$GENERATED_NAME" "$OUTPUT_IDL"

echo "Wrote $OUTPUT_IDL"