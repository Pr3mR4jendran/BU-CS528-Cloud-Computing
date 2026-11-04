#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

run_id=__HW6_RUN_ID__
trap 'rc=$?; printf "HW6_RUN_FAILED %s exit=%s\n" "$run_id" "$rc"; exit "$rc"' ERR
printf 'HW6_RUN_STARTED %s\n' "$run_id"
cd "$(mktemp -d /var/tmp/hw6.XXXXXXXX)"
base64 -d <<'HW6_BUNDLE' | tar -xzf -
__HW6_BUNDLE__
HW6_BUNDLE

export DEBIAN_FRONTEND=noninteractive
apt-get -q -o Acquire::Retries=3 update
apt-get -q -y -o Acquire::Retries=3 -o DPkg::Lock::Timeout=120 install python3-venv
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt

# A short retry covers late propagation of the VM's temporary secret access.
if ! timeout 360 .venv/bin/python -u train_models.py --db-config runtime.json --output-dir results; then
    sleep 20
    timeout 360 .venv/bin/python -u train_models.py --db-config runtime.json --output-dir results
fi
.venv/bin/python -u upload_results.py
printf 'HW6_RUN_UPLOADED %s\n' "$run_id"
