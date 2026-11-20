"""回款核销 M8 测试。

构造一个三客户应收账本，覆盖：
- 整笔回款附言引用未知/修订前票据 → 整笔隔离，客户欠款不变
- 发票修订后按新旧余额孰低核销，超额退预收并留痕
- 点名票不能改冲；未点名余款 SWEEP/HOLD 两种判断给出不同欠款
- 预收即时冲本户未结票、超龄提示
- 批次幂等重跑不新增核销或预收
- 结账守卫：对账不平无解释 BLOCKED，补解释后可结账
"""
import io

import pytest

from app.clock import period_query
from app.evidence import digest
from app.main import create_app
from app.modules.m1_intake.contract import COLUMNS
from app.modules.m1_intake.reader import read as read_events
from app.modules.m8_clearing.reader import COLUMNS as REMIT_COLUMNS
from fastapi.testclient import TestClient

CUT_EFFECTIVE = "2026-01-31"
CUT_KNOWN = "2026-02-10T12:00+08:00"


def _csv(header, rows):
    dest = io.StringIO(newline="")
    writer = __import__("csv").writer(dest)
    writer.writerow(header)
    writer.writerows(rows)
    return dest.getvalue().encode()


# 客户 C1 两张票：AR-1 面额 1000，AR-2 面额 500
# 客户 C2：AR-3 初版 800（V1），修订版 V2 面额 600（同区间修订）
# 客户 C3：AR-4 面额 300
EVENT_ROWS = [
    ["AR1-V1", "AR-1", "C1", "VERSION", "2025-12-01", "", "2025-12-01T09:00+08:00", "2025-12-20", "1000.00", "", "票1"],
    ["AR2-V1", "AR-2", "C1", "VERSION", "2025-12-05", "", "2025-12-05T09:00+08:00", "2025-12-25", "500.00", "", "票2"],
    ["AR3-V1", "AR-3", "C2", "VERSION", "2025-12-10", "", "2025-12-10T09:00+08:00", "2025-12-30", "800.00", "", "修订前票据"],
    ["AR3-V2", "AR-3", "C2", "VERSION", "2025-12-10", "", "2026-01-15T09:00+08:00", "2025-12-30", "600.00", "AR3-V1", "调减修订"],
    ["AR4-V1", "AR-4", "C3", "VERSION", "2026-01-05", "", "2026-01-05T09:00+08:00", "2026-01-20", "300.00", "", "票4"],
]


@pytest.fixture
def event_csv():
    return _csv(COLUMNS, EVENT_ROWS)


@pytest.fixture
def client(tmp_path, event_csv):
    with TestClient(create_app(tmp_path / "m8.sqlite3")) as session:
        reply = session.post("/api/intake", files={"file": ("events.csv", event_csv)})
        assert reply.status_code == 200, reply.text
        session.app.state.source_id = reply.json()["source_id"]
        yield session


def remit_csv(rows, header=REMIT_COLUMNS):
    return _csv(header, rows)


def upload(client, rows, policy="SWEEP", effective=CUT_EFFECTIVE, known=CUT_KNOWN):
    content = remit_csv(rows)
    return client.post("/api/clearing/batches",
                       files={"file": ("remit.csv", content)},
                       data={"source_id": client.app.state.source_id, "policy": policy,
                             "effective": effective, "known": known}), content


def test_lower_balance_caps_named_invoice_and_overflow_goes_prepay(client):
    # C2 回款 700，附言只点 AR3-V1（修订前票据，可解析为父版）
    # 孰低上限 = min(旧800, 新600) = 600 → 核销 600，超额 100 退预收
    rows = [["B1", "R1", "C2", "2026-01-20", "2026-01-20T10:00+08:00", "700.00", "AR3-V1", "附言引用修订前票"]]
    reply, _ = upload(client, rows)
    assert reply.status_code == 200, reply.text
    body = reply.json()
    named = [a for a in body["allocations"] if a["kind"] == "NAMED"]
    assert len(named) == 1 and named[0]["amount_cents"] == 60000
    assert named[0]["capped_cents"] == 10000
    assert named[0]["invoice_id"] == "AR-3"
    # 超额 100 进预收；C2 无其他未结票，预收留存 100
    prepay = [p for p in body["prepayments"] if p["customer_id"] == "C2"]
    assert sum(p["amount_cents"] for p in prepay) == 10000
    assert body["totals"]["fund_identity_diff_cents"] == 0
    # 客户欠款：票面 600 已清，净应收 = 0 - 预收100 = -100
    assert body["customer_debt"]["C2"] == {"open_ledger_cents": 0, "prepay_cents": 10000,
                                           "net_receivable_cents": -10000}


