"""Unit tests for Chinese chapter / heading detection (synthetic fixtures only)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pdf2epub as p


H = 731.0
W = 509.0


def bbox_for(y0, h_pt, x0=0.25, w_pt=180.0):
    return [x0, y0 / H, x0 + w_pt / W, (y0 + h_pt) / H]


def block(typ, text, bbox, **extra):
    b = {"type": typ, "bbox": bbox, "content": [{"type": "text", "content": text}]}
    b.update(extra)
    return b


def title(text, y0, h_pt=18.0, x0=0.25, w_pt=180.0, typ="paragraph_title"):
    return block(typ, text, bbox_for(y0, h_pt, x0, w_pt))


def para(text, y0, h_pt=14.0, x0=0.07, w_pt=320.0):
    return block("text", text, bbox_for(y0, h_pt, x0, w_pt))


def figure(y0, h_pt=200.0, x0=0.12, w_pt=360.0):
    return block("image", "", bbox_for(y0, h_pt, x0, w_pt))


def mineru(pages_blocks, height_pt=H, width_pt=W):
    pages, layout = [], []
    for i, blocks in enumerate(pages_blocks):
        pages.append({"page_idx": i, "blocks": blocks})
        layout.append({"page_idx": i, "width_pt": width_pt, "height_pt": height_pt})
    return {"pages": pages, "extensions": {"docvortex_layout": {"version": 1, "pages": layout}}}


def build(pages_blocks, **kw):
    mj = mineru(pages_blocks)
    info = p.page_info_from_mineru(mj)
    return p.build_document(mj, info, img_dir=None, punct=True, **kw)


def headings(items, level=None):
    hs = [it for it in items if it["kind"] == "heading"]
    if level is not None:
        hs = [it for it in hs if it.get("level") == level]
    return hs


class ChapterRegexTests(unittest.TestCase):
    def test_chinese_and_arabic_chapter_forms(self):
        yes = [
            "前言", "序", "序言", "译者的话", "译者的话我看技术分析",
            "致谢", "致 谢", "目 录", "目录",
            "第一章 技术分析的理论基础", "第十章 摆动指数", "第十六章 资金管理",
            "第1章 引言", "第16章 Foo", "第十一章 日内点数图",
            "第二篇 总论", "第三部分 附录之前", "第五卷 资料",
            "附录一 差价交易", "附录二期权交易",
            "附录三:W·D·江恩:几何角度和百分比例",
            "全书大会串一张清单", "索引", "后记", "关于本书",
        ]
        for t in yes:
            self.assertTrue(p.is_chapter_heading(t), msg=t)

    def test_rejects_body_sentences_that_mention_a_chapter(self):
        no = [
            "第一章介绍技术分析的理论出发点，给出了其定义和前提。我始终觉得大部分围绕技术分析的争论",
            "我们在第四章讨论趋势概念时，将采用与这里几乎一致的术语。",
            "本章最后，附录了一张期货交易要目。",
            "引言",  # section, not chapter
            "结语",
            "第一节 不是章",
        ]
        for t in no:
            self.assertFalse(p.is_chapter_heading(t), msg=t)


class LineHeightTests(unittest.TestCase):
    def test_two_line_section_is_not_taller_than_one_line_chapter(self):
        # One-line chapter ~23pt vs two-line section ~40pt box — the old bug.
        items = build([[
            title("第一章 技术分析的理论基础", 300, h_pt=23.4, w_pt=273),
            para("引言", 430, h_pt=18.3, w_pt=65),
            title("技术分析在股市和期货市场应用上的简要比较", 460, h_pt=39.5, w_pt=188, x0=0.09),
            title("随机行走理论", 530, h_pt=18.3, w_pt=98),
        ]])
        ch = headings(items, 1)
        self.assertEqual([p.compact_heading(h["text"]) for h in ch],
                         [p.compact_heading("第一章 技术分析的理论基础")])
        secs = headings(items)
        self.assertTrue(any("简要比较" in h["text"] and h["level"] >= 2 for h in secs))
        self.assertTrue(any(h["text"] == "引言" and h["level"] >= 2 for h in secs))


class FalseHeadingTests(unittest.TestCase):
    def test_sentence_after_figure_is_not_a_heading(self):
        items = build([[
            title("第四章 趋势的基本概念", 80, h_pt=23.4, w_pt=231),
            para("趋势不但具有三个方向。", 120),
            figure(150, h_pt=280),
            title("有一定混乱了。", 440, h_pt=13.9, w_pt=74),
            para("例如在道氏理论中，主要趋势实际上是针对长于一年者而言。", 470),
        ]])
        texts = [h["text"] for h in headings(items)]
        self.assertNotIn("有一定混乱了。", texts)
        self.assertTrue(any(it["kind"] == "para" and "有一定混乱了。" in it["text"] for it in items))

    def test_comma_fragment_merges_or_is_demoted(self):
        items = build([[
            title("第四章 趋势的基本概念", 80, h_pt=23.4),
            title("支撑转化为阻挡，", 520, h_pt=13.9, w_pt=92, x0=0.26),
            title("反之亦然:穿越程度", 537, h_pt=15.4, w_pt=105, x0=0.26),
            para("支撑水平被市场穿越到一定程度之后，就转化为阻挡水平。", 580),
        ]])
        texts = [h["text"] for h in headings(items)]
        self.assertFalse(any(h == "支撑转化为阻挡，" for h in texts))
        self.assertTrue(any("反之亦然" in h for h in texts))

    def test_glossary_labels_beside_figure_are_not_headings(self):
        items = build([[
            title("第十二章 三点转向和优化点数图", 80, h_pt=24.1, w_pt=315, x0=0.21),
            title("图表释义", 67, h_pt=13.2, w_pt=49, x0=0.408),
            figure(80, h_pt=300, x0=0.124, w_pt=343),
            title("点数图技术", 387, h_pt=11.7, w_pt=58, x0=0.417),
            title("逐日图表的刷新", 457, h_pt=11.0, w_pt=69, x0=0.609),
            para("买入信号发生在某个X列向上超过前一个X列时。", 410, x0=0.07, w_pt=200),
        ]])
        texts = [h["text"] for h in headings(items)]
        self.assertNotIn("图表释义", texts)
        self.assertNotIn("点数图技术", texts)
        self.assertNotIn("逐日图表的刷新", texts)
        self.assertTrue(any(h.startswith("第十二章") for h in texts))


class YinYanAndSplitTests(unittest.TestCase):
    def test_yinyan_text_is_promoted(self):
        items = build([[
            title("第一章 技术分析的理论基础", 300, h_pt=23.4, w_pt=273),
            para("引言", 430, h_pt=18.3, w_pt=65, x0=0.08),
            para("技术分析是以预测市场价格变化的未来趋势为目的。", 460),
        ]])
        yin = [h for h in headings(items) if h["text"] == "引言"]
        self.assertEqual(len(yin), 1)
        self.assertGreaterEqual(yin[0]["level"], 2)

    def test_split_two_line_title_is_merged(self):
        items = build([[
            title("第一章 标题", 80, h_pt=23.4),
            title("三条移动平均线相结合，", 400, h_pt=13.9, w_pt=125, x0=0.22),
            title("或曰三重交叉法", 417, h_pt=14.6, w_pt=90, x0=0.22),
            para("既然两条移动平均线似乎比一条更好。", 450),
        ]])
        texts = [h["text"] for h in headings(items)]
        self.assertTrue(any("三重交叉法" in t and "三条移动平均线" in t for t in texts))
        self.assertNotIn("三条移动平均线相结合，", texts)


class SplitChaptersTests(unittest.TestCase):
    def test_front_matter_chapters_and_nested_toc(self):
        items = build([
            [title("前言", 300, h_pt=23.4, w_pt=86), para("这是一本讲技术分析的书。", 360)],
            [title("译者的话我看技术分析", 300, h_pt=49.7, w_pt=171),
             title("一、基础分析的尴尬", 500, h_pt=19.0, w_pt=136)],
            [title("第一章 技术分析的理论基础", 300, h_pt=23.4, w_pt=273),
             para("引言", 430, h_pt=18.3, w_pt=65, x0=0.08),
             title("理论基础", 470, h_pt=17.3, w_pt=86)],
            [title("第十章 摆动指数和相反意见理论", 300, h_pt=49.7, w_pt=231),
             para("引言", 450, h_pt=19.0, w_pt=66, x0=0.08)],
            [title("全书大会串一张清单", 300, h_pt=49.7, w_pt=128)],
            [title("附录一 差价交易和相对力度的概念", 290, h_pt=50.4, w_pt=252)],
            [title("附录二期权交易", 300, h_pt=24.1, w_pt=193)],
            [title("附录三:W·D·江恩:几何角度和百分比例", 300, h_pt=51.2, w_pt=288, typ="doc_title")],
            [title("索引", 220, h_pt=27.8, w_pt=86, x0=0.40)],
        ])
        chapters = p.split_chapters(items, "期货市场技术分析")
        titles = [p.compact_heading(c["title"]) for c in chapters]
        self.assertEqual(titles, [
            p.compact_heading(t) for t in [
                "前言", "译者的话我看技术分析",
                "第一章 技术分析的理论基础", "第十章 摆动指数和相反意见理论",
                "全书大会串一张清单",
                "附录一 差价交易和相对力度的概念", "附录二期权交易",
                "附录三:W·D·江恩:几何角度和百分比例", "索引",
            ]
        ])
        ch1 = next(c for c in chapters if c["title"].startswith("第一章"))
        nested = [it["text"] for it in ch1["items"] if it["kind"] == "heading" and it["level"] >= 2]
        self.assertIn("引言", nested)
        self.assertIn("理论基础", nested)
        self.assertGreaterEqual(p.toc_size(chapters), len(chapters) + 2)

    def test_two_line_chapter_stays_level_one(self):
        items = build([[
            title("第十章 摆动指数和相反意见理论", 300, h_pt=49.7, w_pt=231),
            title("摆动指数与趋势分析的配合用法", 420, h_pt=40.2, w_pt=145, x0=0.06),
        ]])
        h1 = headings(items, 1)
        self.assertEqual(len(h1), 1)
        self.assertTrue(h1[0]["text"].startswith("第十章"))
        h2 = [h for h in headings(items) if h["text"].startswith("摆动指数与趋势")]
        self.assertEqual(h2[0]["level"], 2)


class OverrideTests(unittest.TestCase):
    def test_custom_chapter_regex(self):
        cre = p.compile_chapter_re(r"^Lesson\s+\d+")
        items = build([[
            title("Lesson 1 Opening", 80, h_pt=24, w_pt=200, x0=0.2),
            title("A section", 140, h_pt=16, w_pt=120),
            title("Lesson 2 Next", 400, h_pt=24, w_pt=180),
        ]], chapter_re=cre, chapter_max_len=None)
        self.assertEqual([h["text"] for h in headings(items, 1)],
                         ["Lesson 1 Opening", "Lesson 2 Next"])

    def test_toc_file_sets_levels(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".toc", delete=False) as f:
            f.write("前言\n第一章 技术分析的理论基础\n  引言\n  理论基础\n")
            toc_path = Path(f.name)
        try:
            entries = p.parse_toc_file(toc_path)
            self.assertEqual([e["title"] for e in entries],
                             ["前言", "第一章 技术分析的理论基础", "引言", "理论基础"])
            self.assertEqual([e["level"] for e in entries], [1, 1, 2, 2])
            items = build([
                [title("前言", 300, h_pt=23.4, w_pt=86), para("hello", 360)],
                [title("第一章 技术分析的理论基础", 300, h_pt=23.4, w_pt=273),
                 para("引言", 430, h_pt=18.3, w_pt=65, x0=0.08),
                 title("理论基础", 470, h_pt=17.3, w_pt=86)],
            ], toc_entries=entries)
            self.assertEqual(headings(items, 1)[0]["text"], "前言")
            yin = next(h for h in headings(items) if h["text"] == "引言")
            self.assertEqual(yin["level"], 2)
        finally:
            toc_path.unlink()


class MissingImagesTests(unittest.TestCase):
    def test_build_document_without_pngs_or_img_dir(self):
        mj = mineru([[
            title("前言", 300, h_pt=23.4, w_pt=86),
            figure(80, h_pt=200),
            para("正文。", 400),
        ]])
        items = p.build_document(mj, None, None, punct=True)
        self.assertTrue(any(it["kind"] == "figure" for it in items))
        self.assertTrue(any(it["kind"] == "heading" and it["text"] == "前言" for it in items))
        chapters = p.split_chapters(items, "书")
        self.assertEqual(chapters[0]["title"], "前言")

    def test_page_info_from_json_roundtrip(self):
        mj = mineru([[title("索引", 200, h_pt=28, w_pt=86)]])
        info = p.page_info_from_mineru(mj)
        self.assertEqual(info[0]["height_pt"], H)
        self.assertIsNone(info[0]["png"])


if __name__ == "__main__":
    unittest.main()
