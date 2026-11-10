# -*- coding: utf-8 -*-
"""内部命令行（python -m gofreight_ap，项目根目录运行）：
  plan                          读多维表 → 打印待录/跳过清单（默认）
  submit [--yes] [...]          真正提交（缺 --yes 时只出计划，红线）；成功后自动传发票备忘录
  memo [--yes] [--tk/--invoice] 给已记账的行补传发票备忘录（应付不动）
  mark --invoice N --tk TK      手工补记「已建立应付」（防重）
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Set

try:  # Windows 控制台中文
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from lark_mail_sync.config import Config, ConfigError, load_env_file
from lark_mail_sync.lark_client import LarkClient, LarkError

from .plan import ApState, build_plan, parse_vendor_map, rows_from_bitable
from .submit import GfConfig, run_batch
from .vendors import VendorCache, merged_map


def _gf(env: dict, key: str, default: str = "") -> str:
    # .env 文件值 + 进程环境变量（进程优先），只在本模块内用，不进公共 Config
    return os.environ.get(key, "").strip() or (env.get(key, default).strip())


def load_all(env_file: str):
    cfg = Config.load(env_file)
    raw = load_env_file(env_file)
    return cfg, raw


def _gather(args) -> tuple:
    cfg, raw = load_all(args.env_file)
    base = _gf(raw, "GOFREIGHT_BASE_URL")
    user = _gf(raw, "GOFREIGHT_USER")
    pwd = _gf(raw, "GOFREIGHT_PASSWORD")
    fee_code = _gf(raw, "GOFREIGHT_FEE_CODE")
    missing = [k for k, v in [("GOFREIGHT_BASE_URL", base), ("GOFREIGHT_USER", user),
                              ("GOFREIGHT_PASSWORD", pwd),
                              ("GOFREIGHT_FEE_CODE", fee_code)] if not v]
    if missing:
        raise ConfigError(f".env 缺 {missing}（GoFreight 建立应付需要）")
    vendor_cache = VendorCache(_gf(raw, "GOFREIGHT_VENDOR_CACHE", "data/gofreight_vendors.json"))
    vendor_map = merged_map(parse_vendor_map(_gf(raw, "GOFREIGHT_VENDOR_MAP")), vendor_cache)
    state = ApState(_gf(raw, "GOFREIGHT_STATE_PATH", "data/gofreight_state.json"))
    gfc = GfConfig(
        base_url=base.rstrip("/"), user=user, password=pwd,
        fee_code=fee_code,
        shot_dir=_gf(raw, "GOFREIGHT_SHOT_DIR", "runtime-outputs/gofreight"),
        browser_state=_gf(raw, "GOFREIGHT_BROWSER_STATE", "runtime-outputs/gofreight/state.json"),
    )
    return cfg, vendor_map, state, gfc, raw, vendor_cache


def _plan(args) -> int:
    cfg, vendor_map, state, gfc, raw, vendor_cache = _gather(args)
    due_fallback = int(_gf(raw, "GOFREIGHT_DUE_FALLBACK_DAYS", "0"))
    invoices: Optional[Set[str]] = set(args.invoice) if args.invoice else None
    tks: Optional[Set[str]] = set(args.tk) if args.tk else None

    rows = rows_from_bitable(LarkClient(cfg), cfg)
    todo, skipped = build_plan(rows, set(state.entries),
                               due_fallback, invoices, tks)
    if args.limit:
        extra = todo[args.limit:]
        skipped += [(r.key, "超出 --limit 本轮不录") for r in extra]
        todo = todo[: args.limit]

    print(f"GoFreight 建立应付计划：待录 {len(todo)} 行，跳过 {len(skipped)} 行"
          f"（费用代码 {gfc.fee_code}，数量 1 × 单价=摊分金额）")
    for i, r in enumerate(todo, 1):
        v = vendor_map.get(r.vendor)
        dest = v["code"] if v else "自动解析"
        print(f"  {i:2d}) 发票{r.invoice_no} | {r.tk} | {r.vendor}→{dest} "
              f"| {r.amount:.2f} {r.currency} | 开票{r.invoice_date:%Y-%m-%d} "
              f"到期{r.due_date:%Y-%m-%d}")
    for key, why in skipped:
        print(f"   - {key} ：{why}")
    return todo, skipped, state, gfc, vendor_map, vendor_cache


def cmd_plan(args) -> int:
    _plan(args)
    return 0


def cmd_submit(args) -> int:
    if not args.yes:
        _plan(args)
        print("\n以上为计划，未提交。确认无误后加 --yes 执行（每行独立提交，失败自动跳下一行）。")
        return 0
    todo, _skipped, state, gfc, vendor_map, vendor_cache = _plan(args)
    cfg, _raw = load_all(args.env_file)
    from .memo import MemoCtx
    memo_ctx = MemoCtx(client=LarkClient(cfg), cfg=cfg,
                       att_dir=Path(_gf(_raw, "GOFREIGHT_ATTACHMENT_DIR",
                                        "runtime-outputs/gofreight/attachments")))

    def on_result(res) -> None:
        if res.ok:
            r = res.row
            info = {
                "invoice_no": r.invoice_no, "tk": r.tk, "amount": r.amount,
                "currency": r.currency, "fee_code": gfc.fee_code,
                "record_id": r.record_id, "note": res.note,
                "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if res.memo_ok is not None:
                info["memo_ok"], info["memo_note"] = res.memo_ok, res.memo_note
            state.mark(r.key, info)
            state.save()

    results = run_batch(todo, vendor_map, gfc, submit=not args.dry, on_result=on_result,
                        vendor_cache=vendor_cache, memo_ctx=memo_ctx)
    fails = [r for r in results if not r.ok]
    memo_fails = [r for r in results if r.memo_ok is False]
    print(f"\n完成：应付成功 {len(results) - len(fails)}/{len(results)}；"
          f"失败 {len(fails)}（截图见 {gfc.shot_dir}/）")
    for r in fails:
        print(f"  失败 {r.row.key} @{r.step} {r.msg}")
    if memo_fails:
        print(f"备忘录未传 {len(memo_fails)} 行（应付已成立，跑 `gofreight_ap memo --yes` 补传）：")
        for r in memo_fails:
            print(f"  备忘录 {r.row.key} {r.memo_note}")
    return 1 if fails else 0


def cmd_memo(args) -> int:
    """给已建立应付（防重账本在册）的行补传发票备忘录；--tk/--invoice 可指定行。"""
    cfg, _raw = load_all(args.env_file)
    client = LarkClient(cfg)
    state = ApState(_gf(_raw, "GOFREIGHT_STATE_PATH", "data/gofreight_state.json"))
    rows = rows_from_bitable(client, cfg)
    if args.tk:
        rows = [r for r in rows if r.tk in set(args.tk)]
    if args.invoice:
        rows = [r for r in rows if r.invoice_no in set(args.invoice)]
    rows = [r for r in rows if state.is_submitted(r.key)]   # 只补已记账的，绝不绕过防重
    with_files = [r for r in rows if r.invoice_files]
    no_files = [r for r in rows if not r.invoice_files]
    print(f"备忘录补传计划：{len(rows)} 行在册，其中 {len(with_files)} 行有发票附件"
          f"{f'，{len(no_files)} 行无附件跳过' if no_files else ''}")
    for i, r in enumerate(with_files, 1):
        files = ", ".join(a.get("name", "?") for a in r.invoice_files)
        print(f"  {i:2d}) {r.key} ← {files}")
    if not with_files:
        return 0
    if not args.yes:
        print("\n以上为计划，未执行。确认后加 --yes（只传备忘录，不动应付）。")
        return 0
    gfc = GfConfig(
        base_url=_gf(_raw, "GOFREIGHT_BASE_URL").rstrip("/"), user=_gf(_raw, "GOFREIGHT_USER"),
        password=_gf(_raw, "GOFREIGHT_PASSWORD"), fee_code=_gf(_raw, "GOFREIGHT_FEE_CODE"),
        shot_dir=_gf(_raw, "GOFREIGHT_SHOT_DIR", "runtime-outputs/gofreight"),
        browser_state=_gf(_raw, "GOFREIGHT_BROWSER_STATE", "runtime-outputs/gofreight/state.json"))
    from .memo import MemoCtx, run_memo_batch
    memo_ctx = MemoCtx(client=client, cfg=cfg,
                       att_dir=Path(_gf(_raw, "GOFREIGHT_ATTACHMENT_DIR",
                                        "runtime-outputs/gofreight/attachments")))
    results = run_memo_batch(with_files, gfc, memo_ctx)
    fails = [r for r in results if not r["ok"]]
    print(f"\n备忘录完成：成功 {len(results) - len(fails)}/{len(results)}")
    for r in fails:
        print(f"  失败 {r['row'].key} {r['note']}")
    return 1 if fails else 0


def cmd_mark(args) -> int:
    if not args.invoice or not args.tk:
        print("mark 需要 --invoice 和 --tk")
        return 2
    cfg, _raw = load_all(args.env_file)
    state = ApState(_gf(_raw, "GOFREIGHT_STATE_PATH", "data/gofreight_state.json"))
    key = f"{args.invoice}|{args.tk}"
    state.mark(key, {"note": args.note or "手工补记",
                     "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    state.save()
    print(f"已补记：{key}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="gofreight_ap",
                                description="多维表 → GoFreight 建立应付（内部）")
    p.add_argument("--env-file", default=".env")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("plan", help="打印待录/跳过清单")
    _add_filters(sp)

    sp = sub.add_parser("submit", help="逐行提交建立应付")
    sp.add_argument("--yes", action="store_true", help="确认提交（缺省只出计划）")
    sp.add_argument("--dry", action="store_true", help="只填表单不点储存（联调）")
    _add_filters(sp)

    sp = sub.add_parser("mark", help="手工补记已建立应付（防重）")
    sp.add_argument("--invoice", required=True)
    sp.add_argument("--tk", required=True)
    sp.add_argument("--note", default="")

    sp = sub.add_parser("memo", help="给已记账的行补传发票备忘录")
    sp.add_argument("--yes", action="store_true", help="确认执行（缺省只出计划）")
    _add_filters(sp)

    args = p.parse_args(argv)
    try:
        return {"plan": cmd_plan, "submit": cmd_submit, "mark": cmd_mark,
                "memo": cmd_memo}.get(args.cmd, cmd_plan)(args)
    except (ConfigError, ValueError) as e:
        print(f"配置错误：{e}")
        return 2
    except LarkError as e:
        print(f"Lark API 错误：{e}")
        return 3


def _add_filters(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--invoice", action="append", help="只处理该发票号（可多次）")
    sp.add_argument("--tk", action="append", help="只处理该 TK（可多次）")
    sp.add_argument("--limit", type=int, default=None, help="本轮最多录 N 行")


if __name__ == "__main__":
    raise SystemExit(main())
