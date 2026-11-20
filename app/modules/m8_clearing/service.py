"""M8 回款核销服务：批次幂等入库、客户欠款、预收台账与结账守卫。"""
from app.clock import cut_query, utc_text
from app.errors import Problem
from app.evidence import digest
from app.modules.m1_intake.service import load as load_source
from app.modules.m8_clearing import repository
from app.modules.m8_clearing.balances import Ledger
from app.modules.m8_clearing.engine import CLEARING_KINDS, POLICIES, PrepayLot, assemble
from app.modules.m8_clearing.reader import read as read_remit
from app.store import Store


def _period_key(effective: str, known: str) -> str:
    # 知悉时间归一到 UTC 文本，避免 Z 与 +08:00 同一时刻生成不同键
    return f"{effective}|{utc_text(cut_query(effective, known).known)}"


def _parse_cut(effective: str, known: str):
    return cut_query(effective, known)


def ingest_batch(store: Store, content: bytes, source_id: str, policy: str,
                 effective: str, known: str) -> dict:
    if policy not in POLICIES:
        raise Problem("INVALID_POLICY", f"裁决策略须为 {POLICIES} 之一")
    cut = _parse_cut(effective, known)
    remit = read_remit(content)
    batch_id = next(item.batch_id for item in remit.receipts)
    content_digest = digest(content)
    data = load_source(store, source_id)
    with store.connect() as connection:
        existing = repository.get_batch(connection, batch_id)
        if existing and (existing["content_digest"] != content_digest or existing["policy"] != policy
                         or existing["source_id"] != source_id):
            raise Problem("BATCH_CONFLICT", "同名批次内容、来源或裁决参数与已归档批次不一致", 409)
        prior_lots = repository.load_prepay_lots(connection)
        prior_batches = {row["batch_id"] for row in repository.list_batches(connection)}
        # 重跑同一批：剔除本批旧留痕后重算；其他批次的预收继续参与
        lots = [lot for lot in prior_lots if lot.receipt_id not in
                {r.receipt_id for r in remit.receipts}]
        result = assemble(data, remit.receipts, policy, lots, cut.effective, cut.known)
        result["batch_id"] = batch_id
        result["source_id"] = source_id
        result["content_digest"] = content_digest
        result["reused"] = batch_id in prior_batches
        result["input_quarantine"] = remit.quarantine
        result["cut"] = cut.public()
        received_at = max(item.known for item in remit.receipts)
        repository.replace_batch(connection, batch_id, source_id, content_digest, policy,
                                 received_at, result)
        stored = repository.get_batch(connection, batch_id)
        if stored["content_digest"] != content_digest or stored["policy"] != policy:
            raise Problem("CLEARING_WRITE_FAILED", "核销批次写入校验失败", 503)
    return result


def _aggregate(store: Store, source_id: str, effective: str, known: str) -> dict:
    cut = _parse_cut(effective, known)
    with store.connect() as connection:
        batches = repository.list_batches(connection)
        receipts_raw = repository.load_all_receipts(connection)
        allocations = repository.load_all_allocations(connection)
        lots = repository.load_prepay_lots(connection)
    # 直接基于持久化留痕聚合，保证工作台与入库裁决完全一致
    return _summarize(batches, receipts_raw, allocations, lots, cut)


