# Degen Trading Desk engine

Engine code behind **Degen Trading Desk**, a rules-based crypto trading bot for Solana. It trades a small wallet between SOL, USDC, Bitcoin (cbBTC) and Ether (WETH) and publishes a live dashboard.

**Version 1.1.0**

## What's here

| Path | What it is |
|---|---|
| `code/` | Readable engine source (Python, dashboard, Cloudflare Worker, scheduler scripts) |
| `TRADING TEMPLATE ENGINE.txt` | The same files packed into one bundle with per-file sha256 hashes. The setup guide downloads this. |
| `engine-version.json` | Small manifest (version, release date, size and sha256 of the bundle). Update checks read only this file. |
| `requirements.txt` | Python dependencies (Python 3.11+) |

Installs normally happen through the Degen Trading Desk Grok Bot template, which downloads the bundle, checks its hash and walks you through setup. `code/update_engine.py check` compares an install against `engine-version.json` here.

## Paper mode by default

A new install starts in **paper mode**. It runs every rule and logs what it would do, but sends no transactions until you switch it to live.

## Risk notice

This is experimental software that trades real cryptocurrency once live mode is on. Crypto prices are volatile, and bugs, bad data, network problems or rule behavior can lose money. Use only funds you can afford to lose. Nothing here is financial advice, and the software comes with no warranty of any kind. You're responsible for your own keys, wallet and trades.

Keep your `.env`, `config.json`, wallet and keys private. They're owner files and never belong in this repo.

## License

No license has been chosen yet, so all rights are reserved for now. You can read the code, but please ask before reusing it.
