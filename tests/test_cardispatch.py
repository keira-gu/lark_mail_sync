# -*- coding: utf-8 -*-
"""卡派对账归档纯逻辑测试：Excel 读取 / 应付行解析 / 金额核对 / 归档命名 / 下载候选。"""
from __future__ import annotations

from datetime import date

import pytest

from gofreight_ap.cardispatch import (
    StmtRow, check_amount, dest_filename, download_doc, is_invoice_name,
    pick_bills_table, read_statement, sanitize_vendor, vendor_match,
)
from gofreight_ap.submit import GoFreightStepError


def test_invoice_no_match_fuzzy():
    from gofreight_ap.cardispatch import invoice_no_match
    assert invoice_no_match("34275", "34275")                       # 全等
    assert invoice_no_match("INV-91001", "91001")                   # INV 前缀
    assert invoice_no_match("inv91001", "91001")
    assert invoice_no_match("6251166014", "6614-6251166014")        # 短号含于长号
    assert invoice_no_match("91001", "INV91001-ABC")                # 互含
    assert invoice_no_match("", "123")                              # 缺失不判不一致
    assert not invoice_no_match("3754", "823973")                   # 真不一致


def test_vendor_match_normalizes():
    assert vendor_match("ACME LOGISTICS LLC", "ACME LOGISTICS LLC")
    assert vendor_match("ACME LOGISTICS", "ACME LOGISTICS LLC")   # 互含
    assert vendor_match("acme logistics llc", "ACME")
    assert not vendor_match("FUSION TRANSPORT LLC", "ACME LOGISTICS LLC")
    assert not vendor_match("", "ACME")
    assert not vendor_match("C.H. ROBERTS WORLDWIDE INC", "SOMEONE ELSE INC")


def test_is_invoice_name():
    assert is_invoice_name("INV6251166014.pdf")
    assert is_invoice_name("Invoice_91001_from_TestVendor.pdf")
    assert is_invoice_name("发票-9月.pdf")
    assert not is_invoice_name("POD-PO1090.zip")          # 非 PDF 且无发票字样
    assert not is_invoice_name("包装标签清单.pdf")
    assert not is_invoice_name("船舶证书.pdf")

HEADS = ["服务商名称", "发票日期", "到期日期", "文件编号", "提单号码", "MB/L号码",
         "HB/L号码", "D/C 发票号码", "币种", "应付金额", "发票金额", "发票情况",
         "重复项", "应收", "毛利率", "付款情况", "备注"]


def _xlsx(tmp_path, rows):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADS)
    for r in rows:
        ws.append(r)
    p = tmp_path / "明细.xlsx"
    wb.save(p)
    return p


