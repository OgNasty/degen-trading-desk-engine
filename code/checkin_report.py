#!/usr/bin/env python3
"""24-hour check-in summary (template). Prints JSON for the one-shot check-in message: checks run, skips,
would-be (paper) fills, wallet value now vs funded (mark-to-market), dashboard link, paused/paper state.
Paper P&L here = wallet value change vs funded_usd (paper mode never trades) plus the count of would-be fills."""
import json
import re
from datetime import datetime, timedelta

import degen_home as _DH

H = _DH.HOME


def main():
    since = datetime.now().astimezone() - timedelta(hours=24)
    checks = skips = paper = 0
    compact = r"^(\d{4}-\d\d-\d\dT[\d:]+[-+]\d\d:\d\d) SKIP elapsed="
    for p in sorted((H / "runner-logs").glob("????-??-??.log"))[-2:]:  # box_runner daily logs
        try:
            recent = False
            block_paper = False
            for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                c = re.match(compact, ln)
                if c:  # quiet SKIP written as one line (no "check start" block)
                    if datetime.fromisoformat(c.group(1)) >= since:
                        checks += 1
                        skips += 1
                    continue
                m = re.search(r"(\d{4}-\d\d-\d\dT[\d:]+[-+]\d\d:\d\d)", ln)
                if m and "check start" in ln:  # "===== <ts> check start =====" opens each full check
                    recent = datetime.fromisoformat(m.group(1)) >= since
                    block_paper = False
                if not recent:
                    continue
                if ln.startswith("LOG_SUMMARY "):  # one per completed check
                    checks += 1
                    try:
                        skips += json.loads(ln[len("LOG_SUMMARY "):]).get("result") == "SKIP"
                    except Exception:
                        pass
                if "PAPER_TRADING: send skipped" in ln and not block_paper:
                    paper += 1  # one would-be fill per check, however many lines mention it
                    block_paper = True
        except Exception:
            pass
    cb = json.loads((H / "cost-basis.json").read_text(encoding="utf-8"))
    funded = float((cb.get("funding") or {}).get("funded_usd") or 0)
    now_usd = None
    try:
        import seed_prices as sp
        A = sp.assets(); px = sp.live_usd_prices(A); b = sp.balances(A)
        now_usd = round(sum(b[s] * px[s] for s in b), 2)
        try:  # v1.1.0 interest mode: lent USDC and the JitoSOL bucket are still the owner's money
            import degen_lend as _L, sol_stake as _S
            _ls, _ss = _L.load_state(), _S.load_state()
            now_usd = round(now_usd + float(_ls.get("lent_usdc") or 0) * float(px.get("USDC") or 1)
                            + _S.bucket_qty(_ss) * float(_ss.get("rate") or 0) * float(px.get("SOL") or 0), 2)
        except Exception:
            pass
    except Exception:
        pass
    out = {"window": "last 24h", "checks": checks, "skips": skips, "would_be_fills": paper,
           "funded_usd": funded, "wallet_usd_now": now_usd,
           "paper_pnl_usd": (round(now_usd - funded, 2) if now_usd is not None and funded else None),
           "paper_pnl_pct": (round((now_usd - funded) / funded * 100, 2) if now_usd is not None and funded else None),
           "paper_mode": _DH.paper_trading(), "paused": _DH.trading_paused(),
           "dashboard": (_DH.config().get("dashboard") or {}).get("public_url")}
    try:  # v1.1.0: interest mode + JitoSOL swap cost vs staking gain (report only)
        import interest_mode as _IM, sol_stake as _S
        out["interest_mode"] = bool(_IM.flag())
        _tk = _S.tracker()
        if _tk:
            out["jitosol_tracker"] = _tk
    except Exception:
        pass
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
