# -*- coding: utf-8 -*-
"""卡派发票对账归档：服务商明细 Excel → 逐 TK 核对 GoFreight 应付 → 下载发票附件归档。

业务口径（2026-11-19 确认）：
  - 输入 Excel 同《美国服务商明细.xlsx》：服务商名称 / 发票日期 / 到期日期 /
    文件编号(TK号) / D/C发票号码 / 币种 / 应付金额（发票金额列为空，正是核对目标）
  - 核对来源 = 会计页【应付区】表格行：发票号 + 金额（按表头列名定位，
    同 submit 费用行的表头映射模式）
  - 发票附件 = 会计页备忘录区已上传的发票 PDF（memo 链路的产出），逐个下载
  - 金额不符：标记继续跑完（清单标红），附件照下
  - 归档名 = 服务商_执行日期.pdf，执行日期=跑批当天；同服务商同天重名自动 _02/_03
  - 默认输出目录 D:\\卡派发票

页面侧控件（应付区表头、附件下载地址）未实测——首跑用 --probe 单 TK dump
表头/行/附件原始字段定稿（模式同 memo 2026-11-10 定稿流程）。
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .submit import GoFreightStepError

# ---------------- 纯逻辑：Excel 读取 / 核对 / 命名（离线可测） ----------------

@dataclass
class StmtRow:
    row_no: int          # Excel 原始行号（报错定位）
    vendor: str          # 服务商名称
    tk: str              # 文件编号（TK-xxxx）
    currency: str        # 币种
    amount: float        # 应付金额（明细侧）
    dc_no: str           # D/C 发票号码
    invoice_date: str    # 发票日期（YYYY-MM-DD 或原样）


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def read_statement(path) -> Tuple[List[StmtRow], List[Tuple[int, str]]]:
    """读《服务商明细》→ (rows, bad)。TK 列为空的行静默跳过（小计/空行），
    TK 有值但金额/服务商解析不出 → bad=[(行号, 原因)]（不进批次也不静默丢）。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    heads = [str(c or "").strip() for c in next(it)]

    def col(*needles) -> Optional[int]:
        for i, h in enumerate(heads):
            if any(n in h for n in needles):
                return i
        return None

    c_vendor = col("服务商")
    c_date = col("发票日期", "开票日期")
    c_tk = col("文件编号", "TK")
    c_dc = col("D/C", "发票号码")
    c_cur = col("币种")
    c_amt = col("应付金额")
    if c_vendor is None or c_tk is None or c_amt is None:
        raise ValueError(f"表头缺列：需要 服务商名称/文件编号(TK)/应付金额，实际表头 {heads}")

    def cell(r, i):
        return r[i] if (i is not None and i < len(r)) else None

    rows: List[StmtRow] = []
    bad: List[Tuple[int, str]] = []
    for no, r in enumerate(it, 2):
        if not r or all(v in (None, "") for v in r):
            continue
        tk = str(cell(r, c_tk) or "").strip().upper()
        if not tk:
            continue
        vendor = str(cell(r, c_vendor) or "").strip()
        amt = _num(cell(r, c_amt))
        if not vendor:
            bad.append((no, "服务商名称为空"))
            continue
        if amt is None:
            bad.append((no, f"应付金额解析失败 {cell(r, c_amt)!r}"))
            continue
        # 负数=供应商贷项/冲减行，属有效对账行，照常参与核对（合计口径）
        d = cell(r, c_date)
        d_str = d.strftime("%Y-%m-%d") if isinstance(d, datetime) else str(d or "")
        rows.append(StmtRow(
            row_no=no, vendor=vendor, tk=tk,
            currency=str(cell(r, c_cur) or "").strip(),
            amount=amt, dc_no=str(cell(r, c_dc) or "").strip(),
            invoice_date=d_str))
    wb.close()
    return rows, bad


def resolve_statement_path(raw: str, search_dirs: Optional[List[Path]] = None) -> Path:
    """明细 Excel 路径解析（v1.5.8）：完整/相对路径存在则直接用；只给文件名时在
    当前目录 → 桌面 → 下载 → 文档（含 OneDrive 重定向）找 .xlsx——**精确同名优先，
    其次名字包含**；唯一命中即用，多个/找不到报 ValueError 列出候选或目录。
    ~$ 临时文件忽略。search_dirs 供测试注入。"""
    p = Path(raw).expanduser()
    if p.is_file():
        return p
    name = p.name.lower()
    if search_dirs is None:
        home = Path.home()
        search_dirs = [Path.cwd(), home / "Desktop", home / "Downloads",
                       home / "Documents", home / "OneDrive" / "Desktop",
                       home / "OneDrive" / "Documents"]
    found = [f for d in search_dirs if d.is_dir()
             for f in sorted(d.glob("*.xlsx"))
             if not f.name.startswith("~$")]
    exact = [f for f in found if f.name.lower() == name]
    if len(exact) == 1:
        return exact[0]
    part = {str(f).lower(): f for f in found if name in f.name.lower()}
    cands = exact + [f for f in part.values() if f not in exact]
    if len(cands) == 1:
        return cands[0]
    if not cands:
        raise ValueError(
            f"找不到明细 Excel {raw!r}（找过 "
            f"{', '.join(str(d) for d in search_dirs if d.is_dir())}）；"
            f"请给完整路径，或把文件放到上面任一目录")
    raise ValueError("找到多个候选明细 Excel，请用完整路径指定其一：\n" +
                     "\n".join(f"  {c}" for c in cands))


