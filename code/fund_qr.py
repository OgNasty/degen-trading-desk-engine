#!/usr/bin/env python3
"""Funding step helper (template). Writes wallet-qr.png (solana:<address> QR) in DEGEN_HOME and prints the
public address. Usage: python fund_qr.py [--out PATH]. The agent sends the PNG plus the address in a code span."""
import argparse
import qrcode
import degen_home as _DH


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--out", default=str(_DH.HOME / "wallet-qr.png"))
    a = ap.parse_args(argv)
    addr = _DH.wallet_address()
    if not addr:
        raise SystemExit("WALLET_ADDRESS not set")
    img = qrcode.make("solana:" + addr, box_size=10, border=4)
    img.save(a.out)
    print(f"QR {a.out}\nADDRESS {addr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
