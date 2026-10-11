#!/bin/bash
set -euo pipefail
dir=$1
exec 9>/run/lock/funding-bot-deploy.lock
flock -n 9 || { echo 'Deployment lock busy'; exit 1; }
systemctl is-active --quiet funding_bot-sf-web.service
# The 1 GiB soft ceiling caused millions of reclaim/throttle events without OOM.
# Keep the 1.5 GiB aggregate hard ceiling, and give the working set headroom.
install -m 644 "$dir/funding-scan.slice" /etc/systemd/system/funding-scan.slice
systemctl daemon-reload
systemctl set-property funding-scan.slice MemoryHigh=1280M
if ! command -v nginx >/dev/null; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx
fi
install -m 644 "$dir/public-ip.nginx.conf" /etc/nginx/sites-available/funding-scanner
ln -sfn /etc/nginx/sites-available/funding-scanner /etc/nginx/sites-enabled/funding-scanner
if [ -L /etc/nginx/sites-enabled/default ]; then unlink /etc/nginx/sites-enabled/default; fi
install -d -m 755 /etc/systemd/system/nginx.service.d
cat > /etc/systemd/system/nginx.service.d/funding-scanner.conf <<'UNIT'
[Service]
Slice=funding-scan.slice
CPUWeight=10
Restart=on-failure
RestartSec=5
UNIT
nginx -t
systemctl daemon-reload
systemctl enable nginx
systemctl restart nginx
curl --fail --max-time 30 -s -o /dev/null http://127.0.0.1/
test "$(curl --max-time 5 -s -o /dev/null -w '%{http_code}' http://127.0.0.1/cabinet)" = 404
systemctl is-active nginx funding_bot-sf-web.service funding_bot-sf-collector.service
