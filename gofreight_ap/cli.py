# -*- coding: utf-8 -*-
"""内部命令行（python -m gofreight_ap，项目根目录运行）：
  plan                          读多维表 → 打印待录/跳过清单（默认）
  submit [--yes] [...]          真正提交（缺 --yes 时只出计划，红线）
  mark --invoice N --tk TK      手工补记「已建立应付」（防重）
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from datetime import datetime, timezone
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

    def on_result(res) -> None:
        if res.ok:
            r = res.row
            state.mark(r.key, {
                "invoice_no": r.invoice_no, "tk": r.tk, "amount": r.amount,
                "currency": r.currency, "fee_code": gfc.fee_code,
                "record_id": r.record_id, "note": res.note,
                "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            state.save()

    results = run_batch(todo, vendor_map, gfc, submit=not args.dry, on_result=on_result,
                        vendor_cache=vendor_cache)
    fails = [r for r in results if not r.ok]
    print(f"\n完成：成功 {len(results) - len(fails)}/{len(results)}；"
          f"失败 {len(fails)}（截图见 {gfc.shot_dir}/）")
    for r in fails:
        print(f"  失败 {r.row.key} @{r.step} {r.msg}")
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

    args = p.parse_args(argv)
    try:
        return {"plan": cmd_plan, "submit": cmd_submit, "mark": cmd_mark}.get(
            args.cmd, cmd_plan)(args)
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
