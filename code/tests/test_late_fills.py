"""Offline: late-landing swaps get logged once; transfers and logged swaps are ignored."""
import json, os, tempfile
import os, sys, tempfile
from pathlib import Path
TMP = tempfile.mkdtemp(prefix="degen-test-")
os.environ["DEGEN_HOME"] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import late_fills as LF

W = "Wa11et" + "1" * 38  # dummy test wallet
J = "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"
U = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

def tx(sol_delta, tok=None, signer=W):
    pre, post = [], []
    if tok:
        mint, a0, a1 = tok
        pre.append({"accountIndex": 3, "owner": W, "mint": mint, "uiTokenAmount": {"uiAmountString": str(a0)}})
        post.append({"accountIndex": 3, "owner": W, "mint": mint, "uiTokenAmount": {"uiAmountString": str(a1)}})
    keys = [signer] + ([W] if signer != W else [])
    i = keys.index(W)
    pb = [0] * len(keys); qb = [0] * len(keys); pb[i] = 3_000_000_000; qb[i] = pb[i] + int(sol_delta * 1e9)
    return {"meta": {"preBalances": pb, "postBalances": qb, "preTokenBalances": pre, "postTokenBalances": post},
            "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in keys]}}}

def run():
    d = tempfile.mkdtemp(); st = os.path.join(d, "s.json"); tr = os.path.join(d, "t.jsonl")
    open(tr, "w").write(json.dumps({"tx": "LOGGED"}) + "\n")
    chain = {"sigs": [{"signature": "OLD", "err": None, "blockTime": 1}], "tx": {}}
    rpc = lambda m, p: ({"result": list(reversed(chain["sigs"]))} if m == "getSignaturesForAddress" else {"result": chain["tx"].get(p[0])})
    assert LF.scan(W, rpc, state_path=st, trades_path=tr) == []          # seed: no backfill
    chain["sigs"] += [
        {"signature": "LATE1", "err": None, "blockTime": 1760496345},     # 19:45:45 own JitoSOL buy
        {"signature": "LOGGED", "err": None, "blockTime": 1760496350},
        {"signature": "DEPOSIT", "err": None, "blockTime": 1760496360},
        {"signature": "FAILED", "err": {"x": 1}, "blockTime": 1760496370},
    ]
    chain["tx"]["LATE1"] = tx(-0.01266978, (J, 0.0296677, 0.0370888))
    chain["tx"]["DEPOSIT"] = tx(-0.0, (U, 14.13, 64.13), signer="SomeoneElse111111111111111111111111111111111")
    rows = LF.scan(W, rpc, state_path=st, trades_path=tr)
    assert len(rows) == 1 and rows[0]["tx"] == "LATE1" and rows[0]["sold_asset"] == "SOL" and rows[0]["bought_asset"] == "JitoSOL", rows
    assert abs(rows[0]["bought_amount"] - 0.0074211) < 1e-9 and rows[0]["kind"] == "late_fill"
    assert LF.scan(W, rpc, state_path=st, trades_path=tr) == []          # only once
    assert sum(1 for _ in open(tr)) == 2
    # a fee-only SOL change is not a swap
    assert LF.swap_row("X", 1, {"SOL": -0.000005}) is None
    print("ok late_fills")

if __name__ == "__main__":
    run()
