#!/usr/bin/env bash
set -euo pipefail
export HW5_PROJECT_ID=bu-cs528-prem-project
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
HW5_PYTHON=python3
if [[ -x .venv/bin/python ]]; then HW5_PYTHON=.venv/bin/python; fi
exec "$HW5_PYTHON" provision.py cleanup "$@"
