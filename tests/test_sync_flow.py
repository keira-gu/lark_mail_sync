# -*- coding: utf-8 -*-
"""同步引擎全链路测试（FakeTransport，零网络，confirm-TK 口径）。"""
from conftest import (
    LEGACY_FIELDS, REAL_CONFIRM_B64, SAMPLE_BODY, FakeTransport, att, make_service, msg,
)


def _seed(t: FakeTransport, messages):
    t.pages = [[m["message_id"] for m in messages]]
    for m in messages:
        t.messages[m["message_id"]] = m


def test_confirm_filter_and_per_tk_records(tmp_path):
    t = FakeTransport()
    _seed(t, [
        msg("c1", "Confirm charges Nov", body=SAMPLE_BODY),
        msg("x1", "Invoice 1002 from X"),  # 非 confirm：不录入
    ])
    svc, t, _ = make_service(tmp_path, t)
    stats = svc.run(incremental=False)
    assert stats.confirm_emails == 1
    assert stats.records_created == 4      # 4 个 TK 行
    assert len(t.records) == 4
    recs = list(t.records.values())
    assert {r["TK号"] for r in recs} == {"TK-26000001", "TK-26000002",
                                         "TK-26000003", "TK-26000004"}
    # 全量填满：每行都带邮件级字段
    for r in recs:
        assert r["邮件主题"] == "Confirm charges Nov"
        assert r["收件人邮箱号"] == "ops@test"
        assert r["收到邮件日期"] == 1793699500000
        assert r["发票金额"] == 1200.0
        assert r["消息ID"] == "c1"
    by_tk = {r["TK号"]: r for r in recs}
    assert by_tk["TK-26000001"]["数量"] == 1
    assert by_tk["TK-26000001"]["摊分金额"] == 100.0
    assert by_tk["TK-26000002"]["数量"] == 5
    assert by_tk["TK-26000002"]["摊分金额"] == 500.0


def test_invoice_fields_on_every_row(tmp_path, monkeypatch):
    """发票 PDF 识别出的字段（服务商/我司/币种/开票日期/DUE/发票名称）每行都写。"""
    import lark_mail_sync.sync as sync_mod
    from lark_mail_sync.invoice import date_to_ms
    t = FakeTransport()
    t.file_contents["https://fake.lark.test/file/a1"] = b"PDF"
    _seed(t, [
        msg("c1", "Confirm X", body=SAMPLE_BODY),
        msg("i1", "Confirm X", attachments=[att("a1", "Inv.pdf")]),
    ])
    monkeypatch.setattr(sync_mod, "extract_invoice_info", lambda content: {
        "vendor": "ABC Logistics", "our_company": "Acme International Inc.",
        "currency": "CAD", "invoice_no": "1001",
        "issue_date": "2026-10-01", "due_date": "2026-10-31"})
    svc, t, _ = make_service(tmp_path, t)
    svc.run(incremental=False)
    recs = list(t.records.values())
    assert len(recs) == 4
    for r in recs:
        assert r["服务商名称"] == "ABC Logistics"
        assert r["我司名称"] == "Acme International Inc."
        assert r["发票币种"] == "CAD"
        assert r["发票名称"] == "Inv.pdf"      # 与附件文件名统一
        assert r["发票号"] == "1001"           # 发票号单独一列（PDF 识别）
        assert r["开票日期"] == date_to_ms("2026-10-01")
        assert r["发票DUE date"] == date_to_ms("2026-10-31")


def test_unparsable_pdf_leaves_invoice_fields_empty(tmp_path):
    """附件不是合法 PDF：发票字段留空，不影响录入。"""
    t = FakeTransport()
    t.file_contents["https://fake.lark.test/file/a1"] = b"not a pdf"
    _seed(t, [
        msg("c1", "Confirm X", body=SAMPLE_BODY),
        msg("i1", "Confirm X", attachments=[att("a1", "Inv.pdf")]),
    ])
    svc, t, _ = make_service(tmp_path, t)
    svc.run(incremental=False)
    recs = list(t.records.values())
    assert len(recs) == 4
    for r in recs:
        assert "服务商名称" not in r and "发票币种" not in r
    assert recs[0]["发票"] == [{"file_token": "ft1"}]  # 附件本身仍正常挂