def _summarize(batches, receipts_raw, allocations, lots, cut) -> dict:
    received = sum(row["amount_cents"] for row in receipts_raw)
    quarantined = sum(row["amount_cents"] for row in receipts_raw if row["status"] == "QUARANTINED")
    allocated = sum(row["amount_cents"] for row in allocations if row["kind"] in CLEARING_KINDS)
    prepay = sum(lot.amount_cents for lot in lots)
    fund_diff = received - quarantined - allocated - prepay
    by_customer = {}
    for row in receipts_raw:
        bucket = by_customer.setdefault(row["customer_id"], {
            "received_cents": 0, "quarantined_cents": 0, "allocated_cents": 0, "prepay_cents": 0})
        bucket["received_cents"] += row["amount_cents"]
        if row["status"] == "QUARANTINED":
            bucket["quarantined_cents"] += row["amount_cents"]
    for row in allocations:
        by_customer.setdefault(row["customer_id"], {
            "received_cents": 0, "quarantined_cents": 0, "allocated_cents": 0, "prepay_cents": 0})
        if row["kind"] in CLEARING_KINDS:
            by_customer[row["customer_id"]]["allocated_cents"] += row["amount_cents"]
    for lot in lots:
        by_customer.setdefault(lot.customer_id, {
            "received_cents": 0, "quarantined_cents": 0, "allocated_cents": 0, "prepay_cents": 0})
        by_customer[lot.customer_id]["prepay_cents"] += lot.amount_cents
    return {"cut": cut.public(), "batches": batches,
            "receipts": receipts_raw, "allocations": allocations,
            "prepayments": [lot.public(cut.effective) for lot in lots],
            "totals": {"received_cents": received, "quarantined_cents": quarantined,
                       "allocated_cents": allocated, "prepay_balance_cents": prepay,
                       "fund_identity_diff_cents": fund_diff},
            "by_customer": by_customer}


def overview(store: Store, source_id: str, effective: str, known: str) -> dict:
    return _aggregate(store, source_id, effective, known)


def batch_detail(store: Store, batch_id: str) -> dict:
    with store.connect() as connection:
        meta = repository.get_batch(connection, batch_id)
        if meta is None:
            raise Problem("BATCH_NOT_FOUND", "回款批次不存在", 404)
        receipts = [dict(row) for row in connection.execute(
            "SELECT * FROM remit_receipts WHERE batch_id=? ORDER BY line", (batch_id,))]
        allocations = [dict(row) for row in connection.execute(
            "SELECT * FROM clearing_allocations WHERE batch_id=? ORDER BY seq", (batch_id,))]
    return {"batch": meta, "receipts": receipts, "allocations": allocations}


def close_guard(store: Store, source_id: str, effective: str, known: str) -> dict:
    """结账守卫：资金恒等式 + 发票余额与预收对账。不平且无解释 → BLOCKED。"""
    summary = _aggregate(store, source_id, effective, known)
    totals = summary["totals"]
    # 恒等式 1：回款来源 = 隔离 + 核销 + 预收留存
    fund_ok = totals["fund_identity_diff_cents"] == 0
    # 恒等式 2：按客户复核 来源 = 隔离 + 核销 + 预收
    customer_diff = {}
    for customer, row in summary["by_customer"].items():
        customer_diff[customer] = (row["received_cents"] - row["quarantined_cents"]
                                   - row["allocated_cents"] - row["prepay_cents"])
    customer_ok = all(value == 0 for value in customer_diff.values())
    # 恒等式 3：核销不得超过发票可核销上限（持久化分配与版本账孰低上限比对）
    over = _over_cap(store, source_id, effective, known, summary["allocations"])
    balanced = fund_ok and customer_ok and not over
    key = _period_key(effective, known)
    with store.connect() as connection:
        explanation = repository.get_explanation(connection, key)
    if balanced:
        status = "MATCHED"
    elif explanation and explanation["note"].strip():
        status = "EXPLAINED"
    else:
        status = "BLOCKED"
    return {"status": status, "balanced": balanced, "cut": summary["cut"],
            "totals": totals, "customer_diff_cents": customer_diff,
            "over_cap": over, "explanation": explanation,
            "blocking_reason": None if balanced else (
                None if explanation and explanation["note"].strip() else "CLEARING_BLOCKED")}


