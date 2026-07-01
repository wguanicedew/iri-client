#!/usr/bin/env bash
# Source this file to add the pure-Python IRI client to PYTHONPATH.
#   source setup.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT_DIR="$SCRIPT_DIR/src/python"

if [[ ":$PYTHONPATH:" != *":$CLIENT_DIR:"* ]]; then
    export PYTHONPATH="$CLIENT_DIR${PYTHONPATH:+:$PYTHONPATH}"
fi

echo "PYTHONPATH updated: $CLIENT_DIR added"
