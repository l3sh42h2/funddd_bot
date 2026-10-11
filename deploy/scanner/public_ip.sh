#!/bin/bash
set -euo pipefail
root=$(cd "$(dirname "$0")" && pwd)
ssh_args=(-i "$HOME/.ssh/google_compute_engine" -o IdentitiesOnly=yes -o UserKnownHostsFile="$HOME/.ssh/google_compute_known_hosts")
vps=admin@34.65.234.12
stage=$(ssh "${ssh_args[@]}" "$vps" 'mktemp -d /var/tmp/funding-public-ip.XXXXXXXX')
[[ "$stage" =~ ^/var/tmp/funding-public-ip\.[a-zA-Z0-9]+$ ]]
scp "${ssh_args[@]}" "$root"/{public_ip_remote.sh,public-ip.nginx.conf,funding-scan.slice} "$vps:$stage/"
ssh "${ssh_args[@]}" "$vps" "sudo chown -R root:root '$stage' && sudo bash '$stage/public_ip_remote.sh' '$stage'"
# GCP prerequisite: reserved IP ireland-static, TCP 80 rule with target tag
# funding-dashboard-public attached to ireland. Inspect the existing rule before
# changing GCP networking; this runner does not broaden cloud firewall access.
curl --fail --max-time 30 -s -o /dev/null http://34.65.234.12/
ssh "${ssh_args[@]}" "$vps" 'sudo flock -n /run/lock/funding-bot-deploy.lock bash -c '\''systemctl disable --now funding_bot-tunnel.service; echo http://34.65.234.12/ > /var/lib/funding-bot/interface/public_url.txt'\'''
