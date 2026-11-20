"""发票可核销余额。

复用双时态版本账与冲减规则：
- 回款当日面额：回款业务日生效版本的面额
- 截面面额：知悉截止下当前采信版本的面额
- 可冲减上限＝min(当日面额, 截面面额) − 已采信冲减（与 M3 movements 同口径）
修订前的原票通过版本账的 accepted_versions / replaced_by 链解析。
"""
from dataclasses import dataclass
from datetime import date, datetime

from app.modules.m2_versions.rules import build
from app.modules.m3_snapshot.movements import evaluate


@dataclass
class InvoiceBalance:
    invoice_id: str
    customer_id: str
    version_event_id: str          # 截面采信版本
    face_cut_cents: int            # 截面面额（新版）
    face_event_cents: int          # 回款当日面额（旧版口径）
    spent_cents: int               # 已采信收款/冲销
    cap_cents: int                 # 孰低后的可冲减上限
    open_cents: int                # 截面应收余额


class Ledger:
    def __init__(self, data, effective: date, known: datetime):
        self.effective = effective
        self.known = known
        self.book = build(data.events, known)
        selected = {}
        for invoice in sorted(self.book.active_by_invoice):
            version = self.book.active(invoice, effective)
            if version is not None:
                selected[invoice] = version
        self.applied, self.pending, self.rejects, self.inactive = evaluate(
            self.book, effective, selected)
        self._selected = selected
        self._by_invoice: dict[str, InvoiceBalance] = {}
        for invoice, version in selected.items():
            spent = sum(row["event"]["amount_cents"] for row in self.applied.get(invoice, ()))
            at_event = self.book.active(invoice, effective)
            face_event = at_event.cents if at_event else version.cents
            cap = max(0, min(face_event, version.cents) - spent)
            self._by_invoice[invoice] = InvoiceBalance(
                invoice, version.customer_id, version.event_id, version.cents,
                face_event, spent, cap, max(0, version.cents - spent))

    def invoices_for(self, customer_id: str) -> list[InvoiceBalance]:
        return [item for item in self._by_invoice.values() if item.customer_id == customer_id]

    def resolve_ref(self, ref: str, customer_id: str, on_day: date) -> tuple[InvoiceBalance | None, str | None]:
        """解析附言引用的版本事件，返回（余额, 失败原因码）。"""
        target = self.book.accepted_versions.get(ref)
        if target is None:
            return None, "REF_UNKNOWN" if ref in self.book.visible else "REF_UNKNOWN"
        if target.customer_id != customer_id:
            return None, "REF_SCOPE"
        balance = self._by_invoice.get(target.invoice_id)
        if balance is None:
            return None, "REF_SCOPE"
        at_event = self.book.active(target.invoice_id, on_day)
        if at_event is None:
            return None, "REF_NO_VERSION"
        return balance, None

    def get(self, invoice_id: str) -> InvoiceBalance | None:
        return self._by_invoice.get(invoice_id)

    def all_open(self) -> list[InvoiceBalance]:
        return [item for item in self._by_invoice.values() if item.open_cents > 0]
