#!/usr/bin/env bash
# One-time setup on a fresh Linux server (Ubuntu/Debian). Run from the repo root:
#   bash setup_server.sh            # bots only
#   bash setup_server.sh --research # also numpy/scipy for research scripts
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null; then
    sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip
fi
python3 -m venv .venv || { sudo apt-get install -y python3-venv && python3 -m venv .venv; }
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r queen-of-heaven/requirements.txt
if [[ "${1:-}" == "--research" ]]; then
    .venv/bin/pip install -r requirements-research.txt
fi

for project in queen-of-heaven veni-vidi-vici; do
    mkdir -p "$project/logs" "$project/reports"
    if [[ -f "$project/.env.example" && ! -f "$project/.env" ]]; then
        cp "$project/.env.example" "$project/.env" && chmod 600 "$project/.env"
    fi
done

echo "Done. Test with: cd queen-of-heaven && ../.venv/bin/python -m pytest -q"