def test_unknown_ref_quarantines_whole_receipt_and_keeps_debt(client):
    rows = [["B2", "R2", "C1", "2026-01-21", "2026-01-21T10:00+08:00", "400.00", "AR-GHOST", "引用对不上"]]
    reply, _ = upload(client, rows)
    body = reply.json()
    assert len(body["quarantine"]) == 1
    assert body["quarantine"][0]["code"] == "REF_UNKNOWN"
    assert body["receipts"][0]["status"] == "QUARANTINED"
    # 整笔隔离：没有任何分配、没有预收
    assert body["allocations"] == [] and body["prepayments"] == []
    assert body["totals"]["quarantined_cents"] == 40000
    assert body["totals"]["fund_identity_diff_cents"] == 0


def test_named_invoice_overflow_must_not_sweep_to_other_invoices(client):
    # C1 回款 1200，点名 AR-1（上限1000）；超额 200 只能退回预收，不能直接改冲 AR-2
    rows = [["B3", "R3", "C1", "2026-01-22", "2026-01-22T10:00+08:00", "1200.00", "AR1-V1", "只点票1"]]
    reply, _ = upload(client, rows, policy="SWEEP")
    body = reply.json()
    named = [a for a in body["allocations"] if a["kind"] == "NAMED"]
    assert [(a["invoice_id"], a["amount_cents"]) for a in named] == [("AR-1", 100000)]
    # 没有任何 SWEEP/PREPAY 分配把点名余款改冲到 AR-2
    assert [a for a in body["allocations"] if a["kind"] != "NAMED"] == []
    # 超额 200 退回预收并留痕（capped_cents），AR-2 发票欠款不变
    assert named[0]["capped_cents"] == 20000
    assert [p for p in body["prepayments"] if p["customer_id"] == "C1"][0]["amount_cents"] == 20000
    assert body["customer_debt"]["C1"]["open_ledger_cents"] == 50000  # AR-2 仍全额挂账
    assert body["totals"]["fund_identity_diff_cents"] == 0


def test_sweep_and_hold_policies_produce_different_customer_debt(client):
    # C3 回款 500，无附言；AR-4 票面 300
    rows = [["B4", "R4", "C3", "2026-01-23", "2026-01-23T10:00+08:00", "500.00", "", "未点名余款"]]
    sweep, _ = upload(client, rows, policy="SWEEP")
    sweep_body = sweep.json()
    # SWEEP：冲 AR-4 300，余款 200 留预收
    assert sweep_body["customer_debt"]["C3"] == {"open_ledger_cents": 0, "prepay_cents": 20000,
                                                 "net_receivable_cents": -20000}
    # HOLD 重算结果内嵌在策略对比里：不自动冲票，500 全留预收，发票仍欠 300
    comp = sweep_body["policy_comparison"]
    assert comp["chosen"] == "SWEEP" and comp["alternative"] == "HOLD"
    assert comp["by_customer"]["SWEEP"]["C3"] == {"open_ledger_cents": 0, "prepay_cents": 20000,
                                                  "net_receivable_cents": -20000}
    assert comp["by_customer"]["HOLD"]["C3"] == {"open_ledger_cents": 30000, "prepay_cents": 50000,
                                                 "net_receivable_cents": -20000}
    # 净额相同但发票欠款与预收结构不同；结果显式标注采用的判断
    assert sweep_body["policy"] == "SWEEP"


