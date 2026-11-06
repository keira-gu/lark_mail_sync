# -*- coding: utf-8 -*-
"""命令行入口。

用法示例：
  python -m lark_mail_sync --env-file .env probe
  python -m lark_mail_sync --env-file .env fields --ensure
  python -m lark_mail_sync --env-file .env backfill --max-pages 20
  python -m lark_mail_sync --env-file .env sync
  python -m lark_mail_sync --env-file .env archive
  python -m lark_mail_sync --env-file .env rebuild-state
  python -m lark_mail_sync --env-file .env watch
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

from .config import Config, ConfigError
from .lark_client import LarkClient, LarkError, default_transport
from .state import SyncState
from .sync import MailSyncService


def _stdout_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass


def build_service(args: argparse.Namespace) -> MailSyncService:
    cfg = Config.load(args.env_file)
    client = LarkClient(cfg)
    state = SyncState(cfg.state_path)
    return MailSyncService(cfg, client, state, dry_run=args.dry_run)


def cmd_probe(args: argparse.Namespace) -> int:
    """连通与权限自检：token / 邮件 scope / 多维表协作者与字段。"""
    cfg = Config.load(args.env_file)
    transport = default_transport
    ok = True

    import json
    import urllib.error
    import urllib.request

    def raw_post(url: str, body: dict) -> tuple:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    for label, aid, sec in [("主应用(mail)", cfg.app_id, cfg.app_secret),
                            ("多维表应用", cfg.bitable_app_id, cfg.bitable_app_secret)]:
        st, d = raw_post(cfg.base_url + "/open-apis/auth/v3/tenant_access_token/internal",
                         {"app_id": aid, "app_secret": sec})
        good = bool(d.get("tenant_access_token"))
        ok = ok and good
        print(f"[token] {label}: {'OK' if good else f'FAIL {d.get('msg')}'}")

    # 邮件权限：故意探测不存在的邮箱，靠错误码区分「缺scope」vs「邮箱不存在」
    client = LarkClient(cfg)
    try:
        list(client.list_message_ids())
        print("[mail] 消息列表：OK（邮箱可读）")
    except LarkError as e:
        if e.code == 99991672:
            ok = False
            print(f"[mail] 缺权限：{e.msg}")
            print("       → 自查两件事：① 应用已开通 mail:user_mailbox.message:readonly"
                  " 且版本已发布（发布后权限才生效）")
            print("         ② 应用「可用范围」已包含本邮箱"
                  "（分步指引见 skill 的 references/应用申请指引.md 第 3、4 步）")
        else:
            print(f"[mail] 非权限类错误（邮箱不存在/参数等）：code={e.code} {e.msg[:80]}")

    try:
        fields = client.bitable_fields()
        names = [f.get("field_name") for f in fields]
        print(f"[bitable] 字段读取 OK，共 {len(names)} 个字段")
        missing = [n for n, _, _ in cfg.field_specs() if n not in names]
        if missing:
            print(f"[bitable] 缺字段 {missing}（可运行 fields --ensure 补建）")
    except LarkError as e:
        ok = False
        print(f"[bitable] 失败：{e}（检查 bitable:app scope 与应用是否已加为多维表协作者）")
    return 0 if ok else 1


def cmd_fields(args: argparse.Namespace) -> int:
    svc = build_service(args)
    missing = svc.ensure_fields(create=bool(args.ensure))
    if not missing:
        print("字段齐全。")
    elif args.ensure and not args.dry_run:
        print("缺字段已补建。")
    else:
        print(f"缺字段：{missing}（用 --ensure 补建）")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    svc = build_service(args)
    stats = svc.run(incremental=False, max_pages=args.max_pages)
    return 1 if stats.errors else 0


def cmd_sync(args: argparse.Namespace) -> int:
    svc = build_service(args)
    stats = svc.run(incremental=True)
    return 1 if stats.errors else 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    svc = build_service(args)
    n = svc.rebuild_state()
    print(f"已从多维表重建状态：{n} 个消息ID。")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    svc = build_service(args)
    r = svc.archive_processed(max_pages=args.max_pages)
    if args.dry_run:
        print(f"INBOX 扫描 {r['scanned']} 封，可归档 {r['targets']} 封（dry-run，未移动）")
    else:
        tail = f"；失败 {len(r['errors'])} 封" if r["errors"] else ""
        print(f"INBOX 扫描 {r['scanned']} 封，本次归档 {r['moved']}/{r['targets']} 封 "
              f"→ {svc.cfg.archive_folder}{tail}")
    return 1 if r["errors"] else 0


def cmd_reset_table(args: argparse.Namespace) -> int:
    if not args.yes:
        print("reset-table 会清空表内全部记录并删除旧版聚合字段；确认请加 --yes")
        return 2
    svc = build_service(args)
    if args.dry_run:
        svc.dry_run = True
    svc.reset_table()
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    svc = build_service(args)
    interval = svc.cfg.sync_interval_seconds
    print(f"watch 启动：每 {interval}s 增量同步一次（Ctrl+C 退出）")
    while True:
        try:
            stats = svc.run(incremental=True)
            if stats.errors:
                print(f"本轮有 {len(stats.errors)} 个错误（继续运行）")
        except Exception as e:  # noqa: BLE001 — 常驻进程单轮失败不应退出
            print(f"!! 本轮同步异常：{e}")
        time.sleep(interval)


def main(argv: Optional[list] = None) -> int:
    _stdout_utf8()
    parser = argparse.ArgumentParser(prog="lark_mail_sync",
                                     description="Lark 邮件 → 多维表 同步器")
    parser.add_argument("--env-file", default=".env", help=".env 路径（默认当前目录）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写入")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="连通与权限自检")
    p = sub.add_parser("fields", help="检查/补建多维表字段")
    p.add_argument("--ensure", action="store_true", help="创建缺失字段")
    p = sub.add_parser("backfill", help="全量回填")
    p.add_argument("--max-pages", type=int, default=None, help="安全阀：最多翻 N 页")
    sub.add_parser("sync", help="增量同步")
    p = sub.add_parser("archive", help="把已处理完毕的邮件移入归档文件夹（默认 ARCHIVED）")
    p.add_argument("--max-pages", type=int, default=None, help="安全阀：最多翻 N 页")
    sub.add_parser("rebuild-state", help="从多维表「消息ID」列重建本地状态")
    p = sub.add_parser("reset-table", help="清空全部记录+删除旧版聚合字段（需 --yes）")
    p.add_argument("--yes", action="store_true", help="确认执行")
    sub.add_parser("watch", help="常驻定时增量")

    args = parser.parse_args(argv)
    try:
        return {"probe": cmd_probe, "fields": cmd_fields, "backfill": cmd_backfill,
                "sync": cmd_sync, "archive": cmd_archive, "rebuild-state": cmd_rebuild,
                "watch": cmd_watch, "reset-table": cmd_reset_table,
                }[args.cmd](args)
    except ConfigError as e:
        print(f"配置错误：{e}")
        return 2
    except LarkError as e:
        print(f"Lark API 错误：{e}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
