"""Where .bak-<tag> backups go (status1012, owner 2026-10-12).

Dashboard files (bot\\dashboard\\*, the Desktop copy ..\\bot-desk.html, dashboard-worker\\public\\*) are backed up into
bot\\backups\\dashboard\\ (Desktop copy -> bot\\backups\\dashboard\\desktop\\), never next to the live files, so the
shared/Desktop and dashboard folders stay clean. Code/data files keep the usual <file>.bak-<tag> beside them.
Nothing (publish, snapshot, desk.json) reads these backups.
"""
import os
import shutil

import degen_home as _DH

BOT = os.path.abspath(_DH.HOME_STR)
DESKTOP = os.path.dirname(BOT)
DASH_DIRS = (os.path.join(BOT, "dashboard"), os.path.join(BOT, "dashboard-worker", "public"))
DEST = os.path.join(BOT, "backups", "dashboard")


def bak_path(path, tag):
    p = os.path.abspath(path)
    name = os.path.basename(p) + ".bak-" + tag
    d = os.path.dirname(p)
    if os.path.normcase(d) == os.path.normcase(DESKTOP):
        return os.path.join(DEST, "desktop", name)
    if any(os.path.normcase(d) == os.path.normcase(x) for x in DASH_DIRS):
        sub = "" if os.path.normcase(d) == os.path.normcase(DASH_DIRS[0]) else "worker-public"
        return os.path.join(DEST, sub, name) if sub else os.path.join(DEST, name)
    return p + ".bak-" + tag


def backup(path, tag):
    """Copy path to its backup location once (never overwrites). Returns the backup path or None."""
    if not os.path.exists(path):
        return None
    b = bak_path(path, tag)
    if os.path.exists(b):
        return b
    os.makedirs(os.path.dirname(b), exist_ok=True)
    shutil.copy2(path, b)
    return b
