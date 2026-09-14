# M5 — verified artifact and supervised three-process deploy

Исполнитель: Codex, GPT-5.6 Sol high, назначен владельцем. Ветка `codex/m5-deploy-agent`, база
`aaf57374c2ef7924e81248bed81d001cebde9b8c`. Независимый reviewer: GPT-6 Astra xhigh, запускается координатором.
Статус: in_progress. Обновлено 2026-09-14.

Scope: только `deploy/**`, `tests/test_m5_deploy*.py`, `docs/migration/M5_DEPLOY*.md` и этот patchnote.
Новый путь выпуска входит только через `deploy/deploy.sh`: неизменяемый артефакт и receipt точного Linux-прогона,
server-side supervised job под стабильным flock, STALE_BASE до live-изменений, drain/execution-lock/readiness gates,
additive DB backup/migration, reader-compatible code-only rollback, изолированные accounts/state и три systemd-службы.
Первый переход явно учитывает legacy trader/web из `/home/admin/hyper/funding_bot`; старый trader без общего lock
сначала fenced и останавливается без timeout-kill, затем проверяется освобождение исполнения. Runtime/credentials,
авторитетная последняя trade.db и offsets переносятся приватно; tunnel/порт 8792 сохраняются.

Production не изменяется, боевые сервисы не запускаются, сетевые ордера/API не вызываются. Linux functional staging,
VPS read-only preflight и фактический release receipt остаются обязательными перед принятием M5.
