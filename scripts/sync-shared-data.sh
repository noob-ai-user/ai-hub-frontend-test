#!/usr/bin/env bash
# Sync canonical shared library and auto-import into Marinara when it is running.
set -uo pipefail

python3 /opt/hub/scripts/hub-sync-import.py
exit $?