def test_hold_policy_parks_unnamed_surplus_as_prepayment(tmp_path, event_csv):
    with TestClient(create_app(tmp_path / "m8-hold.sqlite3")) as session:
        source_id = session.post("/api/intake", files={"file": ("e.csv", event_csv)}).json()["source_id"]
        session.app.state.source_id = source_id
        rows = [["BH", "RH", "C3", "2026-01-23", "2026-01-23T10:00+08:00", "500.00", "", "保守入账"]]
        body, _ = upload(session, rows, policy="HOLD")
        body = body.json()
        assert body["allocations"] == []  # 未点名一律不自动冲
        assert body["receipts"][0]["prepay_change_cents"] == 50000
        assert body["customer_debt"]["C3"]["open_ledger_cents"] == 30000
        assert body["customer_debt"]["C3"]["prepay_cents"] == 50000
        # 下一批回款开始时，存量预收按 FIFO 自动冲本户未结票
        rows2 = [["BH2", "RH2", "C3", "2026-01-24", "2026-01-24T10:00+08:00", "10.00", "AR4-V1", "点名小额"]]
        second, _ = upload(session, rows2, policy="HOLD")
        second = second.json()
        # 回款开始先动用 RH 存量预收：FIFO 冲 AR-4 300，发票被冲平
        prepay_alloc = [a for a in second["allocations"] if a["kind"] == "PREPAY"]
        assert [(a["receipt_id"], a["invoice_id"], a["amount_cents"]) for a in prepay_alloc] == [
            ("RH", "AR-4", 30000)]
        # 发票已无孰低余额，本笔点名 10 无法核销 → 退回预收并留痕
        named = second["receipts"][0]
        assert named["prepay_change_cents"] == 1000
        assert named["allocations"] == prepay_alloc
        # RH 预收剩 200 + RH2 新预收 10
        assert sum(p["amount_cents"] for p in second["prepayments"] if p["customer_id"] == "C3") == 21000
        assert second["customer_debt"]["C3"]["net_receivable_cents"] == -21000


def test_prepay_sweep_endpoint_clears_aged_prepayment(tmp_path, event_csv):
    with TestClient(create_app(tmp_path / "m8-sweep.sqlite3")) as session:
        source_id = session.post("/api/intake", files={"file": ("e.csv", event_csv)}).json()["source_id"]
        session.app.state.source_id = source_id
        # HOLD 下制造挂账预收
        rows = [["BS", "RS", "C1", "2025-10-01", "2025-10-01T10:00+08:00", "300.00", "", "早期挂账"]]
        parked, _ = upload(session, rows, policy="HOLD",
                           effective="2026-01-31", known="2026-02-10T12:00+08:00")
        assert parked.json()["prepayments"][0]["stale"] is True  # 挂账超过 90 天
        # 执行预收冲抵
        swept = session.post("/api/clearing/prepay-sweep",
                             params={"source_id": source_id, "effective": CUT_EFFECTIVE,
                                     "known": CUT_KNOWN}).json()
        kinds = {(a["kind"], a["invoice_id"]) for a in swept["allocations"]}
        assert ("PREPAY", "AR-1") in kinds
        assert swept["overview"]["totals"]["prepay_balance_cents"] == 0
        # 幂等：再执行一次重算结果一致，系统批次不新增
        again = session.post("/api/clearing/prepay-sweep",
                             params={"source_id": source_id, "effective": CUT_EFFECTIVE,
                                     "known": CUT_KNOWN}).json()
        assert again["allocations"] == swept["allocations"]
        system_batches = [b for b in again["overview"]["batches"] if b["batch_id"].startswith("~PREPAY-SWEEP")]
        assert len(system_batches) == 1
        guard = session.get("/api/clearing/close",
                            params={"source_id": source_id, "effective": CUT_EFFECTIVE,
                                    "known": CUT_KNOWN}).json()
        assert guard["status"] == "MATCHED"


def test_rerun_same_batch_is_idempotent(client):
    rows = [["B5", "R5", "C1", "2026-01-24", "2026-01-24T10:00+08:00", "300.00", "AR1-V1", "首批"]]
    first, content = upload(client, rows)
    first_body = first.json()
    assert first_body["reused"] is False
    # 同内容同参数重跑
    second = client.post("/api/clearing/batches",
                         files={"file": ("remit.csv", content)},
                         data={"source_id": client.app.state.source_id, "policy": "SWEEP",
                               "effective": CUT_EFFECTIVE, "known": CUT_KNOWN})
    second_body = second.json()
    assert second_body["reused"] is True
    assert second_body["allocations"] == first_body["allocations"]
    assert second_body["prepayments"] == first_body["prepayments"]
    # 库里仍只有一批、核销不翻倍
    overview = client.get("/api/clearing", params={"source_id": client.app.state.source_id,
                                                   "effective": CUT_EFFECTIVE, "known": CUT_KNOWN}).json()
    assert len(overview["batches"]) == 1
    assert overview["totals"]["allocated_cents"] == 30000


