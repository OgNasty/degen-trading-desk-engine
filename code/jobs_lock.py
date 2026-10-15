"""Scheduled-job lock (bugfix1012, 2026-10-12, owner).

Scheduled jobs (morning task / daily v2, Friday RSI/trim, savings writers run by
them) hold scheduled-job.lock for their whole run. The minute edge check never
waits on it: it SKIPs at start when the lock is held, and aborts (no write, no
send) if the lock appears mid-check. A job that takes the lock first waits
(bounded) for a running edge check to finish (edge-check.running marker), so the
edge check can never overwrite a job's cost-basis.json writes.
"""
import json, os, time
from contextlib import contextmanager
import degen_home as _DH

LOCK_PATH = os.path.join(_DH.HOME_STR, "scheduled-job.lock")
EDGE_MARK = os.path.join(_DH.HOME_STR, "edge-check.running")
STALE_SEC = 15 * 60
EDGE_STALE_SEC = 5 * 60


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"job": "unknown", "t": os.path.getmtime(path)} if os.path.exists(path) else None


def held(now=None, path=None):
    """Lock info dict when a non-stale lock exists, else None (stale lock is ignored)."""
    path = path or LOCK_PATH
    info = _read(path)
    if not info:
        return None
    now = time.time() if now is None else now
    if now - float(info.get("t") or 0) > STALE_SEC:
        return None
    return info


def acquire(job, path=None, edge_mark=None, wait_edge_sec=180, poll=1.0, now=time.time, sleep=time.sleep):
    path = path or LOCK_PATH
    edge_mark = edge_mark or EDGE_MARK
    cur = held(path=path)
    if cur and int(cur.get("pid") or 0) != os.getpid():
        # another job holds it: wait for it (jobs are short), then take over if stale
        end = now() + STALE_SEC
        while held(path=path) and now() < end:
            sleep(poll)
    info = {"job": job, "pid": os.getpid(), "t": now()}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f)
    os.replace(tmp, path)
    # let an edge check that started before us finish (it aborts on its own if it sees the lock)
    end = now() + wait_edge_sec
    while now() < end:
        m = _read(edge_mark)
        if not m or now() - float(m.get("t") or 0) > EDGE_STALE_SEC:
            break
        sleep(poll)
    return info


def release(info, path=None):
    path = path or LOCK_PATH
    cur = _read(path)
    if cur and info and cur.get("pid") == info.get("pid") and cur.get("t") == info.get("t"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


@contextmanager
def hold(job, **kw):
    info = acquire(job, **kw)
    try:
        yield info
    finally:
        release(info, path=kw.get("path"))


def edge_mark_set(path=None):
    path = path or EDGE_MARK
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "t": time.time()}, f)
    os.replace(tmp, path)


def edge_mark_clear(path=None):
    try:
        os.remove(path or EDGE_MARK)
    except FileNotFoundError:
        pass
