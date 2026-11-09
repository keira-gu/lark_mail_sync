# -*- coding: utf-8 -*-
"""计划与防重状态：读多维表 → 待录清单（纯逻辑，可离线测试）。

Playwright 提交在 submit.py；本模块不 import playwright。"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Set, Tuple


@dataclass(frozen=True)
class ApRow:
    """一条待建立应付的多维表行（= 一个 TK 一笔）。"""
    record_id: str
    invoice_no: str
    tk: str
    vendor: str
    currency: str
    amount: float          # 摊分金额（单价）
    invoice_date: Optional[date]
    due_date: Optional[date]
    error: str = ""
    subject: str = ""

    @property
    def key(self) -> str:
        return f"{self.invoice_no}|{self.tk}"


def txt(v) -> str:
    """bitable 文本列读回 [{text}]，数字列读回字符串。"""
    if isinstance(v, list):
        return "".join(s.get("text", "") for s in v if isinstance(s, dict))
    if v is None:
        return ""
    return v if isinstance(v, str) else str(v)


def ms_to_date(v) -> Optional[date]:
    """bitable 日期列读回毫秒时间戳（写入端 invoice.date_to_ms 是 UTC 零点）。"""
    if v in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(v) / 1000, tz=timezone.utc).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def parse_vendor_map(raw: str) -> Dict[str, Dict[str, str]]:
    """GOFREIGHT_VENDOR_MAP JSON：{"ABC Logistics": {"search": "ABC", "code": "VN-000001"}}

    search 必须是无空格短词（ng-select 服务端搜索：带空格会关掉候选下拉）。"""
    raw = (raw or "").strip()
    if not raw:
        return {}
    d = json.loads(raw)
    if not isinstance(d, dict):
        raise ValueError("GOFREIGHT_VENDOR_MAP 必须是 {服务商: {search, code}} JSON")
    out: Dict[str, Dict[str, str]] = {}
    for name, spec in d.items():
        search = str((spec or {}).get("search", "")).strip()
        code = str((spec or {}).get("code", "")).strip()
        if not search or not code:
            raise ValueError(f"供应商映射不完整：{name} 需要 search 和 code")
        if " " in search:
            raise ValueError(f"供应商搜索词不能含空格（GoFreight 下拉限制）：{name} {search!r}")
        out[str(name).strip()] = {"search": search, "code": code}
    return out


def rows_from_bitable(client, cfg) -> List[ApRow]:
    """读全表 → ApRow 列表（只读不写；client 只需 bitable_list_records）。"""
    rows: List[ApRow] = []
    for rec in client.bitable_list_records():
        f = rec.get("fields", {})
        try:
            amount = float(txt(f.get(cfg.field_alloc_amount)) or 0)
        except ValueError:
            amount = 0.0
        rows.append(ApRow(
            record_id=rec.get("record_id", ""),
            invoice_no=txt(f.get(cfg.field_invoice_no)).strip(),
            tk=txt(f.get(cfg.field_tk)).strip(),
            vendor=txt(f.get(cfg.field_vendor)).strip(),
            currency=txt(f.get(cfg.field_currency)).strip() or "CAD",
            amount=amount,
            invoice_date=ms_to_date(f.get(cfg.field_invoice_date)),
            due_date=ms_to_date(f.get(cfg.field_invoice_due)),
            error=txt(f.get(cfg.field_error)).strip(),
            subject=txt(f.get(cfg.field_subject)).strip(),
        ))
    return rows


def build_plan(
    rows: Sequence[ApRow],
    submitted: Set[str],
    vendor_map: Dict[str, Dict[str, str]],
    due_fallback_days: int = 0,
    invoices: Optional[Set[str]] = None,
    tks: Optional[Set[str]] = None,
) -> Tuple[List[ApRow], List[Tuple[str, str]]]:
    """→ (待录, 跳过[(key, 原因)])。

    任何信息缺口的行都进跳过清单、绝不进待录（盲提交红线）。
    DUE 缺失时：due_fallback_days>0 则按 开票日期+N 天 回退，否则跳过。"""
    todo: List[ApRow] = []
    skipped: List[Tuple[str, str]] = []
    for r in rows:
        if invoices and r.invoice_no not in invoices:
            continue
        if tks and r.tk not in tks:
            continue
        why = ""
        if r.error:
            why = f"表内异常未清：{r.error[:40]}"
        elif not r.invoice_no or not r.tk:
            why = "缺发票号/TK"
        elif r.amount <= 0:
            why = "摊分金额缺失或为0"
        elif r.key in submitted:
            why = "已建立应付（state 记账）"
        elif r.vendor not in vendor_map:
            why = f"服务商无映射（在 .env 的 GOFREIGHT_VENDOR_MAP 配 search/code）：{r.vendor or '(空)'}"
        elif not r.invoice_date:
            why = "开票日期缺失"
        elif not r.due_date:
            if due_fallback_days > 0:
                r = replace(r, due_date=r.invoice_date + timedelta(days=due_fallback_days))
            else:
                why = "发票DUE date 缺失"
        if why:
            skipped.append((r.key, why))
        else:
            todo.append(r)
    return todo, skipped


class ApState:
    """「发票号|TK」→ 提交记录（JSON，原子写）。丢失后可用 mark 逐条补记。"""

    def __init__(self, path: str):
        self.path = path
        self.entries: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.entries = {str(k): dict(v) for k, v in (d.get("entries") or {}).items()}
        except (json.JSONDecodeError, ValueError, OSError):
            self.entries = {}

    def save(self) -> None:
        d = {"version": 1, "entries": {k: self.entries[k] for k in sorted(self.entries)}}
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".gfs-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def is_submitted(self, key: str) -> bool:
        return key in self.entries

    def mark(self, key: str, info: Optional[dict] = None) -> None:
        self.entries[key] = dict(info or {})
