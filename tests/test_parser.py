# -*- coding: utf-8 -*-
"""解析器单元测试：TK 行、金额提取、按数量摊分。"""
import base64

from lark_mail_sync.parser import (
    allocate, is_confirm, parse_amount, parse_confirm_email, parse_tk_rows,
    try_base64_decode,
)

SAMPLE = """TSTU0000001        TK-26000001        1
TSTU0000002        TK-26000002        5
TSTU0000003        TK-26000003        1
TSTU0000004        TK-26000004        5"""


def test_parse_tk_rows_user_sample():
    rows = parse_tk_rows(SAMPLE)
    assert rows == [("TK-26000001", 1), ("TK-26000002", 5),
                    ("TK-26000003", 1), ("TK-26000004", 5)]


def test_parse_tk_rows_flexible():
    # 无横杠、制表符、括号数量、大小写
    rows = parse_tk_rows("ABC123 TK12345678\t2\nxyz tk-87654321 (3)")
    assert rows == [("TK-12345678", 2), ("TK-87654321", 3)]


def test_parse_tk_rows_aggregates_same_tk():
    assert parse_tk_rows("TK-11111111 2\nTK-11111111 3") == [("TK-11111111", 5)]


def test_parse_tk_rows_default_qty_one():
    assert parse_tk_rows("CONT123 TK-22222222") == [("TK-22222222", 1)]


def test_parse_tk_rows_none():
    assert parse_tk_rows("普通正文没有业务行") == []


def test_is_confirm_case_insensitive():
    assert is_confirm("Please Confirm the charges")
    assert is_confirm("CONFIRM")
    assert not is_confirm("Invoice 1002")


# ===== 2026-11-05 真实邮件形状 =====

def test_try_base64_decode_urlsafe():
    enc = base64.urlsafe_b64encode("Confirmed. TK-26000013 13 —谢谢".encode()).decode()
    assert try_base64_decode(enc) == "Confirmed. TK-26000013 13 —谢谢"


def test_try_base64_decode_no_false_positive():
    # 普通文本（含标点/空格）原样返回；纯字母数字短串不误判；空串安全
    assert try_base64_decode("Total: 260.00 CAD") == "Total: 260.00 CAD"
    assert try_base64_decode("hello") == "hello"
    assert try_base64_decode("") == ""


def test_parse_tk_rows_flattened_nbsp_one_line():
    # 真实解码后正文：换行全丢、NBSP 分列、整封一行
    gap = "      "
    body = ("Confirmed. TSTU0000001" + gap + "TK-26000001" + gap + "1 "
            "TSTU0000002" + gap + "TK-26000002" + gap + "5 "
            "TSTU0000003 TK-26000003 1 TSTU0000004 TK-26000004 5 Thank you")
    assert parse_tk_rows(body) == [("TK-26000001", 1), ("TK-26000002", 5),
                                   ("TK-26000003", 1), ("TK-26000004", 5)]


def test_parse_confirm_email_base64_body():
    body = base64.urlsafe_b64encode(
        "Confirmed. TSTU0000013 TK-26000013 13 Total CAD 260.00".encode()).decode()
    result = parse_confirm_email({"body_plain_text": body})
    assert result.tk_rows == [("TK-26000013", 13)]
    assert result.amount == 260.0
    assert not result.errors


def test_is_confirm_matches_body_not_only_subject():
    # 真实邮件主题是 Fw: Invoice…不含 confirm，「Confirmed.」在正文
    assert is_confirm("Fw: Invoice 1002 from Sample Carrier Inc.",
                      "Confirmed. TSTU0000013 TK-26000013 13")
    assert not is_confirm("Fw: Invoice 1002 from Sample Carrier Inc.",
                          "INVOICE 1002 DETAILS CAD 260.00")
    assert is_confirm("Please CONFIRM", "")