def test_invoice_pdf_amount_fallback(tmp_path, monkeypatch):
    """邮件正文无金额时回退发票 PDF 金额：发票金额/摊分照常，异常清除。"""
    import lark_mail_sync.sync as sync_mod
    t = FakeTransport()
    t.file_contents["https://fake.lark.test/file/a1"] = b"PDF"
    body = "Confirmed. TSTU0000013 TK-26000013 13 Thank you"  # 无金额行
    _seed(t, [
        msg("c1", "Confirm X", body=body),
        msg("i1", "Confirm X", attachments=[att("a1", "Inv.pdf")]),
    ])
    monkeypatch.setattr(sync_mod, "extract_invoice_info", lambda content: {"amount": "375.00"})
    svc, t, _ = make_service(tmp_path, t)
    svc.run(incremental=False)
    recs = list(t.records.values())
    assert len(recs) == 1
    assert recs[0]["发票金额"] == 375.0
    assert recs[0]["摊分金额"] == 375.0
    assert "未识别发票金额" not in (recs[0].get("异常报错") or "")


def test_real_shape_base64_body_confirm_in_body(tmp_path):
    """2026-11-05 真实邮件：主题无 confirm、正文 URL-safe base64 且无换行；
    同主题的发票转发（无 confirm）只当附件来源。"""
    t = FakeTransport()
    t.file_contents["https://fake.lark.test/file/a1"] = b"PDF"
    _seed(t, [
        msg("c1", "Fw: Invoice 1002 from Sample Carrier Inc.",
            date="1793699569763", body=REAL_CONFIRM_B64),
        msg("i1", "Fw: Invoice 1002 from Sample Carrier Inc.",
            date="1793699598483", body="INVOICE 1002 DETAILS DUE CAD 260.00",
            attachments=[att("a1", "Invoice_1002_from_A_B_C_Logistics.pdf")]),
    ])
    svc, t, _ = make_service(tmp_path, t)
    stats = svc.run(incremental=False)
    assert stats.confirm_emails == 1            # 仅正文含 Confirmed 的那封
    assert stats.records_created == 1
    rec = list(t.records.values())[0]
    assert rec["TK号"] == "TK-26000013"
    assert rec["数量"] == 13
    assert rec["发票金额"] == 260.0
    assert rec["摊分金额"] == 260.0              # 单行摊分=全额
    assert rec["发票"] == [{"file_token": "ft1"}]  # 同名转发附件已挂
    assert rec["发票名称"] == "Invoice_1002_from_A_B_C_Logistics.pdf"  # 与附件名统一
    assert not rec.get("异常报错")


def test_same_subject_attachment_on_every_row(tmp_path):
    t = FakeTransport()
    t.file_contents[f"https://fake.lark.test/file/a1"] = b"PDF"
    _seed(t, [
        msg("c1", "Confirm charges Nov", body=SAMPLE_BODY),           # 无附件
        msg("i1", "Confirm charges Nov",                              # 同名带发票
            attachments=[att("a1", "Invoice_Nov.pdf")]),
    ])
    svc, t, _ = make_service(tmp_path, t)
    stats = svc.run(incremental=False)
    assert stats.attachments_uploaded == 1
    for r in t.records.values():
        assert r["发票"] == [{"file_token": "ft1"}]   # 附件每行都挂
    # 上传正解参数（bitable_file + app_token）
    assert t.uploads[0]["parent_type"] == "bitable_file"
    assert t.uploads[0]["parent_node"] == "bapp"


def test_attachment_filename_dedupe(tmp_path):
    t = FakeTransport()
    t.file_contents[f"https://fake.lark.test/file/a1"] = b"PDF"
    t.file_contents[f"https://fake.lark.test/file/a2"] = b"PDF"
    _seed(t, [
        msg("c1", "Confirm X", body=SAMPLE_BODY),
        msg("i1", "Confirm X", attachments=[att("a1", "Inv.pdf")]),
        msg("i2", "Confirm X", attachments=[att("a2", "Inv.pdf")]),  # 转发重复
    ])
    svc, t, _ = make_service(tmp_path, t)
    stats = svc.run(incremental=False)
    assert stats.attachments_uploaded == 1


def test_no_tk_rows_still_records_with_error(tmp_path):
    t = FakeTransport()
    _seed(t, [msg("c1", "Please confirm", body="no rows here 55.00")])
    svc, t, _ = make_service(tmp_path, t)
    stats = svc.run(incremental=False)
    assert stats.records_created == 1
    rec = list(t.records.values())[0]
    assert rec["TK号"] == ""
    assert "未解析到TK行" in rec["异常报错"]
    assert "同名邮件无附件" in rec["异常报错"]


