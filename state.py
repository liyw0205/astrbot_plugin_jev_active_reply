"""Bounded runtime state and durable counters; no chat or key persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo


def scope_id(umo: str) -> str:
    return hashlib.sha256(umo.encode()).hexdigest()[:24]


@dataclass
class Room:
    rows: deque = field(default_factory=lambda: deque(maxlen=64))
    seen: deque = field(default_factory=lambda: deque(maxlen=100))
    generation: int = 0
    cid: str = ""
    pending: str = ""
    pending_since: float = 0
    evaluated_at: float = -1e12
    last_sent: float = 0
    last_sender: str = ""
    last_text: str = ""
    delivery_backoff_until: float = 0
    sent_times: deque = field(default_factory=lambda: deque(maxlen=30))
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    phase: str = "idle"
    burst: dict = field(default_factory=dict)

    def notify(self):
        signal, self.changed = self.changed, asyncio.Event()
        signal.set()

    def claim(self, ticket: str, now: float):
        self.pending, self.pending_since, self.phase = ticket, now, "judging"
        self.notify()

    def observe(self, row: dict, message_id: str) -> bool:
        if message_id in self.seen:
            return False
        self.seen.append(message_id)
        self.rows.append(row)
        # Bound stored text by bytes, not destructive per-message previews.
        while (
            len(self.rows) > 1
            and sum(len(json.dumps(r, ensure_ascii=False).encode()) for r in self.rows)
            > 262144
        ):
            self.rows.popleft()
        self.generation += 1
        self.notify()
        return True

    def busy(self, now: float, timeout=120) -> bool:
        if self.pending and now - self.pending_since > timeout:
            self.pending = ""
            self.phase = "idle"
            self.notify()
        return bool(self.pending)

    def release(self, ticket: str):
        if self.pending == ticket:
            self.pending = ""
            self.phase = "idle"
            self.notify()


class RoomBook:
    def __init__(self, capacity=256):
        self.capacity = capacity
        self.rooms = OrderedDict()

    def get(self, umo: str) -> Room | None:
        if umo in self.rooms:
            self.rooms.move_to_end(umo)
            return self.rooms[umo]
        if len(self.rooms) >= self.capacity:
            free = next(
                (
                    key
                    for key, room in self.rooms.items()
                    if not room.busy(time.monotonic())
                ),
                None,
            )
            if free is None:
                return None
            del self.rooms[free]
        room = Room()
        self.rooms[umo] = room
        return room


class Ledger:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.closed = False
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA busy_timeout=500")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS counts(day TEXT, scope TEXT, kind TEXT, n INTEGER, PRIMARY KEY(day,scope,kind));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts REAL, scope TEXT, reason TEXT, details TEXT);
        CREATE TABLE IF NOT EXISTS cooldowns(scope TEXT PRIMARY KEY, until REAL);
        """)
        self.db.commit()

    @staticmethod
    def day():
        return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()

    def count(self, umo, kind):
        with self.lock:
            row = self.db.execute(
                "SELECT n FROM counts WHERE day=? AND scope=? AND kind=?",
                (self.day(), scope_id(umo), kind),
            ).fetchone()
        return row[0] if row else 0

    def bump(self, umo, kind):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO counts VALUES(?,?,?,1) ON CONFLICT(day,scope,kind) DO UPDATE SET n=n+1",
                (self.day(), scope_id(umo), kind),
            )

    def record(self, umo, reason, details=None):
        # Explicit allowlist, never persist upstream bodies or chat content.
        allowed = {
            k: v
            for k, v in (details or {}).items()
            if k
            in ("channel", "key_id", "model", "values", "mode", "status", "truncated")
        }
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO audit(ts,scope,reason,details) VALUES(?,?,?,?)",
                (
                    time.time(),
                    scope_id(umo),
                    reason,
                    json.dumps(allowed, ensure_ascii=False),
                ),
            )
            self.db.execute(
                "DELETE FROM audit WHERE id <= (SELECT COALESCE(MAX(id),0)-5000 FROM audit)"
            )
            self.db.execute(
                "DELETE FROM counts WHERE day < date(?, '-30 days')", (self.day(),)
            )

    def recent(self, umo):
        with self.lock:
            return self.db.execute(
                "SELECT reason FROM audit WHERE scope=? ORDER BY id DESC LIMIT 5",
                (scope_id(umo),),
            ).fetchall()

    async def acount(self, umo, kind):
        return await asyncio.to_thread(self.count, umo, kind)

    async def abump(self, umo, kind):
        await asyncio.to_thread(self.bump, umo, kind)

    async def arecord(self, umo, reason, details=None):
        await asyncio.to_thread(self.record, umo, reason, details)

    async def arecent(self, umo):
        return await asyncio.to_thread(self.recent, umo)

    def cooldown(self, scope, until=None):
        with self.lock, self.db:
            if until is not None:
                self.db.execute(
                    "INSERT INTO cooldowns VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET until=excluded.until",
                    (scope, until),
                )
            row = self.db.execute(
                "SELECT until FROM cooldowns WHERE scope=?", (scope,)
            ).fetchone()
            return row[0] if row else 0

    async def acooldown(self, scope, until=None):
        return await asyncio.to_thread(self.cooldown, scope, until)

    def close(self):
        with self.lock:
            if not self.closed:
                self.db.close()
                self.closed = True

    async def aclose(self):
        await asyncio.to_thread(self.close)
