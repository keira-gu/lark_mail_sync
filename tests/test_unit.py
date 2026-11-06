# -*- coding: utf-8 -*-
"""杂项单元测试：时间防御、文件名清洗。"""
from lark_mail_sync.lark_client import safe_filename
from lark_mail_sync.sync import to_ms


def test_to_ms_units():
    assert to_ms(1762300000) == 1762300000000        # 秒 → 毫秒
    assert to_ms(1762300000000) == 1762300000000     # 已是毫秒
    assert to_ms("1793699500000") == 1793699500000   # 实测：字符串毫秒
    assert to_ms(None) is None
    assert to_ms("") is None
    assert to_ms(0) is None
    assert to_ms("not-a-number") is None


def test_safe_filename():
    assert safe_filename("Invoice_1002_from_A_B_C.pdf") == "Invoice_1002_from_A_B_C.pdf"
    assert safe_filename("发票 2026\\/:.pdf") != ""
    assert safe_filename("") == "attachment"
    assert safe_filename("   ") == "attachment"