def _over_cap(store: Store, source_id: str, effective: str, known: str, allocations) -> list[dict]:
    cut = _parse_cut(effective, known)
    data = load_source(store, source_id)
    ledger = Ledger(data, cut.effective, cut.known)
    used = {}
    violations = []
    for row in allocations:
        if row["kind"] not in CLEARING_KINDS:
            continue
        used[row["invoice_id"]] = used.get(row["invoice_id"], 0) + row["amount_cents"]
    for invoice_id, amount in used.items():
        balance = ledger.get(invoice_id)
        cap = balance.cap_cents if balance else 0
        if amount > cap:
            violations.append({"invoice_id": invoice_id, "allocated_cents": amount, "cap_cents": cap,
                               "excess_cents": amount - cap})
    return violations


def record_explanation(store: Store, effective: str, known: str, note: str) -> dict:
    note = (note or "").strip()
    if not note:
        raise Problem("EMPTY_EXPLANATION", "对账解释不能为空")
    key = _period_key(effective, known)
    with store.connect() as connection:
        repository.upsert_explanation(connection, key, note)
        return repository.get_explanation(connection, key)


def sweep_prepayments(store: Store, source_id: str, effective: str, known: str) -> dict:
    """预收冲抵：把挂账预收按 FIFO 冲本户未结票，结果进系统批次，幂等可重跑。"""
    cut = _parse_cut(effective, known)
    data = load_source(store, source_id)
    with store.connect() as connection:
        system_batch = f"~PREPAY-SWEEP|{effective}|{known}"
        # 先剔除本系统批次旧留痕再重算，保证重跑结果一致
        lots = repository.load_prepay_lots(connection, exclude_batch=system_batch)
        ledger = Ledger(data, cut.effective, cut.known)
        caps = {key: value.cap_cents for key, value in ledger._by_invoice.items()}
        allocations = []
        seq = 0
        remaining_lots = [PrepayLot(lot.customer_id, lot.receipt_id, lot.opened, lot.amount_cents)
                          for lot in lots]
        for lot in sorted(remaining_lots, key=lambda item: (item.opened, item.receipt_id)):
            for invoice in sorted((item for item in ledger.invoices_for(lot.customer_id)
                                   if caps.get(item.invoice_id, 0) > 0),
                                  key=lambda item: (item.invoice_id, item.version_event_id)):
                if lot.amount_cents <= 0:
                    break
                take = min(lot.amount_cents, caps[invoice.invoice_id])
                if take <= 0:
                    continue
                lot.amount_cents -= take
                caps[invoice.invoice_id] -= take
                seq += 1
                allocations.append({"seq": seq, "receipt_id": lot.receipt_id,
                                    "customer_id": lot.customer_id, "invoice_id": invoice.invoice_id,
                                    "version_event_id": invoice.version_event_id, "kind": "PREPAY",
                                    "amount_cents": take, "capped_cents": 0,
                                    "basis": f"预收冲抵：动用 {lot.receipt_id} 挂账预收（{lot.opened}）"})
        # 系统批次整体替换，重跑不新增核销
        connection.execute("DELETE FROM clearing_allocations WHERE batch_id=?", (system_batch,))
        connection.execute("""
            INSERT INTO remit_batches(batch_id, source_id, content_digest, policy, received_at)
            VALUES (?, ?, ?, 'SWEEP', ?)
            ON CONFLICT(batch_id) DO UPDATE SET received_at=excluded.received_at
        """, (system_batch, source_id, digest(b"~prepay-sweep"), utc_text(cut.known)))
        for row in allocations:
            connection.execute("""
                INSERT INTO clearing_allocations(batch_id, seq, receipt_id, customer_id, invoice_id,
                    version_event_id, kind, amount_cents, basis, capped_cents)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (system_batch, row["seq"], row["receipt_id"], row["customer_id"], row["invoice_id"],
                  row["version_event_id"], row["kind"], row["amount_cents"], row["basis"], row["capped_cents"]))
    return {"batch_id": system_batch, "allocations": allocations,
            "overview": _aggregate(store, source_id, effective, known)}
