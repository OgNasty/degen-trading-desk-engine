"""Split sol-usdc-log.md into hourly chunks for the dashboard Worker (chunks 2026-10-11).

head.md = everything before the first dated '## '/'### ' section.
YYYY-MM-DDTHH.md (PT hour) = sections whose header timestamp is in that hour.
Undated sections (and dated ones whose hour is not later than the current chunk,
which keeps names unique and order intact) stay with the preceding chunk.
join(split(x)) == x byte-for-byte. Pure Python, Windows-safe.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from degen_home import TZ_NAME as _TZ_NAME

PT = ZoneInfo(_TZ_NAME)
_TS = re.compile(rb"(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(Z|[+-]\d{2}:?\d{2})?")


def _hour_key(line: bytes) -> str | None:
    m = _TS.search(line)
    if not m:
        return None
    d, hh, mm, ss, off = (g.decode() if g else None for g in m.groups())
    try:
        if off:
            off = "+00:00" if off == "Z" else (off if ":" in off else off[:3] + ":" + off[3:])
            dt = datetime.fromisoformat(f"{d}T{hh}:{mm}:{ss}{off}").astimezone(PT)
        else:
            dt = datetime.fromisoformat(f"{d}T{hh}:{mm}:{ss}")
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%dT%H")


def split(data: bytes) -> list[tuple[str, bytes]]:
    """Return ordered [(name, bytes)], first is always 'head.md'."""
    chunks: list[list] = [["head.md", bytearray()]]
    cur_key = ""
    for line in data.splitlines(keepends=True):
        if line.startswith(b"## ") or line.startswith(b"### "):
            k = _hour_key(line)
            if k and k > cur_key:
                cur_key = k
                chunks.append([f"{k}.md", bytearray()])
        chunks[-1][1] += line
    return [(n, bytes(b)) for n, b in chunks]


def join(parts: list[bytes]) -> bytes:
    return b"".join(parts)


def write_chunks(src: Path, out_dir: Path) -> dict[str, bytes]:
    """Write chunks + index.json into out_dir (only rewriting changed files),
    delete stale chunks. Returns {relative name: bytes} of everything written/kept."""
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = split(src.read_bytes())
    files = {n: b for n, b in parts}
    files["index.json"] = (json.dumps({"chunks": [n for n, _ in parts]}, indent=0) + "\n").encode()
    for name, data in files.items():
        dst = out_dir / name
        if dst.is_file() and dst.read_bytes() == data:
            continue
        tmp = dst.with_name(f".{name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(dst)
    for p in out_dir.iterdir():
        if p.name not in files:
            if p.is_file():
                p.unlink()
    return files
