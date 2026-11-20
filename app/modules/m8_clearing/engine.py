"""回款核销裁决引擎（纯函数）。

规则：
1. 回款只在本客户内分配；附言点名的票据只冲该票，不能改冲别的票。
2. 附言引用无法解析（未知/客户不符/当日无有效版本）→ 整笔回款隔离，不产生任何分配。
3. 点名票据按 min(旧版当日面额, 新版截面面额)−已冲减 的孰低上限核销，超额退回预收并留痕。
4. 每笔回款开始先按 FIFO 动用该客户存量预收冲未结票（PREPAY 留痕追溯到预收原始回款）。
5. 本笔未点名余款：SWEEP 自动冲本户其他未结票（确定性顺序）；HOLD 全部留作预收。
   两种判断会算出不同的客户发票欠款与预收结构，结果显式标注采用的判断。
6. 冲不完的留作客户预收；工作台提示超龄预收，并提供独立的预收冲抵操作，避免长期挂账。
7. 同一批次重算结果确定；状态由 service 整体替换，天然幂等。
"""
from dataclasses import dataclass, field
from datetime import date

from app.reasons import reason
from app.modules.m8_clearing.balances import InvoiceBalance, Ledger
from app.modules.m8_clearing.reader import Receipt

POLICIES = ("SWEEP", "HOLD")
SWEEP, HOLD = POLICIES
PREPAY_AGE_DAYS = 90  # 预收挂账超过该天数在工作台提示
CLEARING_KINDS = ("NAMED", "SWEEP", "PREPAY")


@dataclass
class Allocation:
    seq: int
    receipt_id: str       # PREPAY 留痕填预收原始回款编号，其余填本笔回款编号
    customer_id: str
    invoice_id: str
    version_event_id: str
    kind: str             # NAMED 点名 / SWEEP 自动 / PREPAY 预收冲销
    amount_cents: int
    basis: str            # 判断依据说明
    capped_cents: int = 0  # 点名票因孰低上限被截留、转预收的金额

    def public(self):
        return {"seq": self.seq, "receipt_id": self.receipt_id, "customer_id": self.customer_id,
                "invoice_id": self.invoice_id, "version_event_id": self.version_event_id, "kind": self.kind,
                "amount_cents": self.amount_cents, "basis": self.basis, "capped_cents": self.capped_cents}


@dataclass
class ClearedReceipt:
    receipt: Receipt
    status: str          # CLEARED 已分配 / QUARANTINED 整笔隔离
    quarantine_code: str | None = None
    allocations: list[Allocation] = field(default_factory=list)
    prepay_change_cents: int = 0  # 本笔新增的预收（正数）；动用存量预收不在此列

    def public(self):
        data = {"receipt_id": self.receipt.receipt_id, "batch_id": self.receipt.batch_id,
                "customer_id": self.receipt.customer_id, "effective": self.receipt.effective,
                "known": self.receipt.known, "amount_cents": self.receipt.amount_cents,
                "refs": list(self.receipt.refs), "note": self.receipt.note, "line": self.receipt.line,
                "status": self.status, "prepay_change_cents": self.prepay_change_cents,
                "allocations": [item.public() for item in self.allocations]}
        if self.quarantine_code:
            data.update(reason(self.quarantine_code))
        return data


@dataclass
class PrepayLot:
    customer_id: str
    receipt_id: str       # 预收原始回款编号
    opened: str           # ISO 生效日
    amount_cents: int

    def public(self, on_day: date):
        age = (on_day - date.fromisoformat(self.opened)).days
        return {"customer_id": self.customer_id, "receipt_id": self.receipt_id, "opened": self.opened,
                "amount_cents": self.amount_cents, "age_days": age, "stale": age > PREPAY_AGE_DAYS}


def _open_invoices(ledger: Ledger, customer_id: str, caps: dict[str, int],
                   exclude: set[str]) -> list[InvoiceBalance]:
    return sorted((item for item in ledger.invoices_for(customer_id)
                   if item.invoice_id not in exclude and caps[item.invoice_id] > 0),
                  key=lambda item: (item.invoice_id, item.version_event_id))


