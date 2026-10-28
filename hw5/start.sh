#!/usr/bin/env bash
set -euo pipefail
export HW5_PROJECT_ID=bu-cs528-prem-project
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
exec bash setup.sh "$@"
