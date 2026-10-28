#!/usr/bin/env bash
set -euo pipefail
exec > >(tee -a /var/log/hw5-startup.log) 2>&1
meta() { curl --fail --silent --show-error --retry 5 -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/$1"; }
ROLE="$(meta instance/attributes/hw5-role)"
BUNDLE="$(meta instance/attributes/hw5-bundle)"
HASH="$(meta instance/attributes/hw5-sha256)"
[[ "$ROLE" =~ ^(server|reporter|client)$ ]] || exit 1
install -d /var/lib/hw5
apply_server_context() {
  /opt/hw5/venv/bin/python - <<'PY'
import json, pathlib, urllib.request, urllib.error
base=pathlib.Path('/opt/hw5/app/runtime.json')
config=json.loads(base.read_text())
req=urllib.request.Request('http://metadata.google.internal/computeMetadata/v1/instance/attributes/hw5-experiment-control',headers={'Metadata-Flavor':'Google'})
try:
    with urllib.request.urlopen(req,timeout=10) as response:
        config['experimentControl']=json.load(response)
except urllib.error.HTTPError as error:
    if error.code != 404: raise
pathlib.Path('/var/lib/hw5/runtime.json').write_text(json.dumps(config))
PY
  chown hw5app:hw5app /var/lib/hw5/runtime.json
}
start_experiment_if_requested() {
  if curl --fail --silent -H 'Metadata-Flavor: Google' 'http://metadata.google.internal/computeMetadata/v1/instance/attributes/hw5-experiment-job' >/var/lib/hw5/experiment-job.json; then
    chown hw5app:hw5app /var/lib/hw5/experiment-job.json
    chmod 600 /var/lib/hw5/experiment-job.json
    systemctl start hw5-clients.service
  fi
}
if [[ -f /var/lib/hw5/startup_already_done ]]; then
  if [[ "$(cat /var/lib/hw5/bundle-sha256)" == "$HASH" ]]; then
    if [[ "$ROLE" == server ]]; then
      systemctl stop hw5-server.service
      apply_server_context
      systemctl start hw5-server.service
    elif [[ "$ROLE" == reporter ]]; then
      systemctl start hw5-reporter.service
    else
      start_experiment_if_requested
    fi
    echo "HW5_READY role=$ROLE sha256=$HASH (existing installation)"
    exit 0
  fi
  echo 'Installing updated release from startup metadata; preserving /var/lib/hw5 evidence.'
  if [[ "$ROLE" != client ]]; then systemctl stop "hw5-$ROLE.service"; fi
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip ca-certificates curl
id hw5app >/dev/null 2>&1 || useradd --system --home-dir /var/lib/hw5 --shell /usr/sbin/nologin hw5app
install -d /opt/hw5/app
export HW5_BUNDLE="$BUNDLE" HW5_SHA256="$HASH"
python3 - <<'PY'
import hashlib, json, os, pathlib, tarfile, urllib.parse, urllib.request
base = 'http://metadata.google.internal/computeMetadata/v1/'
req = urllib.request.Request(base+'instance/service-accounts/default/token', headers={'Metadata-Flavor':'Google'})
with urllib.request.urlopen(req, timeout=30) as response:
    token = json.load(response)['access_token']
bucket, obj = os.environ['HW5_BUNDLE'][5:].split('/', 1)
url = 'https://storage.googleapis.com/storage/v1/b/'+bucket+'/o/'+urllib.parse.quote(obj, safe='')+'?alt=media'
req = urllib.request.Request(url, headers={'Authorization':'Bearer '+token})
with urllib.request.urlopen(req, timeout=120) as response:
    payload = response.read()
if hashlib.sha256(payload).hexdigest() != os.environ['HW5_SHA256']:
    raise RuntimeError('Bundle checksum mismatch')
archive = pathlib.Path('/opt/hw5/bundle.tar.gz')
archive.write_bytes(payload)
with tarfile.open(archive) as bundle:
    bundle.extractall('/opt/hw5/app', filter='data')
PY
python3 -m venv /opt/hw5/venv
/opt/hw5/venv/bin/python -m pip install --disable-pip-version-check -r /opt/hw5/app/requirements.txt
chmod 755 /opt/hw5/app/http-client
chown hw5app:hw5app /var/lib/hw5
if [[ "$ROLE" == server ]]; then
  touch /var/lib/hw5/timings.jsonl /var/lib/hw5/database-failures.jsonl
  chown hw5app:hw5app /var/lib/hw5/timings.jsonl /var/lib/hw5/database-failures.jsonl
fi
if [[ "$ROLE" == client ]]; then
  /opt/hw5/app/http-client --help > /var/lib/hw5/http-client-help.txt
  cat > /etc/systemd/system/hw5-clients.service <<EOF
[Unit]
Description=CS528 HW5 unattended pilot and two-client experiment
Wants=network-online.target
After=network-online.target
[Service]
User=hw5app
Group=hw5app
WorkingDirectory=/opt/hw5/app
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/hw5/venv/bin/python /opt/hw5/app/run_clients.py --job /var/lib/hw5/experiment-job.json
Restart=no
TimeoutStopSec=60
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/hw5
StandardOutput=journal+console
StandardError=journal+console
EOF
  systemctl daemon-reload
fi
if [[ "$ROLE" != client ]]; then
  APP=server.py
  RUNTIME=/opt/hw5/app/runtime.json
  if [[ "$ROLE" == server ]]; then apply_server_context; RUNTIME=/var/lib/hw5/runtime.json; fi
  if [[ "$ROLE" == reporter ]]; then APP=subscriber.py; fi
  cat > "/etc/systemd/system/hw5-$ROLE.service" <<EOF
[Unit]
Description=CS528 HW5 $ROLE
Wants=network-online.target
After=network-online.target
[Service]
User=hw5app
Group=hw5app
WorkingDirectory=/opt/hw5/app
Environment=PYTHONUNBUFFERED=1
Environment=HW5_BUNDLE_SHA256=$HASH
ExecStart=/opt/hw5/venv/bin/python /opt/hw5/app/$APP --config $RUNTIME
Restart=on-failure
RestartSec=5
TimeoutStopSec=60
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/hw5
StandardOutput=journal+console
StandardError=journal+console
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "hw5-$ROLE.service"
  systemctl restart "hw5-$ROLE.service"
fi
printf '%s' "$HASH" > /var/lib/hw5/bundle-sha256
touch /var/lib/hw5/startup_already_done
if [[ "$ROLE" == client ]]; then start_experiment_if_requested; fi
echo "HW5_READY role=$ROLE sha256=$HASH"
