#!/usr/bin/env bash
# macOS: dois cliques no Finder abrem o Audiobook Generator no Terminal
cd "$(dirname "$0")" || exit 1
./audiobook-cli.sh "$@"
status=$?
if [ $status -ne 0 ]; then
    read -r -p "Pressione Enter para fechar..."
fi
exit $status
