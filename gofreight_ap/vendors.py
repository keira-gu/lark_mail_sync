# -*- coding: utf-8 -*-
"""供应商编码：.env 手动映射 + 自动解析缓存。

设计（2026-11-09 定版，用户拍板「自动解析+缓存」）：
- .env GOFREIGHT_VENDOR_MAP = 人工确认过的映射（白名单，最高优先）；
- data/gofreight_vendors.json = submit 自动解析的缓存（新供应商第一次提交时
  在建立应付表单的供应商下拉里搜，唯一候选且名称吻合 → 记入缓存，下次直接命中）；
- 解析失败（多候选/对不上/搜不到）绝不瞎选，该行转人工——录错供应商=挂错账。

本模块纯逻辑（无 playwright）：名称归一化、搜索词推导、候选解析、匹配判定、
缓存读写。浏览器交互在 submit.py。"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

# 候选文本形如 "SHORTNAME (VN-000009)"：名称 + 括号内编码
_CODE_RE = re.compile(r"\(([A-Z]{1,6}-[A-Z0-9]{2,})\)")
# 归一化时丢弃的标点（名称两侧书写差异：Inc. / Co.,Ltd / 顿号空格）
_PUNCT_RE = re.compile(r"[^0-9A-Z\s]")
_WS_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """大写 + 去标点 + 压空白："Bluegate Logistics Solutions Inc." → "BLUEGATE LOGISTICS SOLUTIONS INC"."""
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", (name or "").upper())).strip()


# 法律形态后缀：无辨识度且极易撞一堆候选，推导搜索词时丢弃
_LEGAL_SUFFIXES = {"INC", "LTD", "LLC", "CO", "CORP", "CORPORATION", "COMPANY",
                   "LIMITED", "PLC", "GMBH", "SA", "NV", "BV", "PTE", "PTY", "SRL", "AG", "SE"}


def derive_search_terms(vendor_name: str) -> List[str]:
    """从表内服务商名推导下拉搜索词（ng-select 服务端搜索：必须无空格短词）。

    顺序：首词（通常最具辨识度）→ 其余词按长度降序；跳过 <3 字符碎片和
    法律后缀（Inc / Ltd / Co.,Ltd 之类既无辨识度又可能撞一堆候选）。"""
    raw = re.split(r"[\s,]+", (vendor_name or "").upper())
    words = []
    for w in raw:
        w = re.sub(r"^[^0-9A-Z]+|[^0-9A-Z]+$", "", w)  # 剥词缘标点（Inc. → INC）
        if len(w) >= 3 and w not in _LEGAL_SUFFIXES:
            words.append(w)
    seen, terms = set(), []
    for w in ([words[0]] if words else []) + sorted(words[1:], key=len, reverse=True):
        if w not in seen:
            seen.add(w)
            terms.append(w)
    return terms


def parse_candidate(text: str) -> Optional[Tuple[str, str]]:
    """候选行文本 → (显示名, 编码)；无编码括号返回 None。"""
    m = _CODE_RE.search(text or "")
    if not m:
        return None
    name = (text[: m.start()]).strip(" \t\r\n-,")
    if not name:
        return None
    return name, m.group(1)


def match_vendor(vendor_name: str,
                 candidates: Sequence[Optional[Tuple[str, str]]]
                 ) -> Tuple[str, Optional[Tuple[str, str]]]:
    """在候选里找 vendor_name 对应的编码。

    → ("unique", (code, 显示名)) / ("ambiguous", None) / ("none", None)
    吻合 = 归一化相等 或 互为包含（系统常用短名，如系统短名 ⊂ 表内全称）；
    包含是弱条件，靠「唯一候选」兜底：>1 个吻合即歧义转人工。"""
    want = normalize_name(vendor_name)
    if not want:
        return "none", None
    hits: List[Tuple[str, str]] = []
    for c in candidates:
        if not c:
            continue
        disp, code = c
        got = normalize_name(disp)
        if not got:
            continue
        if want == got or want in got or got in want:
            if (code, disp) not in hits:
                hits.append((code, disp))
    if len(hits) == 1:
        return "unique", hits[0]
    if len(hits) > 1:
        return "ambiguous", None
    return "none", None


class VendorCache:
    """自动解析结果缓存（JSON，原子写）。格式：
    {"version": 1, "vendors": {"Bluegate Logistics Solutions Inc.": {"search": "BLUEGATE", "code": "VN-000009"}}}"""

    def __init__(self, path: str):
        self.path = path
        self.vendors: Dict[str, Dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.vendors = {str(k): dict(v) for k, v in (d.get("vendors") or {}).items()}
        except (json.JSONDecodeError, ValueError, OSError):
            self.vendors = {}

    def get(self, vendor: str) -> Optional[Dict[str, str]]:
        spec = self.vendors.get(vendor)
        if spec and spec.get("search") and spec.get("code"):
            return {"search": spec["search"], "code": spec["code"]}
        return None

    def put(self, vendor: str, search: str, code: str) -> None:
        self.vendors[vendor] = {
            "search": search, "code": code, "source": "auto",
            "resolved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def save(self) -> None:
        d = {"version": 1, "vendors": {k: self.vendors[k] for k in sorted(self.vendors)}}
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".gvc-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def merged_map(env_map: Dict[str, Dict[str, str]],
               cache: Optional[VendorCache]) -> Dict[str, Dict[str, str]]:
    """env 手动映射优先，缓存补缺（plan/submit 统一从这里取）。"""
    out = dict(env_map)
    if cache:
        for name, spec in cache.vendors.items():
            out.setdefault(name, {"search": spec["search"], "code": spec["code"]})
    return out
