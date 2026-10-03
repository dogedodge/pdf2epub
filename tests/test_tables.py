"""Synthetic tests for MinerU table handling (prose vs image crop, heading order)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pdf2epub as p
from test_headings import H, W, block, build, para, title


def table_block(html, bbox, caption=None):
    content = [{"type": "table_body", "bbox": bbox, "content": html}]
    if caption:
        content.append({"type": "table_caption", "bbox": bbox, "content": [
            {"type": "text", "content": caption},
        ]})
    return {"type": "table", "bbox": bbox, "content": content}


def kinds_and_texts(items):
    out = []
    for it in items:
        if it["kind"] == "heading":
            out.append(("heading", it["text"]))
        elif it["kind"] == "para":
            out.append(("para", it["text"]))
        else:
            out.append((it["kind"], it.get("src") or it.get("caption") or ""))
    return out


class SplitAndClassifyTests(unittest.TestCase):
    def test_split_ocr_paragraphs_uses_space_after_period(self):
        raw = (
            "第一段有两句。第二句中间夹着换行空 格。"
            " 第二段从缩进处开始。同一段内句号后没有空格。"
            " 第三段也是新段落。"
        )
        parts = p.split_ocr_paragraphs(raw)
        self.assertEqual(len(parts), 3)
        self.assertTrue(parts[0].startswith("第一段"))
        self.assertTrue(parts[1].startswith("第二段"))
        self.assertTrue(parts[2].startswith("第三段"))
        cleaned = [p.normalize_punct(x) for x in parts]
        self.assertIn("空格", cleaned[0])
        self.assertNotIn("空 格", cleaned[0])

    def test_split_keeps_for_example_in_same_paragraph(self):
        raw = (
            "在结束讨论之前还要指出某些重要区别。 举例来说，多数情况并非如此。"
            " 下一段才是新的段落并且足够长。"
        )
        parts = p.split_ocr_paragraphs(raw)
        self.assertEqual(len(parts), 2, parts)
        self.assertIn("举例来说", parts[0])
        self.assertTrue(parts[1].startswith("下一段"))

    def test_prose_vs_real_classification(self):
        prose_rows = [[
            "这是一段很长的说明文字。它看起来像正文而不是表格单元格。"
            "下一段仍然是完整的中文句子，用来把平均字数抬高到阈值以上。"
            "再补一句，保证这一格的字数明显高于普通表头。"
        ], [
            "另一格同样是叙述体。继续写几句，让这一列的总字数足够长。"
            " 再来一段带句号的内容，避免被当成短标签。"
            "最后再加一句收尾，这样两格都像正文。"
        ]]
        self.assertTrue(p.is_prose_table(prose_rows, n_cols=1))
        real = [
            ["品种", "价格"],
            ["大豆", "12.5"],
            ["玉米", "8.0"],
            ["小麦", "9.2"],
        ]
        self.assertFalse(p.is_prose_table(real, n_cols=2))
        # page-64 shape: several cells, but each is a long paragraph
        long = "技术分析的基本前提是市场行为包容消化一切。" * 8
        eight = [[long] for _ in range(8)]
        self.assertTrue(p.is_prose_table(eight, n_cols=1))


class ProseTableDocumentTests(unittest.TestCase):
    def test_prose_table_becomes_paragraphs_with_margin_headings(self):
        # Same geometry as the scanned chapter-end page: one column "table"
        # plus 总结 / 结语 in the left gutter.
        cell1 = (
            "第一段承接上一页写完这一句。中间有换行空 格。随后还有一句把字数补足。"
            " 第二段另起一行。它在总结之前结束。这里再写一句以免过短。"
            " 第三段仍属前文。这一句以句号收尾。并再补一句完整的说明。"
        )
        cell2 = (
            "第四段是总结后的正文。行末也会出现空 格。随后仍是同一段的句子。"
            " 第五段是结语后的第一段。同一段里句号后没有空格。再写一句。"
            " 第六段写到页末还没有结束"
        )
        html = (
            "<table><tr><td>" + cell1 + "</td></tr>"
            "<tr><td>" + cell2 + "</td></tr></table>"
        )
        items = build([
            [para("上一页最后一句没有写完所以", 680, h_pt=14.0, x0=0.28, w_pt=300)],
            [
                table_block(html, [0.241, 0.092, 0.871, 0.898]),
                title("总结", 0.437 * H, h_pt=0.026 * H, x0=0.062, w_pt=0.13 * W),
                title("结 语", 0.661 * H, h_pt=0.025 * H, x0=0.059, w_pt=0.132 * W),
                block("text", "章名页脚", [0.754, 0.912, 0.848, 0.927]),
            ],
            [para("并且在下一页把这句话续完。", 80, h_pt=20.0, x0=0.27, w_pt=300)],
        ])
        kinds = [it["kind"] for it in items]
        self.assertNotIn("figure", kinds)
        self.assertNotIn("table", kinds)
        texts = kinds_and_texts(items)
        # footer-band leftover dropped; no whole-page image
        self.assertFalse(any(t[1] == "章名页脚" for t in texts))
        paras = [t for k, t in texts if k == "para"]
        heads = [t for k, t in texts if k == "heading"]
        self.assertIn("总结", heads)
        self.assertIn("结语", heads)
        # cross-page merge at both ends
        self.assertTrue(any("没有写完所以" in t and "第一段承接" in t for t in paras))
        self.assertTrue(any("还没有结束" in t and "并且在下一页" in t for t in paras))
        # no stray CJK spaces
        for t in paras:
            self.assertNotRegex(t, r"[\u3400-\u9fff]\s+[\u3400-\u9fff]")
        # heading order: 总结 before its section, 结语 before its section
        seq = [t for k, t in texts if k in ("para", "heading")]
        i_sum = next(i for i, t in enumerate(seq) if t == "总结")
        i_end = next(i for i, t in enumerate(seq) if t == "结语")
        self.assertLess(i_sum, i_end)
        before_sum = " ".join(seq[:i_sum])
        between = " ".join(seq[i_sum + 1:i_end])
        after_end = " ".join(seq[i_end + 1:])
        self.assertIn("第三段", before_sum)
        self.assertNotIn("第四段", before_sum)
        self.assertIn("第四段", between)
        self.assertNotIn("第五段", between)
        self.assertIn("第五段", after_end)

    def test_tables_image_keeps_legacy_crop(self):
        html = "<table><tr><td>" + ("这是一段很长的说明文字。它不应该出现在正文里。" * 4) + "</td></tr></table>"
        items = build([[
            table_block(html, [0.2, 0.1, 0.9, 0.8]),
            title("总结", 0.4 * H, h_pt=16, x0=0.06, w_pt=0.12 * W),
        ]], table_mode="image")
        self.assertTrue(any(it["kind"] == "figure" for it in items))
        self.assertFalse(any(it["kind"] == "para" and "说明文字" in it["text"] for it in items))
        self.assertTrue(any(it["kind"] == "heading" and it["text"] == "总结" for it in items))


class RealTableDocumentTests(unittest.TestCase):
    def test_real_table_stays_cropped_figure(self):
        raw = (
            "<table><tr><th>品种</th><th>价格</th></tr>"
            "<tr><td>大豆</td><td>12.5</td></tr>"
            "<tr><td>玉米</td><td>8.0</td></tr></table>"
        )
        items = build([[
            title("第一章 示例", 80, h_pt=23.4, w_pt=200),
            table_block(raw, [0.15, 0.30, 0.85, 0.55], caption="表 1.1 价格"),
            para("表后正文。", 500),
        ]])
        figs = [it for it in items if it["kind"] == "figure"]
        self.assertEqual(len(figs), 1)
        self.assertEqual(figs[0]["type"], "table")
        self.assertIn("表 1.1", figs[0]["caption"])
        self.assertFalse(any(it["kind"] == "table" for it in items))
        self.assertFalse(any(it["kind"] == "para" and "大豆" in it.get("text", "") for it in items))
        body = p.render_xhtml({"title": "第一章 示例", "items": items}, [])
        self.assertIn("<img", body)
        self.assertNotIn("<table>", body)
        self.assertIn("表 1.1", body)


class InterleaveUnitTests(unittest.TestCase):
    def test_margin_heading_inserts_by_midpoint(self):
        items = [
            {"kind": "para", "text": "A", "bbox": [0.3, 0.10, 0.9, 0.30]},
            {"kind": "para", "text": "B", "bbox": [0.3, 0.30, 0.9, 0.50]},
            {"kind": "para", "text": "C", "bbox": [0.3, 0.50, 0.9, 0.70]},
            {"kind": "para", "text": "D", "bbox": [0.3, 0.70, 0.9, 0.90]},
            {"kind": "heading", "text": "总结", "bbox": [0.06, 0.48, 0.18, 0.52]},
            {"kind": "heading", "text": "结语", "bbox": [0.06, 0.68, 0.18, 0.72]},
        ]
        out = p.interleave_margin_headings(items)
        seq = [it["text"] for it in out]
        self.assertEqual(seq, ["A", "B", "总结", "C", "结语", "D"])


if __name__ == "__main__":
    unittest.main()