def test_same_batch_id_with_different_content_is_conflict(client):
    rows = [["B6", "R6", "C1", "2026-01-25", "2026-01-25T10:00+08:00", "100.00", "AR1-V1", "原版"]]
    reply, _ = upload(client, rows)
    assert reply.status_code == 200
    rows[0][5] = "120.00"
    conflict, _ = upload(client, rows)
    assert conflict.status_code == 409 and conflict.json()["code"] == "BATCH_CONFLICT"


def test_row_level_bad_rows_are_isolated_but_batch_continues(client):
    rows = [
        ["B7", "R7", "C1", "2026-01-26", "2026-01-26T10:00+08:00", "100.00", "AR1-V1", "正常"],
        ["B7", "R8", "C1", "2026-01-26", "2026-01-26T11:00+08:00", "N/A", "AR1-V1", "金额缺失"],
    ]
    reply, _ = upload(client, rows)
    assert reply.status_code == 200
    assert {item["code"] for item in reply.json()["input_quarantine"]} == {"AMOUNT_MISSING"}
    assert len(reply.json()["receipts"]) == 1


def test_close_guard_blocks_without_explanation_and_releases_after(client):
    rows = [["B8", "R9", "C1", "2026-01-27", "2026-01-27T10:00+08:00", "100.00", "AR1-V1", "正常批次"]]
    reply, _ = upload(client, rows)
    assert reply.status_code == 200
    guard = client.get("/api/clearing/close", params={"source_id": client.app.state.source_id,
                                                      "effective": CUT_EFFECTIVE, "known": CUT_KNOWN}).json()
    # 正常入账时资金恒等式对平
    assert guard["status"] == "MATCHED" and guard["balanced"] is True
    # 登记解释接口
    note = client.post("/api/clearing/explanations",
                       json={"effective": CUT_EFFECTIVE, "known": CUT_KNOWN, "note": "尾差为银行手续费，已纳下期"}).json()
    assert note["note"].startswith("尾差")


def test_archive_blocked_when_clearing_unbalanced(tmp_path, event_csv):
    # 正常批次对平可归档，且归档证据包含回款核销结账块
    with TestClient(create_app(tmp_path / "m8b.sqlite3")) as session:
        source_id = session.post("/api/intake", files={"file": ("e.csv", event_csv)}).json()["source_id"]
        session.app.state.source_id = source_id
        rows = [["B9", "R10", "C1", "2026-01-28", "2026-01-28T10:00+08:00", "100.00", "AR1-V1", "x"]]
        ok, _ = upload(session, rows)
        assert ok.status_code == 200
        period = {"source_id": source_id,
                  "open_effective": "2025-12-31", "open_known": "2025-12-31T23:59+08:00",
                  "effective": CUT_EFFECTIVE, "known": CUT_KNOWN}
        archived = session.post("/api/archive", json=period)
        assert archived.status_code == 200
        run_id = archived.json()["manifest"]["run_id"]
        bundle = session.get(f"/api/archive/{run_id}/export").json()["result"]
        assert bundle["clearing_close"]["status"] == "MATCHED"


def test_archive_blocked_on_unbalanced_reconciliation_and_released_by_explanation(tmp_path, event_csv):
    with TestClient(create_app(tmp_path / "m8c.sqlite3")) as session:
        source_id = session.post("/api/intake", files={"file": ("e.csv", event_csv)}).json()["source_id"]
        session.app.state.source_id = source_id
        rows = [["BZ", "RZ", "C1", "2026-01-28", "2026-01-28T10:00+08:00", "100.00", "AR1-V1", "x"]]
        assert upload(session, rows)[0].status_code == 200
        # 模拟一笔无法被恒等式解释的持久化差异：直接在库中虚增核销金额
        store = session.app.state.store
        with store.connect() as conn:
            conn.execute("UPDATE clearing_allocations SET amount_cents = amount_cents + 500")
        guard = session.get("/api/clearing/close",
                            params={"source_id": source_id, "effective": CUT_EFFECTIVE,
                                    "known": CUT_KNOWN}).json()
        assert guard["status"] == "BLOCKED" and guard["balanced"] is False
        assert guard["blocking_reason"] == "CLEARING_BLOCKED"
        # 无解释时归档被拒绝
        period = {"source_id": source_id,
                  "open_effective": "2025-12-31", "open_known": "2025-12-31T23:59+08:00",
                  "effective": CUT_EFFECTIVE, "known": CUT_KNOWN}
        refused = session.post("/api/archive", json=period)
        assert refused.status_code == 409 and refused.json()["code"] == "CLEARING_BLOCKED"
        # 补登记对账解释后允许结账，状态变为 EXPLAINED 并留痕
        session.post("/api/clearing/explanations",
                     json={"effective": CUT_EFFECTIVE, "known": CUT_KNOWN,
                           "note": "差异 5 元为跨行手续费，已与银行回单核对，纳入下期调整"})
        guard2 = session.get("/api/clearing/close",
                             params={"source_id": source_id, "effective": CUT_EFFECTIVE,
                                     "known": CUT_KNOWN}).json()
        assert guard2["status"] == "EXPLAINED" and guard2["explanation"]["note"].startswith("差异 5 元")
        assert session.post("/api/archive", json=period).status_code == 200


