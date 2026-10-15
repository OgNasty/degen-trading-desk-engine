#!/usr/bin/env python3
"""rent1014: token-account rent is never a cost, deposit or withdrawal; Friday closes empty accounts.
Offline (fake RPC / fake send). python test_rent_reclaim.py"""
import os, sys, tempfile, json, base64
from pathlib import Path
TMP = tempfile.mkdtemp(prefix="degen-test-")
HERE = Path(__file__).resolve().parent
if HERE.name == "tests":  # template layout: isolated home; the live copy keeps its own home
    os.environ["DEGEN_HOME"] = TMP
    os.environ.setdefault("PAPER_TRADING", "true")
    Path(TMP, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\n")
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
import test_depfix as T
import savings_mode as SM
import late_fills as LF
import token_rent as TR

W = "Wa11et" + "1" * 38                       # dummy wallet (base58-valid characters)
RENT = 1488440                                # lamports per token account in these tests
J = "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"
X1, X2 = "Xtok" + "1" * 40, "Ytok" + "1" * 40  # route helper mints (dummy)


def _tb(idx, mint, amt, dec=9):
    return {"accountIndex": idx, "mint": mint, "owner": W,
            "uiTokenAmount": {"uiAmountString": str(amt), "amount": str(int(round(amt * 10 ** dec))), "decimals": dec}}


def test_late_fill_records_swap_amount_only():
    # SOL -> JitoSOL that also paid a 6398-lamport fee and opened two helper token accounts
    swap, fee = 9686502, 6398
    keys = [{"pubkey": W}, {"pubkey": "jitoAcct"}, {"pubkey": "help1"}, {"pubkey": "help2"}]
    pre = [2503396512, 2039280, 0, 0]
    post = [pre[0] - swap - fee - 2 * RENT, 2039280, RENT, RENT]
    tx = {"blockTime": 1791000000, "transaction": {"message": {"accountKeys": keys}},
          "meta": {"fee": fee, "preBalances": pre, "postBalances": post,
                   "preTokenBalances": [_tb(1, J, 0.029667716)],
                   "postTokenBalances": [_tb(1, J, 0.037088784), _tb(2, X1, 0), _tb(3, X2, 0)]}}
    ch, signer = LF.deltas(tx, W)
    row = LF.swap_row("sigA", tx["blockTime"], ch)
    assert signer and abs(row["sold_amount"] - swap / 1e9) < 1e-12, row
    assert abs(row["bought_amount"] - 0.007421068) < 1e-12


def test_rent_sol_and_candidates():
    accts = [{"pubkey": "a", "mint": J, "program": TR.TOKEN, "amount": "0", "lamports": RENT, "state": "initialized"},
             {"pubkey": "b", "mint": X1, "program": TR.TOKEN, "amount": "0", "lamports": RENT, "state": "initialized"},
             {"pubkey": "c", "mint": X2, "program": TR.TOKEN22, "amount": "5", "lamports": RENT, "state": "initialized"},
             {"pubkey": "d", "mint": TR.WSOL, "program": TR.TOKEN, "amount": "1000", "lamports": RENT + 1000, "state": "initialized"},
             {"pubkey": "e", "mint": X2, "program": TR.TOKEN, "amount": "0", "lamports": RENT, "state": "frozen"}]
    assert abs(TR.rent_sol(accts) - 5 * RENT / 1e9) < 1e-12   # wrapped SOL amount is not rent
    assert [a["pubkey"] for a in TR.close_candidates(accts, keep_mints={J, TR.WSOL})] == ["b"]


def test_close_tx_shape():
    from solders.transaction import VersionedTransaction
    from solders.keypair import Keypair
    owner = str(Keypair().pubkey())
    accts = [{"pubkey": str(Keypair().pubkey()), "program": TR.TOKEN}, {"pubkey": str(Keypair().pubkey()), "program": TR.TOKEN22}]
    vtx = VersionedTransaction.from_bytes(base64.b64decode(TR.build_close_tx(owner, accts)))
    keys = [str(k) for k in vtx.message.account_keys]
    ixs = [ix for ix in vtx.message.instructions if keys[ix.program_id_index] != TR.COMPUTE_BUDGET]
    assert len(vtx.message.instructions) == 4  # compute limit + priority fee + 2 closes
    assert len(ixs) == 2 and all(bytes(ix.data) == bytes([9]) for ix in ixs)
    assert keys[0] == owner and {keys[ix.program_id_index] for ix in ixs} == {TR.TOKEN, TR.TOKEN22}


def test_opening_and_closing_accounts_is_not_a_flow():
    r, cb = T.Run(), T.book()
    base = dict(T.BASE)
    chk = lambda b, rent: SM.apply_external_deposits(cb, dict(b, _rent_sol=rent), T.PX,
                                                     aux={"JitoSOL": 0.0, "GreedyGoats": 0.0, "_rent_sol": rent}, log_path=r.log)
    def run(b, rent):
        r.rid += 1; SM.RUN_ID = r.rid; SM._DETECT_CACHE.clear(); return chk(b, rent)
    d = run(base, 4 * RENT / 1e9)                      # first read stores the rent baseline
    assert d["reason"] in ("unchanged", "sol_fee"), d
    # a logged SOL->USDC swap of 0.05 SOL that also opened 4 helper accounts (rent 0.00595 SOL > tolerance)
    r.add("SOL", 0.05, "USDC", 5.15)
    b2 = dict(base, SOL=base["SOL"] - 0.05 - 4 * RENT / 1e9 - 6e-6, USDC=base["USDC"] + 5.15)
    d = run(b2, 8 * RENT / 1e9)
    assert d["reason"] not in ("external_pending_confirm", "deposit", "outflow"), d
    # closing 6 empty accounts brings 0.00893 SOL back: not a deposit
    b3 = dict(b2, SOL=b2["SOL"] + 6 * RENT / 1e9 - 5e-6)
    for _ in range(2):
        d = run(b3, 2 * RENT / 1e9)
        assert d["reason"] not in ("external_pending_confirm", "deposit", "outflow"), d
    assert abs(cb["funding"]["funded_usd"] - 450.59) < 1e-9
    # without the rent read the same +0.00893 SOL would have looked like a deposit
    r2, cb2 = T.Run(), T.book()
    r2.rid = 5000; SM.RUN_ID = r2.rid; SM._DETECT_CACHE.clear()
    d = SM.apply_external_deposits(cb2, dict(base, SOL=base["SOL"] + 6 * RENT / 1e9), T.PX, log_path=r2.log)
    assert d["reason"] == "external_pending_confirm", d


def test_note_recovered_keeps_baseline_consistent_without_rent_read():
    r, cb = T.Run(), T.book()
    cb["deposit_watch"]["rent_sol"] = 6 * RENT / 1e9
    TR.note_recovered(cb, 6 * RENT / 1e9)
    b = dict(T.BASE, SOL=T.BASE["SOL"] + 6 * RENT / 1e9)
    for _ in range(2):
        r.rid += 1; SM.RUN_ID = r.rid; SM._DETECT_CACHE.clear()
        d = SM.apply_external_deposits(cb, b, T.PX, log_path=r.log)   # rent not read this check
        assert d["reason"] in ("unchanged", "sol_fee"), d


def test_friday_reclaim_skips_savings_and_book_and_logs():
    import friday_rsi as FR
    cbp = Path(TMP, "cb-friday.json"); logp = Path(TMP, "log-friday.md")
    cb = T.book(); cb["deposit_watch"]["rent_sol"] = 5 * RENT / 1e9
    cb["savings_mode"]["mint"] = J; cb["memecoin_mode"]["mint"] = "GGiveYrqhfJc5pgBnjuY3NP5cW3ni1nFHeJXYZGFappF"
    cbp.write_text(json.dumps(cb))
    from solders.keypair import Keypair
    P = {n: str(Keypair().pubkey()) for n in ("J", "USDC", "WSOL", "H1", "H2", "Full")}
    vals = [  # (pubkey, mint, amount)
        (P["J"], J, "0"), (P["USDC"], FR.MINTS["USDC"], "0"), (P["WSOL"], FR.MINTS["SOL"], "0"),
        (P["H1"], X1, "0"), (P["H2"], X2, "0"), (P["Full"], X2, "7")]
    def rpc(m, p):
        prog = p[1]["programId"]
        return {"result": {"value": [{"pubkey": k, "account": {"lamports": RENT, "data": {"parsed": {"info": {
            "mint": mint, "state": "initialized", "tokenAmount": {"amount": a, "decimals": 6}}}}}}
            for k, mint, a in vals if prog == TR.TOKEN]}}
    sent = []
    def send(txs):
        sent.append(txs); return ("SIGclose1", None, False)
    import venue_quotes as VQ
    VQ.ADDR = str(Keypair().pubkey())  # dummy wallet for this test
    res = FR.reclaim_rent(rpc=rpc, send_fn=send, cb_path=str(cbp), log_path=str(logp))
    assert sorted(res["closed"]) == sorted([P["H1"], P["H2"]]) and len(sent) == 1, res
    assert abs(res["sol_recovered"] - 2 * RENT / 1e9) < 1e-12 and res["signatures"] == ["SIGclose1"]
    cb2 = json.loads(cbp.read_text())
    assert abs(cb2["deposit_watch"]["balances"]["SOL"] - (T.BASE["SOL"] + 2 * RENT / 1e9)) < 1e-12
    assert abs(cb2["deposit_watch"]["rent_sol"] - 3 * RENT / 1e9) < 1e-12
    assert "RENT_RECLAIM" in logp.read_text() and "not a deposit" in logp.read_text()
    # dry run sends nothing
    res2 = FR.reclaim_rent(dry_run=True, rpc=rpc, send_fn=lambda t: 1 / 0, cb_path=str(cbp), log_path=str(logp))
    assert res2["candidates"] == 2 and not res2["signatures"]


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"): v(); print("ok", k)
