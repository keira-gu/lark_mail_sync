# -*- coding: utf-8 -*-
"""离线测试夹具：FakeTransport 打桩全部 Lark HTTP 端点（confirm-TK 口径）。"""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lark_mail_sync.config import Config  # noqa: E402
from lark_mail_sync.lark_client import LarkClient  # noqa: E402
from lark_mail_sync.state import SyncState  # noqa: E402
from lark_mail_sync.sync import MailSyncService  # noqa: E402

BASE = "https://fake.lark.test"

NEW_FIELDS = ["收到邮件日期", "邮件主题", "收件人邮箱号", "TK号", "数量",
              "摊分金额", "发票金额", "发票", "异常报错", "费用项-结算手动提供", "消息ID"]
LEGACY_FIELDS = ["主题", "邮件数", "最新邮件时间", "发件人", "附件数", "附件", "消息IDs"]


class FakeTransport:
    def __init__(self) -> None:
        self.pages: List[List[str]] = [[]]
        self.messages: Dict[str, Dict[str, Any]] = {}
        self.records: Dict[str, Dict[str, Any]] = {}
        self.table_fields: List[str] = list(NEW_FIELDS) + ["用户自建列"]
        self.next_rid = 1000
        self.next_ftoken = 1
        self.uploads: List[Dict[str, str]] = []
        self.file_contents: Dict[str, bytes] = {}
        self.deleted_fields: List[str] = []
        self.archive_ops: List[Dict[str, Any]] = []  # PUT modify 记录（归档测试用）
        self.fail_modify_code: Optional[int] = None  # 置为错误码可模拟归档失败
        self.log: List[str] = []

    def __call__(self, method: str, url: str, *, headers: Any = None,
                 json_body: Any = None, data: Optional[bytes] = None,
                 timeout: int = 30, stream: bool = False) -> Tuple[int, bytes]:
        self.log.append(f"{method} {url}")
        body: Dict[str, Any] = {"code": 0, "msg": "ok"}

        if "tenant_access_token" in url:
            body = {"code": 0, "tenant_access_token": "t-fake", "expire_in": 7000}

        elif method == "PUT" and "/modify" in url:
            if self.fail_modify_code:
                body = {"code": self.fail_modify_code, "msg": "simulated failure"}
            else:
                mid = url.split("/messages/", 1)[1].rsplit("/modify", 1)[0]
                self.archive_ops.append({"message_id": mid, "body": json_body})
                body = {"code": 0, "data": {}}

        elif "/folders" in url:
            body = {"code": 0, "data": {"items": [
                {"id": "INBOX", "name": "Inbox", "folder_type": 1},
                {"id": "ARCHIVED", "name": "Archived", "folder_type": 1},
                {"id": "TRASH", "name": "Trash", "folder_type": 1}]}}

        elif method == "GET" and "/messages" in url and "download_url" not in url:
            page_token = _qs(url).get("page_token", [None])[0]
            idx = int(page_token) if page_token else 0
            ids = self.pages[idx] if idx < len(self.pages) else []
            has_more = idx + 1 < len(self.pages)
            body = {"code": 0, "data": {
                "items": ids, "has_more": has_more,
                "page_token": str(idx + 1) if has_more else ""}}

        elif "batch_get" in url:
            mids = (json_body or {}).get("message_ids", [])
            body = {"code": 0, "data": {
                "messages": [self.messages[m] for m in mids if m in self.messages]}}

        elif "download_url" in url:
            att_ids = _qs(url).get("attachment_ids", [])
            body = {"code": 0, "data": {"download_urls": [
                {"attachment_id": a, "download_url": f"{BASE}/file/{a}"} for a in att_ids]}}

        elif url.startswith(f"{BASE}/file/"):
            return 200, self.file_contents.get(url, b"")

        elif method == "GET" and "/fields" in url:
            body = {"code": 0, "data": {
                "items": [{"field_name": n, "field_id": f"fld_{n}", "type": 1}
                          for n in self.table_fields], "has_more": False}}

        elif method == "DELETE" and "/fields/" in url:
            fid = url.rsplit("/", 1)[-1]
            name = fid.replace("fld_", "")
            self.deleted_fields.append(name)
            self.table_fields = [f for f in self.table_fields if f != name]

        elif method == "GET" and "/records" in url:
            body = {"code": 0, "data": {
                "items": [{"record_id": rid, "fields": f}
                          for rid, f in self.records.items()], "has_more": False}}

        elif method == "POST" and "/records" in url:
            rid = f"rec{self.next_rid}"
            self.next_rid += 1
            self.records[rid] = (json_body or {}).get("fields", {})
            body = {"code": 0, "data": {"record": {"record_id": rid}}}

        elif method == "DELETE" and "/records/" in url:
            self.records.pop(url.rsplit("/", 1)[-1], None)

        elif "upload_all" in url:
            self.uploads.append(_parse_multipart(headers, data))
            token = f"ft{self.next_ftoken}"
            self.next_ftoken += 1
            body = {"code": 0, "data": {"file_token": token}}

        return 200, json.dumps(body).encode()


