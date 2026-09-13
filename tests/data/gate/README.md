# Живые публичные фикстуры Gate — FATCOIN_USDT

Сняты 13.09.2026 прямыми запросами к публичным (без ключей) эндпоинтам `api.gateio.ws/api/v4`
(разрешено ТЗ: «сеть — только публичные read-only … Gate публичные эндпоинты /api/v4/futures/usdt/contracts,
order_book, funding_rate, trades»). Значения не редактировались — как ответила площадка.

- `contract_fatcoin.json` — `GET /futures/usdt/contracts/FATCOIN_USDT`.
  quanto_multiplier="100", order_size_min=1, order_price_round="0.00001", funding_interval=14400 (4 ч),
  enable_decimal=false, contract_type="" — совпадает с условиями сделки владельца.
- `order_book_fatcoin.json` — `GET /futures/usdt/order_book?contract=FATCOIN_USDT&limit=5`.
- `funding_rate_fatcoin.json` — `GET /futures/usdt/funding_rate?contract=FATCOIN_USDT&limit=3` (новые первыми).
- `spot_time.json` — `GET /spot/time` (в тот же момент, для проверки check_clock офлайн-логики на реальном формате).

Использованы в tests/test_gate_trade_public.py как готовые данные FakeGate (не живой сетевой вызов в тестах —
тесты офлайн, как и остальной проект).
