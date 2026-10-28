#!/usr/bin/env bash
set -euo pipefail
export HW5_PROJECT_ID=bu-cs528-prem-project
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
  printf '%s\n' 'Run ./setup.sh first to create the virtual environment.' >&2
  exit 1
fi
exec .venv/bin/python provision.py update "$@"
