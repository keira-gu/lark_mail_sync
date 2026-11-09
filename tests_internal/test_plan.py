# -*- coding: utf-8 -*-
"""gofreight_ap 计划层离线测试：过滤矩阵 / 日期换算 / 供应商映射 / 防重状态。

注意：本目录不放假 conftest.py——与 tests/conftest.py 同名无包名会互相顶掉
（pytest 无 __init__.py 时按 basename 解析）。路径自举放在本文件内。"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gofreight_ap import ApRow, ApState, build_plan, ms_to_date, parse_vendor_map, rows_from_bitable  # noqa: E402
from lark_mail_sync.invoice import date_to_ms  # noqa: E402

VMAP = parse_vendor_map(json.dumps(
    {"ABC Logistics": {"search": "ABC", "code": "VN-000001"}}, ensure_ascii=False))


def row(**kw):
    base = dict(record_id="rec1", invoice_no="1001", tk="TK-20990011", vendor="ABC Logistics",
                currency="CAD", amount=15.42, invoice_date=date(2026, 10, 15),
                due_date=date(2026, 11, 14), error="", subject="Fw: Invoice")
    base.update(kw)
    return ApRow(**base)


# ---------- ms_to_date：与公共包写入端 date_to_ms 互逆 ----------

def test_ms_to_date_roundtrip():
    ms = date_to_ms("2026-10-15")
    assert ms is not None
    assert ms_to_date(ms) == date(2026, 10, 15)   # UTC 零点写入 → UTC 零点读回，不跨日


def test_ms_to_date_bad():
    assert ms_to_date(None) is None
    assert ms_to_date("") is None
    assert ms_to_date("abc") is None


# ---------- 供应商映射 ----------

def test_vendor_map_ok():
    m = parse_vendor_map('{"ABC Logistics": {"search": "ABC", "code": "VN-000001"}}')
    assert m["ABC Logistics"] == {"search": "ABC", "code": "VN-000001"}


def test_vendor_map_empty_and_blank():
    assert parse_vendor_map("") == {}
    assert parse_vendor_map("   ") == {}


def test_vendor_map_space_in_search_rejected():
    with pytest.raises(ValueError, match="空格"):
        parse_vendor_map('{"X": {"search": "two words", "code": "TP-1"}}')


def test_vendor_map_incomplete_rejected():
    with pytest.raises(ValueError, match="不完整"):
        parse_vendor_map('{"X": {"search": "abc"}}')


# ---------- build_plan 过滤矩阵 ----------

def test_happy_path_into_todo():
    todo, skipped = build_plan([row()], set(), VMAP)
    assert len(todo) == 1 and not skipped
    assert todo[0].key == "1001|TK-20990011"


def test_skip_error_row():
    todo, skipped = build_plan([row(error="未识别发票金额")], set(), VMAP)
    assert not todo and "异常未清" in skipped[0][1]


def test_skip_missing_invoice_or_tk():
    _, skipped = build_plan([row(invoice_no=""), row(tk="")], set(), VMAP)
    assert {w for _, w in skipped} == {"缺发票号/TK"}


def test_skip_zero_amount():
    _, skipped = build_plan([row(amount=0)], set(), VMAP)
    assert "摊分金额缺失或为0" in skipped[0][1]


def test_skip_already_submitted():
    _, skipped = build_plan([row()], {"1001|TK-20990011"}, VMAP)
    assert "已建立应付" in skipped[0][1]


def test_skip_unmapped_vendor():
    _, skipped = build_plan([row(vendor="Northline Transport")], set(), VMAP)
    assert "无映射" in skipped[0][1]


def test_skip_missing_invoice_date():
    _, skipped = build_plan([row(invoice_date=None)], set(), VMAP)
    assert "开票日期缺失" in skipped[0][1]


def test_due_missing_skips_or_fallback():
    r = row(due_date=None)
    _, skipped = build_plan([r], set(), VMAP)
    assert "DUE" in skipped[0][1]
    todo, _ = build_plan([row(due_date=None)], set(), VMAP, due_fallback_days=30)
    assert todo and todo[0].due_date == date(2026, 11, 14)  # 10-15 + 30


def test_filters_invoice_and_tk():
    rows = [row(), row(invoice_no="1002", tk="TK-20990012")]
    todo, _ = build_plan(rows, set(), VMAP, invoices={"1002"})
    assert [r.invoice_no for r in todo] == ["1002"]
    todo, _ = build_plan(rows, set(), VMAP, tks={"TK-20990011"})
    assert [r.tk for r in todo] == ["TK-20990011"]


# ---------- rows_from_bitable（假 client，不碰网络） ----------

class FakeClient:
    def __init__(self, records):
        self._records = records

    def bitable_list_records(self, field_names=None, page_size=500):
        yield from self._records


class FakeCfg:
    field_invoice_no = "发票号"
    field_tk = "TK号"
    field_vendor = "服务商名称"
    field_currency = "发票币种"
    field_alloc_amount = "摊分金额"
    field_invoice_date = "开票日期"
    field_invoice_due = "发票DUE date"
    field_error = "异常报错"
    field_subject = "邮件主题"


def test_rows_from_bitable_shapes():
    recs = [
        {"record_id": "recA", "fields": {
            "发票号": [{"text": "1001"}], "TK号": "TK-20990011",
            "服务商名称": [{"text": "ABC Logistics"}], "发票币种": "CAD",
            "摊分金额": "15.42", "开票日期": date_to_ms("2026-10-15"),
            "发票DUE date": date_to_ms("2026-11-14"), "异常报错": "", "邮件主题": "Fw: x"}},
        {"record_id": "recB", "fields": {
            "发票号": "", "TK号": "", "摊分金额": "", "异常报错": "未识别发票金额"}},
    ]
    rows = rows_from_bitable(FakeClient(recs), FakeCfg())
    assert len(rows) == 2
    a = rows[0]
    assert (a.invoice_no, a.tk, a.vendor, a.currency, a.amount) == \
        ("1001", "TK-20990011", "ABC Logistics", "CAD", 15.42)
    assert a.invoice_date == date(2026, 10, 15) and a.due_date == date(2026, 11, 14)
    assert rows[1].amount == 0.0 and rows[1].error == "未识别发票金额"


def test_rows_from_bitable_default_currency_cad():
    rows = rows_from_bitable(FakeClient([{"record_id": "r", "fields": {
        "发票号": "1", "TK号": "TK-1", "摊分金额": "10"}}]), FakeCfg())
    assert rows[0].currency == "CAD"


# ---------- ApState ----------

def test_state_roundtrip(tmp_path):
    p = tmp_path / "gfs.json"
    s = ApState(str(p))
    assert not s.is_submitted("1001|TK-20990011")
    s.mark("1001|TK-20990011", {"amount": 15.42, "note": "补记"})
    s.save()
    s2 = ApState(str(p))
    assert s2.is_submitted("1001|TK-20990011")
    assert s2.entries["1001|TK-20990011"]["amount"] == 15.42


def test_state_corrupt_file_recovers(tmp_path):
    p = tmp_path / "gfs.json"
    p.write_text("{not json", encoding="utf-8")
    s = ApState(str(p))
    assert s.entries == {}
    s.mark("a|b", {})
    s.save()
    assert ApState(str(p)).is_submitted("a|b")