def test_read_statement_normal_and_dirty(tmp_path):
    p = _xlsx(tmp_path, [
        ["ACME LOGISTICS LLC", "2026-09-02", "2026-09-02", "TK-20990034",
         "", "", "", "34275", "USD", 475, "", "", "", "", "", "", ""],
        ["  Alpha Trucking Express LLC ", "2026-09-03", "2026-09-10", "tk-20990051",
         "", "", "", "34247", "USD", "1,250.50", "", "", "", "", "", ""],  # 字符串金额带逗号
        ["BILMARK INC", "2026-09-04", "", "TK-20990060", "", "", "", "", "USD",
         "abc", "", "", "", "", "", "", ""],   # 金额解析失败 → bad
        ["CREDIT INC", "2026-09-04", "", "TK-20990061", "", "", "", "", "USD",
         -870, "", "", "", "", "", "", ""],    # 负数=贷项冲减，有效行
        ["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],  # 全空行跳过
        ["无TK行", "2026-09-05", "", "", "", "", "", "", "", 100, "", "", "", "", "", "", ""],  # 无TK跳过
    ])
    rows, bad = read_statement(p)
    assert len(rows) == 3 and len(bad) == 1
    r1, r2, r3 = rows
    assert r1.tk == "TK-20990034" and r1.amount == 475.0 and r1.dc_no == "34275"
    assert r2.vendor == "Alpha Trucking Express LLC"      # 去首尾空格
    assert r2.tk == "TK-20990051" and abs(r2.amount - 1250.50) < 1e-9  # 大写归一 + 逗号金额
    assert r3.amount == -870.0                              # 贷项负数行保留参与对账
    assert bad == [(4, "应付金额解析失败 'abc'")]


def test_read_statement_missing_header(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["随便", "表头"])
    p = tmp_path / "x.xlsx"; wb.save(p)
    with pytest.raises(ValueError, match="表头缺列"):
        read_statement(p)


def test_check_amount():
    assert check_amount(475.0, 475) == "OK"
    assert check_amount(475.004, 475) == "OK"          # 容差 0.01
    assert check_amount(474.5, 475) == "MISMATCH"
    assert check_amount(None, 475) == "NO_AMOUNT"
    assert check_amount(1250.0, 1250.0, tol=0.5) == "OK"


def test_sanitize_and_dest_filename():
    assert sanitize_vendor("A/B: C*?D") == "A B C D"
    assert sanitize_vendor("  X   Y  ") == "X Y"
    assert sanitize_vendor("") == "UNKNOWN"
    used = set()
    day = date(2026, 11, 19)
    n1 = dest_filename("ACME LOGISTICS LLC", day, used)
    n2 = dest_filename("ACME LOGISTICS LLC", day, used)
    n3 = dest_filename("acme logistics llc", day, used)   # 大小写不敏感防撞
    n4 = dest_filename("另一家", day, used)
    assert (n1, n2, n3) == ("ACME LOGISTICS LLC_2026-11-19.pdf",
                            "ACME LOGISTICS LLC_2026-11-19_02.pdf",
                            "acme logistics llc_2026-11-19_03.pdf")  # 保留传入大小写，防撞按小写
    assert n4 == "另一家_2026-11-19.pdf"


# 2026-11-19 probe 实测结构（会计页表格无表头，按行特征识别）
SCAN = [
    {"caption": "MB/L", "tables": [
        {"heads": [], "rows": [
            ["成本(应付)", "", "34275", "ACME LOGISTICS LLC", "0.00", "475.00",
             "0.00", "", "09-03-2026", "09-02-2026", "", "功能"],
            ["收入(应收)", "", "XX", "SOMEONE", "0.00", "999.00", "0.00", "", "", "", "", ""],
            ["加总", "0.00", "475.00", "0.00"],
            ["金额", "-475.00"],
        ]},
        {"heads": [], "rows": [["总利润", "", "", ""]]},
    ]},
    {"caption": "备忘录", "tables": [
        {"heads": ["标题", "上一次修改", "第一次建立", "功能/来源"], "rows": []}]},
]


def test_pick_bills_table_real_shape():
    picked = pick_bills_table(SCAN)
    assert picked is not None and picked["caption"] == "MB/L"
    bills = picked["bills"]
    assert len(bills) == 1                       # 应收行/加总/金额 全跳过
    b = bills[0]
    assert b["invoice_no"] == "34275"            # = Excel D/C 发票号码
    assert b["vendor"] == "ACME LOGISTICS LLC"
    assert b["amount"] == 475.0
    assert b["due_date"] == "09-03-2026" and b["invoice_date"] == "09-02-2026"


def test_pick_bills_table_multi_rows_sum():
    scan = [{"caption": "MB/L", "tables": [{"heads": [], "rows": [
        ["成本(应付)", "", "11", "V", "0.00", "100.00"],
        ["成本(应付)", "", "12", "V", "0.00", "375.00"],
        ["加总", "0.00", "475.00"],
    ]}]}]
    picked = pick_bills_table(scan)
    total = sum(b["amount"] for b in picked["bills"])
    assert len(picked["bills"]) == 2 and total == 475.0


def test_pick_bills_table_miss_returns_none():
    assert pick_bills_table([]) is None
    assert pick_bills_table([{"caption": "备忘录", "tables": [
        {"heads": [], "rows": [["成本(应付)", "", "x", "v", "0", "1"]]}]}]) is None  # 备忘录内不算
    assert pick_bills_table([{"caption": "MB/L", "tables": [
        {"heads": [], "rows": [["加总", "0.00"]]}]}]) is None


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def body(self):
        return self._body


def test_download_doc_direct_url_field():
    doc = {"filename": "a.pdf", "url": "https://gf.example/files/a.pdf"}
    called = []

    def get(u):
        called.append(u)
        return _FakeResp(200, b"%PDF-1.4 rest")

    body, hit = download_doc(get, "https://gf.example", doc)
    assert body.startswith(b"%PDF") and hit.endswith("/files/a.pdf") and called == [hit]


def test_download_doc_id_patterns_and_verify_magic():
    doc = {"filename": "a.pdf", "id": 77}
    urls = []

    def get(u):
        urls.append(u)
        if u.endswith("/accounting/memo/attachment/download/77"):
            return _FakeResp(200, b"<html>login</html>")   # 非 PDF → 继续试
        if u.endswith("/document/download/77/"):
            return _FakeResp(200, b"%PDF-2.0 ok")
        return _FakeResp(404, b"")

    body, hit = download_doc(get, "https://gf.example", doc)
    assert hit.endswith("/document/download/77/") and body.startswith(b"%PDF")
    assert len(urls) == 1   # /document/download/{id}/ 是首位候选，命中即停


def test_download_doc_preview_link_variants():
    """2026-11-19 probe 实测：docs 只带 preview_link=/document/preview/{id}/"""
    doc = {"filename": "a.pdf", "id": 4135, "preview_link": "/document/preview/4135/"}
    urls = []

    def get(u):
        urls.append(u)
        if u.endswith("/document/download/4135/"):
            return _FakeResp(200, b"%PDF-1.7 real")
        return _FakeResp(200, b"<html>viewer</html>")      # preview 直开=HTML 页 → 跳过

    body, hit = download_doc(get, "https://gf.example", doc)
    assert hit.endswith("/document/download/4135/") and body.startswith(b"%PDF")
    assert urls[0].endswith("/document/download/4135/")    # preview→download 变体最先试


def test_download_doc_all_miss_raises_with_probe_hint():
    doc = {"filename": "a.pdf", "id": 1}

    def get(u):
        return _FakeResp(404, b"")

    with pytest.raises(GoFreightStepError, match="probe"):
        download_doc(get, "https://gf.example", doc)


def test_stmt_row_smoke():
    r = StmtRow(2, "V", "TK-1", "USD", 10.0, "DC1", "2026-09-02")
    assert r.tk == "TK-1"


def test_resolve_statement_path(tmp_path, monkeypatch):
    """v1.5.8：只给文件名 → 当前目录外的常见位置自动定位；精确名优先于模糊包含。"""
    from gofreight_ap.cardispatch import resolve_statement_path
    monkeypatch.chdir(tmp_path)   # 隔离 CWD：项目根躺着的真实明细表不得干扰断言
    dl = tmp_path / "Downloads"
    dl.mkdir()
    f = dl / "美国服务商明细.xlsx"
    f.write_bytes(b"x")
    (dl / "美国服务商明细.backup-20261120.xlsx").write_bytes(b"x")   # 同目录含名文件
    # 精确同名命中（备份文件不干扰）
    assert resolve_statement_path("美国服务商明细.xlsx", [dl]) == f
    # 完整路径直接用
    assert resolve_statement_path(str(f), [dl]) == f
    # 只给文件名（无扩展名）→ 唯一模糊命中
    f2 = dl / "11月服务商明细.xlsx"
    f2.write_bytes(b"x")
    assert resolve_statement_path("11月服务商明细", [dl]) == f2
    # 多个模糊命中 → 列候选
    import pytest
    with pytest.raises(ValueError, match="多个候选"):
        resolve_statement_path("服务商明细", [dl])
    # 找不到 → 报错带目录清单
    with pytest.raises(ValueError, match="找不到明细 Excel"):
        resolve_statement_path("不存在.xlsx", [dl])
    # ~$ 临时文件忽略
    (dl / "~$美国服务商明细.xlsx").write_bytes(b"x")
    assert resolve_statement_path("美国服务商明细.xlsx", [dl]) == f


def test_archive_pdf_idempotent(tmp_path):
    """全局内容去重（用户口径：不要重复发票）——同一份发票全目录只存一份。"""
    from gofreight_ap.cardispatch import archive_pdf, preload_seen
    day = date(2026, 11, 20)
    used, seen = preload_seen(tmp_path)
    b1 = b"%PDF-1.4 " + b"A" * 100
    n1 = archive_pdf(b1, "AAA LLC", day, tmp_path, used, seen)
    assert n1 == "AAA LLC_2026-11-20.pdf" and len(list(tmp_path.glob("*.pdf"))) == 1
    # 重跑同供应商同内容 → 复用，不新增
    assert archive_pdf(b1, "AAA LLC", day, tmp_path, used, seen) == n1
    # **其他供应商**共票同发票 → 也复用同一份，绝不复制（全局去重）
    assert archive_pdf(b1, "BBB LLC", day, tmp_path, used, seen) == n1
    assert len(list(tmp_path.glob("*.pdf"))) == 1
    # 另一份内容 → 正常新文件
    assert archive_pdf(b"%PDF-1.4 " + b"B" * 100, "AAA LLC", day, tmp_path, used, seen) \
        == "AAA LLC_2026-11-20_02.pdf"
    assert len(list(tmp_path.glob("*.pdf"))) == 2


def test_preload_seen_recovers_from_existing_dir(tmp_path):
    """重启后从归档目录恢复全局去重索引：同内容（任何供应商行）仍零重复。"""
    from gofreight_ap.cardispatch import archive_pdf, preload_seen
    day = date(2026, 11, 20)
    used, seen = preload_seen(tmp_path)
    n1 = archive_pdf(b"%PDF-1.4 X", "AAA LLC", day, tmp_path, used, seen)
    # 模拟下次运行：重新从目录预热，换成另一供应商的行 → 仍复用
    used2, seen2 = preload_seen(tmp_path)
    assert archive_pdf(b"%PDF-1.4 X", "BBB LLC", day, tmp_path, used2, seen2) == n1
    assert len(list(tmp_path.glob("*.pdf"))) == 1


def _mk_excel(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(HEADS)
    ws.append(["AAA LLC", "2026-09-02", "2026-09-02", "TK-1", "", "", "", "D1", "USD", 100, "", "", "", "", "", "", ""])
    ws.append(["BBB LLC", "2026-09-03", "2026-09-03", "TK-2", "", "", "", "D2", "USD", 200, "", "", "", "", "", "", ""])
    p = tmp_path / "s.xlsx"; wb.save(p)
    return p


def test_amount_from_text_priority():
    from gofreight_ap.cardispatch import amount_from_text
    txt = "\n".join(["Subtotal 300.00", "Tax 27.00", "Total 327.00", "Balance Due USD 327.00"])
    assert amount_from_text(txt) == 327.0            # Balance Due 优先于 Total


def test_amount_from_text_comma_and_next_line():
    from gofreight_ap.cardispatch import amount_from_text
    assert amount_from_text("Total Due\n$ 2,046.32") == 2046.32
    assert amount_from_text("foo 100.00 bar 1,234.50 baz") == 1234.5  # 无标签兜底最大
    assert amount_from_text("no numbers") is None
    assert amount_from_text("") is None


def test_vendor_in_text():
    from gofreight_ap.cardispatch import vendor_in_text
    assert vendor_in_text("ACME LOGISTICS LLC",
                          "Invoice from Acme Logistics LLC, thanks")
    assert vendor_in_text("C.H. ROBERTS WORLDWIDE INC", "C H ROBERTS WORLDWIDE INC invoice")
    assert not vendor_in_text("FUSION TRANSPORT LLC", "Some other vendor entirely")


def test_pdf_amount_for_row_matching(monkeypatch):
    from gofreight_ap import cardispatch as cd
    texts = {b"p1": "ACME LOGISTICS\nBalance Due 475.00",
             b"p2": "FUSION TRANSPORT LLC\nTotal 1200.00",
             b"scan": ""}                       # 扫描件：无文本层
    monkeypatch.setattr(cd, "parse_pdf_text", lambda b: texts.get(b, ""))
    pdfs = [(b"p1", "a.pdf"), (b"p2", "b.pdf")]
    assert cd.pdf_amount_for_row(pdfs, "ACME LOGISTICS LLC") == (475.0, "ok")
    assert cd.pdf_amount_for_row(pdfs, "FUSION TRANSPORT LLC") == (1200.0, "ok")
    # 多张里没有本供应商的 → no_vendor_invoice（发票没传，不瞎猜）
    assert cd.pdf_amount_for_row(pdfs, "Unknown Vendor") == (None, "no_vendor_invoice")
    # 唯一张兜底
    assert cd.pdf_amount_for_row([(b"p2", "b.pdf")], "Unknown Vendor") == (1200.0, "ok")
    # 全扫描件 → no_text
    assert cd.pdf_amount_for_row([(b"scan", "s.pdf")], "X LLC") == (None, "no_text")


def test_problem_text_states_missing_attachment():
    """无附件的行问题列必须明确写出（用户口径 2026-11-20）。"""
    from gofreight_ap.cardispatch import _problem_text
    # 金额对但附件没下到：note 带完整说明
    r1 = {"status": "NO_PDF", "files": [],
          "note": "文件中心有 4 个 PDF 但无发票样文件名（INV/发票）；无发票附件（GoFreight文件中心与备忘录均无）"}
    t1 = _problem_text(r1)
    assert "无发票附件" in t1 and "发票样" in t1
    # note 为空的兜底：也要写出附件缺失
    r2 = {"status": "NO_PDF", "files": [], "note": ""}
    assert "未找到发票附件" in _problem_text(r2)
    # 金额不符且无附件：两个问题都写
    r3 = {"status": "MISMATCH", "files": [], "note": "金额不符：X 在 GoFreight 150.00 vs 明细 200.00 USD"}
    t3 = _problem_text(r3)
    assert "金额不符" in t3 and "未找到发票附件" in t3
    # 全通过：留空；有问题但有附件：不写附件
    assert _problem_text({"status": "OK", "files": ["a.pdf"], "note": "附件×1"}) == ""
    t4 = _problem_text({"status": "MISMATCH", "files": ["a.pdf"],
                        "note": "金额不符：X 150 vs 200；附件×1（GoFreight文件中心）"})
    assert "金额不符" in t4 and "附件" not in t4.replace("金额不符", "")


def test_invoice_case_text_records_anomalies():
    """发票情况列 = 异常标签+附件+识别情况（用户口径：异常也记到这列）。"""
    from gofreight_ap.cardispatch import invoice_case_text
    # 正常：只有已下载
    assert invoice_case_text({"status": "OK", "files": ["a.pdf"],
                              "pdf_amount": 475.0}) == "已下载1份"
    # 金额不符 + 无附件：两个异常都写
    assert invoice_case_text({"status": "MISMATCH", "files": [],
                              "pdf_amount": None}) == "金额不符；未找到发票附件"
    # 无账单 + 有附件
    assert invoice_case_text({"status": "NO_BILL", "files": ["a.pdf", "b.pdf"],
                              "pdf_amount": 100.0}) == "GoFreight无账单；已下载2份"
    # 金额对 + 有附件但没识别出金额（三档细分原因）
    assert invoice_case_text({"status": "NO_PDF", "files": [],
                              "pdf_amount": None}) == "未找到发票附件"
    assert invoice_case_text({"status": "OK", "files": ["a.pdf"], "pdf_amount": None,
                              "pdf_why": "no_vendor_invoice"}) == "已下载1份；未找到本供应商发票"
    assert invoice_case_text({"status": "OK", "files": ["a.pdf"], "pdf_amount": None,
                              "pdf_why": "no_text"}) == "已下载1份；发票金额未识别(扫描件)"
    assert invoice_case_text({"status": "OK", "files": ["a.pdf"], "pdf_amount": None,
                              "pdf_why": "unparseable"}) == "已下载1份；发票金额未识别"
    assert invoice_case_text({"status": "READ_FAIL", "files": [],
                              "pdf_amount": None}) == "账单读取失败；未找到发票附件"


def test_annotate_excel_marks_and_backs_up(tmp_path):
    from gofreight_ap.cardispatch import annotate_excel
    p = _mk_excel(tmp_path)
    rows = [StmtRow(2, "AAA LLC", "TK-1", "USD", 100.0, "D1", "2026-09-02"),
            StmtRow(3, "BBB LLC", "TK-2", "USD", 200.0, "D2", "2026-09-03")]
    results = [
        {"row_no": 2, "tk": "TK-1", "vendor": "AAA LLC", "stmt_amount": 100.0, "currency": "USD",
         "status": "OK", "note": "附件×1（GoFreight文件中心）", "invoices": "D1",
         "gf_amount": 100.0, "pdf_amount": 100.0, "files": ["AAA LLC_2026-11-20.pdf"]},
        {"row_no": 3, "tk": "TK-2", "vendor": "BBB LLC", "stmt_amount": 200.0, "currency": "USD",
         "status": "MISMATCH", "note": "金额不符：BBB LLC 在 GoFreight 150.00 vs 明细 200.00 USD；无发票附件（GoFreight文件中心与备忘录均无）",
         "invoices": "X9", "gf_amount": 150.0, "pdf_amount": None, "files": []},
    ]
    src, bak = annotate_excel(p, results, rows)
    assert src == p and bak.exists()                      # 原地标注 + 自动备份
    import openpyxl
    ws = openpyxl.load_workbook(p).worksheets[0]
    # 发票金额列 = PDF 识别金额（有 PDF 行填值；无 PDF 行置空不留旧 GF 金额）
    assert ws.cell(2, 11).value == 100.0 and ws.cell(3, 11).value is None
    # 发票情况列（第12列）：异常标签也记入
    assert ws.cell(2, 12).value == "已下载1份"
    assert ws.cell(3, 12).value == "金额不符；未找到发票附件"
    # 「对账问题」列：表尾空列上新建（第18列），OK 行留空，
    # 问题行写明问题（含附件缺失）；原备注列（第17列）不动
    assert ws.cell(1, 18).value == "对账问题"
    assert not ws.cell(2, 18).value                         # OK+有附件 → 留空
    prob = ws.cell(3, 18).value
    assert "金额不符" in prob and "150.00" in prob and "无发票附件" in prob
    assert ws.cell(2, 17).value is None                     # 备注列未被触碰
    # 底色停用（用户口径 2026-11-20）：问题行不标红，任何行都无填充
    for r in (2, 3):
        assert not str(ws.cell(r, 1).fill.fgColor.rgb).endswith(("FFC7CE", "FFEB9C"))


def test_annotate_excel_locked_falls_back(tmp_path, monkeypatch):
    """原文件被占用 → 落 _对账标注 副本，结果不丢。"""
    import openpyxl
    from gofreight_ap import cardispatch as cd

    class _WB:
        def __init__(self, wb): self._wb = wb

        @property
        def worksheets(self): return self._wb.worksheets

        def save(self, path):
            if "对账标注" not in str(path):
                raise PermissionError("locked")
            self._wb.save(path)

    monkeypatch.setattr(openpyxl, "load_workbook", lambda p: _WB(openpyxl.Workbook()))
    p = _mk_excel(tmp_path)
    rows = [StmtRow(2, "AAA LLC", "TK-1", "USD", 100.0, "D1", "")]
    results = [{"row_no": 2, "tk": "TK-1", "vendor": "AAA LLC", "stmt_amount": 100.0,
                "currency": "USD", "status": "OK", "note": "", "invoices": "D1",
                "gf_amount": 100.0, "files": []}]
    src, _bak = cd.annotate_excel(p, results, rows)
    assert "对账标注" in src.name and src.exists()
