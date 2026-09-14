# M4: корневая цель выхода при нескольких клипах

- Исполнитель: Codex; назначенный независимый ревьюер: Astra xhigh.
- База: e5559fa; ветка codex/migration-m4-m5; статус: ready (исходники, не выкат); обновлено 2026-09-14.
- Полный тестовый прогон обнаружил отказ нового root adapter для штатного выхода несколькими клипами.
- Цель выхода остаётся замороженной в raw tokens; принимается только точная сумма положительных клипов.
  Пустой план, нецелые/отрицательные/нулевые количества и несовпадение с approved units отказываются.
- Resize/SL и новые торговые режимы не включаются; production не менялся.
- Проверки: root/resume/recovery profile 49 passed, 1.36 s. Независимый Astra xhigh profile 105 passed,
  4.01 s; 4 valid sums (включая большие int), 11 invalid plans и rollback approval CAS проверены отдельно.
- Старые executor corruption tests одобряют исходный план до подмены: они по-прежнему проверяют второй
  барьер до отправок, а новый root approval barrier тестируется отдельно. Раздутый spec не увеличивает root.
- Полный прогон перед исправлением: 39 failed, 2507 passed, 4 skipped; это незелёный промежуточный результат,
  его нельзя использовать как acceptance receipt.
- Финальный полный локальный набор после исправлений: 2547 passed, 4 skipped, 155.88 s (macOS).
  Это regression checkpoint, не полный Linux artifact acceptance и не release receipt VPS.
