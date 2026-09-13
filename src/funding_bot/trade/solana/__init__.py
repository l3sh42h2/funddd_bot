"""Solana-нога связки «спот Solana × шорт Hyperliquid» (ТЗ SOL_HYPERLIQUID_ANSEM 13.09: §2.2, §9, §12;
SOLANA_ROUTERS §5–7). Слои, от нижнего к верхнему:

  b58.py       — base58 строго: адрес = 32 байта, регистр значим (никакого .lower());
  ed25519.py   — точка «на кривой» (для PDA/ATA), проверка подписи, публичный ключ из seed — чистый Python;
  wire.py      — структурный разбор байтов транзакции (legacy и v0), явная кодировка payload (base64/base58);
  rpc.py       — JSON-RPC: id, дедлайн, повтор только чтений, два источника, сверка genesis, URL без ключа;
  accounts.py  — mint и token-счета (Token и Token-2022, allowlist расширений), PDA/ATA по программе mint, rent;
  receipt.py   — разбор getTransaction: meta.err, fee, токены по owner+mint (raw), rent/wSOL/переводы SOL;
  resolver.py  — исход подписи: история двух RPC, EXPIRED_NOT_LANDED только по доказанному истечению blockhash;
  journal.py   — sol_tx_attempts и чеки: запись-до, повтор тех же байт, одна попытка в полёте на кошелёк;
  keypair.py   — разбор секрета (base58 64 байта или keypair-файл) со сверкой публичной половины;
  sign.py      — подпись только закреплённого сообщения, независимая проверка подписи.

Ждут solders: message.py (раскрытие ALT, сборка MessageV0), validate.py (манифест программ и проверка до
подписи). Пока их нет, они ОТКАЗЫВАЮТ, а не делают вид, что проверили: закрепить нечего — подписать нечего.
Подписант — keys.SolanaKey (pycryptodome) или sign.SeedSigner с внешним бэкендом. Окружение с секретом читает
только keys.py (SOLANA_SECRET_B58 / SOLANA_KEYPAIR_FILE).

Пакет не импортирует engine/store/planner: интеграция — после фазы 1.
"""

MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"   # getGenesisHash mainnet-beta (сверено 13.09)

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
ALT_PROGRAM = "AddressLookupTab1e1111111111111111111111111"
TOKEN_PROGRAMS = frozenset({TOKEN_PROGRAM, TOKEN_2022_PROGRAM})

NATIVE_MINT = "So11111111111111111111111111111111111111112"          # wSOL обычной Token-программы
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ANSEM_MINT = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"          # Token-2022, decimals 6


class WaitsSolders(NotImplementedError):
    """Нужна библиотека solders (сборка/подпись/раскрытие сообщений). Отказ, а не заглушка «всё хорошо»."""
