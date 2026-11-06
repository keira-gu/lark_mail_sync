# -*- coding: utf-8 -*-
"""配置加载：.env 文件 + 进程环境变量（进程变量优先）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


def load_env_file(path: str) -> Dict[str, str]:
    """解析简单 KEY=VALUE 行；忽略注释与空行；去引号。"""
    env: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


class ConfigError(ValueError):
    pass


# 多维表字段类型编号（bitable field type：1=文本 2=数字 5=日期 17=附件 1001=创建时间 1002=修改时间 1005=修改人）
FIELD_TYPE_TEXT = 1
FIELD_TYPE_NUMBER = 2
FIELD_TYPE_DATETIME = 5
FIELD_TYPE_ATTACHMENT = 17


@dataclass(frozen=True)
class Config:
    base_url: str
    app_id: str
    app_secret: str
    mailbox_id: str
    folder_id: Optional[str]
    message_format: str
    bitable_app_id: str
    bitable_app_secret: str
    bitable_app_token: str
    bitable_table_id: str
    confirm_keyword: str
    field_date: str
    field_subject: str
    field_recipient: str
    field_tk: str
    field_qty: str
    field_alloc_amount: str
    field_invoice_amount: str
    field_vendor: str
    field_our_company: str
    field_currency: str
    field_invoice_date: str
    field_invoice_due: str
    field_invoice_name: str
    field_invoice_no: str
    field_invoice_file: str
    field_error: str
    field_fee_item: str
    field_msg_id: str
    attachment_max_bytes: int
    sync_interval_seconds: int
    scan_stop_after_seen: int
    archive_folder: str
    state_path: str
    request_timeout: int

    @classmethod
    def from_env(cls, env: Dict[str, str]) -> "Config":
        def req(key: str) -> str:
            v = env.get(key, "").strip()
            if not v:
                raise ConfigError(f"缺少必填配置 {key}")
            return v

        def opt(key: str, default: str = "") -> str:
            return env.get(key, default).strip()

        return cls(
            base_url=opt("LARK_BASE_URL", "https://open.larksuite.com").rstrip("/"),
            app_id=req("LARK_APP_ID"),
            app_secret=req("LARK_APP_SECRET"),
            mailbox_id=req("MAIL_USER_MAILBOX_ID"),
            folder_id=opt("MAIL_FOLDER_ID") or None,
            message_format=opt("MAIL_MESSAGE_FORMAT", "full"),
            bitable_app_id=opt("BITABLE_APP_ID") or req("LARK_APP_ID"),
            bitable_app_secret=opt("BITABLE_APP_SECRET") or req("LARK_APP_SECRET"),
            bitable_app_token=req("BITABLE_APP_TOKEN"),
            bitable_table_id=req("BITABLE_TABLE_ID"),
            confirm_keyword=opt("CONFIRM_KEYWORD", "confirm"),
            field_date=opt("FIELD_DATE", "收到邮件日期"),
            field_subject=opt("FIELD_SUBJECT", "邮件主题"),
            field_recipient=opt("FIELD_RECIPIENT", "收件人邮箱号"),
            field_tk=opt("FIELD_TK", "TK号"),
            field_qty=opt("FIELD_QTY", "数量"),
            field_alloc_amount=opt("FIELD_ALLOC_AMOUNT", "摊分金额"),
            field_invoice_amount=opt("FIELD_INVOICE_AMOUNT", "发票金额"),
            field_vendor=opt("FIELD_VENDOR", "服务商名称"),
            field_our_company=opt("FIELD_OUR_COMPANY", "我司名称"),
            field_currency=opt("FIELD_CURRENCY", "发票币种"),
            field_invoice_date=opt("FIELD_INVOICE_DATE", "开票日期"),
            field_invoice_due=opt("FIELD_INVOICE_DUE", "发票DUE date"),
            field_invoice_name=opt("FIELD_INVOICE_NAME", "发票名称"),
            field_invoice_no=opt("FIELD_INVOICE_NO", "发票号"),
            field_invoice_file=opt("FIELD_INVOICE_FILE", "发票"),
            field_error=opt("FIELD_ERROR", "异常报错"),
            field_fee_item=opt("FIELD_FEE_ITEM", "费用项-结算手动提供"),
            field_msg_id=opt("FIELD_MSG_ID", "消息ID"),
            attachment_max_bytes=int(float(opt("ATTACHMENT_MAX_MB", "30")) * 1024 * 1024),
            sync_interval_seconds=int(opt("SYNC_INTERVAL_SECONDS", "300")),
            scan_stop_after_seen=int(opt("SCAN_STOP_AFTER_SEEN", "100")),
            archive_folder=opt("ARCHIVE_FOLDER_ID", "ARCHIVED"),
            state_path=opt("STATE_PATH", "data/state.json"),
            request_timeout=int(opt("REQUEST_TIMEOUT", "30")),
        )

    @classmethod
    def load(cls, env_file: Optional[str] = None) -> "Config":
        env: Dict[str, str] = {}
        if env_file:
            env.update(load_env_file(env_file))
        env.update({k: v for k, v in os.environ.items() if k in _ALLOWED_KEYS})
        return cls.from_env(env)

    def field_specs(self) -> List[Tuple[str, int, str]]:
        """(字段名, 类型, 数字格式) 清单，用于 ensure-fields 自检/补建。

        服务商名称/我司名称/发票币种/开票日期/发票DUE date/发票名称 由发票 PDF 识别，
        只写每组首行；消息ID 为系统幂等列（rebuild-state 依赖），建议在视图里隐藏。
        """
        return [
            (self.field_date, FIELD_TYPE_DATETIME, ""),
            (self.field_subject, FIELD_TYPE_TEXT, ""),
            (self.field_recipient, FIELD_TYPE_TEXT, ""),
            (self.field_tk, FIELD_TYPE_TEXT, ""),
            (self.field_qty, FIELD_TYPE_NUMBER, "0"),
            (self.field_alloc_amount, FIELD_TYPE_NUMBER, "0.00"),
            (self.field_invoice_amount, FIELD_TYPE_NUMBER, "0.00"),
            (self.field_vendor, FIELD_TYPE_TEXT, ""),
            (self.field_our_company, FIELD_TYPE_TEXT, ""),
            (self.field_currency, FIELD_TYPE_TEXT, ""),
            (self.field_invoice_date, FIELD_TYPE_DATETIME, ""),
            (self.field_invoice_due, FIELD_TYPE_DATETIME, ""),
            (self.field_invoice_name, FIELD_TYPE_TEXT, ""),
            (self.field_invoice_no, FIELD_TYPE_TEXT, ""),
            (self.field_invoice_file, FIELD_TYPE_ATTACHMENT, ""),
            (self.field_error, FIELD_TYPE_TEXT, ""),
            (self.field_fee_item, FIELD_TYPE_TEXT, ""),
            (self.field_msg_id, FIELD_TYPE_TEXT, ""),
        ]


_ALLOWED_KEYS = {
    "LARK_BASE_URL", "LARK_APP_ID", "LARK_APP_SECRET",
    "MAIL_USER_MAILBOX_ID", "MAIL_FOLDER_ID", "MAIL_MESSAGE_FORMAT",
    "BITABLE_APP_ID", "BITABLE_APP_SECRET", "BITABLE_APP_TOKEN", "BITABLE_TABLE_ID",
    "CONFIRM_KEYWORD",
    "FIELD_DATE", "FIELD_SUBJECT", "FIELD_RECIPIENT", "FIELD_TK", "FIELD_QTY",
    "FIELD_ALLOC_AMOUNT", "FIELD_INVOICE_AMOUNT", "FIELD_VENDOR",
    "FIELD_OUR_COMPANY", "FIELD_CURRENCY", "FIELD_INVOICE_DATE",
    "FIELD_INVOICE_DUE", "FIELD_INVOICE_NAME", "FIELD_INVOICE_NO", "FIELD_INVOICE_FILE",
    "FIELD_ERROR", "FIELD_FEE_ITEM", "FIELD_MSG_ID",
    "ATTACHMENT_MAX_MB", "SYNC_INTERVAL_SECONDS",
    "SCAN_STOP_AFTER_SEEN", "ARCHIVE_FOLDER_ID", "STATE_PATH", "REQUEST_TIMEOUT",
}
