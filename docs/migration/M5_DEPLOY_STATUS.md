# M5: промежуточный выкат состоялся

Актуально на 2026-09-14 19:54:34 Москва. Release `aa446f216047-a82af1f56cf0` установлен на Ireland VPS
штатным `deploy/deploy.sh` и прошёл post-cutover проверки. Collector, core и interface
работают, legacy trader/web остановлены. Полная приёмка M4 ещё не завершена.

Подробности, доказательства, измеренные времена и оставшийся scope:
[MIGRATION_RESULT.md](MIGRATION_RESULT.md).

Авторитетная БД: `/var/lib/funding-bot/core/trade.db`. Код: `/opt/funding-bot/current`.
Ветка источников: `codex/migration-m4-m5`; установленный SHA `aa446f2160479e037d4c99a956cc8a83c35dbe54`.
