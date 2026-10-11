#!/usr/bin/env bash
# Public scanner service installation only. Never switches the bot release.
set -euo pipefail
incoming=$1
expected_release=$2
expected_bundle=$3
exec 9>/run/lock/funding-bot-deploy.lock
flock -n 9 || { echo 'Deployment lock is busy'; exit 1; }
cd "$incoming"
actual_bundle=$(sha256sum bundle.tar | cut -d ' ' -f 1)
test "$actual_bundle" = "$expected_bundle"
mkdir unpacked
tar -xf bundle.tar -C unpacked
cd unpacked
release=$(readlink -f /opt/funding-bot/current)
test "$(basename "$release")" = "$expected_release"
python3 - "$release" <<'PY'
import hashlib,json,pathlib,sys
r=pathlib.Path(sys.argv[1]); m=json.loads((r/'release-manifest.json').read_text())
for name in ('config','collector','client','universe','cli','serve','market_snapshot'):
    p=f'src/funding_bot/{name}.py'
    assert hashlib.sha256((r/p).read_bytes()).hexdigest()==m['sources'][p], p
print('Pinned release source hashes verified')
PY
for u in funding_bot-collector funding_bot-core funding_bot-interface funding_bot-trader; do
    if systemctl is-active --quiet "$u.service"; then
        echo "Refusing startup: existing $u is active"; exit 1
    fi
done
# Only this profile's own units are restarted; existing bot units were checked
# inactive above. Stop them before replacing a running launcher.
systemctl stop funding_bot-sf-web.service funding_bot-sf-collector.service 2>/dev/null || true
install -d -o root -g root -m 755 /opt/funding-bot/scanner-profile
install -m 644 profile_runner.py test_profile.py /opt/funding-bot/scanner-profile/
# systemd reads the root-only EnvironmentFile, exactly as for production units.
# The collector user must not gain direct access to the secrets directory.
systemd-run --quiet --wait --pipe --collect --unit="funding-sf-check-$$" \
    --uid=funding-collector \
    --property=EnvironmentFile=/var/lib/funding-bot/secrets/collector.env \
    --setenv=PYTHONPATH=/opt/funding-bot/current/src \
    --setenv=PYTHONDONTWRITEBYTECODE=1 \
    /opt/funding-bot/current/.venv/bin/python /opt/funding-bot/scanner-profile/test_profile.py
systemd-analyze verify funding_bot-sf-collector.service funding_bot-sf-web.service funding-scan.slice
install -d -o funding-collector -g funding-market -m 750 /var/lib/funding-bot/collector/public/sf-only
install -m 644 funding_bot-sf-collector.service funding_bot-sf-web.service funding-scan.slice /etc/systemd/system/
install -m 644 patchnote.md /opt/funding-bot/scanner-profile/PATCHNOTE.md
python3 - "$expected_release" "$expected_bundle" <<'PY'
import json,sys,time
from pathlib import Path
Path('/opt/funding-bot/scanner-profile/install-record.json').write_text(json.dumps(
    {'base_release':sys.argv[1],'bundle_sha256':sys.argv[2],'installed_at':time.time(),
     'profile':'spot_dex_futures_only','trading_started':False},indent=2))
PY
systemctl daemon-reload
systemctl enable --now funding_bot-sf-collector.service
# Start public web only after the collector publishes a new, nonempty snapshot.
python3 - <<'PY'
import json,time
from pathlib import Path
p=Path('/var/lib/funding-bot/collector/public/sf-only/table.json')
deadline=time.monotonic()+300
while time.monotonic()<deadline:
    try:
        d=json.loads(p.read_text())
        if d.get('scan_mode')=='spot_dex_futures_only' and d.get('sf_rows') and not d.get('ff_rows') and time.time()-d['tick_ts']<60:
            print('Fresh SF-only table:',len(d['sf_rows']),'rows'); break
    except (OSError,KeyError,ValueError):
        pass
    time.sleep(5)
else:
    raise SystemExit('No fresh SF-only snapshot; web was not started')
PY
systemctl enable --now funding_bot-sf-web.service
if [ -e /etc/nginx/sites-enabled/funding-scanner ]; then
    systemctl start nginx
else
    systemctl start funding_bot-tunnel.service
fi
systemctl is-active funding_bot-sf-collector.service funding_bot-sf-web.service
