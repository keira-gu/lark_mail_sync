# -*- coding: utf-8 -*-
"""Lark 开放平台 HTTP 客户端（租户 token）。

端点均经实测/SDK 核实（国际版 open.larksuite.com）：
- 邮件： GET  /mail/v1/user_mailboxes/:id/messages                → 分页返回 message_id 列表
        POST /mail/v1/user_mailboxes/:id/messages/batch_get       → 批量取邮件详情
        GET  /mail/v1/user_mailboxes/:id/messages/:mid/attachments/download_url?attachment_ids=..
        GET  <download_url>                                        → 附件内容（预签名直链）
- 多维表：bitable/v1 fields/records CRUD
- 附件上传正解（2026-11-05 实测）：POST /drive/v1/medias/upload_all
        multipart: file_name / parent_type=bitable_file / parent_node=<bitable app_token> / size / file
        → file_token，写入附件字段 [{"file_token": t}]
        （网传 bitai_file 为错误值；parent_node 传 table_id 会报 1061044）

传输层抽象成可注入的 transport(method, url, headers, json_body, data, timeout) -> (status, bytes)，
便于离线单测。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlencode

from .config import Config

# token 失效类错误码：失效后强制刷新重试一次
_TOKEN_INVALID_CODES = {99991661, 99991663, 99991668}
_SAFE_FILENAME = re.compile(r"[^\w.\-]+")


class LarkError(RuntimeError):
    def __init__(self, op: str, status: int, code: int, msg: str):
        super().__init__(f"[{op}] http={status} code={code} msg={msg}")
        self.op = op
        self.status = status
        self.code = code
        self.msg = msg


Transport = Callable[..., Tuple[int, bytes]]


def default_transport(method: str, url: str, *, headers: Dict[str, str],
                      json_body: Any = None, data: Optional[bytes] = None,
                      timeout: int = 30, stream: bool = False) -> Tuple[int, bytes]:
    import requests

    kwargs: Dict[str, Any] = {"headers": headers, "timeout": timeout}
    if json_body is not None:
        kwargs["json"] = json_body
    elif data is not None:
        kwargs["data"] = data
    resp = requests.request(method, url, **kwargs)
    return resp.status_code, resp.content


def safe_filename(name: str, fallback: str = "attachment") -> str:
    cleaned = _SAFE_FILENAME.sub("_", (name or "").strip()).strip("._")
    return cleaned or fallback


class _TokenCache:
    def __init__(self) -> None:
        self._tokens: Dict[Tuple[str, str], Tuple[str, float]] = {}

    def get(self, cfg: Config, base: str, app_id: str, app_secret: str,
            transport: Transport) -> str:
        key = (app_id, app_secret)
        hit = self._tokens.get(key)
        if hit and hit[1] > time.time():
            return hit[0]
        status, raw = transport(
            "POST", base + "/open-apis/auth/v3/tenant_access_token/internal",
            headers={"Content-Type": "application/json; charset=utf-8"},
            json_body={"app_id": app_id, "app_secret": app_secret},
            timeout=cfg.request_timeout,
        )
        d = json.loads(raw)
        token = d.get("tenant_access_token")
        if not token:
            raise LarkError("tenant_access_token", status, d.get("code", -1), d.get("msg", ""))
        self._tokens[key] = (token, time.time() + 7000)
        return token

    def invalidate(self, app_id: str, app_secret: str) -> None:
        self._tokens.pop((app_id, app_secret), None)


class LarkClient:
    def __init__(self, cfg: Config, transport: Optional[Transport] = None):
        self.cfg = cfg
        self.transport = transport or default_transport
        self._tokens = _TokenCache()

    # ---------- 基础请求 ----------

    def _api(self, op: str, method: str, path: str, *, app: str = "mail",
             json_body: Any = None, query: Optional[Dict[str, Any]] = None,
             data: Optional[bytes] = None, content_type: Optional[str] = None,
             timeout: Optional[int] = None) -> Dict[str, Any]:
        """app="mail" 用主应用凭证，app="bitable" 用多维表凭证（未单配则同主应用）。"""
        app_id, app_secret = self.cfg.app_id, self.cfg.app_secret
        if app == "bitable":
            app_id, app_secret = self.cfg.bitable_app_id, self.cfg.bitable_app_secret
        url = self.cfg.base_url + path
        if query:
            qs = urlencode({k: v for k, v in query.items() if v is not None}, doseq=True)
            url += ("&" if "?" in url else "?") + qs
        headers: Dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        for attempt in (1, 2):
            headers["Authorization"] = "Bearer " + self._tokens.get(
                self.cfg, self.cfg.base_url, app_id, app_secret, self.transport)
            status, raw = self.transport(
                method, url, headers=headers, json_body=json_body, data=data,
                timeout=timeout or self.cfg.request_timeout,
            )
            try:
                d = json.loads(raw)
            except Exception:
                raise LarkError(op, status, -1, raw[:200].decode("utf-8", "replace"))
            if d.get("code") in _TOKEN_INVALID_CODES and attempt == 1:
                self._tokens.invalidate(app_id, app_secret)
                continue
            if d.get("code") != 0:
                raise LarkError(op, status, d.get("code", -1), d.get("msg", ""))
            return d.get("data") or {}
        raise LarkError(op, -1, -1, "unreachable")

    # ---------- 邮件 ----------

    def list_message_ids(self, page_size: int = 20) -> Iterator[str]:
        """按分页枚举邮箱消息 id（新→旧）。folder 未配置时为默认收件箱视图。

        实测（2026-11-05 国际版）：page_size 上限 20，超限报 99992402。
        """
        page_size = min(int(page_size) or 20, 20)
        page_token: Optional[str] = None
        while True:
            data = self._api("mail.list", "GET",
                             f"/open-apis/mail/v1/user_mailboxes/{self.cfg.mailbox_id}/messages",
                             query={"page_size": page_size, "page_token": page_token,
                                    "folder_id": self.cfg.folder_id})
            for mid in data.get("items") or []:
                yield mid
            if not data.get("has_more"):
                return
            page_token = data.get("page_token")
            if not page_token:
                return

    def batch_get_messages(self, message_ids: List[str],
                           chunk: int = 20) -> List[Dict[str, Any]]:
        """批量取邮件详情。实测（2026-11-05 国际版）message_ids 每批上限 20，
        超限报 99992402 field validation failed。"""
        out: List[Dict[str, Any]] = []
        for i in range(0, len(message_ids), chunk):
            part = message_ids[i:i + chunk]
            data = self._api(
                "mail.batch_get", "POST",
                f"/open-apis/mail/v1/user_mailboxes/{self.cfg.mailbox_id}/messages/batch_get",
                json_body={"format": self.cfg.message_format, "message_ids": part})
            out.extend(data.get("messages") or [])
        return out

    def attachment_download_urls(self, message_id: str,
                                 attachment_ids: List[str]) -> Dict[str, str]:
        qs = "&".join(f"attachment_ids={a}" for a in attachment_ids)
        data = self._api(
            "mail.attachment_url", "GET",
            f"/open-apis/mail/v1/user_mailboxes/{self.cfg.mailbox_id}/messages/{message_id}"
            f"/attachments/download_url?{qs}")
        return {item["attachment_id"]: item["download_url"]
                for item in data.get("download_urls") or []
                if item.get("attachment_id") and item.get("download_url")}

    def download_file(self, url: str, max_bytes: int) -> bytes:
        status, raw = self.transport("GET", url, headers={}, timeout=self.cfg.request_timeout)
        if status != 200:
            raise LarkError("mail.attachment_download", status, -1,
                            raw[:200].decode("utf-8", "replace"))
        if len(raw) > max_bytes:
            raise LarkError("mail.attachment_download", status, -1,
                            f"file too large: {len(raw)} > {max_bytes}")
        return raw

    def list_folders(self) -> List[Dict[str, Any]]:
        """枚举邮箱文件夹。实测（2026-11-06 国际版）系统文件夹 id 为字面量：
        INBOX / ARCHIVED / SENT / TRASH / DRAFT / SPAM。"""
        data = self._api("mail.folders", "GET",
                         f"/open-apis/mail/v1/user_mailboxes/{self.cfg.mailbox_id}/folders")
        return data.get("items") or []

    def modify_message_folder(self, message_id: str, folder_id: str) -> None:
        """移动邮件到指定文件夹（归档）。实测（2026-11-06 国际版，可逆）：
        PUT .../messages/:mid/modify，body {"add_folder": "<folder_id>"}；
        空 body 报 1230001（参数校验先于权限检查，勿据此误判 scope）；
        归档往返 ARCHIVED↔INBOX 均验证通过，readonly 应用同样可调。"""
        self._api("mail.modify_message", "PUT",
                  f"/open-apis/mail/v1/user_mailboxes/{self.cfg.mailbox_id}"
                  f"/messages/{message_id}/modify",
                  json_body={"add_folder": folder_id})

    # ---------- 多维表 ----------

    def bitable_fields(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        while True:
            data = self._api("bitable.fields", "GET",
                             f"/open-apis/bitable/v1/apps/{self.cfg.bitable_app_token}"
                             f"/tables/{self.cfg.bitable_table_id}/fields",
                             app="bitable",
                             query={"page_size": 100, "page_token": page_token})
            items.extend(data.get("items") or [])
            if not data.get("has_more") or not data.get("page_token"):
                return items
            page_token = data["page_token"]

    def bitable_create_field(self, name: str, field_type: int,
                             property_: Optional[Dict[str, Any]] = None) -> None:
        body: Dict[str, Any] = {"field_name": name, "type": field_type}
        if property_:
            body["property"] = property_
        self._api("bitable.create_field", "POST",
                  f"/open-apis/bitable/v1/apps/{self.cfg.bitable_app_token}"
                  f"/tables/{self.cfg.bitable_table_id}/fields",
                  app="bitable", json_body=body)

    def bitable_delete_field(self, field_id: str) -> None:
        self._api("bitable.delete_field", "DELETE",
                  f"/open-apis/bitable/v1/apps/{self.cfg.bitable_app_token}"
                  f"/tables/{self.cfg.bitable_table_id}/fields/{field_id}",
                  app="bitable")

    def bitable_delete_records(self, record_ids: List[str]) -> None:
        """逐条删除。实测（2026-11-05 国际版）：batch_delete 的 DELETE body
        会被网关丢弃→服务端视作空列表报 1254043 RecordIdNotFound；
        无 body 的单条 DELETE /records/:id 正常。"""
        for rid in record_ids:
            self._api("bitable.delete_record", "DELETE",
                      f"/open-apis/bitable/v1/apps/{self.cfg.bitable_app_token}"
                      f"/tables/{self.cfg.bitable_table_id}/records/{rid}",
                      app="bitable")

    def bitable_list_records(self, field_names: Optional[List[str]] = None,
                             page_size: int = 500) -> Iterator[Dict[str, Any]]:
        page_token: Optional[str] = None
        while True:
            data = self._api(
                "bitable.records", "GET",
                f"/open-apis/bitable/v1/apps/{self.cfg.bitable_app_token}"
                f"/tables/{self.cfg.bitable_table_id}/records",
                app="bitable",
                query={"page_size": page_size, "page_token": page_token,
                       "field_names": json.dumps(field_names, ensure_ascii=False)
                       if field_names else None})
            for rec in data.get("items") or []:
                yield rec
            if not data.get("has_more") or not data.get("page_token"):
                return
            page_token = data["page_token"]

    def bitable_create_record(self, fields: Dict[str, Any]) -> str:
        data = self._api(
            "bitable.create_record", "POST",
            f"/open-apis/bitable/v1/apps/{self.cfg.bitable_app_token}"
            f"/tables/{self.cfg.bitable_table_id}/records",
            app="bitable", json_body={"fields": fields})
        rec = (data.get("record") or {})
        return rec.get("record_id", "")

    def bitable_update_record(self, record_id: str, fields: Dict[str, Any]) -> None:
        self._api(
            "bitable.update_record", "PUT",
            f"/open-apis/bitable/v1/apps/{self.cfg.bitable_app_token}"
            f"/tables/{self.cfg.bitable_table_id}/records/{record_id}",
            app="bitable", json_body={"fields": fields})

    # ---------- 附件上传（drive 通用上传，parent_type=bitable_file 实测正解） ----------

    def upload_attachment(self, filename: str, content: bytes) -> str:
        boundary = uuid.uuid4().hex
        parts: List[bytes] = []
        for k, v in {
            "file_name": filename,
            "parent_type": "bitable_file",
            "parent_node": self.cfg.bitable_app_token,
            "size": str(len(content)),
        }.items():
            parts.append(
                (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n"
                 f"\r\n{v}\r\n").encode())
        parts.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
             ).encode() + content + b"\r\n")
        parts.append((f"--{boundary}--\r\n").encode())
        data = b"".join(parts)
        resp = self._api(
            "bitable.upload_attachment", "POST",
            "/open-apis/drive/v1/medias/upload_all",
            app="bitable", data=data,
            content_type="multipart/form-data; boundary=" + boundary)
        token = (resp or {}).get("file_token")
        if not token:
            raise LarkError("bitable.upload_attachment", 200, -1, "no file_token in response")
        return token