class _Engine:
    def __init__(self, data, receipts, policy, existing_prepays, effective, known):
        self.ledger = Ledger(data, effective, known)
        self.receipts = receipts
        self.policy = policy
        self.caps = {key: value.cap_cents for key, value in self.ledger._by_invoice.items()}
        self.lots: list[PrepayLot] = [PrepayLot(l.customer_id, l.receipt_id, l.opened, l.amount_cents)
                                      for l in existing_prepays]
        self.allocations: list[Allocation] = []
        self.seq = 0
        self.cleared: list[ClearedReceipt] = []
        self.quarantine: list[dict] = []
        self.effective = effective
        self.prepay_added = 0
        self.prepay_used = 0

    def add_allocation(self, **kwargs):
        self.seq += 1
        row = Allocation(seq=self.seq, **kwargs)
        self.allocations.append(row)
        return row

    def validate_refs(self, receipt: Receipt):
        targets, seen = [], set()
        day = date.fromisoformat(receipt.effective)
        for ref in receipt.refs:
            balance, code = self.ledger.resolve_ref(ref, receipt.customer_id, day)
            if code:
                return [], code
            if balance.invoice_id not in seen:
                seen.add(balance.invoice_id)
                targets.append(balance)
        return targets, None

    def open_prepay(self, customer_id: str, amount: int, receipt: Receipt):
        if amount <= 0:
            return
        self.lots.append(PrepayLot(customer_id, receipt.receipt_id, receipt.effective, amount))
        self.prepay_added += amount

    def apply_existing_prepay(self, customer_id: str) -> list[Allocation]:
        """回款开始时动用存量预收（FIFO）冲本户未结票，留痕追溯到预收原始回款。

        预收是客户的钱，可冲本户任何票；受“不能改冲”约束的只是本笔回款被点名的资金。
        """
        rows = []
        for lot in sorted((lot for lot in self.lots if lot.customer_id == customer_id and lot.amount_cents),
                          key=lambda lot: (lot.opened, lot.receipt_id)):
            for invoice in _open_invoices(self.ledger, customer_id, self.caps, set()):
                if lot.amount_cents <= 0:
                    break
                take = min(lot.amount_cents, self.caps[invoice.invoice_id])
                if take <= 0:
                    continue
                lot.amount_cents -= take
                self.caps[invoice.invoice_id] -= take
                self.prepay_used += take
                rows.append(self.add_allocation(
                    receipt_id=lot.receipt_id, customer_id=customer_id,
                    invoice_id=invoice.invoice_id, version_event_id=invoice.version_event_id,
                    kind="PREPAY", amount_cents=take,
                    basis=f"动用 {lot.receipt_id} 留存预收（{lot.opened} 入账）冲本户未结票"))
        return rows

    def process(self):
        ordered = sorted(self.receipts, key=lambda r: (r.effective, r.known, r.receipt_id, r.line))
        for receipt in ordered:
            targets, code = self.validate_refs(receipt)
            if code:
                self.quarantine.append({"receipt_id": receipt.receipt_id, "line": receipt.line,
                                        "customer_id": receipt.customer_id, "amount_cents": receipt.amount_cents,
                                        **reason(code)})
                self.cleared.append(ClearedReceipt(receipt, "QUARANTINED", code))
                continue
            self.clear(receipt, targets)
        self.lots = [lot for lot in self.lots if lot.amount_cents > 0]

    def clear(self, receipt: Receipt, targets: list[InvoiceBalance]):
        record = ClearedReceipt(receipt, "CLEARED")
        named = {item.invoice_id for item in targets}
        # 0) 先动用存量预收（FIFO）冲本户未结票
        record.allocations.extend(self.apply_existing_prepay(receipt.customer_id))
        remaining = receipt.amount_cents
        # 1) 点名票逐票按孰低上限核销；点名金额不能改冲别的票，超额退回预收并留痕
        for invoice in targets:
            if remaining <= 0:
                break
            cap = self.caps[invoice.invoice_id]
            take = min(remaining, cap)
            overflow = remaining - take
            if take:
                self.caps[invoice.invoice_id] -= take
                record.allocations.append(self.add_allocation(
                    receipt_id=receipt.receipt_id, customer_id=receipt.customer_id,
                    invoice_id=invoice.invoice_id, version_event_id=invoice.version_event_id,
                    kind="NAMED", amount_cents=take, capped_cents=overflow,
                    basis=f"附言点名票；按新旧余额孰低上限 {cap} 核销，超额 {overflow} 退回预收"))
            if overflow:
                self.open_prepay(receipt.customer_id, overflow, receipt)
                record.prepay_change_cents += overflow
            remaining -= take + overflow
        # 2) 本笔未点名余款：SWEEP 冲本户其他未结票；HOLD 全部留预收
        if remaining > 0 and self.policy == SWEEP:
            for invoice in _open_invoices(self.ledger, receipt.customer_id, self.caps, named):
                if remaining <= 0:
                    break
                take = min(remaining, self.caps[invoice.invoice_id])
                self.caps[invoice.invoice_id] -= take
                record.allocations.append(self.add_allocation(
                    receipt_id=receipt.receipt_id, customer_id=receipt.customer_id,
                    invoice_id=invoice.invoice_id, version_event_id=invoice.version_event_id,
                    kind="SWEEP", amount_cents=take,
                    basis="附言未点名；SWEEP 策略按发票号顺序自动冲本户未结票"))
                remaining -= take
        # 3) 仍冲不完 → 留作预收
        if remaining > 0:
            self.open_prepay(receipt.customer_id, remaining, receipt)
            record.prepay_change_cents += remaining
        self.cleared.append(record)

    def totals_block(self):
        received = sum(r.amount_cents for r in self.receipts)
        quarantined = sum(r.receipt.amount_cents for r in self.cleared if r.status == "QUARANTINED")
        allocated = sum(a.amount_cents for a in self.allocations if a.kind in CLEARING_KINDS)
        prepay = sum(lot.amount_cents for lot in self.lots)
        return {"received_cents": received, "quarantined_cents": quarantined,
                "allocated_cents": allocated, "prepay_balance_cents": prepay,
                "prepay_added_cents": self.prepay_added, "prepay_used_cents": self.prepay_used,
                "fund_identity_diff_cents": received - quarantined - allocated - prepay}


