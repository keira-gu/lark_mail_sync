# -*- coding: utf-8 -*-
"""GoFreight 建立应付自动化（多维表行 → GoFreight AP 单）。

口径（2026-11 试点真提交验证）：
  入口 /accounting/invoice/entry?mbl=<TK>&type=B
  发布日期 = 开票日期；发票日期 = 开票日期；到期日 = 发票DUE date
  费用代码 = GOFREIGHT_FEE_CODE（必填配置，无默认值）；数量 1 × 单价 = 摊分金额；币种 = 表内发票币种

防重：gofreight_state.json 按「发票号|TK」记账，提交成功即落盘，重跑只处理未录行；
提交前先查该 TK 会计页是否已有同发票号（他人手工已录 → 跳过防双录并记账）。
供应商映射：GOFREIGHT_VENDOR_MAP（JSON），没配映射的服务商只进计划清单并注明原因，绝不盲提交。
"""
from .plan import ApRow, ApState, build_plan, ms_to_date, parse_vendor_map, rows_from_bitable  # noqa: F401

__all__ = ["ApRow", "ApState", "build_plan", "ms_to_date", "parse_vendor_map", "rows_from_bitable"]
