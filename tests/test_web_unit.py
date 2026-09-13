"""Веб-служба — публичный процесс (туннель Cloudflare): в её окружение не попадает .env с ключами торгового контура,
только свой файл кабинета (логин и хэш пароля)."""
from pathlib import Path

UNIT = Path(__file__).resolve().parents[1] / "deploy" / "funding_bot-web.service"


def test_web_unit_reads_only_cabinet_env_not_trading_keys():
    env_files = [ln.split("=", 1)[1].strip() for ln in UNIT.read_text().splitlines()
                 if ln.strip().startswith("EnvironmentFile=")]
    assert env_files == ["-/home/admin/hyper/funding_bot/runtime/cabinet.env"]    # runtime/ выкат не трогает
    assert not any(f.lstrip("-").endswith("/.env") for f in env_files)
