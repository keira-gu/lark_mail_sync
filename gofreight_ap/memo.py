# -*- coding: utf-8 -*-
"""备忘录发票上传：建立应付后在该 TK 会计页「备忘录」区建「标题=发票号」备忘录并上传发票附件。

链路（控件均 2026-11-10 试点 TK 实测定稿、全链真弹验证通过）：
  多维表「发票」附件列 file_token → Lark batch_get_tmp_download_url 下载 PDF →
  Playwright：备忘录 portlet 展开（带 expand 类=当前折叠）→ 加→建备忘录
  （标题=发票号，正文=附件说明）→ 点选行 → hc-attachment 常驻 input[type=file]
  set_input_files → 轮询 scope selectedMemo.attached_documents 验证（DOM tag 不可靠）。

防重：任一备忘录已挂同名文件即跳过（不管标题——专人手工传过的标题口径不一）；
标题=发票号且无附件的旧备忘录 → 直接补挂。备忘录失败不影响应付记账（应付已
成立），只在结果里标注转人工；`gofreight_ap memo` 子命令可事后补传。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .submit import GoFreightStepError, _shot

# 页面侧脚本：定位备忘录 portlet + memo 控制器 scope（AngularJS，沿 DOM 向上找）
_JS_FIND = """() => {
    const spans = [...document.querySelectorAll('.portlet .caption span, .portlet .caption')];
    const hit = spans.find(s => (s.textContent || '').trim() === '备忘录');
    if (!hit) return null;
    let el = hit.closest('.portlet');
    for (let i = 0; i < 10 && el; i++) {
        const s = (typeof angular !== 'undefined') && angular.element(el).scope();
        if (s && s.vm && s.vm.memos) {
            window.__memoPortlet = el;
            return true;
        }
        el = el.parentElement;
    }
    return null;
}"""

_JS_STATE = """() => {
    const p = window.__memoPortlet;
    const s = angular.element(p).scope();
    const vm = s.vm;
    const m = vm.selectedMemo;
    return {found: true, expanded: !!vm.expand,
            memos: vm.memos.map(x => ({id: x.id, subject: x.subject,
                                       docs: (x.attached_documents || []).map(d => d.filename)})),
            selected: m ? {id: m.id, subject: m.subject,
                           docs: (m.attached_documents || []).map(d => d.filename)} : null,
            uploadUrl: m ? vm.getAttachmentUploadUrl() : null};
}"""

_JS_EXPAND = """() => {
    const p = window.__memoPortlet;
    const c = p.querySelector('.collapse-btn');
    // ng-class="{expand: !vm.expand, collapse: vm.expand}"：带 expand 类=当前折叠
    if (c && c.classList.contains('expand')) c.click();
    return true;
}"""


# ---------- 纯逻辑：去重决策（离线可测） ----------

def memo_action(existing: Sequence[Dict], invoice_no: str,
                filenames: Sequence[str]) -> Tuple[str, str]:
    """→ ("skip"|"attach"|"create", 说明)。

    existing: [{subject, docs: [filename]}]（页面现有备忘录）。
    规则：
      1. 目标文件已全部挂在任一备忘录上（标题无所谓，专人手传口径不一）→ skip；
      2. 标题=发票号的备忘录已存在 → attach（选中补挂缺失文件；部分文件挂在
         别的备忘录上也不重复传，只补缺失集）；
      3. 否则 → create（新建后挂缺失集）。"""
    have = {d for m in existing for d in m.get("docs", [])}
    missing = [f for f in filenames if f not in have]
    if not missing:
        return "skip", (f"备忘录已挂全部 {len(filenames)} 个附件"
                        f"（不重复传）")
    hit = next((m for m in existing if m.get("subject") == invoice_no), None)
    if hit is not None:
        return "attach", (f"备忘录「{invoice_no}」已存在，补挂 {len(missing)} 个"
                          f"（现有 {len(hit.get('docs', []))} 个）")
    return "create", f"新建备忘录「{invoice_no}」并上传 {len(missing)} 个附件"


def download_invoice_files(client: Any, cfg: Any, row: Any, dest_dir: Path) -> List[Path]:
    """多维表附件 → 本地 PDF。按文件名+大小缓存，二次运行不重复下载。"""
    from lark_mail_sync.lark_client import safe_filename

    dest_dir.mkdir(parents=True, exist_ok=True)
    out: List[Path] = []
    for att in row.invoice_files:
        name = safe_filename(str(att.get("name") or f"invoice_{row.invoice_no}.pdf"))
        path = dest_dir / name
        size = int(att.get("size") or 0)
        if path.exists() and (size <= 0 or path.stat().st_size == size):
            out.append(path)
            continue
        d = client._api("drive.tmp_url", "GET",
                        "/open-apis/drive/v1/medias/batch_get_tmp_download_url",
                        app="bitable", query={"file_tokens": att["file_token"]})
        urls = {x["file_token"]: x["tmp_download_url"] for x in d.get("tmp_download_urls", [])}
        tmp = urls.get(att["file_token"])
        if not tmp:
            raise GoFreightStepError("memo_download", f"附件 {name} 拿不到下载链接")
        content = client.download_file(tmp, cfg.attachment_max_bytes)
        if not content.startswith(b"%PDF") and name.lower().endswith(".pdf"):
            raise GoFreightStepError("memo_download", f"附件 {name} 不是 PDF（头 {content[:4]!r}）")
        path.write_bytes(content)
        out.append(path)
    return out


# ---------- Playwright：单 TK 备忘录上传 ----------

@dataclass
class MemoCtx:
    client: Any                 # LarkClient（下载附件）
    cfg: Any                    # lark Config（attachment_max_bytes）
    att_dir: Path               # PDF 缓存目录


def _read_state(page) -> Optional[Dict]:
    if not page.evaluate(_JS_FIND):
        return None
    return page.evaluate(_JS_STATE)


def upload_memo(page, row, ctx: MemoCtx, gfc, prefix: str) -> str:
    """在会计页（submit 后停留页 / memo 子命令跳转页）为该行 TK 上传发票备忘录。

    返回备注；失败抛 GoFreightStepError（调用方标注转人工，不影响应付记账）。"""
    if not row.invoice_no:
        raise GoFreightStepError(f"{prefix}_memo", "发票号为空，无法定备忘录标题")
    pdfs = download_invoice_files(ctx.client, ctx.cfg, row, ctx.att_dir)
    if not pdfs:
        return "多维表无发票附件，未传备忘录"
    names = [p.name for p in pdfs]

    if _read_state(page) is None:
        raise GoFreightStepError(f"{prefix}_memo_portlet", "页面上找不到备忘录 portlet")
    page.evaluate(_JS_EXPAND)
    page.wait_for_timeout(1200)
    st = _read_state(page)
    if st is None or not st.get("found"):
        raise GoFreightStepError(f"{prefix}_memo_portlet", "备忘录 portlet 读取失败")

    act, why = memo_action(st["memos"], row.invoice_no, names)
    print(f"    备忘录：{act} —— {why}")
    if act == "skip":
        _shot(page, gfc, f"{prefix}_memo_skip")
        return why
    # 只传缺失集：部分文件已挂在别的备忘录上的不重传，验证也只看缺失集
    have = {d for m in st["memos"] for d in m.get("docs", [])}
    todo_pdfs = [p for p in pdfs if p.name not in have]
    todo_names = [p.name for p in todo_pdfs]

    if act == "create":
        page.locator("button[ng-click*='openMemoModal']").first.click()
        page.wait_for_timeout(2000)
        page.locator("input[ng-model='vm.memo.subject']").fill(row.invoice_no)
        page.locator("textarea[ng-model='vm.memo.content']").fill(
            f"发票附件 {'、'.join(names)}（lark_mail_sync 自动上传）")
        page.wait_for_timeout(300)
        save = page.locator('.modal button[ng-click="vm.save()"]')
        if save.first.is_disabled():
            raise GoFreightStepError(f"{prefix}_memo_save_disabled", "储存按钮不可点（标题没填上？）")
        _shot(page, gfc, f"{prefix}_memo_01_filled")
        save.first.click()
        page.wait_for_timeout(4000)

    # 选行：按「标题列（第2列）文本全等」精确匹配。不能整行 has_text——
    # 行内会显示附件文件名（内含其他发票号），且同柜专人的「发票」备忘录
    # 挂着别的发票，模糊匹配会点错行（11/10 补传 6 行翻车实锤）。
    rows_loc = page.locator(
        '.portlet:has(.caption:has-text("备忘录")) .table-scroll-body tbody tr')
    selected = False
    last_sel = None
    for _ in range(10):  # 新建行渲染可能滞后，轮询
        n = rows_loc.count()
        clicked = False
        for i in range(n):
            subj = rows_loc.nth(i).locator("td").nth(1).inner_text().strip()
            if subj == row.invoice_no:
                rows_loc.nth(i).scroll_into_view_if_needed()
                rows_loc.nth(i).click()
                clicked = True
                break
        if clicked:
            page.wait_for_timeout(1500)
            st = _read_state(page)
            last_sel = ((st or {}).get("selected") or {}).get("subject")
            if last_sel == row.invoice_no:
                selected = True
                break
        else:
            page.wait_for_timeout(1500)
    if not selected:
        _shot(page, gfc, f"{prefix}_memo_FAIL_select")
        raise GoFreightStepError(
            f"{prefix}_memo_select",
            f"选中失败：selected={last_sel!r} 期望 {row.invoice_no!r}"
            f"（标题列无全等行？人工核对）")

    # 上传：选中后 ngIf 渲染常驻 input[type=file]，set_input_files 直接触发 uploader
    finp = page.locator("hc-attachment input[type=file]")
    for _ in range(8):
        if finp.count():
            break
        page.wait_for_timeout(1000)
    if not finp.count():
        _shot(page, gfc, f"{prefix}_memo_FAIL_upload_input")
        raise GoFreightStepError(f"{prefix}_memo_upload_input", "hc-attachment 上传控件没渲染")
    finp.first.set_input_files(*[str(p) for p in todo_pdfs])

    # 验证：轮询 attached_documents 直到缺失集到齐（服务端落库才算成）
    done = False
    for _ in range(26):  # 最多 ~40s
        page.wait_for_timeout(1500)
        st = _read_state(page)
        docs = ((st or {}).get("selected") or {}).get("docs", [])
        if all(n in docs for n in todo_names):
            done = True
            break
    _shot(page, gfc, f"{prefix}_memo_02_after_upload")
    if not done:
        docs = ((st or {}).get("selected") or {}).get("docs", [])
        raise GoFreightStepError(
            f"{prefix}_memo_verify",
            f"附件没到齐：已挂 {docs} 期望 {todo_names}（截图留证，人工核对后可重跑补传）")
    return f"备忘录「{row.invoice_no}」已挂 {len(todo_names)} 个附件"


def run_memo_batch(rows: List, gfc, ctx: MemoCtx,
                   on_result: Optional[Callable] = None) -> List[Dict]:
    """补传场景（memo 子命令）：一个浏览器会话顺序处理多行；只传备忘录不建应付。"""
    from playwright.sync_api import sync_playwright
    from .submit import _ensure_logged_in, _launch

    results: List[Dict] = []
    with sync_playwright() as pw:
        browser = _launch(pw)
        bctx = browser.new_context(
            storage_state=gfc.browser_state if Path(gfc.browser_state).exists() else None,
            viewport={"width": 1680, "height": 950},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
            locale="zh-CN")
        page = bctx.new_page()
        page.set_default_timeout(20000)
        for i, row in enumerate(rows, 1):
            prefix = f"{i:02d}_memo{row.invoice_no}_{row.tk.replace('-', '')}"
            r = {"row": row, "ok": False, "note": ""}
            try:
                acct = f"{gfc.base_url}/truck/shipment/accounting/{row.tk}/"
                _ensure_logged_in(page, gfc, acct)
                page.wait_for_timeout(6000)
                r["note"] = upload_memo(page, row, ctx, gfc, prefix)
                r["ok"] = True
                print(f"  [{i}/{len(rows)}] {row.key} → 备忘录 OK {r['note']}")
            except GoFreightStepError as e:
                r["note"] = f"{e.step}: {e.msg}"
                print(f"  [{i}/{len(rows)}] {row.key} → 备忘录失败 @{e.step} {e.msg}")
            except Exception as e:  # noqa: BLE001 — 单行意外崩溃不应终止整批
                r["note"] = f"unexpected: {str(e)[:200]}"
                _shot(page, gfc, f"{prefix}_FAIL_unexpected")
                print(f"  [{i}/{len(rows)}] {row.key} → 意外异常 {str(e)[:120]}")
            results.append(r)
            try:
                bctx.storage_state(path=gfc.browser_state)
            except Exception:
                pass
            if on_result:
                on_result(r)
        browser.close()
    return results
