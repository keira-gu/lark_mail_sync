# -*- coding: utf-8 -*-
"""Playwright 提交端：把一条 ApRow 录进 GoFreight 建立应付（试点 pilot2 全量移植）。

每步失败即截图并抛 GoFreightStepError，批处理记下该行失败、继续下一行；
会话过期自动重登（约 15 分钟过期 + 单会话互踢，实测）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .plan import ApRow


class GoFreightStepError(Exception):
    """某个步骤失败；step 用于截图命名与日志。"""

    def __init__(self, step: str, msg: str = ""):
        super().__init__(f"{step}: {msg}")
        self.step = step
        self.msg = msg


@dataclass
class GfConfig:
    base_url: str
    user: str
    password: str
    fee_code: str = ""   # 必填：来自 .env GOFREIGHT_FEE_CODE（口径值由管理员发）
    shot_dir: str = "runtime-outputs/gofreight"
    browser_state: str = "runtime-outputs/gofreight/state.json"
    headless: bool = True


@dataclass
class RowResult:
    row: ApRow
    ok: bool
    step: str = ""
    msg: str = ""
    note: str = ""
    shots: List[str] = field(default_factory=list)


def _shot_dir(cfg: GfConfig) -> Path:
    p = Path(cfg.shot_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _shot(page, cfg: GfConfig, name: str) -> str:
    path = _shot_dir(cfg) / f"{name}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
    except Exception:
        pass
    return path.name


def _die(page, cfg: GfConfig, step: str, msg: str = "") -> None:
    _shot(page, cfg, f"FAIL_{step}")
    raise GoFreightStepError(step, msg)


def _launch(pw):
    for kw in ({"channel": "chrome"}, {"channel": "msedge"}):
        try:
            return pw.chromium.launch(headless=True, **kw)
        except Exception as e:  # noqa: BLE001 — 换下一个 channel
            print(f"  launch {kw} 失败: {str(e)[:80]}")
    raise SystemExit("找不到 Chrome/Edge，无法启动浏览器")


def _ensure_logged_in(page, cfg: GfConfig, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    if "/login/" not in page.url:
        return
    print("    会话过期，重新登录…")
    page.wait_for_timeout(3000)
    page.fill("input[name='username']", cfg.user)
    page.fill("input[name='password']", cfg.password)
    page.wait_for_timeout(500)
    page.keyboard.press("Enter")
    page.wait_for_timeout(8000)
    if "/login/" in page.url:
        _die(page, cfg, "login", "登录未成功（账号密码？单会话互踢？）")
    page.goto(url, wait_until="domcontentloaded", timeout=45000)


def _set_date(page, cfg: GfConfig, field_name: str, value: str) -> None:
    if "/accounting/invoice/entry" not in page.url:
        _die(page, cfg, f"date_{field_name}", f"页面已离开表单: {page.url}")
    inp = page.locator(f"input[name='{field_name}']")
    if not inp.count():
        _die(page, cfg, f"date_{field_name}", "input 不存在")
    inp.first.fill("")
    inp.first.fill(value)
    page.keyboard.press("Escape")   # datepicker 挡点击，按两次收起（试点实测）
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    if inp.first.input_value() != value:
        _die(page, cfg, f"date_{field_name}",
             f"填入={inp.first.input_value()!r} 期望={value!r}")


def submit_one(page, row: ApRow, vendor: Dict[str, str], cfg: GfConfig,
               prefix: str, submit: bool = True) -> str:
    """录一行。返回备注（如「已存在」提示）。失败抛 GoFreightStepError。

    日期口径：发布日期=发票日期=开票日期；到期=发票DUE date。"""
    mmdd = lambda d: d.strftime("%m-%d-%Y")  # noqa: E731 — 表单格式 MM-DD-YYYY
    url = (f"{cfg.base_url}/accounting/invoice/entry?mbl={row.tk}&type=B"
           f"&next=/truck/shipment/accounting/{row.tk}")
    # 预检：先看该 TK 会计页是否已有同发票号（专人手工已录→跳过防双录，记账本）
    acct = f"{cfg.base_url}/truck/shipment/accounting/{row.tk}/"
    _ensure_logged_in(page, cfg, acct)
    page.wait_for_timeout(6000)
    b_pre = page.inner_text("body")
    if row.invoice_no in b_pre:
        hit_line = next((l.strip() for l in b_pre.splitlines()
                         if row.invoice_no in l), "")[:90]
        _shot(page, cfg, f"{prefix}_00_precheck_exists")
        return f"会计页已有该发票号（可能专人已录），跳过防双录：{hit_line}"
    _ensure_logged_in(page, cfg, url)
    try:
        page.wait_for_selector(".ng-select-container", timeout=30000)
    except Exception:
        _die(page, cfg, f"{prefix}_form_load", f"表单没渲染出来 url={page.url}")
    page.wait_for_timeout(2000)

    # 供应商（ng-select 服务端搜索：只能打无空格短词）
    page.locator(".ng-select-container").first.click()
    page.wait_for_timeout(1200)
    vopt = page.locator(f".ng-option:has-text('{vendor['code']}')")
    for attempt in range(3):
        page.keyboard.type(vendor["search"], delay=90)
        page.wait_for_timeout(2500)
        if vopt.count():
            break
        for _ in range(len(vendor["search"])):
            page.keyboard.press("Backspace")
        page.wait_for_timeout(500)
    if not vopt.count():
        _die(page, cfg, f"{prefix}_vendor", f"搜不到 {vendor['code']}（候选列表）")
    vopt.first.click()
    page.wait_for_timeout(800)

    # 日期 + 发票号
    _set_date(page, cfg, "post_date", mmdd(row.invoice_date))
    _set_date(page, cfg, "invoice_date", mmdd(row.invoice_date))
    _set_date(page, cfg, "due_date", mmdd(row.due_date))
    inv = page.locator("input[name='inv_no']")
    inv.first.fill(row.invoice_no)
    if inv.first.input_value() != row.invoice_no:
        _die(page, cfg, f"{prefix}_inv_no", f"填入={inv.first.input_value()!r}")
    _shot(page, cfg, f"{prefix}_01_header")

    # 费用行（ui-grid；绿+ / 空态文字 双保险加行）
    area = page.locator("invoice-entry-freight-ap").first
    area.scroll_into_view_if_needed()
    page.wait_for_timeout(600)
    rows = page.locator("invoice-entry-freight-ap .ui-grid-row")
    for attempt in range(3):
        if rows.count():
            break
        if attempt == 0:
            page.locator("invoice-entry-freight-ap button.btn.green").first.click()
        elif attempt == 1:
            empty_td = page.locator("invoice-entry-freight-ap .col-empty")
            if empty_td.count():
                empty_td.first.click()
            else:
                page.locator("invoice-entry-freight-ap button.btn.green").first.click()
        else:
            page.evaluate(
                "() => { document.querySelector('invoice-entry-freight-ap button.btn.green').click(); }")
        page.wait_for_timeout(2500)
    if not rows.count():
        _die(page, cfg, f"{prefix}_add_row", "三次尝试加费用行均失败")
    cols = page.evaluate(
        """() => {
            const hs = document.querySelectorAll('invoice-entry-freight-ap .ui-grid-header-cell');
            return [...hs].map(h => (h.innerText || '').trim().replace(/\\s+/g, ' '));
        }""")
    try:
        idx = {"fee": next(i for i, c in enumerate(cols) if "费用代码" in c),
               "cur": next(i for i, c in enumerate(cols) if "币种" in c),
               "qty": next(i for i, c in enumerate(cols) if "数量" in c),
               "price": next(i for i, c in enumerate(cols) if "单价" in c),
               "amt": next(i for i, c in enumerate(cols) if c.startswith("金额"))}
    except StopIteration:
        _die(page, cfg, f"{prefix}_grid_cols", f"表头列识别失败: {cols}")
    r0 = rows.first
    r0.locator(".ui-grid-cell").nth(idx["fee"]).click()
    page.wait_for_timeout(1000)
    page.keyboard.type(cfg.fee_code, delay=50)
    page.wait_for_timeout(2200)
    # 候选挂在 body 层（ui-select appendToBody），全局找
    hit = page.locator(f".ui-select-choices-row:has-text('{cfg.fee_code}'), "
                       f".ui-select-highlight:has-text('{cfg.fee_code}')")
    if not hit.count():
        _die(page, cfg, f"{prefix}_fee_pick", f"费用代码 {cfg.fee_code} 无候选")
    hit.first.click()
    page.wait_for_timeout(1000)
    fee_txt = r0.locator(".ui-grid-cell").nth(idx["fee"]).inner_text().strip()
    if cfg.fee_code not in fee_txt.upper():
        _die(page, cfg, f"{prefix}_fee_check", f"费用代码格={fee_txt!r}")
    _shot(page, cfg, f"{prefix}_02_fee")

    # 币种（行内原生 select）
    r0.locator(".ui-grid-cell").nth(idx["cur"]).click()
    page.wait_for_timeout(800)
    target = row.currency
    j = page.evaluate(
        """(cur) => {
            const row = document.querySelector('invoice-entry-freight-ap .ui-grid-row');
            if (!row) return -1;
            const sels = [...row.querySelectorAll('select')];
            for (let i = 0; i < sels.length; i++) {
                if ([...sels[i].options].some(o => o.text.trim() === cur)) return i;
            }
            return -1;
        }""", target)
    if j < 0:
        _die(page, cfg, f"{prefix}_currency", f"行内无 {target} 币种选项")
    r0.locator("select").nth(j).select_option(label=target)
    page.wait_for_timeout(600)

    # 数量 1 × 单价 = 摊分金额（口径：金额自动算）
    price = f"{row.amount:.2f}"
    r0.locator(".ui-grid-cell").nth(idx["qty"]).click()
    page.wait_for_timeout(600)
    page.keyboard.press("Control+a")
    page.keyboard.type("1", delay=40)
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    r0.locator(".ui-grid-cell").nth(idx["price"]).click()
    page.wait_for_timeout(600)
    page.keyboard.press("Control+a")
    page.keyboard.type(price, delay=40)
    page.keyboard.press("Enter")
    page.wait_for_timeout(1000)
    amt_txt = r0.locator(".ui-grid-cell").nth(idx["amt"]).inner_text().strip()
    try:
        amt_val = float(amt_txt.replace(",", ""))
    except ValueError:
        _die(page, cfg, f"{prefix}_amt", f"金额格读不出数字：{amt_txt!r}")
    if abs(amt_val - row.amount) > 0.005:   # 整数金额格显示 260 而非 260.00，按数值比
        _die(page, cfg, f"{prefix}_amt", f"金额格={amt_txt!r} 期望 {price}")
    _shot(page, cfg, f"{prefix}_03_row")

    if not submit:
        return "--dry：只填未提交"

    # 提交 + 验证（留在表单页=校验失败）。
    # 同供应商同发票号第二笔起会弹确认框「此供应商发票号码已存在 你确定要继续储存？」
    # ——多 TK 摊分发票属正常业务，点「确认」继续。
    page.locator("button:has-text('储存')").first.click()
    note = ""
    saved = False
    for _ in range(10):  # 最多 ~20s
        page.wait_for_timeout(2000)
        if "/accounting/invoice/entry" not in page.url:
            saved = True
            break
        body = page.inner_text("body")
        if "已存在" in body and "确定要继续储存" in body:
            page.locator("button:has-text('确认')").first.click()
            note = "同供应商发票号重复，已确认继续"
            _shot(page, cfg, f"{prefix}_04_dup_confirm")
            continue
        if "已存在" in body and "确定要继续吗" in body:
            # MBL（柜号）层级防重：该供应商在此 MBL 上已有应付单——本发票可能已被
            # 专人挂在 MBL 层录过（TK 会计页看不到），绝不能自动点「继续」造成真重复。
            # 截图留证、该行转人工：核对既有单后用 mark 补记，或确认无误再手工录。
            _shot(page, cfg, f"{prefix}_04_mbl_dup")
            hit = next((l.strip() for l in body.splitlines() if "已存在" in l), "")
            _die(page, cfg, f"{prefix}_mbl_dup",
                 f"MBL 层级防重弹窗，转人工核对（弹窗列的既有单里若有本发票号=已录过，"
                 f"用 mark 补记勿重录）：{hit[:90]}")
    _shot(page, cfg, f"{prefix}_04_after_save")
    if not saved:
        body = page.inner_text("body")[:400].replace("\n", " | ")
        _die(page, cfg, f"{prefix}_save", f"提交后仍在表单页：{body[:120]}")
    page.goto(f"{cfg.base_url}/truck/shipment/accounting/{row.tk}/",
              wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(8000)
    _shot(page, cfg, f"{prefix}_05_verify")
    b2 = page.inner_text("body")
    if row.invoice_no not in b2:
        _die(page, cfg, f"{prefix}_verify", f"会计页没找到发票号 {row.invoice_no}")
    return note


def run_batch(rows: List[ApRow], vendor_map: Dict[str, Dict[str, str]], cfg: GfConfig,
              submit: bool = True,
              on_result: Optional[Callable[[RowResult], None]] = None) -> List[RowResult]:
    """一个浏览器会话顺序录完所有行；单行失败截图记错、继续下一行。"""
    from playwright.sync_api import sync_playwright  # 延迟 import：plan/测试不需要

    results: List[RowResult] = []
    with sync_playwright() as pw:
        browser = _launch(pw)
        ctx = browser.new_context(
            storage_state=cfg.browser_state if Path(cfg.browser_state).exists() else None,
            viewport={"width": 1680, "height": 950},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
            locale="zh-CN",
        )
        page = ctx.new_page()
        page.set_default_timeout(20000)
        for i, row in enumerate(rows, 1):
            prefix = f"{i:02d}_inv{row.invoice_no}_{row.tk.replace('-', '')}"
            res = RowResult(row=row, ok=False)
            try:
                note = submit_one(page, row, vendor_map[row.vendor], cfg, prefix, submit)
                res.ok, res.note = True, note
                print(f"  [{i}/{len(rows)}] {row.key} → OK {note}")
            except GoFreightStepError as e:
                res.step, res.msg = e.step, e.msg
                print(f"  [{i}/{len(rows)}] {row.key} → 失败@{e.step} {e.msg}")
            except Exception as e:  # noqa: BLE001 — 单行意外崩溃不应终止整批
                res.step, res.msg = "unexpected", str(e)[:200]
                _shot(page, cfg, f"{prefix}_FAIL_unexpected")
                print(f"  [{i}/{len(rows)}] {row.key} → 意外异常 {str(e)[:120]}")
            results.append(res)
            try:
                ctx.storage_state(path=cfg.browser_state)  # 会话落盘，断点续跑
            except Exception:
                pass
            if on_result:
                on_result(res)
        browser.close()
    return results
