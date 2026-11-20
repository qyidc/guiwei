#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分类路由的单元测试:一级老规则不变 + 多级/占位符/sub 的新行为。

跑法:python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import organize as gw


def cfg_with(rules):
    return {"doc_genre_folders": rules, "types": {"文档": ["pdf"], "图片": ["png"]}}


class NormalizeTests(unittest.TestCase):
    def test_backslash_and_slash(self):
        self.assertEqual(gw.normalize_category("01 规章制度\\01-2 SOP"), "01 规章制度/01-2 SOP")

    def test_empty_segments_dropped(self):
        self.assertEqual(gw.normalize_category("合同//华远/"), "合同/华远")

    def test_illegal_chars(self):
        self.assertEqual(gw.normalize_category('a/b<d>:c|d?*'), "a/bdcd")  # 非法字符直接删
        self.assertNotIn(":", gw.normalize_category("x:y"))

    def test_traversal_rejected(self):
        self.assertEqual(gw.normalize_category("a/../../b"), "a/b")
        self.assertEqual(gw.normalize_category(".."), "文档")

    def test_blank_falls_back(self):
        self.assertEqual(gw.normalize_category(""), "文档")
        self.assertEqual(gw.normalize_category("   "), "文档")

    def test_top_segment(self):
        self.assertEqual(gw.top_segment("01 规章制度/01-2 SOP"), "01 规章制度")
        self.assertEqual(gw.top_segment("财务"), "财务")


class RouteTests(unittest.TestCase):
    def route(self, rules, label="", name="", tags=None):
        return gw.route_category("文档", label, name, cfg_with(rules), tags or {})

    def test_old_single_level_unchanged(self):
        rules = [{"folder": "会议", "keywords": ["会议纪要"]}]
        self.assertEqual(self.route(rules, label="会议纪要"), "会议")

    def test_non_document_passthrough(self):
        cfg = cfg_with([{"folder": "x", "keywords": ["x"]}])
        self.assertEqual(gw.route_category("图片", "x", "x.png", cfg), "图片")

    def test_static_nested(self):
        rules = [{"folder": "01 规章制度/01-2 SOP", "keywords": ["SOP"]}]
        self.assertEqual(self.route(rules, label="SOP"), "01 规章制度/01-2 SOP")

    def test_placeholder_filled(self):
        rules = [{"folder": "合同/{客户}", "keywords": ["合同"]}]
        self.assertEqual(self.route(rules, label="合同", tags={"客户": "华远科技"}), "合同/华远科技")

    def test_placeholder_empty_dropped(self):
        rules = [{"folder": "合同/{客户}", "keywords": ["合同"]}]
        self.assertEqual(self.route(rules, label="合同", tags={"客户": ""}), "合同")

    def test_placeholder_unknown_left_alone(self):
        rules = [{"folder": "合同/{不存在}", "keywords": ["合同"]}]
        self.assertEqual(self.route(rules, label="合同"), "合同/{不存在}")

    def test_year_placeholder(self):
        rules = [{"folder": "年度/{年}/付款", "keywords": ["付款"]}]
        self.assertEqual(self.route(rules, label="付款", tags={"_date": "2026-11-03"}), "年度/2026/付款")

    def test_sub_appends_tag(self):
        rules = [{"folder": "财务", "sub": "客户", "keywords": ["报价单"]}]
        self.assertEqual(self.route(rules, label="报价单", tags={"客户": "华远科技"}), "财务/华远科技")

    def test_sub_empty_keeps_parent(self):
        rules = [{"folder": "财务", "sub": "客户", "keywords": ["报价单"]}]
        self.assertEqual(self.route(rules, label="报价单", tags={"客户": ""}), "财务")

    def test_sub_with_traversal_tag_is_sanitized(self):
        rules = [{"folder": "财务", "sub": "客户", "keywords": ["报价单"]}]
        self.assertEqual(self.route(rules, label="报价单", tags={"客户": "../.."}), "财务")

    def test_first_match_wins(self):
        rules = [{"folder": "口播", "match": "name", "keywords": ["口播稿"]},
                 {"folder": "会议", "match": "name", "keywords": ["逐字稿"]}]
        self.assertEqual(self.route(rules, name="2026 口播稿 第3期 逐字稿"), "口播")

    def test_name_vs_label_source(self):
        rules = [{"folder": "知识", "keywords": ["方法论"]}]
        # 默认只看类型标签,主题里的"方法论"不该误伤
        self.assertEqual(self.route(rules, label="书籍", name="深度学习方法论.pdf"), "文档")
        self.assertEqual(self.route(rules, label="方法论"), "知识")


class DestFoldersTests(unittest.TestCase):
    def test_includes_top_segments(self):
        cfg = cfg_with([{"folder": "01 规章制度/01-2 SOP", "keywords": ["SOP"]},
                        {"folder": "合同/{客户}", "keywords": ["合同"]},
                        {"folder": "财务", "keywords": ["报价单"]}])
        names = gw.dest_folder_names(cfg)
        self.assertIn("01 规章制度", names)
        self.assertIn("01 规章制度/01-2 SOP", names)
        self.assertIn("合同", names)          # 占位符那条只贡献首段
        self.assertNotIn("合同/{客户}", names)
        self.assertIn("财务", names)
        self.assertTrue({"文档", "图片", "其他"} <= names)


if __name__ == "__main__":
    unittest.main()
