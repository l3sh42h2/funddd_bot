#!/usr/bin/env bash
# Builds a short, immutable Linux receipt only for narrowly-scoped critical fixes.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${1:?usage: deploy/critical_hotfix.sh BASE_SHA --output ARTIFACT.tar --patchnote PATCHNOTES/name.md}"
shift
cd "$ROOT"
git rev-parse --verify "$BASE^{commit}" >/dev/null
bad="$(git diff --name-only "$BASE..HEAD" -- . ':!tests/**' ':!PATCHNOTES/**' ':!deploy/critical_hotfix.sh' ':!deploy/deploy.sh' ':!deploy/migration/test-profile-critical-hotfix.json' | grep -Ev '^src/funding_bot/(core/(approvals|authority|commands|service)\.py|ipc/|trade/(store|engine|reconcile|operation_roots|generic_operations)\.py)$' || true)"
[ -z "$bad" ] || { echo "critical hotfix scope refused:" >&2; echo "$bad" >&2; exit 2; }
exec deploy/deploy.sh build --profile deploy/migration/test-profile-critical-hotfix.json "$@"
