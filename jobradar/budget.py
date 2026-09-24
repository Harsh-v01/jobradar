"""Daily token budget.

The free tier is a fixed number of tokens per rolling day. Exhausting it does
not just stop the run - it blocks every other use of the key until reset. So
spend is recorded as it happens and the scan stops while there is still room,
rather than discovering the ceiling by hitting it.
"""
from __future__ import annotations

import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (
    day     TEXT PRIMARY KEY,
    tokens  INTEGER NOT NULL DEFAULT 0
);
"""


class Budget:
    def __init__(self, db: sqlite3.Connection, daily_limit: int, reserve: int = 40_000):
        self.db = db
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.daily_limit = daily_limit
        # Held back so a digest, an alert or a profile rebuild can still run
        # after a big scan has taken its share.
        self.reserve = reserve

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def used(self) -> int:
        row = self.db.execute("SELECT tokens FROM spend WHERE day = ?", (self._today(),)).fetchone()
        return (row[0] if row else 0) or 0

    def remaining(self) -> int:
        return max(0, self.daily_limit - self.reserve - self.used())

    def record(self, tokens: int) -> None:
        self.db.execute(
            "INSERT INTO spend (day, tokens) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET tokens = tokens + excluded.tokens",
            (self._today(), max(0, tokens)),
        )
        self.db.commit()

    def can_afford(self, tokens: int) -> bool:
        return self.remaining() >= tokens

    def report(self) -> str:
        used, limit = self.used(), self.daily_limit
        pct = 100 * used // limit if limit else 0
        return f"{used:,}/{limit:,} tokens today ({pct}%), {self.remaining():,} spendable"
