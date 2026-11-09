# -*- coding: utf-8 -*-
"""vendors.py 离线测试：名称归一化 / 搜索词推导 / 候选解析 / 唯一吻合判定 / 缓存。

真实形状锚点（2026-11 实测，示例为虚构名）：下拉候选文本形如
"SHORTNAME\\n(CODE) / ()"，表内是全称——靠归一化互含配上（系统常用短名）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gofreight_ap.vendors import (VendorCache, derive_search_terms, match_vendor,  # noqa: E402
                                  merged_map, normalize_name, parse_candidate)


# ---------- normalize_name ----------

def test_normalize_strips_punct_and_case():
    assert normalize_name("Bluegate Logistics Solutions Inc.") == "BLUEGATE LOGISTICS SOLUTIONS INC"
    assert normalize_name("  Northline International Logistics Co.,Ltd ") == \
        "NORTHLINE INTERNATIONAL LOGISTICS CO LTD"


def test_normalize_empty():
    assert normalize_name("") == ""
    assert normalize_name("., ") == ""


# ---------- derive_search_terms ----------

def test_terms_first_word_then_longest():
    # 首词最有辨识度排最前；Inc/Co/Ltd 等法律后缀被丢弃；无空格
    terms = derive_search_terms("Bluegate Logistics Solutions Inc.")
    assert terms[0] == "BLUEGATE"
    assert "INC" not in terms
    assert all(" " not in t for t in terms)
    assert set(terms) == {"BLUEGATE", "LOGISTICS", "SOLUTIONS"}


def test_terms_single_word():
    assert derive_search_terms("Redwood Transport") == ["REDWOOD", "TRANSPORT"]


def test_terms_too_short_all():
    assert derive_search_terms("Co. Ltd.") == []
    assert derive_search_terms("Northline International Logistics Co.,Ltd")[-1] != "CO"


# ---------- parse_candidate ----------

def test_parse_candidate_shapes():
    assert parse_candidate("BLUEGATE (VN-000009)") == ("BLUEGATE", "VN-000009")
    assert parse_candidate("Northline International Logistics Co.,Ltd (VN-000171)") == \
        ("Northline International Logistics Co.,Ltd", "VN-000171")
    assert parse_candidate("ATL LOGISTICS  (VN-000092)  ") == ("ATL LOGISTICS", "VN-000092")


def test_parse_candidate_multiline_text():
    # 真实下拉候选 inner_text 带换行与尾巴（2026-11 实测形状）
    assert parse_candidate("BLUEGATE\n(VN-000009) / ()") == ("BLUEGATE", "VN-000009")


def test_parse_candidate_no_code():
    assert parse_candidate("没有编码的行") is None
    assert parse_candidate("") is None
    assert parse_candidate("(VN-1)") is None  # 无名称


# ---------- match_vendor ----------

def test_match_unique_by_containment():
    # 系统短名 ⊂ 表内全称 + 唯一候选 → 采用
    verdict, hit = match_vendor("Bluegate Logistics Solutions Inc.",
                                [parse_candidate("BLUEGATE\n(VN-000009) / ()")])
    assert verdict == "unique" and hit == ("VN-000009", "BLUEGATE")


def test_match_unique_by_reverse_containment():
    # 表内名比系统名还短（系统带后缀）
    verdict, hit = match_vendor("Redwood Transport",
                                [parse_candidate("Redwood Transport Inc (VN-000106)")])
    assert verdict == "unique" and hit[0] == "VN-000106"


def test_match_equal_after_normalize():
    verdict, hit = match_vendor("Northline International Logistics Co.,Ltd",
                                [parse_candidate("Northline International Logistics Co.,Ltd (VN-000171)")])
    assert verdict == "unique" and hit[0] == "VN-000171"


def test_match_ambiguous_two_hits():
    # 表内短名同时含于两家候选 → 歧义转人工
    verdict, hit = match_vendor("ATL",
                                [parse_candidate("ATL LOGISTICS (VN-000092)"),
                                 parse_candidate("ATLASLINE TRANSPORT (VN-000123)")])
    assert verdict == "ambiguous" and hit is None


def test_match_full_name_beats_lookalike():
    # 表内全称与某候选整串相等（强匹配），近似名不含全称 → 唯一
    verdict, hit = match_vendor("ATL Logistics",
                                [parse_candidate("ATL LOGISTICS (VN-000092)"),
                                 parse_candidate("ATLASLINE TRANSPORT (VN-000123)")])
    assert verdict == "unique" and hit[0] == "VN-000092"


def test_match_none():
    verdict, hit = match_vendor("Northline Transport",
                                [parse_candidate("SOMETHING ELSE (VN-1)")])
    assert verdict == "none" and hit is None
    assert match_vendor("", [parse_candidate("X (VN-1)")])[0] == "none"


# ---------- VendorCache + merged_map ----------

def test_cache_roundtrip_and_merge(tmp_path):
    p = tmp_path / "gvc.json"
    cache = VendorCache(str(p))
    assert cache.get("Bluegate Logistics Solutions Inc.") is None
    cache.put("Bluegate Logistics Solutions Inc.", "BLUEGATE", "VN-000009")
    cache.save()
    cache2 = VendorCache(str(p))
    assert cache2.get("Bluegate Logistics Solutions Inc.") == \
        {"search": "BLUEGATE", "code": "VN-000009"}
    # env 人工映射优先于缓存（同名不覆盖）
    env = {"Bluegate Logistics Solutions Inc.": {"search": "BG", "code": "VN-999999"}}
    merged = merged_map(env, cache2)
    assert merged["Bluegate Logistics Solutions Inc."]["code"] == "VN-999999"
    # 缓存补缺
    merged2 = merged_map({}, cache2)
    assert merged2["Bluegate Logistics Solutions Inc."]["code"] == "VN-000009"


def test_cache_corrupt_recovers(tmp_path):
    p = tmp_path / "gvc.json"
    p.write_text("{bad json", encoding="utf-8")
    assert VendorCache(str(p)).vendors == {}
