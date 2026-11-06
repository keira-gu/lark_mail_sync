# -*- coding: utf-8 -*-
"""发票 PDF 信息识别测试（夹具仿自真实发票的 pypdf 抽取文本，已全部脱敏）。"""
from lark_mail_sync.invoice import date_to_ms, extract_invoice_info, parse_invoice_text

# A B C Logistics 版式（Invoice 1001 原文截取）
ABC_TEXT = """Page 1 of 1
 A B C Logistics
123 Main Street
Anytown ON  A1A 1A1
contact@abclogistics.example
GST/HST Registration No.: 123456789RT0001
INVOICE
BILL TO
Acme International Inc.
500 Commerce Blvd
Anytown ON  B2B 2B2
INVOICE 1001
DATE 10/01/2026
TERMS Net 30
DUE DATE 10/31/2026
CURRENCY
CAD
DATE ACTIVITY TAX QTY RATE AMOUNT TRIP NUMBER
09/30/2026 FTL
Exempt 1 185.00 185.00
 SUBTOTAL 185.00
TOTAL 185.00
BALANCE DUE CAD 185.00"""

# Bluegate Transport 版式（币种在金额后、ISO 日期、Invoice No.）
BLUEGATE_TEXT = """BLUEGATE TRANSPORT
1234 GATEWAY BLVD UNIT 100
ANYTOWN A1A1A1 ON CANADA
BILL TO:
ACME INTERNATIONAL INC.
789 INDUSTRIAL AVE UNIT 300
INVOICEInvoice No. INV-100001
Invoice Date 2026-10-23
Due Date 2026-11-22
Balance Due $375.00 CAD
Subtotal $375.00"""

# Northline 版式（DD/MM 日期：18/10 只能日月；无 CURRENCY 行，币种在 BALANCE DUE 后）
NORTHLINE_TEXT = """Northline International Logistics Co.,Ltd
12 Example Rd
INVOICE
BILL TO
Acme International Inc.
INVOICE 1005
DATE 18/10/2026
TERMS Net 30
DUE DATE 17/11/2026
Container: TSTU0000021 SUBTOTAL 310.00
TOTAL 310.00
BALANCE DUE CAD 310.00"""


def test_abc_invoice():
    info = parse_invoice_text(ABC_TEXT)
    assert info["vendor"] == "ABC Logistics"        # A B C 单字母序列已合并
    assert info["our_company"] == "Acme International Inc."
    assert info["invoice_no"] == "1001"
    assert info["currency"] == "CAD"
    assert info["issue_date"] == "2026-10-01"      # MM/DD 北美
    assert info["due_date"] == "2026-10-31"


def test_bluegate_invoice():
    info = parse_invoice_text(BLUEGATE_TEXT)
    assert info["vendor"] == "BLUEGATE TRANSPORT"
    assert info["our_company"] == "ACME INTERNATIONAL INC."
    assert info["invoice_no"] == "INV-100001"
    assert info["currency"] == "CAD"               # $375.00 CAD 金额后
    assert info["issue_date"] == "2026-10-23"
    assert info["due_date"] == "2026-11-22"


def test_northline_invoice_ddmm_dates():
    info = parse_invoice_text(NORTHLINE_TEXT)
    assert info["vendor"] == "Northline International Logistics Co.,Ltd"
    assert info["invoice_no"] == "1005"
    assert info["currency"] == "CAD"               # BALANCE DUE CAD 310.00
    assert info["issue_date"] == "2026-10-18"      # 18/10 只能是 DD/MM
    assert info["due_date"] == "2026-11-17"


# QuickBooks 两页发票（Invoice 1006 版式）：无 CURRENCY 块、尾款只有 $
QUICKBOOKS_TEXT = """Page 1 of 2
 A B C Logistics
GST/HST Registration No.: 123456789RT0001
INVOICE
BILL TO
Acme International Inc.
INVOICE 1006
DATE 08/31/2026
DUE DATE 09/30/2026
08/24/2026 FTL
Zero-rated 1 300.00 300.00
 SUBTOTAL 3,450.00
GST @ 0% 0.00
TOTAL 3,450.00
BALANCE DUE $3,450.00
TAX SUMMARY
GST @ 0% 0.00 3,450.00"""


def test_quickbooks_no_currency_block_infers_cad():
    info = parse_invoice_text(QUICKBOOKS_TEXT)
    assert info["currency"] == "CAD"           # $ + GST/HST 信号 → CAD
    assert info["amount"] == "3,450.00"        # BALANCE DUE $3,450.00
    assert info["vendor"] == "ABC Logistics"


def test_dollar_without_ca_signal_leaves_currency_empty():
    text = "ACME Corp\nBILL TO\nSomeone\nTotal $100.00\nBALANCE DUE $100.00"
    info = parse_invoice_text(text)
    assert "currency" not in info               # 无加拿大信号不猜


def test_clean_company_single_letters():
    from lark_mail_sync.invoice import _clean_company
    assert _clean_company("A B C Logistics") == "ABC Logistics"
    assert _clean_company("A B C") == "ABC"
    assert _clean_company("BLUEGATE TRANSPORT") == "BLUEGATE TRANSPORT"      # 无单字母不动
    assert _clean_company("A Business  Corp") == "A Business Corp"     # 多空格压缩


def test_empty_and_garbage():
    assert parse_invoice_text("") == {}
    assert extract_invoice_info(b"not a pdf") == {}


def test_amount_extraction():
    assert parse_invoice_text(ABC_TEXT)["amount"] == "185.00"    # BALANCE DUE CAD 185.00
    assert parse_invoice_text(BLUEGATE_TEXT)["amount"] == "375.00"  # Balance Due $375.00 CAD
    assert parse_invoice_text(NORTHLINE_TEXT)["amount"] == "310.00"


def test_date_to_ms():
    assert date_to_ms("2026-10-01") == 1790812800000  # UTC 零点（1790812800 整除 86400）
    assert date_to_ms("bad") is None
