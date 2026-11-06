# -*- coding: utf-8 -*-
"""confirm 邮件正文解析：TK 行（柜号/TK号/数量）与发票金额提取、按数量摊分。

正文样例（用户 2026-11-05 提供，空白/制表符分隔）：
    TSTU0000001        TK-26000001        1
    TSTU0000002        TK-26000002        5
    TSTU0000003        TK-26000003        1
    TSTU0000004        TK-26000004        5
第一列柜号不录入；金额在正文其余行（Total/Amount/合计 等关键词或唯一金额数字）。

2026-11-05 真实邮件实测两个关键形状：
- body_plain_text 可能整段是 **URL-safe base64**（含 -/_ 字符），需先解码；
- 解码后换行全部丢失（整封压成一行），列分隔是 NBSP(\\xa0)/em-space，
  故 TK 行解析不能依赖换行，按「TK 号 + 紧随独立整数=数量」全文匹配。
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

CONFIRM_KEYWORD_DEFAULT = "confirm"

_TK = re.compile(r"TK[-\s]?(\d{4,})", re.IGNORECASE)
# TK 号之后：空白（含 NBSP/em-space，\s 均覆盖）或括号/冒号/逗号等分隔，再接独立整数=数量
_QTY_AFTER = re.compile(r"[\s(（\[:：,，]+\s*(\d{1,5})(?!\d)")
_B64URL = re.compile(r"[A-Za-z0-9\-_]+={0,2}")
_MONEY = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})*\.\d{1,2}|\d+\.\d{1,2})(?![\d.])")
_AMOUNT_KEYWORD = re.compile(
    r"(?:total|amount|balance|due|invoice|grand\s*total|总计|合计|金额|总额)[^0-9\n]{0,30}"
    r"(\d{1,3}(?:,\d{3})*\.\d{1,2})", re.IGNORECASE)
_CURRENCY_PREFIX = re.compile(
    r"(?:CAD|USD|CNY|EUR|MXN|AUD|\$|¥|￥|€)\s*(\d{1,3}(?:,\d{3})*\.\d{1,2})", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    return _TAG.sub(" ", html or "").replace("&nbsp;", " ").replace("&amp;", "&")


def try_base64_decode(text: str) -> str:
    """Lark body_plain_text 整段 URL-safe base64 的场景（2026-11-05 真实邮件实测）。

    严格校验防止把普通英文误判成 base64：字符集须全匹配 + 严格 UTF-8 解码
    + 可打印率 ≥85%（普通文本按 base64 解出来的字节几乎不可能全过）。
    """
    s = re.sub(r"\s+", "", text or "")
    if len(s) < 24 or len(s) % 4 == 1 or not _B64URL.fullmatch(s):
        return text or ""
    try:
        decoded = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8")
    except Exception:
        return text or ""
    if not decoded:
        return text or ""
    printable = sum(1 for ch in decoded if ch.isprintable() or ch in "\n\r\t")
    if printable / len(decoded) < 0.85:
        return text or ""
    return decoded


def body_text(message: dict) -> str:
    body = message.get("body_plain_text") or ""
    if body:
        body = try_base64_decode(body)
    if not body:
        body = html_to_text(message.get("body_html") or "")
    return body


def is_confirm(subject: str, body: str = "",
               keyword: str = CONFIRM_KEYWORD_DEFAULT) -> bool:
    """主题或正文（解码后）含关键词即算（大小写不敏感）。

    真实结算邮件主题是「Fw: Invoice 1002 from …」不含 confirm，
    「Confirmed.」在正文首行——只查主题会整批漏掉（2026-11-05 实测）。
    """
    if not keyword:
        return False
    kw = keyword.lower()
    return kw in (subject or "").lower() or kw in (body or "").lower()


@dataclass
class ParsedEmail:
    tk_rows: List[Tuple[str, int]] = field(default_factory=list)   # [(TK号, 数量)] 同TK聚合
    amount: Optional[float] = None
    amount_source: str = ""           # keyword / currency / unique / ""
    errors: List[str] = field(default_factory=list)


def parse_tk_rows(text: str) -> List[Tuple[str, int]]:
    """全文匹配 TK 号；其后紧随的独立整数=数量（默认 1）。

    真实正文常被压成一行、列分隔为 NBSP（2026-11-05 实测），不依赖换行；
    同一 TK 出现多次 → 数量累加（摊分口径按 TK 聚合）。
    """
    text = text or ""
    qty_by_tk: "dict[str, int]" = {}
    order: List[str] = []
    for m in _TK.finditer(text):
        tk = "TK-" + m.group(1)
        qm = _QTY_AFTER.match(text, m.end())
        qty = int(qm.group(1)) if qm else 1
        if qty <= 0:
            qty = 1
        if tk not in qty_by_tk:
            qty_by_tk[tk] = 0
            order.append(tk)
        qty_by_tk[tk] += qty
    return [(tk, qty_by_tk[tk]) for tk in order]


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def parse_amount(text: str, tk_lines_included: bool = True) -> Tuple[Optional[float], str]:
    """金额提取三级策略：关键词行 > 货币符号前缀 > 全文唯一金额。"""
    text = text or ""
    m = _AMOUNT_KEYWORD.search(text)
    if m:
        return _to_float(m.group(1)), "keyword"
    m = _CURRENCY_PREFIX.search(text)
    if m:
        return _to_float(m.group(1)), "currency"
    moneys = {_to_float(x) for x in _MONEY.findall(text)}
    if len(moneys) == 1:
        return moneys.pop(), "unique"
    return None, ""


def parse_confirm_email(message: dict) -> ParsedEmail:
    text = body_text(message)
    result = ParsedEmail()
    result.tk_rows = parse_tk_rows(text)
    if not result.tk_rows:
        result.errors.append("未解析到TK行")
    result.amount, result.amount_source = parse_amount(text)
    if result.amount is None:
        result.errors.append("未识别发票金额")
    return result


def allocate(amount: float, tk_rows: List[Tuple[str, int]]) -> List[Tuple[str, int, float]]:
    """按数量摊分：round(2)，尾差补到数量最大的一行，保证 Σ摊分 == 发票金额。"""
    if not tk_rows:
        return []
    total_qty = sum(q for _, q in tk_rows)
    if total_qty <= 0:
        return [(tk, q, 0.0) for tk, q in tk_rows]
    out = [(tk, q, round(amount * q / total_qty, 2)) for tk, q in tk_rows]
    diff = round(amount - sum(a for _, _, a in out), 2)
    if diff:
        idx = max(range(len(out)), key=lambda i: out[i][1])
        out[idx] = (out[idx][0], out[idx][1], round(out[idx][2] + diff, 2))
    return out
