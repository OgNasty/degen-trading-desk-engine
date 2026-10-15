#!/usr/bin/env python3
"""Trim sol-usdc-log.md to entries from the last N days (default 5).

Keeps the title block (everything before the first '## ' heading). Each '## ' or
'### ' section is dated by the first YYYY-MM-DD in its heading, else in its body;
sections older than the cutoff date are dropped, undated sections are kept.
Bytes appended by a running check while trimming are carried over.
deskjson 2026-10-11: no scheduled day trim any more (owner: the log stays on the PC and
may grow). prune_by_size() is the only automatic trim: over 350 MB it drops the oldest
dated sections until the file is under 300 MB (logdiet 2026-10-11); box_runner.py calls maybe_prune_by_size() hourly.
"""
import degen_home as _DH  # portable base dir (pcmove 2026-10-10)
import os, re, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from degen_home import TZ_NAME as _TZ_NAME

LOG = _DH.HOME_STR + "/sol-usdc-log.md"
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
PT = ZoneInfo(_TZ_NAME)


KEEP_DAYS = 2  # owner 2026-10-11: daily trim, keep 2 days (60s checks)


def prune(path=LOG, days=KEEP_DAYS, now=None):
    now = now or datetime.now(PT)
    cutoff = (now - timedelta(days=days)).date().isoformat()
    with open(path, "rb") as f:
        raw = f.read()
    orig_len = len(raw)
    lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
    head, sections, cur = [], [], None
    for ln in lines:
        if ln.startswith("## ") or ln.startswith("### "):
            cur = [ln]
            sections.append(cur)
        elif cur is None:
            head.append(ln)
        else:
            cur.append(ln)
    kept, dropped = [], 0
    for sec in sections:
        m = DATE_RE.search(sec[0]) or DATE_RE.search("".join(sec[1:]))
        if m and m.group(1) < cutoff:
            dropped += 1
            continue
        kept.append(sec)
    out = "".join(head) + "".join("".join(s) for s in kept)
    tmp = path + ".prune.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:  # keep "\n" on Windows (no CRLF)
        f.write(out)
        with open(path, "rb") as src:  # carry over anything appended meanwhile
            src.seek(orig_len)
            extra = src.read()
        if extra:
            f.write(extra.decode("utf-8", errors="replace"))
    os.replace(tmp, path)
    return {"cutoff": cutoff, "sections_dropped": dropped, "sections_kept": len(kept),
            "bytes_before": orig_len, "bytes_after": os.path.getsize(path)}


MAX_BYTES = 350 * 1024 * 1024  # logdiet 2026-10-11: trim at 350 MB
TARGET_BYTES = 300 * 1024 * 1024  # ...down to under 300 MB


def _split(lines):
    head, sections, cur = [], [], None
    for ln in lines:
        if ln.startswith("## ") or ln.startswith("### "):
            cur = [ln]
            sections.append(cur)
        elif cur is None:
            head.append(ln)
        else:
            cur.append(ln)
    return head, sections


def prune_by_size(path=LOG, max_bytes=MAX_BYTES, target=TARGET_BYTES):
    """Size safety valve: if path > max_bytes, drop the oldest sections (keeping the head
    block) until it is under target. Atomic (tmp + os.replace); bytes appended meanwhile
    are carried over."""
    size = os.path.getsize(path)
    if size <= max_bytes:
        return {"pruned": False, "bytes": size}
    with open(path, "rb") as f:
        raw = f.read()
    orig_len = len(raw)
    head, sections = _split(raw.decode("utf-8", errors="replace").splitlines(keepends=True))
    head_b = len("".join(head).encode("utf-8"))
    sizes = [len("".join(s).encode("utf-8")) for s in sections]
    total = head_b + sum(sizes)
    drop = 0
    while drop < len(sections) - 1 and total > target:
        total -= sizes[drop]
        drop += 1
    out = "".join(head) + "".join("".join(s) for s in sections[drop:])
    tmp = path + ".prune.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(out)
        with open(path, "rb") as src:  # carry over anything appended meanwhile
            src.seek(orig_len)
            extra = src.read()
        if extra:
            f.write(extra.decode("utf-8", errors="replace"))
    os.replace(tmp, path)
    return {"pruned": True, "sections_dropped": drop, "sections_kept": len(sections) - drop,
            "bytes_before": orig_len, "bytes_after": os.path.getsize(path)}


def maybe_prune_by_size(path=LOG):
    """Cheap: one stat; only reads the file when it is over MAX_BYTES."""
    try:
        if os.path.getsize(path) <= MAX_BYTES:
            return None
    except OSError:
        return None
    return prune_by_size(path)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--size":
        print(prune_by_size())
        raise SystemExit(0)
    days = int(sys.argv[1]) if len(sys.argv) > 1 else KEEP_DAYS
    print(prune(days=days))
