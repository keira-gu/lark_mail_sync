# -*- coding: utf-8 -*-
"""GoFreight 建立应付自动化（多维表行 → GoFreight AP 单）。

口径（2026-11 试点真提交验证）：
  入口 /accounting/invoice/entry?mbl=<TK>&type=B
  发布日期 = 开票日期；发票日期 = 开票日期；到期日 = 发票DUE date
  费用代码 = GOFREIGHT_FEE_CODE（必填配置，无默认值）；数量 1 × 单价 = 摊分金额；币种 = 表内发票币种

防重：gofreight_state.json 按「发票号|TK」记账，提交成功即落盘，重跑只处理未录行；
提交前先查该 TK 会计页是否已有同发票号（他人手工已录 → 跳过防双录并记账）。
供应商编码：GOFREIGHT_VENDOR_MAP（JSON，人工确认优先）+ 自动解析缓存
（gofreight_vendors.json）——无映射时 submit 在供应商下拉搜，唯一候选且名称
吻合才采用并缓存（vendors.py），失败该行转人工，绝不瞎选。
发票备忘录（2026-11-10 起）：应付成功后在该 TK 会计页「备忘录」区建「标题=发票号」
备忘录并上传多维表「发票」附件列的 PDF（memo.py，控件当日实弹定稿）；失败不影响
应付记账，`gofreight_ap memo --yes` 可补传，多 TK 摊分发票每个 TK 各挂一份。
"""
from .plan import ApRow, ApState, build_plan, ms_to_date, parse_vendor_map, rows_from_bitable  # noqa: F401
from .vendors import (VendorCache, derive_search_terms, match_vendor,  # noqa: F401
                      merged_map, normalize_name, parse_candidate)

__all__ = [
    "ApRow", "ApState", "build_plan", "ms_to_date", "parse_vendor_map", "rows_from_bitable",
    "VendorCache", "derive_search_terms", "match_vendor", "merged_map", "normalize_name",
    "parse_candidate",
]