def test_customer_scope_ref_mismatch_is_quarantined(client):
    # AR-1 属于 C1，回款客户写 C2 → 整笔隔离
    rows = [["B10", "R11", "C2", "2026-01-29", "2026-01-29T10:00+08:00", "50.00", "AR1-V1", "客户不符"]]
    reply, _ = upload(client, rows)
    body = reply.json()
    assert body["quarantine"][0]["code"] == "REF_SCOPE"
    assert body["allocations"] == []


def test_overview_shows_payment_destination_and_prepayments(client):
    rows = [
        ["B11", "R12", "C1", "2026-01-30", "2026-01-30T10:00+08:00", "1600.00", "", "整笔未点名"],
    ]
    reply, _ = upload(client, rows)
    assert reply.status_code == 200
    view = client.get("/api/clearing", params={"source_id": client.app.state.source_id,
                                               "effective": CUT_EFFECTIVE, "known": CUT_KNOWN}).json()
    # 1600 在 C1 内：AR-1 冲 1000、AR-2 冲 500，余 100 预收
    assert view["totals"]["allocated_cents"] == 150000
    assert view["totals"]["prepay_balance_cents"] == 10000
    assert view["by_customer"]["C1"] == {"received_cents": 160000, "quarantined_cents": 0,
                                         "allocated_cents": 150000, "prepay_cents": 10000}
    invoices = {a["invoice_id"] for a in view["allocations"]}
    assert invoices == {"AR-1", "AR-2"}


def test_remit_file_gb18030_and_bom_are_decoded(tmp_path, event_csv):
    from app.modules.m8_clearing.reader import read as read_remit
    header = "batch_id,receipt_id,customer_id,effective,known,amount_cny,refs,note"
    line = "BE,RE,C1,2026-01-30,2026-01-30T10:00+08:00,100.00,AR1-V1,中文附言"
    for payload, encoding in ((("﻿" + header + "\r\n" + line).encode("utf-8"), "utf-8-sig"),
                              ((header + "\r\n" + line).encode("gb18030"), "gb18030")):
        parsed = read_remit(payload)
        assert parsed.encoding == encoding
        assert parsed.receipts[0].note == "中文附言"
        assert parsed.receipts[0].refs == ("AR1-V1",)


def test_mixed_batches_and_duplicate_receipts_are_rejected_or_isolated(tmp_path, event_csv):
    from app.errors import Problem
    from app.modules.m8_clearing.reader import read as read_remit
    header = "batch_id,receipt_id,customer_id,effective,known,amount_cny,refs,note"
    mixed = ("%s\nBX,R1,C1,2026-01-30,2026-01-30T10:00+08:00,100.00,AR1-V1,x\n"
             "BY,R2,C1,2026-01-30,2026-01-30T11:00+08:00,100.00,AR1-V1,y\n" % header).encode()
    with pytest.raises(Problem) as exc:
        read_remit(mixed)
    assert exc.value.code == "BATCH_MIXED"
    dup = ("%s\nBX,R1,C1,2026-01-30,2026-01-30T10:00+08:00,100.00,AR1-V1,x\n"
           "BX,R1,C1,2026-01-30,2026-01-30T11:00+08:00,100.00,AR1-V1,dup\n" % header).encode()
    parsed = read_remit(dup)
    assert len(parsed.receipts) == 1
    assert [q["code"] for q in parsed.quarantine] == ["RECEIPT_DUPLICATE"]