_DISCOVER_KEY = "服务商明细"
_DISCOVER_EXCLUDE = (".backup-", "对账结果", "对账标注")


def discover_statement_path(search_dirs: Optional[List[Path]] = None) -> Path:
    """省略 Excel 参数时自动发现明细表（v1.5.9）：常见目录（当前目录→桌面→下载→文档，
    含 OneDrive 重定向）里名字含「服务商明细」的 .xlsx，排除 备份/对账结果/对账标注/
    ~$ 变体，取**最近修改**的一份；找不到报 ValueError（提示放文件或写文件名）。
    发现结果由计划首行明确打印，确认计划时把关。search_dirs 供测试注入。"""
    if search_dirs is None:
        home = Path.home()
        search_dirs = [Path.cwd(), home / "Desktop", home / "Downloads",
                       home / "Documents", home / "OneDrive" / "Desktop",
                       home / "OneDrive" / "Documents"]
    cands = [f for d in search_dirs if d.is_dir()
             for f in sorted(d.glob("*.xlsx"))
             if _DISCOVER_KEY in f.name and not f.name.startswith("~$")
             and not any(x in f.name for x in _DISCOVER_EXCLUDE)]
    if not cands:
        raise ValueError(
            "没找到《服务商明细》Excel（找过 "
            f"{', '.join(str(d) for d in search_dirs if d.is_dir())}）；"
            "把明细表放到桌面/下载/文档任一目录，或把文件名/完整路径写进话术里")
    return max(cands, key=lambda f: f.stat().st_mtime)


def archive_pdf(body: bytes, vendor: str, day: date, out: Path,
                used: set, seen: Dict) -> str:
    """归档一份 PDF（幂等 + **全局内容去重**，用户口径 2026-11-20「不要重复发票」）：
    同一份发票（md5 相同）全目录只存一份——首个归档者的命名生效，后续行（含其他
    供应商共票的行）直接复用该文件，不再复制副本。seen 预热后重跑安全。"""
    h = hashlib.md5(body).hexdigest()
    if h in seen:
        return seen[h]
    name = dest_filename(vendor, day, used)
    (out / name).write_bytes(body)
    seen[h] = name
    return name


def preload_seen(out: Path) -> Tuple[set, Dict]:
    """从归档目录预热 used（文件名集合）与 seen（md5 → 文件名，全局去重索引）。"""
    used: set = set()
    seen: Dict = {}
    if not out.exists():
        return used, seen
    for p in out.iterdir():
        if not p.is_file():
            continue
        used.add(p.name.lower())
        try:
            seen[hashlib.md5(p.read_bytes()).hexdigest()] = p.name
        except OSError:
            continue
    return used, seen


# ---------- 发票 PDF 金额识别（发票金额列=发票真实金额，用户口径 2026-11-20） ----------

_AMOUNT_RE = re.compile(r"((?:[0-9]{1,3}(?:,[0-9]{3})*|[0-9]{4,})\.[0-9]{2})")
# 标签优先级：Balance Due > Amount/Total Due > Invoice/Grand/Total Amount > Total > Subtotal
_LABELS = [("balancedue", 1.0), ("amountdue", 0.9), ("totaldue", 0.9),
           ("invoicetotal", 0.8), ("grandtotal", 0.8), ("totalamount", 0.8),
           ("total", 0.5), ("subtotal", 0.3)]


