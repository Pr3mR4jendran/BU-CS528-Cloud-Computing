#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
  printf '%s\n' 'Run ./setup.sh first to create the virtual environment.' >&2
  exit 1
fi
exec .venv/bin/python verify.py --gcloud-auth "$@"