def run(data, receipts, policy: str, existing_prepays: list[PrepayLot], effective: date, known) -> _Engine:
    if policy not in POLICIES:
        raise ValueError(f"未知裁决策略 {policy}")
    engine = _Engine(data, receipts, policy, existing_prepays, effective, known)
    engine.process()
    return engine


def customer_debt(engine: _Engine) -> dict:
    debt = {}
    customers = {r.customer_id for r in engine.receipts} | {lot.customer_id for lot in engine.lots}
    for customer in sorted(customers):
        face = sum(item.open_cents for item in engine.ledger.invoices_for(customer))
        cleared = sum(a.amount_cents for a in engine.allocations
                      if a.customer_id == customer and a.kind in CLEARING_KINDS)
        prepay = sum(lot.amount_cents for lot in engine.lots if lot.customer_id == customer)
        debt[customer] = {"open_ledger_cents": face - cleared, "prepay_cents": prepay,
                          "net_receivable_cents": face - cleared - prepay}
    return debt


def compare_policies(data, receipts, existing_prepays, effective, known, chosen: str) -> dict:
    """两种判断分别重算，给出客户欠款差异，让结果能看出采用的判断。"""
    outcomes = {}
    for policy in POLICIES:
        engine = run(data, receipts, policy, existing_prepays, effective, known)
        outcomes[policy] = customer_debt(engine)
    return {"chosen": chosen, "alternative": HOLD if chosen == SWEEP else SWEEP,
            "by_customer": outcomes}


def assemble(data, receipts, policy, existing_prepays, effective, known) -> dict:
    engine = run(data, receipts, policy, existing_prepays, effective, known)
    return {"policy": policy, "totals": engine.totals_block(),
            "receipts": [item.public() for item in engine.cleared],
            "allocations": [item.public() for item in engine.allocations],
            "prepayments": [lot.public(effective) for lot in engine.lots],
            "quarantine": engine.quarantine,
            "customer_debt": customer_debt(engine),
            "policy_comparison": compare_policies(data, receipts, existing_prepays, effective, known, policy)}
