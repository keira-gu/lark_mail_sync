# -*- coding: utf-8 -*-
"""同步引擎（confirm-TK 口径）：

INBOX → 主题或正文（base64 解码后）含 confirm（大小写不敏感）→
解析正文 TK 行与发票金额 → 同名邮件（含本封）附件下载上传 →
按 TK 逐行写入多维表，金额按数量摊分。
解析失败不丢邮件：TK 行为空也落一行，问题写进「异常报错」列。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .config import (
    Config, FIELD_TYPE_ATTACHMENT, FIELD_TYPE_DATETIME, FIELD_TYPE_NUMBER, FIELD_TYPE_TEXT,
)
from .invoice import date_to_ms, extract_invoice_info
from .lark_client import LarkClient, LarkError, safe_filename
from .parser import allocate, body_text, is_confirm, parse_confirm_email, parse_tk_rows
from .state import SyncState

Log = Callable[[str], None]

# 旧版通用聚合模式的字段（reset-table 时清理）
LEGACY_FIELDS = ["主题", "邮件数", "最新邮件时间", "发件人", "附件数", "附件", "消息IDs"]


def to_ms(raw: Any) -> Optional[int]:
    """internal_date 实测为字符串毫秒；防御秒级时间戳。"""
    try:
        v = int(str(raw)) if raw not in (None, "") else 0
    except (TypeError, ValueError):
        return None
    if not v:
        return None
    return v * 1000 if v < 10 ** 11 else v


@dataclass
class SyncStats:
    scanned: int = 0
    confirm_emails: int = 0
    records_created: int = 0
    attachments_uploaded: int = 0
    attachments_deduped: int = 0
    parse_failures: int = 0
    errors: List[str] = field(default_factory=list)

    def line(self) -> str:
        return (f"scanned={self.scanned} confirm={self.confirm_emails} "
                f"created={self.records_created} att_uploaded={self.attachments_uploaded} "
                f"att_dedup={self.attachments_deduped} parse_fail={self.parse_failures} "
                f"errors={len(self.errors)}")


class MailSyncService:
    def __init__(self, cfg: Config, client: LarkClient, state: SyncState,
                 log: Log = print, dry_run: bool = False,
                 now: Callable[[], float] = time.time):
        self.cfg = cfg
        self.client = client
        self.state = state
        self.log = log
        self.dry_run = dry_run
        self.now = now

    # ---------- 扫描 ----------

    def collect_messages(self, max_pages: Optional[int] = None,
                         stop_after_seen: Optional[int] = None) -> List[Dict[str, Any]]:
        ids: List[str] = []
        seen_run = 0
        for mid in self.client.list_message_ids():
            ids.append(mid)
            if self.state.is_processed(mid):
                seen_run += 1
            else:
                seen_run = 0
            if len(ids) % 20 == 0:  # list 接口 page_size 上限 20
                if max_pages and len(ids) // 20 >= max_pages:
                    break
            if (stop_after_seen is not None and seen_run >= stop_after_seen
                    and any(not self.state.is_processed(i) for i in ids)):
                break
        return self.client.batch_get_messages(ids) if ids else []

    # ---------- 字段管理 ----------

    def ensure_fields(self, create: bool = False) -> List[str]:
        existing = {f.get("field_name") for f in self.client.bitable_fields()}
        missing = [name for name, _, _ in self.cfg.field_specs() if name not in existing]
        if missing and create and not self.dry_run:
            for name, ftype, fmt in self.cfg.field_specs():
                if name in missing:
                    self.client.bitable_create_field(name, ftype, property_={"formatter": fmt} if fmt else None)
                    self.log(f"已创建字段 {name} (type={ftype})")
            missing = []
        return missing

    def reset_table(self) -> None:
        """清空全部记录 + 删除旧版聚合字段（不动用户自建字段）。"""
        rids = [rec.get("record_id") for rec in self.client.bitable_list_records()]
        rids = [r for r in rids if r]
        if rids:
            self.client.bitable_delete_records(rids)
            self.log(f"已删除 {len(rids)} 条记录")
        existing = {f.get("field_name"): f.get("field_id")
                    for f in self.client.bitable_fields()}
        removed = 0
        for name in LEGACY_FIELDS:
            fid = existing.get(name)
            if fid:
                self.client.bitable_delete_field(fid)
                removed += 1
        self.log(f"已删除旧字段 {removed} 个")

    # ---------- 状态重建 ----------

    def rebuild_state(self) -> int:
        """按表内「消息ID」列反向重建 processed 游标。"""
        count = 0
        for rec in self.client.bitable_list_records(
                field_names=[self.cfg.field_msg_id]):
            raw = (rec.get("fields") or {}).get(self.cfg.field_msg_id)
            if isinstance(raw, list):  # 文本字段读回 [{text},...]
                raw = "".join(seg.get("text", "") for seg in raw if isinstance(seg, dict))
            mid = str(raw or "").strip()
            if mid:
                self.state.mark_processed(mid)
                count += 1
        self.state.save()
        return count

    # ---------- 归档（把处理完的邮件移出收件箱，2026-11-06 实测可用） ----------

    def _table_message_ids(self) -> set:
        """表内「消息ID」列全集 = 已写入多维表的主题组标记。"""
        out = set()
        for rec in self.client.bitable_list_records(
                field_names=[self.cfg.field_msg_id]):
            raw = (rec.get("fields") or {}).get(self.cfg.field_msg_id)
            if isinstance(raw, list):
                raw = "".join(seg.get("text", "") for seg in raw if isinstance(seg, dict))
            mid = str(raw or "").strip()
            if mid:
                out.add(mid)
        return out

    def archive_processed(self, max_pages: Optional[int] = None) -> Dict[str, Any]:
        """把「已处理完毕」的收件箱邮件移入归档文件夹（默认 ARCHIVED）。

        安全规则（宁可漏归，绝不误动）：
        1. 只动收件箱当前可见的邮件；
        2. 只动本地游标 state.processed 已含的邮件；
        3. 只动「所在主题组已写入多维表」的组——组内任一消息ID 在表内才算处理完；
           组没入表 = 还没录完或解析失败待人工，以及与本工具无关的普通邮件，一律不动。
        归档不影响去重：表和游标都不变，rebuild-state 依旧以表内消息ID 为准。
        """
        table_mids = self._table_message_ids()
        pool = self.collect_messages(max_pages=max_pages)
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for m in pool:
            groups.setdefault(m.get("subject") or "", []).append(m)
        targets: List[Dict[str, Any]] = []
        for subject_msgs in groups.values():
            if not any(m.get("message_id") in table_mids for m in subject_msgs):
                continue  # 组未入表：未处理完 / 未识别待人工 / 与本工具无关
            targets.extend(m for m in subject_msgs
                           if self.state.is_processed(m.get("message_id", "")))
        errors: List[str] = []
        moved = 0
        for m in targets:
            subject = (m.get("subject") or "")[:40]
            if self.dry_run:
                self.log(f"[dry-run] archive 「{subject}」")
                continue
            try:
                self.client.modify_message_folder(m["message_id"], self.cfg.archive_folder)
                moved += 1
            except LarkError as e:
                errors.append(f"{subject}: {e}")
                self.log(f"!! archive 「{subject}」失败：{e}")
        return {"scanned": len(pool), "targets": len(targets), "moved": moved,
                "errors": errors}

    # ---------- 主流程 ----------

    def run(self, incremental: bool, max_pages: Optional[int] = None) -> SyncStats:
        stats = SyncStats()
        stop_after = self.cfg.scan_stop_after_seen if incremental else None
        pool = self.collect_messages(max_pages=max_pages, stop_after_seen=stop_after)
        stats.scanned = len(pool)
        confirms = [m for m in pool
                    if not self.state.is_processed(m.get("message_id", ""))
                    and is_confirm(m.get("subject", ""), body_text(m),
                                   self.cfg.confirm_keyword)]
        stats.confirm_emails = len(confirms)
        confirms.sort(key=lambda m: str(m.get("internal_date", "")))  # 旧→新
        if not confirms:
            self.log("无待处理 confirm 邮件。")
            if not self.dry_run:
                self.state.last_sync_at = self.now()
                self.state.save()
            return stats

        # 同主题分组：有 TK 行的=业务邮件；其余（纯附件转发/引用原文的重复转发）
        # 只当附件来源，不重复录入；TK 行完全一致的后续邮件视为重复转发跳过；
        # 整组都没有 TK 行时，取最早一封落异常行（保证异常可见）。
        business: List[Dict[str, Any]] = []
        carriers: List[str] = []
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for m in confirms:
            groups.setdefault(m.get("subject") or "", []).append(m)
        for subject_msgs in groups.values():
            parsed_map = {m["message_id"]: parse_confirm_email(m) for m in subject_msgs}
            seen_rows: List[tuple] = []
            biz, carry = [], []
            for m in subject_msgs:  # 已按时间旧→新
                rows = tuple(parsed_map[m["message_id"]].tk_rows)
                if rows and rows not in seen_rows:
                    seen_rows.append(rows)
                    biz.append((m, parsed_map[m["message_id"]]))
                else:
                    carry.append(m["message_id"])
            if biz:
                business.extend(biz)
                carriers.extend(carry)
            else:
                m0 = subject_msgs[0]
                business.append((m0, parsed_map[m0["message_id"]]))

        for m, parsed in business:
            try:
                self._process_confirm(m, parsed, pool, stats)
            except LarkError as e:
                stats.errors.append(f"{(m.get('subject') or '')[:40]}: {e}")
                self.log(f"!! 「{(m.get('subject') or '')[:40]}」失败：{e}")
                continue  # 不 mark_processed，下轮重试
            if not self.dry_run:
                self.state.mark_processed(m["message_id"])
                self.state.save()

        if not self.dry_run:
            for mid in carriers:
                self.state.mark_processed(mid)
            self.state.last_sync_at = self.now()
            self.state.save()
        self.log(stats.line())
        return stats

    def _process_confirm(self, m: Dict[str, Any], parsed, pool: List[Dict[str, Any]],
                         stats: SyncStats) -> None:
        cfg = self.cfg
        if parsed.errors:
            stats.parse_failures += 1

        # 附件：同名邮件（含本封）非内嵌附件，按文件名去重
        att_errors: List[str] = []
        candidates = self._attachment_candidates(m, pool)
        if not candidates:
            att_errors.append("同名邮件无附件")
        tokens: List[str] = []
        invoice_info: Dict[str, str] = {}
        if not self.dry_run:
            for cand in candidates:
                urls = self.client.attachment_download_urls(
                    cand["message_id"], [cand["att"]["id"]])
                url = urls.get(cand["att"]["id"])
                if not url:
                    att_errors.append(f"附件 {cand['filename']} 无下载链接")
                    continue
                try:
                    content = self.client.download_file(url, self.cfg.attachment_max_bytes)
                    tokens.append(self.client.upload_attachment(cand["filename"], content))
                    stats.attachments_uploaded += 1
                    if not invoice_info:  # 用首个 PDF 识别发票字段
                        invoice_info = extract_invoice_info(content)
                except LarkError as e:
                    att_errors.append(f"附件 {cand['filename']} 失败: {e}")
        errors = parsed.errors + att_errors

        # 发票金额：邮件正文优先，缺失时回退发票 PDF（Balance Due/Total）
        amount = parsed.amount
        if amount is None and invoice_info.get("amount"):
            try:
                amount = float(str(invoice_info["amount"]).replace(",", ""))
            except ValueError:
                pass
        if amount is not None:
            errors = [e for e in errors if e != "未识别发票金额"]

        rows = allocate(amount, parsed.tk_rows) if amount else \
            [(tk, q, None) for tk, q in parsed.tk_rows]

        to_list = m.get("to") or []
        recipient = (to_list[0] or {}).get("mail_address", "") if to_list else ""
        date_ms = to_ms(m.get("internal_date"))
        subject = m.get("subject", "")
        mid = m["message_id"]

        if not rows:  # 无 TK 行也要落一行让用户看见异常
            rows = [("", 0, None)]

        # 邮件级+发票级字段写到每一行（全量冗余，便于按行筛选/导出）。
        # 消息ID 每行保留（视图隐藏），保证 rebuild-state 删行后仍可重建。
        shared: Dict[str, Any] = {
            cfg.field_subject: subject,
            cfg.field_recipient: recipient,
        }
        if date_ms is not None:
            shared[cfg.field_date] = date_ms
        if amount is not None:
            shared[cfg.field_invoice_amount] = amount
        if tokens:
            shared[cfg.field_invoice_file] = [{"file_token": t} for t in tokens]
        if errors:
            shared[cfg.field_error] = "; ".join(errors)
        # 发票 PDF 识别的字段（识别不到则留空不报错）
        if invoice_info.get("vendor"):
            shared[cfg.field_vendor] = invoice_info["vendor"]
        if invoice_info.get("our_company"):
            shared[cfg.field_our_company] = invoice_info["our_company"]
        if invoice_info.get("currency"):
            shared[cfg.field_currency] = invoice_info["currency"]
        # 发票名称与「发票」列附件文件名统一；发票号单独一列从 PDF 识别
        if candidates:
            shared[cfg.field_invoice_name] = candidates[0]["filename"]
        if invoice_info.get("invoice_no"):
            shared[cfg.field_invoice_no] = invoice_info["invoice_no"]
        if invoice_info.get("issue_date"):
            ms = date_to_ms(invoice_info["issue_date"])
            if ms:
                shared[cfg.field_invoice_date] = ms
        if invoice_info.get("due_date"):
            ms = date_to_ms(invoice_info["due_date"])
            if ms:
                shared[cfg.field_invoice_due] = ms

        for tk, qty, alloc in rows:
            fields: Dict[str, Any] = {
                cfg.field_tk: tk,
                cfg.field_qty: qty,
                cfg.field_msg_id: mid,
            }
            if alloc is not None:
                fields[cfg.field_alloc_amount] = alloc
            fields.update(shared)
            if self.dry_run:
                self.log(f"[dry-run] create 「{subject[:40]}」 tk={tk} qty={qty} "
                         f"alloc={alloc} att={len(candidates)} err={errors or '无'}")
            else:
                self.client.bitable_create_record(fields)
                stats.records_created += 1

    # ---------- 同名邮件附件 ----------

    def _attachment_candidates(self, m: Dict[str, Any], pool: List[Dict[str, Any]]
                               ) -> List[Dict[str, Any]]:
        """同名（主题全等）邮件的非内嵌附件，按文件名去重（同名转发常见）。"""
        subject = m.get("subject") or ""
        seen_names = set()
        out: List[Dict[str, Any]] = []
        for x in pool:
            if (x.get("subject") or "") != subject:
                continue
            for a in x.get("attachments") or []:
                if a.get("is_inline") or not a.get("id"):
                    continue
                name = safe_filename(str(a.get("filename", "")), a["id"])
                if name in seen_names:
                    continue
                seen_names.add(name)
                out.append({"message_id": x["message_id"], "att": a, "filename": name})
        return out
