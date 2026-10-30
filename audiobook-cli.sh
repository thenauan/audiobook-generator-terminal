#!/usr/bin/env bash
# Audiobook Generator no terminal (macOS e Linux): ./audiobook-cli.sh [livros...] [-o pasta]
cd "$(dirname "$0")" || exit 1

PYTHON=""
for p in python3 python; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        PYTHON="$p"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "Precisa do Python 3.10 ou mais novo."
    echo "  macOS:  brew install python"
    echo "  Linux:  sudo apt install python3 python3-pip python3-tk   (ou dnf / pacman)"
    exit 1
fi

exec "$PYTHON" audiobook_cli.py "$@"
