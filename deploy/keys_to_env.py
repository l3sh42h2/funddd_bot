#!/usr/bin/env python3
"""Ключи торгового контура — в .env на сервере. ЗАПУСКАЕТ ВЛАДЕЛЕЦ сам на своём Маке:

    python3 ~/Documents/vps/funding_bot/deploy/keys_to_env.py

Берёт из заметки ~/Documents/hyper/botLP.rtf только нужные строки (evmkey, evm_address, aster_add, aster_private, bot),
переносит их в ~/hyper/funding_bot/.env на сервере (права 0600, прочие строки сохраняются). Значения никуда не печатаются:
на экран — только имена и длины. Приватные ключи других кошельков (api_private, sol, gate…) не трогаются.
"""
import os, re, shlex, subprocess, sys

NOTE = os.path.expanduser("~/Documents/hyper/botLP.rtf")
VPS = "admin@34.65.234.12"
SSH = ["ssh", "-i", os.path.expanduser("~/.ssh/google_compute_engine"), "-o", "IdentitiesOnly=yes",
       "-o", "UserKnownHostsFile=" + os.path.expanduser("~/.ssh/google_compute_known_hosts")]
# Aster v3: user — ОСНОВНОЙ кошелёк аккаунта Aster; signer — адрес API-кошелька (агента), ключ которого aster_private
# (12.09: адрес из aster_private совпал с aster_add — значит aster_add это адрес API-кошелька, а основной нужен отдельно)
MAP = {"evmkey": "DEX_EVM_KEY", "evm_address": "DEX_EVM_ADDRESS", "aster_user": "ASTER_USER",
       "aster_add": "ASTER_SIGNER_ADDRESS", "aster_private": "ASTER_SIGNER_KEY", "bot": "TG_BOT_TOKEN"}
MERGE = r'''
import os, sys
new = dict(l.split("=", 1) for l in sys.stdin.read().splitlines() if "=" in l)
p = os.path.expanduser("~/hyper/funding_bot/.env")
old = open(p).read().splitlines() if os.path.exists(p) else []
keep = [l for l in old if l.split("=", 1)[0] not in new]
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write("\n".join(keep + [k + "=" + v for k, v in new.items()]) + "\n")
os.chmod(p, 0o600)
print("на сервере записано:", {k: str(len(v)) + " симв." for k, v in new.items()}, "| прочих строк:", len(keep))
'''

txt = subprocess.run(["textutil", "-convert", "txt", "-stdout", NOTE], capture_output=True, text=True, check=True).stdout
found = {}
for ln in txt.splitlines():
    m = re.match(r"^\s*([^:=]+?)\s*[:=]\s*(\S+)\s*$", ln)
    if m and m.group(1).strip() in MAP:
        found[MAP[m.group(1).strip()]] = m.group(2)
missing = [k for k, v in MAP.items() if v not in found]
if missing:
    print("в заметке нет строк:", missing, "— допишите их и запустите снова"); sys.exit(1)
payload = "".join(f"{k}={v}\n" for k, v in found.items())
# команда для shell сервера — через shlex.quote (repr() не экранирование для shell: переводы строк уходили как «\n»)
remote = os.environ.get("KEYS_TO_ENV_REMOTE")          # для проверки без сервера: команда-заменитель ssh
cmd = ["bash", "-c", "python3 -c " + shlex.quote(MERGE)] if remote == "local" else SSH + [VPS, "python3 -c " + shlex.quote(MERGE)]
r = subprocess.run(cmd, input=payload, capture_output=True, text=True)
if r.returncode != 0:
    print("не записано: сервер ответил ошибкой —", (r.stderr.strip().splitlines() or ["?"])[-1]); sys.exit(1)
print(r.stdout.strip())
