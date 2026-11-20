"""M8 回款核销持久化。整批次原子替换，保证重跑不新增核销或预收。"""
import json
from datetime import datetime, timezone

from app.clock import utc_text
from app.modules.m8_clearing.engine import PrepayLot
from app.modules.m8_clearing.reader import Receipt


def replace_batch(connection, batch_id, source_id, content_digest, policy, received_at, result: dict):
    connection.execute("DELETE FROM clearing_allocations WHERE batch_id=?", (batch_id,))
    connection.execute("DELETE FROM remit_receipts WHERE batch_id=?", (batch_id,))
    connection.execute("""
        INSERT INTO remit_batches(batch_id, source_id, content_digest, policy, received_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(batch_id) DO UPDATE SET source_id=excluded.source_id,
            content_digest=excluded.content_digest, policy=excluded.policy, received_at=excluded.received_at
    """, (batch_id, source_id, content_digest, policy, received_at))
    for item in result["receipts"]:
        connection.execute("""
            INSERT INTO remit_receipts(batch_id, receipt_id, customer_id, effective, known,
                amount_cents, refs, note, line, status, quarantine_code, prepay_change_cents)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (batch_id, item["receipt_id"], item["customer_id"], item["effective"], item["known"],
              item["amount_cents"], json.dumps(item["refs"], ensure_ascii=False), item["note"], item["line"],
              item["status"], item.get("code"), item["prepay_change_cents"]))
    for row in result["allocations"]:
        connection.execute("""
            INSERT INTO clearing_allocations(batch_id, seq, receipt_id, customer_id, invoice_id,
                version_event_id, kind, amount_cents, basis, capped_cents)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (batch_id, row["seq"], row["receipt_id"], row["customer_id"], row["invoice_id"],
              row["version_event_id"], row["kind"], row["amount_cents"], row["basis"], row["capped_cents"]))


def get_batch(connection, batch_id):
    row = connection.execute("SELECT * FROM remit_batches WHERE batch_id=?", (batch_id,)).fetchone()
    return dict(row) if row else None


def list_batches(connection):
    return [dict(row) for row in connection.execute(
        "SELECT batch_id, source_id, content_digest, policy, received_at FROM remit_batches "
        "ORDER BY received_at, batch_id")]


def load_receipts(connection, batch_id) -> list[Receipt]:
    rows = connection.execute(
        "SELECT * FROM remit_receipts WHERE batch_id=? ORDER BY line", (batch_id,))
    return [Receipt(line=row["line"], batch_id=batch_id, receipt_id=row["receipt_id"],
                    customer_id=row["customer_id"], effective=row["effective"], known=row["known"],
                    amount_cents=row["amount_cents"], refs=tuple(json.loads(row["refs"])),
                    note=row["note"]) for row in rows]


def load_prepay_lots(connection, exclude_batch: str | None = None) -> list[PrepayLot]:
    """预收台账＝各回款的预收净留存，减去该笔预收已被 PREPAY 冲抵的金额（留痕追溯原始回款）。

    exclude_batch 用于系统批次重算：先剔除该批次旧留痕，再按相同输入重算。
    """
    rows = connection.execute("""
        SELECT r.customer_id, r.receipt_id, r.effective AS opened,
               r.prepay_change_cents
               - COALESCE((SELECT SUM(a.amount_cents) FROM clearing_allocations a
                           WHERE a.receipt_id = r.receipt_id AND a.kind = 'PREPAY'
                             AND (? IS NULL OR a.batch_id <> ?)), 0) AS amount_cents
        FROM remit_receipts r
        WHERE r.status = 'CLEARED' AND r.prepay_change_cents > 0
        ORDER BY r.effective, r.receipt_id
    """, (exclude_batch, exclude_batch)).fetchall()
    return [PrepayLot(row["customer_id"], row["receipt_id"], row["opened"], row["amount_cents"])
            for row in rows if row["amount_cents"] > 0]


def load_all_allocations(connection):
    return [dict(row) for row in connection.execute(
        "SELECT * FROM clearing_allocations ORDER BY batch_id, seq")]


def load_all_receipts(connection):
    return [dict(row) for row in connection.execute(
        "SELECT * FROM remit_receipts ORDER BY batch_id, line")]


def upsert_explanation(connection, period_key, note):
    connection.execute("""
        INSERT INTO clearing_explanations(period_key, note, recorded_at) VALUES (?, ?, ?)
        ON CONFLICT(period_key) DO UPDATE SET note=excluded.note, recorded_at=excluded.recorded_at
    """, (period_key, note, utc_text(datetime.now(timezone.utc))))


def get_explanation(connection, period_key):
    row = connection.execute(
        "SELECT * FROM clearing_explanations WHERE period_key=?", (period_key,)).fetchone()
    return dict(row) if row else None
