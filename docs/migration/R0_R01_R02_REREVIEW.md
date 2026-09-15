# R0 — повторное ревью R01/R02

## Точный кандидат

- Код: `a28f5a9ec2ed07dc72b6d6b7f128b1121098df3d`.
- Ветка: `codex/migration-m4-m5`.
- Production baseline: `0f9037cf72de88131dcbcf8129bfd9455973dcba`.
- Production не менялся.
- Linux artifact SHA-256:
  `14b8e865d4c576533b9b9f040cda9c6fde6f713f128417196f4063c774d121a7`.

Предыдущий candidate `b6832d7` заблокирован reviewer findings R01/R02 и не
должен рассматриваться для установки.

## Что проверить

### R01: exact sizing path

`quantity_units.py` добавляет context-independent `copy_abs`, `exact_sum` и
локальный `exact_leg_rebuild`. `generic_operations.py` и
`generic_recovery.py` используют их на пути facts → owned leg quantity → parent
delta → magnitude → native floor → residual/cross-zero. Глобальный Decimal
context и persisted accounting schema не меняются.

Проверить, что локальная precision в `exact_leg_rebuild` действительно достаточна
для входных fact payloads и не меняет semantics обычного accounting rebuild.

### R02: corrupted active peer

`operation_roots._assert_generic_scopes_available()` теперь fail-closed для
active peer с missing, malformed, non-object или incomplete frozen identity.
Новый generic proposal не создаёт root/intent/dispatch; UNKNOWN peer сохраняет
состояние и reserve. Сохранная generic identity по-прежнему проверяет точное
пересечение scopes; live generic execution остаётся отключённым.

Проверить, что консервативный отказ не превращается в обход через approval или
admission и что valid legacy active scope остаётся совместим с прежней policy.

## Воспроизводимые проверки

```bash
git fetch origin
git checkout a28f5a9
PYTHONPATH=src:tests python -m pytest -q \
  /absolute/path/R0_REVIEW_b6832d7_ALL/test_repository_regressions.py \
  tests/test_generic_operations.py tests/test_generic_recovery_owned_inventory.py \
  tests/test_generic_admission.py tests/test_m4_operation_roots.py
```

Результаты на candidate:

- приложенный reviewer regression file: **5 passed**;
- связанный локальный набор: **70 passed**;
- полный `test_generic*.py` + `test_m4_operation_roots.py`: **108 passed**;
- immutable Linux profile: **2,845 passed, 12 skipped, 1,386.24 s**;
- macOS JavaScriptCore на предыдущем common candidate: 8 passed; R01/R02 не
  меняют UI sources;
- exact-value secret scan: pass, 0 matches.

Linux skips: JavaScriptCore-only UI tests, root/DAC fixtures, release import-path
fixture и opt-in network probe. Они не считаются pass.

## Требуемый verdict

Нужен новый независимый verdict именно для `a28f5a9`: `approved` либо замечания
с файлом, строкой, нарушенным инвариантом и воспроизводимым сценарием. Approval
не означает deploy: перед установкой отдельно выполняются fresh production
position/UNKNOWN/base/reader checks и штатный drain.
