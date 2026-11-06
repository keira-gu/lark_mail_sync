# -*- coding: utf-8 -*-
"""发票 PDF 信息识别（2026-11-05 按真实发票定版）。

已验证的三种版式：
- A B C Logistics / Northline（表格式）：
    抬头首行=服务商；BILL TO 下一行=我司；`INVOICE 1001`=发票号；
    `DATE 10/01/2026`=开票日期（北美 MM/DD）；`DUE DATE 11/04/2026`=到期；
    `CURRENCY\\nCAD` 或 `BALANCE DUE CAD 185.00`=币种
- Bluegate Transport：`Invoice No. INV-100001`、ISO 日期 `2026-10-23`、
    `Balance Due $375.00 CAD`（币种在金额后）

日期歧义处理：先按 MM/DD 解析，月>12 才回退 DD/MM（Northline 18/10/2026 实测为 DD/MM）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_CURRENCIES = r"CAD|USD|CNY|EUR|MXN|AUD|GBP|JPY"

_RE_INVOICE_NO = re.compile(
    r"(?i)invoice\s*(?:no\.?|number|#)?\s*[:\-]?\s*((?:[A-Z]+-)?\d[\dA-Z\-]*)")
_RE_CURRENCY_BLOCK = re.compile(r"(?i)currency\s*[:\-]?\s*(" + _CURRENCIES + r")")
_RE_CURRENCY_BEFORE = re.compile(
    r"(?i)(?:balance\s*due|total|grand\s*total|amount\s*due)\s*[:\-]?\s*(" + _CURRENCIES + r")\s*\$?\s*[\d,]+")
_RE_CURRENCY_AFTER = re.compile(
    r"(?i)\$?\s*\d[\d,]*\.\d{2}\s*(" + _CURRENCIES + r")")
_RE_DUE_DATE = re.compile(
    r"(?i)due\s*date\s*[:\-]?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})")
_RE_ISSUE_DATE = re.compile(
    r"(?i)(?<!due )(?<!due:)(?:invoice\s*)?date\s*[:\-]?\s*"
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})")
_RE_BALANCE = re.compile(r"(?i)balance\s*due[^\d\n]{0,20}(\d[\d,]*\.\d{2})")
_RE_TOTAL_AMT = re.compile(r"(?i)total[^\d\n]{0,20}(\d[\d,]*\.\d{2})")
# QuickBooks 两页发票（如 Invoice 1006）无 CURRENCY 块、尾款只有 $ 符号；
# $ + 加拿大税务信号（GST/HST）→ CAD，避免币种列空白
_RE_DOLLAR_AMOUNT = re.compile(r"\$\s*\d[\d,]*\.\d{2}")
_RE_CA_SIGNAL = re.compile(r"(?i)GST/HST\s*Registration|GST\s*@|HST\s*@|\bCANADA\b")
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y", "%d/%m/%y")


def _parse_date(token: str) -> Optional[str]:
    """→ 'YYYY-MM-DD'；北美 MM/DD 优先，月>12 回退 DD/MM。"""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(token, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def date_to_ms(iso: str) -> Optional[int]:
    """ISO 日期 → UTC 零点毫秒（bitable 日期字段，避免时区偏移跨日）。"""
    try:
        return int(datetime.strptime(iso, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


def _clean_company(name: str) -> str:
    """公司名归一：合并单字母序列（A B C Logistics → ABC Logistics）。

    发票排版常把缩写拆成单字母空格隔开，录入时合并成一个词；
    其余多余空白压缩为单空格。
    """
    tokens = [t for t in re.split(r"\s+", (name or "").strip()) if t]
    merged: list = []
    run = ""
    for t in tokens:
        if len(t) == 1 and t.isalpha():
            run += t
        else:
            if run:
                merged.append(run)
                run = ""
            merged.append(t)
    if run:
        merged.append(run)
    return " ".join(merged)


def parse_invoice_text(text: str) -> Dict[str, str]:
    """从发票全文提取：服务商/我司/发票号/币种/开票日期/到期日期（ISO）。"""
    info: Dict[str, str] = {}
    if not text:
        return info
    lines = (text or "").splitlines()

    # 服务商名称：首个非空行（跳过页码行）
    for line in lines:
        s = line.strip()
        if not s or re.match(r"(?i)^page\s+\d+", s):
            continue
        info["vendor"] = _clean_company(s)
        break

    # 我司名称：BILL TO 行的下一非空行
    for i, line in enumerate(lines):
        if re.match(r"(?i)^bill\s*to\b", line.strip()):
            for j in range(i + 1, min(i + 4, len(lines))):
                s = lines[j].strip()
                if s:
                    info["our_company"] = _clean_company(s)
                    break
            break

    m = _RE_INVOICE_NO.search(text)
    if m:
        info["invoice_no"] = m.group(1).rstrip("-")
    m = _RE_DUE_DATE.search(text)
    if m:
        info["due_date"] = _parse_date(m.group(1)) or ""
    # 开票日期须排除 DUE DATE（负向后顾），且只认 DATE 关键字后的日期串
    for m in _RE_ISSUE_DATE.finditer(text):
        iso = _parse_date(m.group(1))
        if iso:
            info["issue_date"] = iso
            break
    m = (_RE_CURRENCY_BLOCK.search(text) or _RE_CURRENCY_BEFORE.search(text)
         or _RE_CURRENCY_AFTER.search(text))
    if m:
        info["currency"] = m.group(1).upper()
    elif _RE_DOLLAR_AMOUNT.search(text) and _RE_CA_SIGNAL.search(text):
        info["currency"] = "CAD"
    # 发票金额（邮件正文无金额时的回退源）：Balance Due 优先于 Total
    m = _RE_BALANCE.search(text) or _RE_TOTAL_AMT.search(text)
    if m:
        info["amount"] = m.group(1)
    return {k: v for k, v in info.items() if v}


def extract_invoice_info(content: bytes) -> Dict[str, str]:
    """PDF 字节 → 发票信息。解析失败返回 {}（不抛错，字段留空）。"""
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(content))
        text = "".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return {}
    return parse_invoice_text(text)
