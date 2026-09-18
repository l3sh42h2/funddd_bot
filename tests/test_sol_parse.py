"""Команды связки SOL × HL в Telegram (ТЗ SOL×HL §14). Приёмка: V01 (разбор). Прежние команды BSC сверяются со
снимком НЕИЗМЕНЁННОГО разбора (legacy_snapshot.json, снят до правок потока profiles) — побайтно."""
import dataclasses, json
from decimal import Decimal as D
import pytest
from funding_bot.tg import parse
from funding_bot.tg.parse import Entry, Exit, ProfileEntry, ProfileExit, ProfilePositions, Unknown
from funding_bot.trade.owner import LEGACY_PROFILE, SOL_HL
from sol_hl_fixtures import DATA

SNAP = json.loads((DATA / "legacy_snapshot.json").read_text(encoding="utf-8"))
# входы снимка, которые до связки были отказом, а теперь — команды связки (или отказ с другим текстом)
NEW = {"вход ANSEM jupiter·sol aster 200", "вход ANSEM jupiter·sol hyperliquid·para 200",
       "вход ANSEM okx·sol hl·para 200 usdc", "вход ANSEM okx·sol hyperliquid·para 200",
       "вход ANSEM sol-auto hyperliquid·para 200", "вход para:ANSEM sol-auto hyperliquid 200",
       "вход ANSEM okx·sol hl 200", "вход ansem okx sol hyperliquid 200",
       "вход ANSEM okx·sol hyperliquid 200", "вход $ansem okx·sol hl 3",
       "вход AIW3 okx dex solana hl 450",
       "выход ANSEM 500 ansem", "выход ANSEM sol", "выход ANSEM sol 200", "позиции hyperliquid·para", "позиции sol"}


def _dict(c):
    return {"type": type(c).__name__,
            "fields": {k: (str(v) if isinstance(v, D) else v) for k, v in dataclasses.asdict(c).items()}}


def test_legacy_commands_byte_for_byte():
    assert NEW <= set(SNAP["parse"])
    for text, want in SNAP["parse"].items():
        if text not in NEW:
            assert _dict(parse.parse(text)) == want, text
    for fn, cases in SNAP["helpers"].items():
        for arg, want in cases.items():
            got = getattr(parse, fn)(arg)
            assert (str(got) if isinstance(got, D) else got) == want, (fn, arg)


def PE(coin, policy, dex, usdc):
    return ProfileEntry(coin, policy, "solana", "hyperliquid", dex, D(usdc), SOL_HL)


@pytest.mark.parametrize("text, want", [
    ("вход ANSEM sol-auto hyperliquid·para 200", PE("ANSEM", "auto", "para", "200")),
    ("вход ANSEM jupiter·sol hyperliquid·para 200", PE("ANSEM", "jupiter", "para", "200")),
    ("вход ANSEM okx·sol hyperliquid·para 200", PE("ANSEM", "okx", "para", "200")),
    ("Вход kPEPE sol-auto HL·PARA $150", PE("kPEPE", "auto", "para", "150")),           # регистр монеты сохранён
    ("вход ANSEM auto·sol hyperliquid:para 200 usdc", PE("ANSEM", "auto", "para", "200")),
    ("вход ANSEM best-solana hl-para 200usdc", PE("ANSEM", "auto", "para", "200")),
    ("вход ANSEM jupiter sol hyperliquid·para 200,5", PE("ANSEM", "jupiter", "para", "200.5")),
    ("вход ANSEM jup/sol hyperliquid_para $ 7", PE("ANSEM", "jupiter", "para", "7")),
    ("вход $ANSEM okx:solana hl·para 1.5 $", PE("ANSEM", "okx", "para", "1.5")),
    ("вход para:ANSEM sol-auto hyperliquid 200", PE("ANSEM", "auto", "para", "200")),   # dex из имени монеты
    ("вход PARA:ANSEM jupiter·sol hl 10", PE("ANSEM", "jupiter", "para", "10")),
    ("вход ANSEM sol-auto hyperliquid 200", PE("ANSEM", "auto", None, "200")),        # dex решает реестр
    ("/вход ANSEM sol-auto hyperliquid·para 200", PE("ANSEM", "auto", "para", "200")),
])
def test_profile_entry(text, want):
    got = parse.parse(text)
    assert got == want and got.name == "profile_entry"
    assert got.fullcoin == (f"{want.perp_dex}:{want.coin}" if want.perp_dex else None)


