# -*- coding: utf-8 -*-
"""gofreight_ap 备忘录发票上传——纯逻辑离线测试：去重决策 / 附件读取 / 下载缓存。

Playwright 页面流程不在此测（控件 2026-11-10 已实弹定稿，见 memo.py 头注）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gofreight_ap import ApRow, rows_from_bitable  # noqa: E402
from gofreight_ap.memo import download_invoice_files, memo_action  # noqa: E402
from gofreight_ap.submit import GoFreightStepError  # noqa: E402

FILES = [{"file_token": "tokA", "name": "Invoice_1001.pdf", "size": 27656,
          "tmp_url": "x", "url": "y"}]


def mrow(**kw):
    base = dict(record_id="rec1", invoice_no="1001", tk="TK-20990011", vendor="V",
                currency="CAD", amount=15.42, invoice_date=None, due_date=None,
                error="", subject="", invoice_files=FILES)
    base.update(kw)
    return ApRow(**base)


# ---------- memo_action：去重决策 ----------

def test_action_skip_when_file_already_on_any_memo():
    # 专人手传过的备忘录标题口径不一——只认文件名，不认标题
    existing = [{"subject": "发票", "docs": ["别的.pdf", "Invoice_1001.pdf"]}]
    act, why = memo_action(existing, "1001", ["Invoice_1001.pdf"])
    assert act == "skip" and "已挂" in why


def test_action_attach_to_existing_titled_memo():
    existing = [{"subject": "1001", "docs": []}]
    act, why = memo_action(existing, "1001", ["Invoice_1001.pdf"])
    assert act == "attach" and "补挂" in why


def test_action_attach_when_partial_files():
    existing = [{"subject": "1001", "docs": ["a.pdf"]}]
    act, _ = memo_action(existing, "1001", ["a.pdf", "b.pdf"])
    assert act == "attach"


def test_action_create_when_nothing_matches():
    existing = [{"subject": "别的单子", "docs": ["z.pdf"]}]
    act, why = memo_action(existing, "1001", ["Invoice_1001.pdf"])
    assert act == "create" and "新建" in why


# ---------- rows_from_bitable 读附件列 ----------

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
    field_invoice_file = "发票"


def test_rows_read_invoice_files_filters_invalid_entries():
    recs = [{"record_id": "r1", "fields": {
        "发票号": "1001", "TK号": "TK-1", "摊分金额": "10",
        "发票": [FILES[0], {"name": "没token"}, "垃圾字符串"]}},
        {"record_id": "r2", "fields": {"发票号": "1002", "TK号": "TK-2", "摊分金额": "5"}}]
    rows = rows_from_bitable(FakeClient(recs), FakeCfg())
    assert rows[0].invoice_files == [FILES[0]]      # 只留有 file_token 的 dict
    assert rows[1].invoice_files == []              # 无附件=空，不报错


# ---------- download_invoice_files：下载 + 按大小缓存 + 非 PDF 拒收 ----------

class FakeLark:
    def __init__(self, content=b"%PDF-fake", fail_token=None):
        self._content, self._fail_token = content, fail_token
        self.tmp_calls = 0

    def _api(self, op, method, path, app="mail", json_body=None, query=None,
             data=None, content_type=None, timeout=None):
        self.tmp_calls += 1
        tok = query["file_tokens"]
        if tok == self._fail_token:
            return {"tmp_download_urls": []}
        return {"tmp_download_urls": [{"file_token": tok,
                                       "tmp_download_url": "https://x/" + tok}]}

    def download_file(self, url, max_bytes):
        assert url.startswith("https://x/")
        return self._content


class FakeLCfg:
    attachment_max_bytes = 1024 * 1024


def test_download_writes_and_caches(tmp_path):
    row = mrow()
    c1 = FakeLark()
    p1 = download_invoice_files(c1, FakeLCfg(), row, tmp_path)
    assert len(p1) == 1 and p1[0].read_bytes() == b"%PDF-fake"
    assert c1.tmp_calls == 1
    p2 = download_invoice_files(FakeLark(), FakeLCfg(), row, tmp_path)  # 同名同大小→缓存
    assert p2[0] == p1[0]
    (tmp_path / FILES[0]["name"]).write_bytes(b"%PDF-changed")          # 大小变了→重下
    p3 = download_invoice_files(FakeLark(b"%PDF-new"), FakeLCfg(), row, tmp_path)
    assert p3[0].read_bytes() == b"%PDF-new"


def test_download_rejects_non_pdf(tmp_path):
    c = FakeLark(content=b"PK\x03\x04zipnotpdf")
    try:
        download_invoice_files(c, FakeLCfg(), mrow(), tmp_path)
        assert False, "应拒收非 PDF"
    except GoFreightStepError as e:
        assert e.step == "memo_download" and "不是 PDF" in e.msg


def test_download_no_link_raises(tmp_path):
    c = FakeLark(fail_token="tokA")
    try:
        download_invoice_files(c, FakeLCfg(), mrow(), tmp_path)
        assert False, "无下载链接应报错"
    except GoFreightStepError as e:
        assert e.step == "memo_download" and "下载链接" in e.msg
