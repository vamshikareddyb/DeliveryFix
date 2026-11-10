"""Persistence for case threads, so a restart doesn't lose paused or monitored cases.

- On LangGraph, use a real checkpointer: make_checkpointer("cases.db") returns a
  SqliteSaver (pip install langgraph-checkpoint-sqlite). In production use the
  Postgres saver instead; nothing else changes.
- The simulator uses SqliteThreadStore: one JSON row per thread, including which
  node it is paused at and the interrupt payload.
"""
from __future__ import annotations

import json
import sqlite3
import threading


def make_checkpointer(path: str):
    """Persistent LangGraph checkpointer backed by SQLite."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    return SqliteSaver(sqlite3.connect(path, check_same_thread=False))


class SqliteThreadStore:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self.conn.execute("CREATE TABLE IF NOT EXISTS sim_threads (thread_id TEXT PRIMARY KEY, state TEXT NOT NULL)")
            self.conn.commit()

    def load(self, thread_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT state FROM sim_threads WHERE thread_id = ?", (thread_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, thread_id: str, state: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO sim_threads (thread_id, state) VALUES (?, ?) "
                "ON CONFLICT(thread_id) DO UPDATE SET state = excluded.state",
                (thread_id, json.dumps(state, default=str)),
            )
            self.conn.commit()
