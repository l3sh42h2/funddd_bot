#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")" && pwd)
vps=admin@34.65.234.12
ssh_args=(-i "$HOME/.ssh/google_compute_engine" -o IdentitiesOnly=yes -o UserKnownHostsFile="$HOME/.ssh/google_compute_known_hosts")
release=$(ssh "${ssh_args[@]}" "$vps" 'basename "$(readlink -f /opt/funding-bot/current)"')
[[ "$release" =~ ^[a-zA-Z0-9._-]+$ ]]
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cp "$root"/{profile_runner.py,test_profile.py,funding_bot-sf-collector.service,funding_bot-sf-web.service,funding-scan.slice} "$tmp/"
cp "$root/../../PATCHNOTES/spot-dex-futures-only-20261010.md" "$tmp/patchnote.md"
tar -cf "$tmp/bundle.tar" -C "$tmp" profile_runner.py test_profile.py funding_bot-sf-collector.service funding_bot-sf-web.service funding-scan.slice patchnote.md
hash=$(shasum -a 256 "$tmp/bundle.tar" | cut -d ' ' -f 1)
incoming=$(ssh "${ssh_args[@]}" "$vps" 'mktemp -d /var/tmp/funding-sf-profile.XXXXXXXX')
[[ "$incoming" =~ ^/var/tmp/funding-sf-profile\.[a-zA-Z0-9]+$ ]]
scp "${ssh_args[@]}" "$tmp/bundle.tar" "$root/remote_start.sh" "$vps:$incoming/"
ssh "${ssh_args[@]}" "$vps" "sudo chown -R root:root '$incoming' && sudo chmod 755 '$incoming' && sudo bash '$incoming/remote_start.sh' '$incoming' '$release' '$hash'"
