"""银行回款文件解析。

回款 CSV 列顺序可变，编码与变更文件一致（UTF-8 含 BOM，回退 GB18030）。
行级问题隔离为结构化记录，整份文件级别错误（空文件、断引号）直接拒收。
"""
import csv
import io
import re
from dataclasses import dataclass, field

from app.clock import parse_day, parse_known
from app.errors import Problem
from app.modules.m1_intake.contract import ID, amount
from app.modules.m1_intake.reader import decode

COLUMNS = ("batch_id", "receipt_id", "customer_id", "effective", "known", "amount_cny", "refs", "note")
MAX_BATCHES_PER_FILE = 1


@dataclass(frozen=True)
class Receipt:
    line: int
    batch_id: str
    receipt_id: str
    customer_id: str
    effective: str  # ISO 日期，裁决时再转 date，保持纯数据
    known: str      # UTC ISO
    amount_cents: int
    refs: tuple[str, ...]
    note: str


@dataclass
class RemitFile:
    encoding: str
    receipts: list[Receipt] = field(default_factory=list)
    quarantine: list[dict] = field(default_factory=list)
    input_rows: int = 0


def split_refs(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in re.split(r"[;,，；、\s]+", text) if part.strip()))


def parse_row(line: int, row: dict[str, str]) -> Receipt:
    try:
        batch_id, receipt_id, customer = (row[key].strip() for key in ("batch_id", "receipt_id", "customer_id"))
        if not all(ID.fullmatch(value) for value in (batch_id, receipt_id, customer)):
            raise ValueError("INVALID_ID")
        effective = parse_day(row["effective"].strip()).isoformat()
        known_iso = parse_known(row["known"].strip()).isoformat().replace("+00:00", "Z")
        cents = amount(row["amount_cny"])
    except ValueError as exc:
        code = exc.args[0] if exc.args and isinstance(exc.args[0], str) else "INVALID_ROW"
        if code not in {"INVALID_ID", "INVALID_AMOUNT", "AMOUNT_MISSING"}:
            code = "INVALID_ROW"
        raise ValueError(code) from exc
    refs = split_refs(row.get("refs", ""))
    if any(not ID.fullmatch(value) for value in refs):
        raise ValueError("INVALID_ID")
    if len(refs) != len(set(refs)):
        raise ValueError("RECEIPT_DUPLICATE")
    return Receipt(line, batch_id, receipt_id, customer, effective, known_iso, cents,
                   refs, row.get("note", "").strip())


def read(content: bytes) -> RemitFile:
    if not content.strip():
        raise Problem("EMPTY_FILE", "回款文件为空", 422)
    text, encoding = decode(content)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        rows = list(reader)
    except csv.Error as exc:
        raise Problem("CSV_PARSE", f"回款文件无法解析：{exc}", 422) from exc
    if not rows:
        raise Problem("HEADER_ONLY", "回款文件只有表头", 422)
    header = [cell.strip() for cell in rows[0]]
    missing = [name for name in COLUMNS if name not in header]
    if missing:
        raise Problem("HEADER_MISSING", f"回款文件缺少列：{'、'.join(missing)}", 422)
    result = RemitFile(encoding=encoding)
    seen_receipts: set[tuple[str, str]] = set()
    for index, raw in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in raw):
            continue
        result.input_rows += 1
        if len(raw) != len(header):
            result.quarantine.append({"line": index, "code": "ROW_WIDTH", "raw": raw})
            continue
        row = dict(zip(header, (cell.strip() for cell in raw)))
        try:
            receipt = parse_row(index, row)
        except ValueError as exc:
            result.quarantine.append({"line": index, "code": exc.args[0], "raw": row})
            continue
        key = (receipt.batch_id, receipt.receipt_id)
        if key in seen_receipts:
            result.quarantine.append({"line": index, "code": "RECEIPT_DUPLICATE",
                                      "raw": {"receipt_id": receipt.receipt_id, "batch_id": receipt.batch_id}})
            continue
        seen_receipts.add(key)
        result.receipts.append(receipt)
    if not result.receipts:
        raise Problem("NO_VALID_RECEIPT", "回款文件没有可用记录", 422)
    batches = {item.batch_id for item in result.receipts}
    if len(batches) > MAX_BATCHES_PER_FILE:
        raise Problem("BATCH_MIXED", "同一回款文件出现多个批次编号，请按批次拆分", 422)
    return result
