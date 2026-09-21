"""SQLite state.

`library` is one row per uploaded track, and the only thing that keeps its
audio alive in sources/. `slots` says which track is in which of the pedal's 99
slots, by hash and nothing else, so a rename has one home.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional

from . import config

log = logging.getLogger(__name__)

_local = threading.local()
# `PRAGMA journal_mode=WAL` takes an exclusive lock without waiting on
# busy_timeout, so threads first-connecting at the same instant are serialized.
_open_lock = threading.Lock()

SCHEMA_VERSION = 4

# Statements, not a script: executescript() commits the transaction _init holds
# to serialize concurrent first connects.
_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS library (
        source_hash TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        duration    REAL NOT NULL DEFAULT 0,
        added       REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS slots (
        slot        INTEGER PRIMARY KEY,
        source_hash TEXT NOT NULL,
        state       TEXT NOT NULL,
        synced_hash TEXT,
        error       TEXT,
        updated     REAL NOT NULL
    )""",
]

# v2 or v3 -> v4: the library loses the folder columns v3 added, and the folders
# and trash tables go. The library is rebuilt by copy so every row survives.
# IF EXISTS because a v2 file never had the tables.
_TO_V4 = [
    """CREATE TABLE library_v4 (
        source_hash TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        duration    REAL NOT NULL DEFAULT 0,
        added       REAL NOT NULL
    )""",
    """INSERT INTO library_v4 (source_hash, name, duration, added)
       SELECT source_hash, name, duration, added FROM library""",
    "DROP TABLE library",
    "ALTER TABLE library_v4 RENAME TO library",
    "DROP TABLE IF EXISTS folders",
    "DROP TABLE IF EXISTS trash",
]


def _has_table(c: sqlite3.Connection, name: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                     (name,)).fetchone() is not None


def _backup(c: sqlite3.Connection, tag: str) -> None:
    """Copy the database beside itself before migrating it, named for the
    version on disk: `mv state.db.v3 state.db` is the way back."""
    dest = f"{config.DB_PATH}.{tag}"
    if os.path.exists(dest):
        return
    try:
        c.execute("VACUUM INTO ?", (dest,))
    except sqlite3.Error as e:
        if not os.path.exists(dest):
            log.warning("could not write the pre-migration backup %s: %s", dest, e)


def _init(c: sqlite3.Connection) -> None:
    """Bring the file to SCHEMA_VERSION. The already-current case costs one
    PRAGMA read; BEGIN IMMEDIATE serializes concurrent first connects."""
    v = c.execute("PRAGMA user_version").fetchone()[0]
    if v == SCHEMA_VERSION:
        return
    if v > SCHEMA_VERSION:
        raise RuntimeError(
            f"{config.DB_PATH} is schema v{v}; this build understands "
            f"v{SCHEMA_VERSION}. Deploy the newer code or restore a backup.")
    if v == 0 and _has_table(c, "slots"):
        # v1 never stamped a version and had no library table.
        raise RuntimeError(
            f"{config.DB_PATH} is a v1 database, which this build cannot "
            "migrate. Move it aside and upload the tracks again.")
    if v > 0:
        _backup(c, f"v{v}")

    c.execute("BEGIN IMMEDIATE")
    try:
        v = c.execute("PRAGMA user_version").fetchone()[0]   # re-read under lock
        if v >= SCHEMA_VERSION:
            c.commit()
            return
        for stmt in (_SCHEMA if v == 0 else _TO_V4):
            c.execute(stmt)
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        c.commit()
    except Exception:
        c.rollback()
        raise


def conn() -> sqlite3.Connection:
    """One connection per thread, cached against the database path so a test
    that repoints DB_PATH gets a fresh one."""
    key = str(config.DB_PATH)
    c = getattr(_local, "conn", None)
    if c is not None and getattr(_local, "path", None) == key:
        return c
    config.DATA.mkdir(parents=True, exist_ok=True)
    with _open_lock:
        c = sqlite3.connect(key, timeout=10)
        try:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            _init(c)
        except Exception:
            c.close()
            raise
    _local.conn, _local.path = c, key
    return c


def _row_to_dict(r: sqlite3.Row) -> Dict:
    d = dict(r)
    # `synced` is derived, never trusted from a stored flag.
    if d.get("state") == "staged" and d.get("synced_hash") == d.get("source_hash"):
        d["state"] = "synced"
    elif d.get("state") == "synced" and d.get("synced_hash") != d.get("source_hash"):
        d["state"] = "staged"
    return d


# LEFT join: a slot whose library row has gone still shows, as a visible
# problem the user can clear, rather than vanishing.
_SLOT_SELECT = """
    SELECT s.*, COALESCE(l.name, '(missing)') AS display_name,
           COALESCE(l.duration, 0) AS duration
      FROM slots s LEFT JOIN library l ON l.source_hash = s.source_hash
"""


