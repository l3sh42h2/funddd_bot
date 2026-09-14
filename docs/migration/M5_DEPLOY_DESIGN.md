# M5 deploy: verified artifact and supervised cutover

Статус 2026-09-14: механизм реализован для ревью и локальных симуляций. Это не акт приёмки и не разрешение
выкатывать его в production. Обязательные Linux staging, независимое ревью, read-only VPS preflight и фактический
release receipt ещё не выполнены.

## Единственная точка входа

Все операции M5 начинаются через `deploy/deploy.sh`:

```text
deploy/deploy.sh build --output /safe/path/release.tar --patchnote PATCHNOTES/change.md
deploy/deploy.sh inspect-base --output /safe/path/expected-base.json
deploy/deploy.sh install --artifact /safe/path/release.tar \
  --receipt /safe/path/release.tar.receipt.json --expected-base /safe/path/expected-base.json
deploy/deploy.sh status RELEASE_ID
```

`build` должен выполняться в совместимом Linux-окружении. Он отказывается от dirty tracked tree, требует tracked
patchnote, скачивает только binary wheels из `deploy/requirements.lock`, дважды создаёт чистый venv и запускает
зафиксированный профиль. Receipt связывает полный список tracked sources и его SHA, lock и hashes всех wheels,
точный `pip freeze --all`, Python/implementation/platform/machine/libc/sqlite/executable hash, argv и bytes тестов/
fixtures, raw artifact SHA и SHA вывода тестов. Изменение любого поля отменяет reuse.

Raw tar не может содержать собственный SHA без самоссылки. Поэтому внутри него лежит
`release-manifest.template.json`; после проверки tar и receipt server job добавляет `artifact_sha256` и
`verification_identity_sha256` в финальный `<release>/release-manifest.json`. Core, collector и interface читают
identity из загруженного immutable release, а не из меняющегося symlink.

`inspect-base` формирует точную read-only базу. До первого перехода это полный manifest файлов legacy tree без
`.git/.venv/runtime/logs` плюс schema/min_reader текущей `trade.db`. После перехода это identity из
`release-state.json`. `install` заново вычисляет текущее значение уже под flock и сравнивает его с expected-base.
`STALE_BASE` возникает до замены кода, изменения БД, credentials, services или release state.

## Server job и порядок владельца исполнения

Mac/CLI только передаёт immutable inputs в уникальную `/var/tmp` и запускает detached `systemd-run` oneshot с
`TimeoutStartSec=infinity`. Один server process держит стабильный `/run/lock/funding-bot-deploy.lock`; потеря SSH
не прерывает job. Retry повторяет base/preflight/drain/health, но reuse receipt допускается только при полном
совпадении identity.

Для уже мигрированного core job вызывает deploy-only `begin_drain` с CAS revision, затем опрашивает тот же epoch.
Переключение разрешено только при `safe_to_switch=true`, свежем recovery evidence, `unresolved=0`, `busy=false` и
подтверждённом владении stable execution lock. У ожидания старого executor нет таймаута: его не убивают ради
завершения deploy.

Первый переход отличается явно. У live `a976bec` нет общего execution lock и M0–M3 там не установлены. Job ставит
runtime systemd fence `Restart=no, TimeoutStopSec=infinity`, отключает и полностью останавливает legacy trader,
проверяет authoritative DB и только после этого создаёт/проверяет новый stable lock и запускает core. Первый core
стартует с durable drain до IPC/recovery. Старые trader и новый core не бывают одновременно владельцами отправки.

## State, services и публичный адрес

Целевой код: `/opt/funding-bot/releases/<release_id>`, `current` — атомарный symlink. Release после offline install
становится root-owned/read-only. Изменяемое состояние и secrets живут в `/var/lib/funding-bot`; UI не получает SQL
или trading credentials.

Первичная миграция строится в отдельной state staging directory и переименовывается атомарно. После остановки
legacy trader SQLite backup API переносит committed WAL в текущую `core/trade.db` и отдельный backup. Сохраняются
Telegram offset, owner/config/journals, collector DB/table/health, общий OKX pace, cabinet state, текущий public URL
и приватный keypair; абсолютный keypair env path переписывается на новый private path без вывода значения.
Опечатка пути БД не создаёт пустой файл.

Устанавливаются accounts/groups и три Python-службы: collector, core, interface. Legacy trader/web отключаются.
Interface остаётся на `127.0.0.1:8792`. Уже работающий Cloudflare tunnel не рестартует и сохраняет URL; его unit
обновляется для нового state path только на будущий естественный restart.

Перед снятием drain проверяются exact identities, PID/boot id, freshness/readiness всех трёх процессов и лимит
health 16 KiB. Затем `end_drain` требует тот же epoch и actual loaded release. Итог сохраняется атомарно в
`/var/lib/funding-bot/deploy/release-state.json` и отдельный report с этапами, backup hash и health evidence.

## UI-only и rollback boundary

Core не рестартует только если manifest доказывает одинаковые core/collector component hashes, dependency set,
IPC, DTO, schema и min_reader. Тогда меняются symlink и interface, а `release-state.json` хранит разные release IDs
компонентов. Любая неоднозначность запускает полный цикл.

Rollback всегда меняет только код. Перед ним повторяются drain/recovery/lock и `compatible_readers` против текущей
БД; backup никогда не восстанавливается поверх новых исполнений. Несовместимый reader оставляет recovery-capable
core в drain и требует compatible fix. На первом переходе автоматического возврата к legacy trader после switch
нет: у него нет общего lock, а его старая DB уже не является authoritative. До switch отказ восстанавливает старые
units/trader; после switch сохраняются текущая DB и failure report.

## Что ещё требуется для AC-21…27

- Интегрировать финальный core drain commit и закрыть замечания Astra xhigh.
- Собрать неизменённый committed tree и выполнить полный test profile в совместимом Linux staging.
- Проверить install/import/config/service simulation с реальными Linux systemd/DAC, без live API/orders.
- На VPS сделать новый `inspect-base`, read-only сверку актуального `a976bec`, диска, открытых позиций, очередей,
  authoritative DB и внешних исполнений на одном срезе.
- Провести согласованное переключение только после отдельного разрешения владельца; сохранить release receipt,
  версии/PID/boot epochs, позиции/очереди/PnL и публичный endpoint.

До выполнения этих пунктов M5 и AC-21…27 не считаются принятыми.
