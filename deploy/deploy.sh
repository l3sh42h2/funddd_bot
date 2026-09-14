#!/usr/bin/env bash
# The only entry point for M5 build, base inspection, install and status.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
M5="$ROOT/deploy/migration"
VPS="${FUNDING_DEPLOY_VPS:-admin@34.65.234.12}"
SSH_KEY="${FUNDING_DEPLOY_SSH_KEY:-$HOME/.ssh/google_compute_engine}"
KNOWN_HOSTS="${FUNDING_DEPLOY_KNOWN_HOSTS:-$HOME/.ssh/google_compute_known_hosts}"
SSH=(ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o UserKnownHostsFile="$KNOWN_HOSTS")
SCP=(scp -i "$SSH_KEY" -o IdentitiesOnly=yes -o UserKnownHostsFile="$KNOWN_HOSTS")

die() { echo "ERROR: $*" >&2; exit 2; }
need_file() { [ -f "$1" ] || die "missing file: $1"; }

usage() {
  cat <<'EOF'
Usage:
  deploy/deploy.sh build --output ARTIFACT.tar --patchnote PATCHNOTES/name.md [--python /linux/python]
  deploy/deploy.sh inspect-base --output expected-base.json
  deploy/deploy.sh install --artifact ARTIFACT.tar --receipt ARTIFACT.tar.receipt.json --expected-base expected-base.json
  deploy/deploy.sh status RELEASE_ID

Build must run in the compatible Linux verification environment. Install uploads
only immutable inputs, then starts one detached systemd job which owns flock,
drain, backup, switch, readiness and any compatible code-only rollback.
EOF
}

export FUNDING_M5_ENTRY=deploy/deploy.sh
cmd="${1:-}"; [ -n "$cmd" ] || { usage; exit 2; }; shift

case "$cmd" in
  build)
    output= patchnote= python="${PYTHON:-python3}"
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --output) output="${2:-}"; shift 2;;
        --patchnote) patchnote="${2:-}"; shift 2;;
        --python) python="${2:-}"; shift 2;;
        *) die "unknown build argument: $1";;
      esac
    done
    [ -n "$output" ] && [ -n "$patchnote" ] || die "--output and --patchnote are required"
    exec "$python" "$M5/build_verified.py" --root "$ROOT" --output "$output" \
      --profile "$M5/test-profile-linux.json" --compatibility "$M5/compatibility.json" \
      --patchnote "$patchnote" --python "$python"
    ;;
  inspect-base)
    output=
    while [ "$#" -gt 0 ]; do
      case "$1" in --output) output="${2:-}"; shift 2;; *) die "unknown inspect argument: $1";; esac
    done
    [ -n "$output" ] || die "--output is required"
    incoming="$("${SSH[@]}" "$VPS" "mktemp -d /var/tmp/funding-m5-inspect.XXXXXXXX")"
    [[ "$incoming" =~ ^/var/tmp/funding-m5-inspect\.[a-zA-Z0-9]+$ ]] || die "unsafe remote temporary path"
    "${SCP[@]}" "$M5/artifacts.py" "$M5/deploy_ipc.py" "$M5/prepare_layout.py" "$M5/server_job.py" "$VPS:$incoming/" >/dev/null
    "${SSH[@]}" "$VPS" "sudo env FUNDING_M5_ENTRY=deploy/deploy.sh /usr/bin/python3 '$incoming/server_job.py' inspect-base" > "$output"
    "${SSH[@]}" "$VPS" "rm -rf '$incoming'"
    echo "base snapshot: $output"
    ;;
  install)
    artifact= receipt= expected=
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --artifact) artifact="${2:-}"; shift 2;;
        --receipt) receipt="${2:-}"; shift 2;;
        --expected-base) expected="${2:-}"; shift 2;;
        *) die "unknown install argument: $1";;
      esac
    done
    [ -n "$artifact" ] && [ -n "$receipt" ] && [ -n "$expected" ] || die "artifact, receipt and expected base required"
    need_file "$artifact"; need_file "$receipt"; need_file "$expected"
    release_id="$("${PYTHON:-python3}" -c 'import json,sys; print(json.load(open(sys.argv[1]))["release_id"])' "$receipt")"
    [[ "$release_id" =~ ^[a-zA-Z0-9._-]{8,80}$ ]] || die "unsafe release id"
    incoming="$("${SSH[@]}" "$VPS" "mktemp -d /var/tmp/funding-m5-$release_id.XXXXXXXX")"
    [[ "$incoming" =~ ^/var/tmp/funding-m5-[a-zA-Z0-9._-]+\.[a-zA-Z0-9]+$ ]] || die "unsafe remote temporary path"
    "${SCP[@]}" "$artifact" "$VPS:$incoming/artifact.tar" >/dev/null
    "${SCP[@]}" "$receipt" "$VPS:$incoming/receipt.json" >/dev/null
    "${SCP[@]}" "$expected" "$VPS:$incoming/expected-base.json" >/dev/null
    "${SCP[@]}" "$M5/artifacts.py" "$M5/deploy_ipc.py" "$M5/prepare_layout.py" "$M5/server_job.py" "$VPS:$incoming/" >/dev/null
    unit="funding-bot-deploy-$release_id"
    "${SSH[@]}" "$VPS" "sudo systemd-run --quiet --collect --unit '$unit' --property=Type=oneshot \
      --property=TimeoutStartSec=infinity --setenv=FUNDING_M5_ENTRY=deploy/deploy.sh \
      /usr/bin/python3 '$incoming/server_job.py' install --artifact '$incoming/artifact.tar' \
      --receipt '$incoming/receipt.json' --expected-base '$incoming/expected-base.json'"
    echo "supervised job started: $unit"
    echo "rerun: deploy/deploy.sh status $release_id"
    ;;
  status)
    release_id="${1:-}"; [[ "$release_id" =~ ^[a-zA-Z0-9._-]{8,80}$ ]] || die "release id required"
    unit="funding-bot-deploy-$release_id"
    "${SSH[@]}" "$VPS" "sudo systemctl status --no-pager '$unit' || true; sudo journalctl -u '$unit' -n 80 --no-pager"
    ;;
  *) usage; die "unknown command: $cmd";;
esac
