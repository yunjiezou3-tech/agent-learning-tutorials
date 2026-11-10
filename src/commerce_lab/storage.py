"""Durable events, a simulated order system, and explicit customer memories."""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class LabStore:
    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_ticket_id ON events(ticket_id, id);
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('paid', 'refunded')),
                version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS refund_operations (
                operation_key TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
                customer_id TEXT NOT NULL,
                memory_key TEXT NOT NULL,
                memory_value TEXT NOT NULL,
                source_ticket TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(customer_id, memory_key)
            );
            """
        )

    def close(self) -> None:
        self.conn.close()

    def append_event(self, ticket_id: str, kind: str, payload: Dict[str, Any]) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO events(ticket_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True), _now()),
            )
        return int(cursor.lastrowid)

    def events(self, ticket_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, kind, payload FROM events WHERE ticket_id = ? ORDER BY id", (ticket_id,)
        ).fetchall()
        return [{"id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload"])} for row in rows]

    def seed_order(self, order_id: str, customer_id: str, amount_cents: int) -> None:
        if amount_cents <= 0:
            raise ValueError("amount_cents must be positive")
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO orders(order_id, customer_id, amount_cents, status, version) "
                "VALUES (?, ?, ?, 'paid', 1)",
                (order_id, customer_id, amount_cents),
            )

    def order(self, order_id: str, customer_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT order_id, customer_id, amount_cents, status, version FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if row is None or (customer_id is not None and row["customer_id"] != customer_id):
            return None
        return dict(row)

    def execute_refund(self, order_id: str, expected_version: int, operation_key: str) -> str:
        """Return executed, already_applied, or stale; the operation key prevents double refunds."""
        with self.conn:
            previous = self.conn.execute(
                "SELECT order_id FROM refund_operations WHERE operation_key = ?", (operation_key,)
            ).fetchone()
            if previous is not None:
                return "already_applied" if previous["order_id"] == order_id else "stale"
            order = self.conn.execute(
                "SELECT amount_cents, status, version FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if order is None or order["status"] != "paid" or order["version"] != expected_version:
                return "stale"
            updated = self.conn.execute(
                "UPDATE orders SET status = 'refunded', version = version + 1 "
                "WHERE order_id = ? AND status = 'paid' AND version = ?",
                (order_id, expected_version),
            )
            if updated.rowcount != 1:
                return "stale"
            self.conn.execute(
                "INSERT INTO refund_operations(operation_key, order_id, amount_cents, created_at) "
                "VALUES (?, ?, ?, ?)",
                (operation_key, order_id, order["amount_cents"], _now()),
            )
        return "executed"

    def remember(self, customer_id: str, key: str, value: str, source_ticket: str) -> None:
        """Only explicit preferences belong here; an old memory is never refund consent."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO memories(customer_id, memory_key, memory_value, source_ticket, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(customer_id, memory_key) DO UPDATE SET "
                "memory_value=excluded.memory_value, source_ticket=excluded.source_ticket, "
                "updated_at=excluded.updated_at",
                (customer_id, key, value, source_ticket, _now()),
            )

    def recall(self, customer_id: str, key: str) -> Optional[Dict[str, str]]:
        row = self.conn.execute(
            "SELECT memory_value, source_ticket, updated_at FROM memories "
            "WHERE customer_id = ? AND memory_key = ?",
            (customer_id, key),
        ).fetchone()
        return dict(row) if row else None

    def refund_count(self, order_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM refund_operations WHERE order_id = ?", (order_id,)
        ).fetchone()
        return int(row["count"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
