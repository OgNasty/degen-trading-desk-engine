"""Token-account rent (rent1014).

Rent is SOL parked in the wallet's own token accounts. Opening an account moves SOL out of the
wallet balance and closing it brings that SOL back. Neither is a deposit, a withdrawal or a cost.
  accounts(rpc, owner)        every token account (classic + Token-2022) with lamports and raw amount
  rent_sol(accts)             SOL held as rent (wrapped-SOL amounts are not rent)
  close_candidates(accts, keep_mints)   empty accounts that may be closed
  build_close_tx(owner, accts)          unsigned v0 tx (base64) with one CloseAccount per account
  close_empty(...)            close the candidates, return {closed, signatures, sol_recovered}
  note_recovered(cb, sol)     move the recovered rent into the deposit-watch baseline (never a deposit)
Reads only, except close_empty, which signs and sends through the caller's send_fn. Never prints keys.
"""
import base64

TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
WSOL = "So11111111111111111111111111111111111111112"
MAX_PER_TX = 8
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
CU_LIMIT = 4000 + 3000 * MAX_PER_TX   # CloseAccount is ~3k CU
CU_PRICE_MICROLAMPORTS = 200000       # small priority fee (< 0.00001 SOL) so the close lands


def _result(body):
    if isinstance(body, dict) and "result" in body:
        return body["result"]
    return body


def accounts(rpc, owner):
    out = []
    for prog in (TOKEN, TOKEN22):
        res = _result(rpc("getTokenAccountsByOwner", [owner, {"programId": prog}, {"encoding": "jsonParsed"}]))
        for v in (res or {}).get("value") or []:
            acc = v.get("account") or {}
            info = ((acc.get("data") or {}).get("parsed") or {}).get("info") or {}
            ta = info.get("tokenAmount") or {}
            out.append({"pubkey": v.get("pubkey"), "mint": info.get("mint"), "program": prog,
                        "amount": str(ta.get("amount") or "0"), "decimals": int(ta.get("decimals") or 0),
                        "lamports": int(acc.get("lamports") or 0), "state": info.get("state")})
    return out


def rent_sol(accts):
    lam = 0
    for a in accts:
        r = int(a.get("lamports") or 0)
        if a.get("mint") == WSOL:
            r -= int(a.get("amount") or 0)  # wrapped SOL is SOL, not rent
        lam += max(r, 0)
    return lam / 1e9


def close_candidates(accts, keep_mints=()):
    keep = {m for m in keep_mints if m}
    return [a for a in accts if str(a.get("amount")) == "0" and a.get("mint") not in keep
            and a.get("state") in (None, "initialized") and a.get("pubkey")]


def build_close_tx(owner, accts, blockhash=None):
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction
    o = Pubkey.from_string(owner)
    import struct
    cb_prog = Pubkey.from_string(COMPUTE_BUDGET)
    ixs = [Instruction(cb_prog, bytes([2]) + struct.pack("<I", CU_LIMIT), []),
           Instruction(cb_prog, bytes([3]) + struct.pack("<Q", CU_PRICE_MICROLAMPORTS), [])]
    ixs += [Instruction(Pubkey.from_string(a["program"]), bytes([9]),  # CloseAccount
                       [AccountMeta(Pubkey.from_string(a["pubkey"]), False, True),
                        AccountMeta(o, False, True), AccountMeta(o, True, False)]) for a in accts]
    msg = MessageV0.try_compile(o, ixs, [], Hash.from_string(blockhash) if blockhash else Hash.default())
    vtx = VersionedTransaction.populate(msg, [Signature.default()])
    return base64.b64encode(bytes(vtx)).decode()


def close_empty(rpc, owner, send_fn, keep_mints=(), dry_run=False, log=print):
    """Close empty token accounts (not keep_mints). send_fn([tx_b64]) -> (sig, err, uncertain)."""
    accts = accounts(rpc, owner)
    cands = close_candidates(accts, keep_mints)
    out = {"candidates": len(cands), "closed": [], "signatures": [], "sol_recovered": 0.0, "errors": []}
    for i in range(0, len(cands), MAX_PER_TX):
        batch = cands[i:i + MAX_PER_TX]
        lam = sum(int(a["lamports"]) for a in batch)
        if dry_run:
            out["closed"] += [a["pubkey"] for a in batch]; out["sol_recovered"] += lam / 1e9
            continue
        sig, err, uncertain = send_fn([build_close_tx(owner, batch)])
        if sig and not err:
            out["closed"] += [a["pubkey"] for a in batch]; out["signatures"].append(sig)
            out["sol_recovered"] += lam / 1e9
        else:
            out["errors"].append(str(err or ("uncertain" if uncertain else "send_failed"))[:200])
    out["sol_recovered"] = round(out["sol_recovered"], 9)
    log(f"RENT_RECLAIM closed={len(out['closed'])}/{len(cands)} sol_recovered={out['sol_recovered']:.9f}"
        + (" dry_run" if dry_run else "") + (f" errors={len(out['errors'])}" if out["errors"] else ""))
    return out


def note_recovered(cb, sol):
    """Recovered rent: wallet SOL rises and token-account rent falls by the same amount. Shift the
    deposit-watch baseline so the next check sees neither a deposit nor a rent change."""
    w = (cb or {}).get("deposit_watch")
    if not isinstance(w, dict) or not sol:
        return False
    b = w.get("balances")
    if isinstance(b, dict) and "SOL" in b:
        b["SOL"] = float(b["SOL"] or 0) + float(sol)
    if w.get("rent_sol") is not None:
        w["rent_sol"] = max(0.0, float(w["rent_sol"]) - float(sol))
    return True
