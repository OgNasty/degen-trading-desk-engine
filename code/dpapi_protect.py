"""dpapi1014: Windows only. Encrypt the wallet key and API secrets with DPAPI (CurrentUser)
so only this Windows user can read them, then blank them in .env / secrets.json.
Run once with the bot's venv python from the bot folder:  .venv\\Scripts\\python.exe dpapi_protect.py
Never prints secret values. After this, plaintext copies are ignored (fail closed)."""
import json, os, re, sys
import degen_home as dh

if not dh.IS_WIN:
    sys.exit("DPAPI is Windows-only; nothing changed.")
os.environ.pop(dh.KEY_NAME, None)
dh._load_dotenv() if hasattr(dh, "_load_dotenv") else None
plain = {}
try:
    plain.update(json.loads(dh.SECRETS_FILE.read_text(encoding="utf-8")))
except Exception:
    pass
for n in (dh.KEY_NAME,) + dh.DPAPI_NAMES:
    if os.environ.get(n) and n not in plain:
        plain[n] = os.environ[n]
key = str(plain.get(dh.KEY_NAME) or "").strip()
if not key:
    sys.exit("No SOLANA_TRADING_PRIVATE_KEY found in .env or secrets.json; nothing changed.")
dh.dpapi_encrypt_to_file(key)
others = {n: plain[n] for n in dh.DPAPI_NAMES if plain.get(n)}
if others:
    dh.dpapi_encrypt_secrets_file(others)
# verify round-trip before removing plaintext
dh._cache = None
got = dh.secrets()
if got.get(dh.KEY_NAME) != key or any(got.get(n) != v for n, v in others.items()):
    sys.exit("Verification failed; plaintext left in place.")
from solders.keypair import Keypair
_kp = Keypair.from_bytes(bytes(json.loads(key))) if key.startswith("[") else Keypair.from_base58_string(key)
print("Encrypted. Wallet:", str(_kp.pubkey()))
names = (dh.KEY_NAME,) + tuple(others)
if dh.SECRETS_FILE.exists():
    d = json.loads(dh.SECRETS_FILE.read_text(encoding="utf-8"))
    for n in names:
        d.pop(n, None)
    dh.SECRETS_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")
env = getattr(dh, "ENV_FILE", None)
if env and env.exists():
    lines = env.read_text(encoding="utf-8").splitlines()
    out = [(ln.split("=", 1)[0] + "=") if ln.split("=", 1)[0].strip() in names else ln for ln in lines]
    env.write_text("\n".join(out) + "\n", encoding="utf-8")
print("Plaintext copies blanked:", ", ".join(names))