@pytest.mark.parametrize("text, needle", [
    ("вход ANSEM sol-auto hyperliquid·para 200 usdt", "USDC"),         # котировка связки — USDC
    ("вход ANSEM sol-auto hyperliquid·para 1e3", "USDC"),
    ("вход ANSEM sol-auto hyperliquid·para 0", "USDC"),
    ("вход ANSEM sol-auto hyperliquid·para 200 300", "USDC"),
    ("вход ANSEM sol-auto hyperliquid·para 200.1234567", "USDC"),
    ("вход ANSEM jupiter·sol aster 200", "связки sol → aster нет"),
    ("вход ANSEM sol-auto bybit·para 200", "перп «bybit·para» не понят"),
    ("вход ANSEM sol-auto hyperliquid·PA-RA 200", "не понят"),
    ("вход para:ANSEM sol-auto hyperliquid·xyz 200", "dex монеты «para» ≠ dex перпа «xyz»"),
    ("вход AN-SEM sol-auto hyperliquid·para 200", "монета"),
    ("вход ANSEM sol-auto hyperliquid·para", "формат"),
    ("вход ANSEM jupiter·bsc hyperliquid·para 200", "спот «jupiter·bsc» не понят"),    # jupiter — только на sol
])
def test_profile_entry_refusals(text, needle):
    got = parse.parse(text)
    assert isinstance(got, Unknown) and needle in got.reason, got


def test_ambiguous_sol_form_refuses_before_legacy_engine():
    """Без dex у Solana × HL нельзя попасть в legacy EVM desk и запросить wallets.solana."""
    for text in ("вход ANSEM okx·sol hl 200", "вход ansem okx sol hyperliquid 200",
                 "вход ANSEM okx·sol hyperliquid 200", "вход $ansem okx·sol hl 3",
                 "вход AIW3 okx dex solana hl 450"):
        got = parse.parse(text)
        assert isinstance(got, Unknown)
        assert "укажите dex" in got.reason and "hyperliquid·<dex>" in got.reason
    assert parse.parse("выход ANSEM 200") == Exit("ANSEM", D(200))
    assert parse.parse("выход ANSEM") == Exit("ANSEM", None)
    assert parse.parse("вход ANSEM okx·sol bybit 200").reason.startswith("перп «bybit» не понят — площадки:")


def PX(target, profile=None, dex=None, tokens=None, usdc=None):
    return ProfileExit(target, profile, dex, None if tokens is None else D(tokens), None if usdc is None else D(usdc))


@pytest.mark.parametrize("text, want", [
    ("выход ANSEM sol", PX("ANSEM", SOL_HL)),
    ("выход ANSEM hyperliquid·para всё", PX("ANSEM", SOL_HL, "para")),
    ("выход ANSEM 500 ansem", PX("ANSEM", tokens="500")),
    ("выход kPEPE 1000,5 токенов", PX("kPEPE", tokens="1000.5")),
    ("выход DK7Q2 sol 120 usdc", PX("DK7Q2", SOL_HL, usdc="120")),
    ("выход dk7q2 solana $120", PX("dk7q2", SOL_HL, usdc="120")),                        # id движок сверит без регистра
    ("выход para:ANSEM sol 12.345678 ansem", PX("para:ANSEM", SOL_HL, "para", tokens="12.345678")),
    ("выход ANSEM bsc", PX("ANSEM", LEGACY_PROFILE)),
])
def test_profile_exit(text, want):
    got = parse.parse(text)
    assert got == want and got.name == "profile_exit"


@pytest.mark.parametrize("text", ["выход ANSEM sol перп", "выход ANSEM sol 100 200", "выход ANSEM 0 ansem",
                                  "выход para:ANSEM hyperliquid·xyz", "выход ANSEM sol 100 usdt", "выход ANSEM 5 btc",
                                  "выход AN-SEM 5 ansem"])
def test_profile_exit_refusals(text):
    assert isinstance(parse.parse(text), Unknown)


def test_profile_positions():
    assert parse.parse("позиции sol") == ProfilePositions(SOL_HL)
    assert parse.parse("позиции hyperliquid·para") == ProfilePositions(SOL_HL, "para")
    assert parse.parse("позиции bsc") == ProfilePositions(LEGACY_PROFILE)
    assert parse.parse("позиции sol").name == "positions"            # старый бот покажет все позиции — безвредно
    assert isinstance(parse.parse("позиции все"), Unknown)


def test_profile_commands_never_reach_legacy_handlers():
    """Главное по деньгам: «выход ANSEM 500 ansem» НЕ становится старым Exit(usd=None) = «выйти целиком», а вход связки —
    старым Entry (старый движок взял бы ноги BSC/Aster). У команд связки свои имена."""
    for text in ("выход ANSEM 500 ansem", "выход ANSEM sol 120", "вход ANSEM sol-auto hyperliquid·para 200",
                 "вход ANSEM okx·sol hyperliquid·para 200"):
        got = parse.parse(text)
        assert not isinstance(got, (Entry, Exit)) and got.name in ("profile_entry", "profile_exit")