def _qs(url: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    if "?" not in url:
        return out
    for pair in url.split("?", 1)[1].split("&"):
        k, _, v = pair.partition("=")
        out.setdefault(k, []).append(v)
    return out


def _parse_multipart(headers: Any, data: Optional[bytes]) -> Dict[str, str]:
    ct = (headers or {}).get("Content-Type", "")
    boundary = re.search(r"boundary=(\S+)", ct).group(1)
    text = data.decode("utf-8", "replace")
    fields: Dict[str, str] = {}
    for part in text.split("--" + boundary):
        name_m = re.search(r'name="([^"]+)"', part)
        body_m = re.search(r"\r\n\r\n(.*)\r\n$", part, re.S)
        if not (name_m and body_m):
            continue
        file_m = re.search(r'filename="([^"]+)"', part)
        fields[name_m.group(1)] = file_m.group(1) if file_m else body_m.group(1)
    return fields


def make_service(tmp_path: Any, transport: Optional[FakeTransport] = None,
                 env_extra: Optional[Dict[str, str]] = None,
                 dry_run: bool = False) -> Tuple[MailSyncService, FakeTransport, SyncState]:
    transport = transport or FakeTransport()
    env = {
        "LARK_BASE_URL": BASE,
        "LARK_APP_ID": "ai", "LARK_APP_SECRET": "as",
        "MAIL_USER_MAILBOX_ID": "box@test",
        "MAIL_FOLDER_ID": "INBOX",
        "BITABLE_APP_TOKEN": "bapp", "BITABLE_TABLE_ID": "tbl",
        "STATE_PATH": str(tmp_path / "state.json"),
    }
    env.update(env_extra or {})
    cfg = Config.from_env(env)
    client = LarkClient(cfg, transport=transport)
    state = SyncState(cfg.state_path)
    svc = MailSyncService(cfg, client, state, log=lambda m: None, dry_run=dry_run)
    return svc, transport, state


SAMPLE_BODY = """Please confirm the following:

TSTU0000001        TK-26000001        1
TSTU0000002        TK-26000002        5
TSTU0000003        TK-26000003        1
TSTU0000004        TK-26000004        5

Total: 1,200.00 CAD
Thanks."""


def msg(mid: str, subject: str, *, date: Any = "1793699500000", to: str = "ops@test",
        body: str = "", attachments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "message_id": mid, "subject": subject, "internal_date": date,
        "head_from": {"mail_address": f"{mid}@t.c", "name": ""},
        "to": [{"mail_address": to, "name": ""}],
        "body_plain_text": body,
        "attachments": attachments or [],
    }


def att(aid: str, filename: str, inline: bool = False) -> Dict[str, Any]:
    return {"id": aid, "filename": filename, "is_inline": inline}


# ===== 2026-11-05 真实邮件形状：整段 URL-safe base64、换行全丢、NBSP(\xa0)分列、
# confirm 在正文不在主题、金额藏在引用的发票链里 =====
GAP = "      "  # 实测列分隔：NBSP 前后带普通空格


REAL_CONFIRM_BODY = (
    "——— SETTLEMENT E-mail: settlement@example.com "
    "Confirmed. TSTU0000013" + GAP + "TK-26000013" + GAP + "13 Thank you "
    "Best regards, Li Lei From: A B C Logistics Subject: Invoice 1002 "
    "INVOICE 1002 DETAILS A B C Logistics DUE 11/04/2026 Total CAD 260.00 "
    "Balance due CAD 260.00"
)
REAL_CONFIRM_B64 = base64.urlsafe_b64encode(REAL_CONFIRM_BODY.encode("utf-8")).decode("ascii")
