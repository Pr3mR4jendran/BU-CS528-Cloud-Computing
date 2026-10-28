#!/usr/bin/env bash
set -euo pipefail
export HW5_PROJECT_ID=bu-cs528-prem-project
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
# The deployment driver creates/reuses Cloud SQL before initializing the schema
# and configuring the VMs. No Console instance creation is required.
# Pass --sql-only to create/start SQL and its schema without provisioning VMs.
exec .venv/bin/python provision.py setup "$@"
