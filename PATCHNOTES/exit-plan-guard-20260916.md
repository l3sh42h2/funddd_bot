# Hotfix — подтверждение плана выхода

## Проблема

Кнопка подтверждения плана выхода AIW3 отвечала «план устарел» сразу после
получения. В production это воспроизвелось тремя intent-ами выхода: callback
пришёл до их 60-секундного TTL, но core завершил его как `stale_plan`.

Причина — техническое поле `loaded` в `OwnerCfg.frozen()`: оно является
временем чтения `owner.toml` и меняется при проверке callback. Оно ошибочно
входило в fingerprint plan guard, поэтому неизменная конфигурация выглядела
новой конфигурацией.

## Изменение

`core/service.py` исключает только `loaded` из fingerprint plan guard. Хеш
файла, путь и все замороженные значения owner-конфигурации по-прежнему входят
в fingerprint: реальная смена конфигурации продолжает блокировать старую
кнопку.

## Проверки

- `tests/test_migration_m1.py tests/test_m4_core_approvals.py`: 27 passed.
- Новая regression-проверка: смена только `loaded` не меняет fingerprint;
  смена owner values меняет его.

## Deployment scope

Hotfix создан от точного production SHA `571d86fd7455caf933b844dcf8a2b381e0dcf671`.
Миграционные изменения из ветки `codex/migration-m4-m5` в него не входят.
