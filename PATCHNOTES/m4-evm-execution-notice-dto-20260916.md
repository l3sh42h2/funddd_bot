# M4: EVM execution/recovery notification DTO

Date: 2026-09-16

- EVM `Engine` now sends crash, explicit refusal, halt, auto-unwind, clip progress, exposed-spot, and fix-complete notifications as versioned facts through `Hooks.notice`.
- `core` persists `execution_notice` events without rendered HTML. The interface renders them; the legacy Telegram bot renders the same facts only for its compatibility path.
- The execution order, state transitions, quantities, prices and external submission paths are unchanged. Progress events remain silent.
- Existing EVM text regressions and DTO wire regression passed: `119 passed, 1 deselected` in `6.04s`.
- A separate Solana final-boundary test was deselected after its fixture router failed before execution with `ModuleNotFoundError`; this patch does not modify Solana routing. It remains to be rerun in its complete dependency profile.
- This does not close all AC-07 work: desk/planning and some legacy `Refused.html` paths still format HTML in `trade/engine.py`.