def test_amount_keyword():
    assert parse_amount("Total: 1,200.00 CAD") == (1200.0, "keyword")
    assert parse_amount("发票金额 800.00") == (800.0, "keyword")
    assert parse_amount("Grand Total Due 3,500.50") == (3500.5, "keyword")


def test_amount_currency_prefix():
    assert parse_amount("CAD 800.00") == (800.0, "currency")
    assert parse_amount("$64.20") == (64.2, "currency")


def test_amount_unique_fallback_and_none():
    assert parse_amount("price is 66.60 only") == (66.6, "unique")
    assert parse_amount("a 1.00 and b 2.00") == (None, "")
    assert parse_amount("") == (None, "")


def test_parse_confirm_email_full():
    result = parse_confirm_email({"body_plain_text": SAMPLE + "\nTotal: 1,200.00"})
    assert result.tk_rows and len(result.tk_rows) == 4
    assert result.amount == 1200.0
    assert not result.errors


def test_parse_confirm_email_failures():
    result = parse_confirm_email({"body_plain_text": "hello"})
    assert result.tk_rows == []
    assert result.amount is None
    assert "未解析到TK行" in result.errors
    assert "未识别发票金额" in result.errors


def test_allocate_exact():
    out = allocate(1200.0, [("TK-A", 1), ("TK-B", 5), ("TK-C", 1), ("TK-D", 5)])
    assert out == [("TK-A", 1, 100.0), ("TK-B", 5, 500.0),
                   ("TK-C", 1, 100.0), ("TK-D", 5, 500.0)]


def test_allocate_rounding_residue():
    out = allocate(100.0, [("TK-A", 1), ("TK-B", 1), ("TK-C", 1)])
    assert sum(a for _, _, a in out) == 100.0
    assert [a for _, _, a in out] == [33.34, 33.33, 33.33]  # 尾差给最大行（并列取首）


def test_allocate_empty():
    assert allocate(100.0, []) == []


# ===== 无数量口径（2026-11-06 业务定版）：TK 行不带数量 → 每单默认 1，多单平摊 AA =====

def test_parse_tk_rows_no_qty_multi_tk_aa():
    """多单完全不带数量：各 TK qty=1，摊分=金额平摊（AA），Σ=发票金额。"""
    r = parse_confirm_email({"body_plain_text":
                             "Confirmed.\n\nTK-26990001\nTK-26990002\nTK-26990003\n\nTotal CAD 300.00"})
    assert [q for _, q in r.tk_rows] == [1, 1, 1]
    out = allocate(r.amount, r.tk_rows)
    assert [a for _, _, a in out] == [100.0, 100.0, 100.0]
    assert sum(a for _, _, a in out) == 300.0


def test_parse_tk_rows_no_qty_single_tk_full_amount():
    """单 TK 不带数量：全额落该 TK。"""
    r = parse_confirm_email({"body_plain_text": "Confirmed. TK-26990099 Total CAD 250.00"})
    out = allocate(r.amount, r.tk_rows)
    assert out == [("TK-26990099", 1, 250.0)]


def test_parse_tk_rows_no_qty_mixed_weighted():
    """混排：带数量的按数量、不带的按 1，加权摊分。"""
    r = parse_confirm_email({"body_plain_text":
                             "Confirmed.\n\nTK-26990001 4\nTK-26990002\n\nTotal CAD 300.00"})
    assert r.tk_rows == [("TK-26990001", 4), ("TK-26990002", 1)]
    out = allocate(r.amount, r.tk_rows)
    assert [a for _, _, a in out] == [240.0, 60.0]


def test_parse_tk_rows_no_qty_flattened_nbsp_one_line():
    """整段一行+NBSP 分隔且无数量：仍识别为多单 AA。"""
    r = parse_confirm_email({"body_plain_text":
                             "Confirmed.\xa0\xa0TK-26990001\xa0\xa0TK-26990005\xa0\xa0Total CAD 100.00"})
    out = allocate(r.amount, r.tk_rows)
    assert [a for _, _, a in out] == [50.0, 50.0]
