"""SQLite state. One row per occupied slot, plus a trash log."""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Dict, List, Optional

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    slot         INTEGER PRIMARY KEY,
    source_hash  TEXT NOT NULL,
    display_name TEXT NOT NULL,
    duration     REAL NOT NULL DEFAULT 0,
    state        TEXT NOT NULL,
    synced_hash  TEXT,
    error        TEXT,
    updated      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trash (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slot         INTEGER NOT NULL,
    source_hash  TEXT NOT NULL,
    display_name TEXT NOT NULL,
    duration     REAL NOT NULL DEFAULT 0,
    deleted      REAL NOT NULL
);
"""


def conn() -> sqlite3.Connection:
    """One connection per thread. SQLite objects aren't shareable."""
    c = getattr(_local, "conn", None)
    if c is None:
        config.DATA.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(config.DB_PATH), timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        c.executescript(SCHEMA)
        _local.conn = c
    return c


def _row_to_dict(r: sqlite3.Row) -> Dict:
    d = dict(r)
    # `synced` is derived, never trusted from a stored flag.
    if d.get("state") == "staged" and d.get("synced_hash") == d.get("source_hash"):
        d["state"] = "synced"
    elif d.get("state") == "synced" and d.get("synced_hash") != d.get("source_hash"):
        d["state"] = "staged"
    return d


def all_slots() -> List[Dict]:
    return [_row_to_dict(r) for r in
            conn().execute("SELECT * FROM slots ORDER BY slot")]


def get_slot(slot: int) -> Optional[Dict]:
    r = conn().execute("SELECT * FROM slots WHERE slot=?", (slot,)).fetchone()
    return _row_to_dict(r) if r else None


def put_slot(slot: int, source_hash: str, display_name: str,
             duration: float, state: str = "converting") -> None:
    conn().execute(
        """INSERT INTO slots (slot, source_hash, display_name, duration,
                              state, synced_hash, error, updated)
           VALUES (?,?,?,?,?,NULL,NULL,?)
           ON CONFLICT(slot) DO UPDATE SET
               source_hash=excluded.source_hash,
               display_name=excluded.display_name,
               duration=excluded.duration,
               state=excluded.state,
               synced_hash=NULL,
               error=NULL,
               updated=excluded.updated""",
        (slot, source_hash, display_name, duration, state, time.time()),
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


def set_duration(slot: int, duration: float) -> None:
    conn().execute("UPDATE slots SET duration=? WHERE slot=?", (duration, slot))
    conn().commit()


def move_slot(src: int, dst: int) -> None:
    """Move into an empty destination."""
    row = get_slot(src)
    if not row:
        return
    c = conn()
    with c:
        c.execute("DELETE FROM slots WHERE slot=?", (dst,))
        c.execute("UPDATE slots SET slot=?, synced_hash=NULL, state='staged', "
                  "updated=? WHERE slot=?", (dst, time.time(), src))


def swap_slots(a: int, b: int) -> None:
    """Exchange two occupied slots. Non-destructive, so reordering a setlist
    never sends anything to the trash. Both are marked unsynced because both
    files must be rewritten on the pedal."""
    if a == b:
        return
    c = conn()
    with c:
        # -1 is a scratch key; the slot column is the primary key so the two
        # updates would otherwise collide.
        c.execute("UPDATE slots SET slot=-1 WHERE slot=?", (a,))
        c.execute("UPDATE slots SET slot=? WHERE slot=?", (a, b))
        c.execute("UPDATE slots SET slot=? WHERE slot=-1", (b,))
        c.execute("UPDATE slots SET synced_hash=NULL, state='staged', updated=? "
                  "WHERE slot IN (?,?)", (time.time(), a, b))


def delete_slot(slot: int, to_trash: bool = True) -> Optional[int]:
    """Returns the new trash id, so an undo can name the exact entry."""
    row = get_slot(slot)
    trash_id = None
    c = conn()
    with c:
        if row and to_trash:
            cur = c.execute(
                """INSERT INTO trash (slot, source_hash, display_name,
                                      duration, deleted)
                   VALUES (?,?,?,?,?)""",
                (slot, row["source_hash"], row["display_name"],
                 row["duration"], time.time()),
            )
            trash_id = cur.lastrowid
        c.execute("DELETE FROM slots WHERE slot=?", (slot,))
    return trash_id


def trash_items() -> List[Dict]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM trash ORDER BY deleted DESC LIMIT 50")]


def trash_pop(trash_id: int) -> Optional[Dict]:
    r = conn().execute("SELECT * FROM trash WHERE id=?", (trash_id,)).fetchone()
    if not r:
        return None
    conn().execute("DELETE FROM trash WHERE id=?", (trash_id,))
    conn().commit()
    return dict(r)


def prune_trash(max_age_days: int) -> List[Dict]:
    cutoff = time.time() - max_age_days * 86400
    c = conn()
    with c:
        rows = [dict(r) for r in
                c.execute("SELECT * FROM trash WHERE deleted < ?", (cutoff,))]
        c.execute("DELETE FROM trash WHERE deleted < ?", (cutoff,))
    return rows


def hash_in_use(source_hash: str) -> bool:
    """Is this staged file still referenced by a slot or by trash?"""
    c = conn()
    a = c.execute("SELECT 1 FROM slots WHERE source_hash=? LIMIT 1",
                  (source_hash,)).fetchone()
    b = c.execute("SELECT 1 FROM trash WHERE source_hash=? LIMIT 1",
                  (source_hash,)).fetchone()
    return bool(a or b)