def test_amount_missing_keeps_rows_without_alloc(tmp_path):
    t = FakeTransport()
    body = "\n".join([  # 只有 TK 行，无任何金额
        "TSTU0000001        TK-26000001        1",
        "TSTU0000002        TK-26000002        5",
        "TSTU0000003        TK-26000003        1",
        "TSTU0000004        TK-26000004        5",
    ])
    _seed(t, [msg("c1", "Confirm no amount", body=body)])
    svc, t, _ = make_service(tmp_path, t)
    svc.run(incremental=False)
    recs = list(t.records.values())
    assert len(recs) == 4
    for r in recs:
        assert "未识别发票金额" in r["异常报错"]        # 异常每行都写
        assert "发票金额" not in r and "摊分金额" not in r


def test_forwarded_duplicate_with_same_rows_not_duplicated(tmp_path):
    """转发引用原文（TK 行一致）→ 只按一封业务邮件录入，转发封只当附件来源。"""
    t = FakeTransport()
    t.file_contents[f"https://fake.lark.test/file/a1"] = b"PDF"
    _seed(t, [
        msg("c1", "Confirm X", date="1793699000000", body=SAMPLE_BODY),
        msg("f1", "Confirm X", date="1793699900000", body="FYI\n\n" + SAMPLE_BODY,
            attachments=[att("a1", "Inv.pdf")]),
    ])
    svc, t, _ = make_service(tmp_path, t)
    stats = svc.run(incremental=False)
    assert len(t.records) == 4                 # 不因转发翻倍
    assert stats.attachments_uploaded == 1     # 附件仍取到
    for r in t.records.values():
        assert r["发票"] == [{"file_token": "ft1"}]
        assert r["收到邮件日期"] == 1793699000000  # 用最早业务邮件的日期


def test_case_insensitive_confirm_and_idempotent(tmp_path):
    t = FakeTransport()
    _seed(t, [msg("c1", "Please CONFIRM the charges", body=SAMPLE_BODY)])
    svc, t, _ = make_service(tmp_path, t)
    svc.run(incremental=False)
    assert len(t.records) == 4
    svc.run(incremental=True)
    assert len(t.records) == 4            # 幂等：不重复录入


def test_lark_error_message_retried_next_run(tmp_path):
    t = FakeTransport()
    _seed(t, [msg("c1", "Confirm X", body=SAMPLE_BODY)])
    svc, t, state = make_service(tmp_path, t)
    # 拦截 create_record 第一次报错
    original = svc.client.bitable_create_record
    calls = {"n": 0}

    def flaky(fields):
        calls["n"] += 1
        if calls["n"] == 1:
            from lark_mail_sync.lark_client import LarkError
            raise LarkError("bitable.create_record", 400, 99991400, "fake quota")
        return original(fields)

    svc.client.bitable_create_record = flaky
    stats = svc.run(incremental=False)
    assert stats.errors and state.processed == set()  # 未标记成功
    svc.client.bitable_create_record = original
    svc.run(incremental=True)
    assert len(t.records) == 4            # 重试成功


def test_rebuild_state_from_msg_id_column(tmp_path):
    t = FakeTransport()
    # 文本列读回是 [{text}] 结构
    t.records["rec1"] = {"消息ID": [{"text": "m1"}]}
    t.records["rec2"] = {"消息ID": [{"text": "m2"}]}
    svc, t, state = make_service(tmp_path, t)
    n = svc.rebuild_state()
    assert n == 2 and state.processed == {"m1", "m2"}


def test_reset_table_clears_records_and_legacy_fields_only(tmp_path):
    t = FakeTransport()
    t.table_fields = list(LEGACY_FIELDS) + list(t.table_fields)  # 表里混有旧版字段
    t.records["rec1"] = {"邮件主题": "x"}
    _seed(t, [])
    svc, t, _ = make_service(tmp_path, t)
    svc.reset_table()
    assert t.records == {}
    assert set(t.deleted_fields) == set(LEGACY_FIELDS)
    assert "用户自建列" in t.table_fields     # 用户字段不动
    assert "邮件主题" in t.table_fields        # 新口径字段不动


def test_dry_run_no_writes(tmp_path):
    t = FakeTransport()
    t.file_contents[f"https://fake.lark.test/file/a1"] = b"PDF"
    _seed(t, [
        msg("c1", "Confirm X", body=SAMPLE_BODY),
        msg("i1", "Confirm X", attachments=[att("a1", "Inv.pdf")]),
    ])
    svc, t, state = make_service(tmp_path, t, dry_run=True)
    stats = svc.run(incremental=False)
    assert stats.records_created == 0
    assert t.records == {} and t.uploads == []
    assert state.processed == set()
