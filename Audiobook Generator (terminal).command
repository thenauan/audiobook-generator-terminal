#!/usr/bin/env bash
# macOS: double-click in Finder to open Audiobook Generator in Terminal
cd "$(dirname "$0")" || exit 1
./audiobook-cli.sh "$@"
status=$?
if [ $status -ne 0 ]; then
    read -r -p "Press Enter to close..."
fi
exit $status