def all_slots() -> List[Dict]:
    return [_row_to_dict(r) for r in
            conn().execute(_SLOT_SELECT + " ORDER BY s.slot")]


def get_slot(slot: int) -> Optional[Dict]:
    r = conn().execute(_SLOT_SELECT + " WHERE s.slot=?", (slot,)).fetchone()
    return _row_to_dict(r) if r else None


def slots_for_hash(source_hash: str) -> List[int]:
    return [r["slot"] for r in conn().execute(
        "SELECT slot FROM slots WHERE source_hash=? ORDER BY slot",
        (source_hash,))]


def put_slot(slot: int, source_hash: str, state: str = "converting") -> None:
    conn().execute(
        """INSERT INTO slots (slot, source_hash, state, synced_hash, error,
                              updated)
           VALUES (?,?,?,NULL,NULL,?)
           ON CONFLICT(slot) DO UPDATE SET
               source_hash=excluded.source_hash,
               state=excluded.state,
               synced_hash=NULL,
               error=NULL,
               updated=excluded.updated""",
        (slot, source_hash, state, time.time()),
    )
    conn().commit()


def set_state(slot: int, state: str, error: Optional[str] = None) -> None:
    conn().execute(
        "UPDATE slots SET state=?, error=?, updated=? WHERE slot=?",
        (state, error, time.time(), slot),
    )
    conn().commit()


def mark_synced(slot: int, source_hash: str) -> None:
    conn().execute(
        """UPDATE slots SET state='synced', synced_hash=?, error=NULL, updated=?
           WHERE slot=?""",
        (source_hash, time.time(), slot),
    )
    conn().commit()


def move_or_swap(src: int, dst: int) -> Optional[str]:
    """Move src into an empty dst, or swap two occupied slots.

    The choice is made inside one BEGIN IMMEDIATE transaction: decided outside
    it, a concurrent upload could fill dst in the gap and a plain move would
    delete it. Returns "move", "swap", or None (src empty, or src == dst).
    """
    if src == dst:
        return None
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        if not get_slot(src):
            c.commit()
            return None
        now = time.time()
        if get_slot(dst):
            # -1 is a scratch key; slot is the primary key.
            c.execute("UPDATE slots SET slot=-1 WHERE slot=?", (src,))
            c.execute("UPDATE slots SET slot=? WHERE slot=?", (src, dst))
            c.execute("UPDATE slots SET slot=? WHERE slot=-1", (dst,))
            c.execute("UPDATE slots SET synced_hash=NULL, state='staged', "
                      "updated=? WHERE slot IN (?,?)", (now, src, dst))
            op = "swap"
        else:
            c.execute("DELETE FROM slots WHERE slot=?", (dst,))
            c.execute("UPDATE slots SET slot=?, synced_hash=NULL, "
                      "state='staged', updated=? WHERE slot=?", (dst, now, src))
            op = "move"
        c.commit()
        return op
    except Exception:
        c.rollback()
        raise


def delete_slot(slot: int) -> None:
    c = conn()
    with c:
        c.execute("DELETE FROM slots WHERE slot=?", (slot,))


# ---------------------------------------------------------------- library

def library_add(source_hash: str, name: str, duration: float) -> bool:
    """Record an uploaded track. DO NOTHING on conflict, so uploading the same
    file again does not undo a rename. Returns True if this call inserted."""
    c = conn()
    cur = c.execute(
        """INSERT INTO library (source_hash, name, duration, added)
           VALUES (?,?,?,?)
           ON CONFLICT(source_hash) DO NOTHING""",
        (source_hash, name, duration, time.time()),
    )
    c.commit()
    return cur.rowcount > 0


def library_all() -> List[Dict]:
    """Newest first. Searching and sorting happen in the browser."""
    return [dict(r) for r in conn().execute(
        "SELECT * FROM library ORDER BY added DESC")]


def library_get(source_hash: str) -> Optional[Dict]:
    r = conn().execute("SELECT * FROM library WHERE source_hash=?",
                       (source_hash,)).fetchone()
    return dict(r) if r else None


def library_rename(source_hash: str, name: str) -> bool:
    c = conn()
    with c:
        cur = c.execute("UPDATE library SET name=? WHERE source_hash=?",
                        (name, source_hash))
    return cur.rowcount > 0


def library_delete(source_hash: str) -> bool:
    c = conn()
    with c:
        cur = c.execute("DELETE FROM library WHERE source_hash=?",
                        (source_hash,))
    return cur.rowcount > 0


def hash_in_library(source_hash: str) -> bool:
    return conn().execute(
        "SELECT 1 FROM library WHERE source_hash=? LIMIT 1",
        (source_hash,)).fetchone() is not None
