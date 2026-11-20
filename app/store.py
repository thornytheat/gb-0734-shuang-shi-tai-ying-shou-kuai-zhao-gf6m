import sqlite3
from contextlib import contextmanager
from pathlib import Path

from fastapi import Request

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY, content BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS archives (
  run_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  rule_version TEXT NOT NULL, generated_at TEXT NOT NULL, elapsed_ms INTEGER NOT NULL,
  result_digest TEXT NOT NULL, payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS archives_time ON archives(generated_at DESC, run_id);
CREATE TABLE IF NOT EXISTS remit_batches (
  batch_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  content_digest TEXT NOT NULL, policy TEXT NOT NULL, received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS remit_receipts (
  batch_id TEXT NOT NULL, receipt_id TEXT NOT NULL, customer_id TEXT NOT NULL,
  effective TEXT NOT NULL, known TEXT NOT NULL, amount_cents INTEGER NOT NULL, refs TEXT NOT NULL,
  note TEXT NOT NULL, line INTEGER NOT NULL, status TEXT NOT NULL, quarantine_code TEXT,
  prepay_change_cents INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (batch_id, receipt_id)
);
CREATE INDEX IF NOT EXISTS remit_receipts_customer ON remit_receipts(customer_id);
CREATE TABLE IF NOT EXISTS clearing_allocations (
  batch_id TEXT NOT NULL, seq INTEGER NOT NULL, receipt_id TEXT NOT NULL, customer_id TEXT NOT NULL,
  invoice_id TEXT NOT NULL, version_event_id TEXT NOT NULL, kind TEXT NOT NULL,
  amount_cents INTEGER NOT NULL, basis TEXT NOT NULL, capped_cents INTEGER NOT NULL,
  PRIMARY KEY (batch_id, seq)
);
CREATE INDEX IF NOT EXISTS clearing_allocations_customer ON clearing_allocations(customer_id, invoice_id);
CREATE TABLE IF NOT EXISTS clearing_explanations (
  period_key TEXT PRIMARY KEY, note TEXT NOT NULL, recorded_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def get_store(request: Request) -> Store:
    return request.app.state.store
