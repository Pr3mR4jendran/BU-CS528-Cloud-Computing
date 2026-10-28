#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
exec /opt/hw5/venv/bin/python run_clients.py "$@"
