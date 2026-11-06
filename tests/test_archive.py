# -*- coding: utf-8 -*-
"""归档子命令安全规则测试（FakeTransport，零网络）。

规则：只动「收件箱可见 ∧ state.processed 已含 ∧ 所在主题组已写入表」的邮件；
未处理完的组、与本工具无关的邮件一律不动。
"""
from conftest import FakeTransport, att, make_service, msg


def _seed(t: FakeTransport) -> None:
    """五类邮件：
    - 组A（Invoice 1）：业务邮件 mA（已入表+processed）+ 附件转发 mF（processed）
    - 组B（Invoice 2）：新 confirm mB（未处理、未入表）→ 不动
    - 组C（Invoice 3）：mC 已 processed 但组未入表（异常待人工）→ 不动
    - 组D（Invoice 4）：业务邮件 mD 已入表+processed，无转发 → 归档
    - 噪音 mN：普通邮件未 processed → 不动
    """
    mails = [
        msg("mA", "Fw: Invoice 1 from ABC", body="Confirmed.", attachments=[att("a1", "p1.pdf")]),
        msg("mF", "Fw: Invoice 1 from ABC", body="see attached", attachments=[att("a2", "p1.pdf")]),
        msg("mB", "Fw: Invoice 2 from ABC", body="Confirmed."),
        msg("mC", "Fw: Invoice 3 from ABC", body="Confirmed."),
        msg("mD", "Fw: Invoice 4 from ABC", body="Confirmed.", attachments=[att("a3", "p4.pdf")]),
        msg("mN", "Weekly report", body="hello world"),
    ]
    t.pages = [[m["message_id"] for m in mails]]
    t.messages = {m["message_id"]: m for m in mails}
    t.records["recA"] = {"消息ID": [{"text": "mA"}]}
    t.records["recD"] = {"消息ID": [{"text": "mD"}]}


def _prepare(tmp_path, env_extra=None, dry_run=False):
    t = FakeTransport()
    _seed(t)
    svc, _, _t = make_service(tmp_path, t, env_extra=env_extra, dry_run=dry_run)
    for mid in ("mA", "mF", "mC", "mD"):  # mB 未处理、mN 噪音
        svc.state.mark_processed(mid)
    return svc, t


def test_archive_only_touched_done_groups(tmp_path):
    svc, t = _prepare(tmp_path)
    r = svc.archive_processed()
    assert r["scanned"] == 6
    assert r["targets"] == 3  # mA + mF + mD
    assert r["moved"] == 3 and not r["errors"]
    assert {op["message_id"] for op in t.archive_ops} == {"mA", "mF", "mD"}
    assert all(op["body"] == {"add_folder": "ARCHIVED"} for op in t.archive_ops)


def test_archive_dry_run_moves_nothing(tmp_path):
    svc, t = _prepare(tmp_path, dry_run=True)
    r = svc.archive_processed()
    assert r["targets"] == 3 and r["moved"] == 0
    assert t.archive_ops == []


def test_archive_custom_folder_config(tmp_path):
    svc, t = _prepare(tmp_path, env_extra={"ARCHIVE_FOLDER_ID": "TRASH"})
    r = svc.archive_processed()
    assert r["moved"] == 3
    assert all(op["body"] == {"add_folder": "TRASH"} for op in t.archive_ops)


def test_archive_failure_reports_and_continues(tmp_path):
    svc, t = _prepare(tmp_path)
    t.fail_modify_code = 99991672
    r = svc.archive_processed()
    assert r["targets"] == 3 and r["moved"] == 0
    assert len(r["errors"]) == 3  # 每封独立报错，不中断


def test_archive_unprocessed_not_in_state_untouched(tmp_path):
    """整表为空（还没写过任何记录）时：即使 state 里有历史，也一封都不动。"""
    svc, t = _prepare(tmp_path)
    t.records.clear()
    r = svc.archive_processed()
    assert r["targets"] == 0 and r["moved"] == 0
    assert t.archive_ops == []
