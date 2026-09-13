"""Шаг C1: deploy/owner.toml.example — профиль sol_best_hyperliquid закомментированным предложением владельцу (пилот
$150, решения 13.09). Как есть файл грузится ровно как прежний (BSC/Aster, M01); раскомментированный блок грузится
текущим owner.py: связка выключена и readonly, заданные числа — предложенные, не заданные — причины live_missing."""
from decimal import Decimal as D
from pathlib import Path

from funding_bot.trade import owner

EXAMPLE = Path(__file__).resolve().parents[1] / "deploy" / "owner.toml.example"
SOL = owner.SOL_HL


def _uncomment(text: str) -> str:
    """Раскомментировать блоки «>>> ПРЕДЛОЖЕНИЕ … <<<»: «# ключ» → «ключ», «## пояснение» → «# пояснение»."""
    out, on = [], False
    for ln in text.splitlines():
        if ln.startswith("# >>> ПРЕДЛОЖЕНИЕ"):
            on = True
            continue
        if ln.startswith("# <<< ПРЕДЛОЖЕНИЕ"):
            on = False
            continue
        if on:
            ln = ln[1:] if ln.startswith("##") else ("" if ln.strip() == "#" else ln[2:])
        out.append(ln)
    return "\n".join(out) + "\n"


def test_example_as_is_is_legacy_only():
    cfg = owner.load(EXAMPLE)
    assert cfg.schema_version == 1 and not cfg.profile_enabled(SOL) and cfg.profile_enabled(owner.LEGACY_PROFILE)
    assert set(cfg.frozen()["values"]) == set(owner.LEGACY_SCHEMA)


def test_uncommented_proposal_loads_disabled_with_pilot_numbers(tmp_path):
    p = tmp_path / "owner.toml"
    p.write_text(_uncomment(EXAMPLE.read_text(encoding="utf-8")), encoding="utf-8")
    cfg = owner.load(p)
    # связка выключена: не выше readonly; общий mode в примере пуст = dry — меньший из двух
    assert cfg.schema_version == 2 and not cfg.profile_enabled(SOL) and cfg.profile_mode(SOL) == "dry"
    lim = f"limits.{SOL}."
    want = {"max_clip_usdc": D(150), "max_operation_usdc": D(150), "max_total_position_usdc": D(150),
            "max_spot_slippage_bps": D(100), "max_price_impact_bps": D(150), "max_hedge_slippage_bps": D(60),
            "max_delta_usdc": D(25), "max_unhedged_ms": 60_000, "min_entry_basis_all_in_bps": D(-150),
            "max_network_fee_lamports_per_tx": 200_000, "max_tip_lamports_per_tx": 0,
            "max_external_fees_usdc_per_operation": D("0.50"), "min_sol_reserve_lamports": 20_000_000}
    for k, v in want.items():
        assert D(str(cfg.get(lim + k))) == D(str(v)), k
    assert cfg.get("perp.hyperliquid.leverage") == 1 and cfg.get("spot.solana.confirmation_trigger") == "finalized"
    assert cfg.get("routing.solana.allow_external_signer_managed_routes") is False
    assert all(cfg.get(f"emergency.{SOL}.{k}") is False for k in ("auto_unwind_spot_after_proven_zero_hedge",
                                                                  "auto_rebuy_spot_after_proven_zero_close",
                                                                  "auto_sell_surplus_after_resolved_hedge",
                                                                  "auto_correct_known_delta"))
    miss = cfg.profile_live_missing(SOL)
    assert "wallets.sol_hl.solana_address" in miss and lim + "max_delta_tokens" in miss, "live закрыт до владельца"
    assert not any(lim + k in miss for k in want) and cfg.profile_unsupported(SOL) == []
    # старая связка — те же значения, что у файла как есть (кроме [perp.hyperliquid], заданного предложением)
    legacy = owner.load(EXAMPLE).frozen()["values"]
    fz = cfg.frozen()["values"]
    assert {k: fz[k] for k in owner.LEGACY_SCHEMA if not k.startswith("perp.hyperliquid.")} == \
        {k: v for k, v in legacy.items() if not k.startswith("perp.hyperliquid.")}
    assert cfg.live_missing("aster") == owner.load(EXAMPLE).live_missing("aster")