def amount_from_text(text: str) -> Optional[float]:
    """发票文本 → 金额。按标签优先级在同行/紧邻行取数；无任何标签兜底取最大金额
    （卡车行发票常见多行数字，无标签时取最大=总额近似）。识别不了返回 None。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    best: Optional[Tuple[float, float]] = None
    for i, line in enumerate(lines):
        low = re.sub(r"[^a-z]", "", line.lower())
        if not low:
            continue
        window = " ".join(lines[i:i + 2])
        for label, score in _LABELS:
            if label in low:
                m = _AMOUNT_RE.search(line) or _AMOUNT_RE.search(window)
                if m:
                    v = float(m.group(1).replace(",", ""))
                    if best is None or score > best[0]:
                        best = (score, v)
                break   # 一行只按命中的最高优先标签算
    if best is not None:
        return round(best[1], 2)
    nums = [float(m.group(1).replace(",", "")) for m in _AMOUNT_RE.finditer(text or "")]
    return round(max(nums), 2) if nums else None


def parse_pdf_text(body: bytes) -> str:
    import io
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(body)).pages)
    except Exception:  # noqa: BLE001 — 扫描件/加密件无文本层
        return ""


def vendor_in_text(vendor: str, text: str) -> bool:
    """发票文本是否属于该供应商：供应商名分词（≥4 字符），全命中（词少）或过半命中。"""
    toks = [t for t in re.split(r"[^A-Za-z0-9]+", (vendor or "").upper()) if len(t) >= 4]
    if not toks:
        return False
    t = re.sub(r"[^A-Za-z0-9]", "", (text or "").upper())
    hits = sum(tok in t for tok in toks)
    return hits == len(toks) if len(toks) <= 3 else hits >= max(2, len(toks) // 2)


def pdf_amount_for_row(pdfs: List[Tuple[bytes, str]], vendor: str,
                       text_cache: Optional[Dict] = None) -> Tuple[Optional[float], str]:
    """该行的发票 PDF 列表 → (金额, 原因)。同一票多张发票按供应商名匹配文本定位
    本行的（多张匹配求和）；仅一张时不匹配也兜底用。原因：
      ok=识别成功 / no_vendor_invoice=多张里没有一张属于本供应商（发票没传）
      / no_text=全是扫描件无文本层 / unparseable=匹配到了但金额读不出。"""
    texts = []
    for body, name in pdfs:
        key = hashlib.md5(body).hexdigest()
        if text_cache is not None and key in text_cache:
            texts.append(text_cache[key])
            continue
        t = parse_pdf_text(body)
        if text_cache is not None:
            text_cache[key] = t
        texts.append(t)
    matched = [(t, amount_from_text(t)) for t in texts if vendor_in_text(vendor, t)]
    if matched:
        vals = [a for _, a in matched if a is not None]
        if vals:
            return round(sum(vals), 2), "ok"
        return None, "unparseable"
    if len(pdfs) == 1:
        a = amount_from_text(texts[0])
        if a is not None:
            return a, "ok"
        return None, ("no_text" if not texts[0].strip() else "unparseable")
    if not any(t.strip() for t in texts):
        return None, "no_text"
    return None, "no_vendor_invoice"


def check_amount(gf_amount: Optional[float], stmt_amount: float, tol: float = 0.01) -> str:
    """→ OK / MISMATCH / NO_AMOUNT。多笔应付行按合计核对（一 TK 多行摊分属正常）。"""
    if gf_amount is None:
        return "NO_AMOUNT"
    return "OK" if abs(gf_amount - stmt_amount) <= tol else "MISMATCH"


_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def sanitize_vendor(v: str) -> str:
    v = _ILLEGAL.sub(" ", v)
    v = re.sub(r"\s+", " ", v).strip().rstrip(".")
    return v or "UNKNOWN"


def dest_filename(vendor: str, day: date, used: set, ext: str = ".pdf") -> str:
    """服务商_执行日期.ext；重名（大小写不敏感，Windows）自动 _02/_03…"""
    base = f"{sanitize_vendor(vendor)}_{day:%Y-%m-%d}"
    name = f"{base}{ext}"
    if name.lower() not in used:
        used.add(name.lower())
        return name
    n = 2
    while f"{base}_{n:02d}{ext}".lower() in used:
        n += 1
    name = f"{base}_{n:02d}{ext}"
    used.add(name.lower())
    return name


def parse_bills(heads, rows) -> List[Dict]:  # 兼容占位：旧表头版解析已由实测行特征版取代
    return []


# ---------------- 下载地址解析（候选试探 + %PDF 魔数校验） ----------------

def download_doc(request_get: Callable[[str], object], base_url: str, doc: Dict) -> Tuple[bytes, str]:
    """备忘录附件 → (bytes, 命中的URL)。request_get 需带会话 Cookie（bctx.request.get）。
    候选顺序：自带 url 字段 → preview_link 变体（2026-11-19 probe 实测 docs 只带
    preview_link=/document/preview/{id}/）→ id 常见形态。%PDF 魔数校验兜底。"""
    did = str(doc.get("id") or doc.get("doc_id") or doc.get("document_id") or "")
    fname = str(doc.get("filename") or doc.get("name") or "?")
    cands: List[str] = []

    def _abs(u) -> Optional[str]:
        u = str(u or "").strip()
        if not u:
            return None
        return u if u.startswith("http") else base_url + u

    for k in ("url", "download_url", "file_url", "path", "download_path"):
        u = _abs(doc.get(k))
        if u:
            cands.append(u)
    pv = str(doc.get("preview_link") or "")
    if pv:
        cands.append(_abs(pv.replace("/preview/", "/download/")))
        cands.append(_abs(pv))
    if did:
        cands += [_abs(f"/document/download/{did}/"),
                  _abs(f"/accounting/memo/attachment/download/{did}"),
                  _abs(f"/accounting/memo/download/{did}")]
    for u in [c for c in cands if c]:
        try:
            r = request_get(u)
            body = r.body()
            if getattr(r, "status", 0) == 200 and bytes(body[:4]) == b"%PDF":
                return bytes(body), u
        except Exception:  # noqa: BLE001 — 候选试探，逐个尝试
            continue
    raise GoFreightStepError(
        "cd_download",
        f"附件 {fname!r} 下载地址未命中（试了 {len(cands)} 个候选）；"
        f"跑 --probe --tk 单号 看 docs 原始字段定地址")


# ---------------- Playwright：会计页读取 / 批量对账归档 ----------------

# 全结构扫描：会计页 portlet 标题不固定（实测=MB/L、备忘录，应付数据在 MB/L
# portlet 内的表格里，2026-11-19 probe 实证）——不猜标题，dump 每个 portlet 的
# 所有表格（表头+样例行+全量行），Python 侧按表头特征挑账单表。
_JS_SCAN = """() => {
    const ports = [...document.querySelectorAll('.portlet')];
    const out = [];
    for (const p of ports) {
        const cap = ((p.querySelector('.caption') || {}).innerText || '')
            .trim().replace(/\\s+/g, ' ');
        const tables = [...p.querySelectorAll('table')].map(t => ({
            heads: [...t.querySelectorAll('thead th, thead td')]
                .map(h => (h.innerText || '').trim().replace(/\\s+/g, ' ')).filter(Boolean),
            rows: [...t.querySelectorAll('tbody tr')]
                .map(tr => [...tr.querySelectorAll('td')].map(td => (td.innerText || '').trim()))
                .filter(cells => cells.some(c => (c || '').trim()))
        })).filter(x => x.heads.length || x.rows.length);
        if (tables.length) out.push({caption: cap, tables});
    }
    return out;
}"""


def invoice_no_match(gf_inv: str, dc_no: str) -> bool:
    """发票号模糊匹配（用户口径 2026-11-20：有的发票号比系统录入的短）：
    归一化（去非字母数字+大写+去 INV 前缀）后 相等 或 互含（短号是长号一部分）
    都算匹配——如 INV-28643 vs 28643、6251166014 vs 6614-6251166014。"""
    def norm(v):
        v = re.sub(r"[^0-9A-Za-z]", "", str(v or "").upper())
        return re.sub(r"^INV", "", v) if v else v
    a, b = norm(gf_inv), norm(dc_no)
    if not a or not b:
        return True     # 任一侧缺失不判不一致（另有无账单/无附件口径兜着）
    return a == b or a in b or b in a


def vendor_match(bill_vendor: str, stmt_vendor: str) -> bool:
    """GoFreight 账单供应商 vs 明细服务商：大写去非字母数字后 相等或互含。
    （2026-11-19 实跑实证：同一 TK 多家供应商各一笔账，必须按供应商过滤后核对。）"""
    def norm(v):
        return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())
    a, b = norm(bill_vendor), norm(stmt_vendor)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def is_invoice_name(name: str) -> bool:
    """文件中心的文件混着包装清单/船舶证书/POD——只认发票样文件名（INV/INVOICE/发票）。"""
    return bool(re.search(r"inv|invoice|发票", str(name or ""), re.IGNORECASE))


def pick_bills_table(structure) -> Optional[Dict]:
    """账单表识别（2026-11-19 probe 实测定稿）：会计页表格**无表头**，MB/L portlet
    内按行特征识别——首格「成本(应付)」= 应付账单行（收应收行/加总/金额行全跳过）。

    行列位（实测）：[0]类型 [2]发票号(=Excel D/C发票号码) [3]供应商
    [5]金额 [8]到期日期 [9]发票日期 [11]来源。→ {caption, bills: [提取行]}"""
    for port in structure or []:
        if "备忘录" in (port.get("caption") or ""):
            continue
        for t in port.get("tables") or []:
            bills = []
            for cells in (t.get("rows") or []):
                if not cells:
                    continue
                head = str(cells[0] or "").strip()
                if "应付" in head and len(cells) > 5:   # 「成本(应付)」
                    bills.append({
                        "invoice_no": str(cells[2] or "").strip(),
                        "vendor": str(cells[3] or "").strip(),
                        "amount": _num(cells[5]),
                        "due_date": str(cells[8] or "").strip() if len(cells) > 8 else "",
                        "invoice_date": str(cells[9] or "").strip() if len(cells) > 9 else "",
                        "raw": " ".join(c or "" for c in cells)[:120],
                    })
            if bills:
                return {"caption": port.get("caption"), "bills": bills}
    return None

# 备忘录附件原始对象（下载地址字段实测定稿用；比 memo._JS_STATE 多带全部字段）
_JS_MEMO_RAW = """() => {
    const spans = [...document.querySelectorAll('.portlet .caption span, .portlet .caption')];
    const hit = spans.find(s => (s.textContent || '').trim() === '备忘录');
    if (!hit) return null;
    let el = hit.closest('.portlet');
    for (let i = 0; i < 10 && el; i++) {
        const s = (typeof angular !== 'undefined') && angular.element(el).scope();
        if (s && s.vm && s.vm.memos) {
            return s.vm.memos.map(m => ({
                subject: m.subject,
                docs: (m.attached_documents || []).map(d => JSON.parse(JSON.stringify(d)))
            }));
        }
        el = el.parentElement;
    }
    return null;
}"""


# 文件中心（shipment 文档页 /truck/shipment/document/{TK}，2026-11-19 实测定稿）：
# 业务传发票就在这（主页「文件(N)」按钮计的是 MBL 层，恒 0 会漏）。表格列=
# 名称/日期/档案大小/类别/功能/建立者；行内预览链接 /document/preview/{id}/ →
# 下载端点 = /document/download/{id}/（doDownload 的 REST 形态），%PDF 校验兜底。
_JS_DOC_ROWS = r"""() => {
    const t = [...document.querySelectorAll('table')].find(t => t.offsetWidth > 0 &&
        [...t.querySelectorAll('thead th')].some(h => (h.innerText||'').includes('名称')));
    if (!t) return [];
    return [...t.querySelectorAll('tbody tr')].map(tr => {
        const a = [...tr.querySelectorAll('a[href*="/document/preview/"]')][0];
        const m = a ? (a.getAttribute('href')||'').match(/preview\/(\d+)/) : null;
        return {name: ((tr.querySelectorAll('td')[1]||{}).innerText||'').trim(),
                id: m ? m[1] : null,
                preview: a ? a.getAttribute('href') : null};
    }).filter(x => x.name);
}"""


def _fetch_pdfs_from_document_page(page, bctx, gfc, tk: str) -> List[Tuple[bytes, str]]:
    """shipment 文档页 → [(pdf bytes, 名称)]。只取 .pdf（发票）；单个失败试下一个。"""
    page.goto(f"{gfc.base_url}/truck/shipment/document/{tk}",
              wait_until="domcontentloaded", timeout=45000)
    rows = []
    deadline = time.time() + 6     # 表格异步渲染：就绪即走（v1.5.7，原固定 5s）
    while not rows and time.time() < deadline:
        rows = page.evaluate(_JS_DOC_ROWS) or []
        if not rows:
            page.wait_for_timeout(700)
    get = lambda u: bctx.request.get(u, timeout=8000)  # noqa: E731
    out: List[Tuple[bytes, str]] = []
    for d in rows:
        if not str(d.get("name") or "").lower().endswith(".pdf"):
            continue
        try:
            body, _u = download_doc(get, gfc.base_url,
                                    {"id": d.get("id"), "filename": d.get("name"),
                                     "preview_link": d.get("preview")})
            out.append((body, str(d.get("name"))))
        except GoFreightStepError:
            continue
    return out


def _new_page(pw, gfc):
    from .submit import _launch
    browser = _launch(pw)
    bctx = browser.new_context(
        storage_state=gfc.browser_state if Path(gfc.browser_state).exists() else None,
        viewport={"width": 1680, "height": 950},
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        locale="zh-CN")
    page = bctx.new_page()
    page.set_default_timeout(20000)
    return browser, bctx, page


def probe_tk(tk: str, gfc) -> Dict:
    """单 TK dump：全 portlet 表格结构 + 备忘录附件原始字段（控件定稿用）。
    备忘录异步加载，轮询 15s 防 memos=[] 时序假象（11/10 实踩）。"""
    import json
    from playwright.sync_api import sync_playwright
    from .submit import _ensure_logged_in
    with sync_playwright() as pw:
        browser, _bctx, page = _new_page(pw, gfc)
        try:
            acct = f"{gfc.base_url}/truck/shipment/accounting/{tk}/"
            _ensure_logged_in(page, gfc, acct)
            page.wait_for_timeout(6000)
            structure = page.evaluate(_JS_SCAN)
            picked = pick_bills_table(structure)
            memos = None
            for _ in range(10):   # 轮询：备忘录异步加载
                memos = page.evaluate(_JS_MEMO_RAW)
                if memos:
                    break
                page.wait_for_timeout(1500)
            out = {"tk": tk, "picked": picked,
                   "structure": structure, "memos": memos}
            print(json.dumps(out, ensure_ascii=False, indent=1)[:12000])
            return out
        finally:
            browser.close()


@dataclass
class CardiCtx:
    """预留扩展位（当前无使用者）：附件源只从 GoFreight 取（用户口径 2026-11-19）。"""
    client: Any = None
    cfg: Any = None
    bitable_by_tk: Dict = None
    att_dir: Path = None


def run_cardispatch(rows: List[StmtRow], gfc, out_dir, day: Optional[date] = None,
                    on_result: Optional[Callable] = None,
                    ctx: Optional[CardiCtx] = None) -> List[Dict]:
    """一个浏览器会话逐 TK：读应付区核对金额 → 下载发票附件归档（**只从 GoFreight
    取**：会计页「文件中心」优先 → 备忘录兜底）→ 归档 服务商_当天日期.pdf（重名 _02/_03）。

    状态：OK / MISMATCH（金额不符，标红）/ NO_BILL（无应付行）/ NO_PDF（无附件）/
    READ_FAIL（账单行定位失败）/ FAIL（异常）。单行失败不终止批次。"""
    from playwright.sync_api import sync_playwright
    from .submit import _ensure_logged_in, _shot

    day = day or date.today()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    used, seen = preload_seen(out)     # 重跑幂等：已归档文件不重复写
    doc_cache: Dict[str, List[Tuple[bytes, str]]] = {}   # 同 TK 多行复用已下载附件
    acct_cache: Dict[str, Dict] = {}   # tk→{"picked","memo_docs"}：同 TK 多行不重复访问会计页
    pdf_text_cache: Dict = {}          # PDF 文本解析缓存（同发票跨行共享）

    results: List[Dict] = []
    with sync_playwright() as pw:
        browser, bctx, page = _new_page(pw, gfc)
        for i, row in enumerate(rows, 1):
            prefix = f"{i:02d}_cd_{row.tk.replace('-', '')}"
            r: Dict = {"tk": row.tk, "row_no": row.row_no, "vendor": row.vendor,
                       "stmt_amount": row.amount, "currency": row.currency,
                       "status": "", "note": "", "invoices": "", "gf_amount": None,
                       "files": []}
            try:
                cached = acct_cache.get(row.tk)
                if cached is not None:
                    picked = cached["picked"]   # 同 TK 多行（多供应商共票）整段复用，不再访问页面
                else:
                    acct = f"{gfc.base_url}/truck/shipment/accounting/{row.tk}/"
                    _ensure_logged_in(page, gfc, acct)
                    picked = None
                    deadline = time.time() + 8  # 表格异步渲染：就绪即走（v1.5.7，原固定 6s+3×2.5s）
                    while picked is None and time.time() < deadline:
                        picked = pick_bills_table(page.evaluate(_JS_SCAN))
                        if picked is None:
                            page.wait_for_timeout(700)
                    acct_cache[row.tk] = {"picked": picked, "memo_docs": None}
                bills = (picked or {}).get("bills") or []
                if picked is None:
                    r["status"] = "READ_FAIL"
                    r["note"] = "账单行未定位（无「成本(应付)」行，跑 --probe 看结构）"
                elif not bills:
                    r["status"] = "NO_BILL"
                    r["note"] = "账单表无应付行（未建立应付？转人工核对）"
                else:
                    # 多供应商共 TK（2026-11-19 实跑实证：单柜 8 家各一笔）：
                    # 只合计本行供应商的账单核对，其余供应商的账不掺进来
                    mine = [b for b in bills if vendor_match(b.get("vendor"), row.vendor)]
                    others = len(bills) - len(mine)
                    if not mine:
                        r["status"] = "NO_BILL"
                        r["note"] = (f"该 TK 有 {len(bills)} 笔账单但无本供应商"
                                     f"（{row.vendor}）的——转人工核对")
                    else:
                        total = round(sum(b["amount"] or 0 for b in mine), 2)
                        r["invoices"] = ";".join(b["invoice_no"] for b in mine)
                        r["gf_amount"] = total
                        verdict = check_amount(total, row.amount)
                        r["status"] = verdict if verdict in ("OK", "MISMATCH") else "NO_AMOUNT"
                        if verdict == "MISMATCH":
                            r["note"] = f"金额不符：{row.vendor} 在 GoFreight {total:.2f} vs 明细 {row.amount:.2f} {row.currency}"
                        # 交叉校验（单笔时）：GoFreight 发票号 vs Excel D/C 号（模糊匹配：
                        # 短号/去INV前缀互含算匹配），不一致只标注不判死
                        if len(mine) == 1 and row.dc_no and \
                                not invoice_no_match(mine[0]["invoice_no"], row.dc_no):
                            r["note"] = (r["note"] + "；" if r["note"] else "") + \
                                f"发票号不一致：GoFreight {mine[0]['invoice_no']} vs 明细D/C {row.dc_no}"
                        if others:
                            r["note"] = (r["note"] + "；" if r["note"] else "") + \
                                f"（另有 {others} 笔其他供应商账单未计入本行核对）"

                # 附件：只从 GoFreight 取（用户口径）——shipment 文档页（业务传发票处）
                # 优先，备忘录兜底。同 TK 多行（多供应商共票）复用缓存，不重复访问页面；
                # 文件中心混着包装清单/船舶证书，只取发票样文件名（唯一 PDF 时兜底取）
                pdfs: List[Tuple[bytes, str]] = []
                att_src = ""
                if row.tk in doc_cache:
                    pdfs = list(doc_cache[row.tk])
                    if pdfs:
                        att_src = "GoFreight文件中心"
                else:
                    try:
                        all_pdfs = _fetch_pdfs_from_document_page(page, bctx, gfc, row.tk)
                    except Exception as e:  # noqa: BLE001 — 文档页异常不阻断，落备忘录
                        all_pdfs = []
                        r["note"] = (r["note"] + "；" if r["note"] else "") + \
                            f"文件中心读取异常：{str(e)[:80]}"
                    inv = [p for p in all_pdfs if is_invoice_name(p[1])]
                    if inv:
                        pdfs = inv
                    elif len(all_pdfs) == 1:
                        pdfs = all_pdfs          # 唯一 PDF 无发票字样也兜底取
                    else:
                        pdfs = []
                        if all_pdfs:
                            r["note"] = (r["note"] + "；" if r["note"] else "") + \
                                f"文件中心有 {len(all_pdfs)} 个 PDF 但无发票样文件名（INV/发票）"
                    doc_cache[row.tk] = list(pdfs)
                    if pdfs:
                        att_src = "GoFreight文件中心"
                    elif acct_cache[row.tk]["memo_docs"] is None:
                        # 备忘录兜底只查一次/TK（v1.5.7）：异步加载、只在会计页读得到——
                        # 此刻页面可能停在文档页，回会计页再轮询；结果缓存给同 TK 后续行
                        back = f"{gfc.base_url}/truck/shipment/accounting/{row.tk}/"
                        _ensure_logged_in(page, gfc, back)
                        memos = []
                        for _ in range(4):
                            memos = page.evaluate(_JS_MEMO_RAW) or []
                            if any(m.get("docs") for m in memos):
                                break
                            page.wait_for_timeout(1200)
                        acct_cache[row.tk]["memo_docs"] = \
                            [d for m in (memos or []) for d in (m.get("docs") or [])]
                memo_docs = acct_cache[row.tk].get("memo_docs") or []
                if not pdfs and memo_docs:
                    get = lambda u: bctx.request.get(u, timeout=8000)  # noqa: E731
                    for d in memo_docs:
                        body, _u = download_doc(get, gfc.base_url, d)
                        pdfs.append((body, str(d.get("filename") or "invoice.pdf")))
                    att_src = att_src or "GoFreight备忘录"
                if not pdfs:
                    if r["status"] == "OK":
                        r["status"] = "NO_PDF"
                    r["note"] = (r["note"] + "；" if r["note"] else "") + \
                        "无发票附件（GoFreight文件中心与备忘录均无）"
                else:
                    for body, _orig in pdfs:
                        r["files"].append(archive_pdf(body, row.vendor, day, out, used, seen))
                    r["pdf_amount"], r["pdf_why"] = pdf_amount_for_row(
                        pdfs, row.vendor, pdf_text_cache)
                    r["note"] = (r["note"] + "；" if r["note"] else "") + \
                        f"附件×{len(pdfs)}（{att_src}）"
                _shot(page, gfc, f"{prefix}_done")
            except GoFreightStepError as e:
                r["status"] = "FAIL"
                r["note"] = f"{e.step}: {e.msg}"
            except Exception as e:  # noqa: BLE001 — 单行意外不终止批次
                r["status"] = "FAIL"
                r["note"] = f"unexpected: {str(e)[:150]}"
                _shot(page, gfc, f"{prefix}_FAIL_unexpected")
            ok = r["status"] in ("OK", "MISMATCH", "NO_PDF")
            print(f"  [{i}/{len(rows)}] {row.tk} {row.vendor[:20]} → {r['status']} {r['note'][:80]}")
            results.append(r)
            try:
                bctx.storage_state(path=gfc.browser_state)
            except Exception:
                pass
            if on_result:
                on_result(r)
        browser.close()
    return results


_BRIEF = {"OK": "金额核对通过", "MISMATCH": "金额不符", "NO_BILL": "未在GoFreight找到本供应商账单",
          "READ_FAIL": "账单读取失败", "NO_AMOUNT": "账单无金额", "NO_PDF": "金额核对通过",
          "FAIL": "处理异常"}


def _problem_text(res: Dict) -> str:
    """「对账问题」列文本：只写问题，通过的行留空。
    问题性说明全保留（金额不符/发票号不一致/无本供应商账单/无发票附件/
    文件中心有PDF但无发票样文件名/读取异常），只剔除纯信息性内容
    （附件×N、其他供应商账单未计入）。**无附件的行必须明确写出**。"""
    files = res.get("files") or []
    if res.get("status") == "OK" and files:
        return ""
    parts = [p.strip() for p in str(res.get("note") or "").split("；")
             if p.strip()
             and not p.strip().startswith("附件×")
             and not (p.strip().startswith("另有") and "未计入" in p)]
    if not files and not any(("发票附件" in p) or ("发票样" in p) for p in parts):
        parts.append("未找到发票附件")
    if not parts:
        parts = [_BRIEF.get(res.get("status"), str(res.get("status") or "?"))]
    return "；".join(parts)[:200]


def invoice_case_text(res: Dict) -> str:
    """「发票情况」列：一行一句话状态 = 金额异常标签 + 附件情况 + 识别情况
    （用户口径 2026-11-20：异常也要记到这列）。正常行保持「已下载N份」。"""
    parts: List[str] = []
    tag = {"MISMATCH": "金额不符", "NO_BILL": "GoFreight无账单",
           "READ_FAIL": "账单读取失败", "NO_AMOUNT": "账单无金额",
           "FAIL": "处理异常"}.get(res.get("status"))
    if tag:
        parts.append(tag)
    files = res.get("files") or []
    if files:
        parts.append(f"已下载{len(files)}份")
        if res.get("pdf_amount") is None:
            why = res.get("pdf_why") or "unparseable"
            parts.append({"no_vendor_invoice": "未找到本供应商发票",
                          "no_text": "发票金额未识别(扫描件)"}
                         .get(why, "发票金额未识别"))
    else:
        parts.append("未找到发票附件")
    return "；".join(parts)


def annotate_excel(excel_path, results: List[Dict], rows: List[StmtRow]) -> Tuple[Path, Path]:
    """对账结果标注回**原 Excel**（先自动备份同名 .backup-时间戳 文件）。

    发票金额列 ← **发票 PDF 识别金额**（用户口径 2026-11-20 全覆盖；无 PDF/识别不出
    置空，不留旧 GoFreight 金额）；发票情况列 ← 附件下载情况；
    **「对账问题」列（优先复用原表空尾列）← 问题说明，通过的行留空**；
    原备注列不动；**不加底色**（用户口径 2026-11-20：问题看列文字，不标红）。
    原文件被占用（用户开着 Excel）时改存 <名>_对账标注.xlsx 并返回该路径。"""
    import openpyxl
    p = Path(excel_path)
    bak = p.with_name(f"{p.stem}.backup-{datetime.now():%Y%m%d-%H%M}{p.suffix}")
    bak.write_bytes(p.read_bytes())
    wb = openpyxl.load_workbook(p)
    ws = wb.worksheets[0]
    heads = [str(c.value or "").strip() if c.value is not None else "" for c in ws[1]]

    def col(*needles) -> Optional[int]:
        for i, h in enumerate(heads, 1):
            if any(n in h for n in needles):
                return i
        return None

    c_amt, c_case = col("发票金额"), col("发票情况")
    c_prob = col("对账问题")
    if c_prob is None:
        # 优先复用原表自带的**空尾巴列**（如本表的 R 列）——超范围新建列
        # 在部分查看器（WPS 实测）里不显示，写入既有列任何工具都必然渲染
        c_prob = next((i for i, h in enumerate(heads, 1) if not h), None)
        if c_prob is None:
            c_prob = len(heads) + 1
            heads.append("对账问题")
        ws.cell(1, c_prob).value = "对账问题"
    by_no = {r["row_no"]: r for r in results}
    for row in rows:
        res = by_no.get(row.row_no)
        if not res:
            continue
        no = row.row_no
        if c_amt:
            ws.cell(no, c_amt).value = res.get("pdf_amount")
        if c_case:
            ws.cell(no, c_case).value = invoice_case_text(res)
        ws.cell(no, c_prob).value = _problem_text(res)
    try:
        wb.save(p)
        return p, bak
    except PermissionError:   # 原文件被 Excel 占用 → 落副本，绝不丢结果
        alt = p.with_name(f"{p.stem}_对账标注{p.suffix}")
        wb.save(alt)
        return alt, bak


def write_report(results: List[Dict], rows: List[StmtRow], path) -> Path:
    """对账清单 xlsx：金额不符/无应付/失败 行标红。"""
    import openpyxl
    from openpyxl.styles import PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "对账清单"
    ws.append(["行号", "服务商", "TK", "币种", "应付金额(明细)", "发票号(GoFreight)",
               "金额(GoFreight)", "核对结果", "归档文件", "状态/备注"])
    red = PatternFill("solid", fgColor="FFC7CE")
    by_tk = {r.tk: r for r in rows}
    for res in results:
        row = by_tk.get(res["tk"])
        ws.append([row.row_no if row else "", row.vendor if row else "", res["tk"],
                   res.get("currency", ""), res.get("stmt_amount", ""),
                   res.get("invoices", ""), res.get("gf_amount", ""),
                   res.get("status", ""), ";".join(res.get("files", [])),
                   res.get("note", "")])
        if res.get("status") in ("MISMATCH", "NO_BILL", "READ_FAIL", "FAIL"):
            for c in ws[ws.max_row]:
                c.fill = red
    for col, w in zip("ABCDEFGHIJ", [6, 28, 14, 6, 12, 24, 12, 10, 34, 40]):
        ws.column_dimensions[col].width = w
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    wb.save(p)
    return p
