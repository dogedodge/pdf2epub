"""Optional check against a real MinerU JSON (not shipped; the book is copyrighted).

Set MINERU_BASIC_JSON or place mineru_basic.json next to the tests / repo root.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pdf2epub as p


def _book_json() -> Path | None:
    env = os.environ.get("MINERU_BASIC_JSON")
    candidates = []
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve()
    candidates.extend([
        here.parent / "data" / "mineru_basic.json",
        here.parent.parent / "mineru_basic.json",
        Path("/home/ubuntu/.cursor/projects/workspace/uploads/mineru_basic_e6f6.json"),
        Path("/home/ubuntu/.cursor/projects/workspace/uploads/mineru_basic.json"),
    ])
    return next((c for c in candidates if c.is_file()), None)


def _compact(s: str) -> str:
    return p.compact_heading(s)


@unittest.skipUnless(_book_json(), "real MinerU JSON not available (copyrighted; not in repo)")
class MurphyBookStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = _book_json()
        mj = json.loads(path.read_text(encoding="utf-8"))
        cls.items = p.build_document(mj, None, None, punct=True)
        cls.chapters = p.split_chapters(cls.items, "期货市场技术分析")
        cls.titles = [c["title"] for c in cls.chapters]
        cls.compact = [_compact(t) for t in cls.titles]

    def test_chapter_list_matches_the_book(self):
        want_substrings = [
            "前言",
            "译者的话",
            "致谢",
            "目录",
            "第一章", "第二章", "第三章", "第四章", "第五章", "第六章",
            "第七章", "第八章", "第九章", "第十章", "第十一章", "第十二章",
            "第十三章", "第十四章", "第十五章", "第十六章",
            "全书大会串",
            "附录一", "附录二", "附录三",
            "索引",
        ]
        joined = "｜".join(self.compact)
        for s in want_substrings:
            self.assertIn(_compact(s), joined, msg=f"missing {s} in {self.titles}")

        numbered = [t for t in self.compact if t.startswith("第") and "章" in t[:6]]
        self.assertEqual(len(numbered), 16, msg=numbered)
        apps = [t for t in self.compact if t.startswith("附录")]
        self.assertGreaterEqual(len(apps), 3, msg=apps)
        # No swallowing: 第一章 must not live inside 译者的话
        yi = next(i for i, t in enumerate(self.titles) if "译者的话" in t)
        ch1 = next(i for i, t in enumerate(self.titles) if t.startswith("第一章"))
        self.assertLess(yi, ch1)
        ch10 = next(i for i, t in enumerate(self.titles) if t.startswith("第十章"))
        ch11 = next(i for i, t in enumerate(self.titles) if t.startswith("第十一章"))
        self.assertLess(ch10, ch11)
        a1 = next(i for i, t in enumerate(self.titles) if t.startswith("附录一"))
        a2 = next(i for i, t in enumerate(self.titles) if t.startswith("附录二"))
        a3 = next(i for i, t in enumerate(self.titles) if t.startswith("附录三"))
        idx = next(i for i, t in enumerate(self.titles) if t.startswith("索引") or "索引" == t)
        self.assertLess(a1, a2)
        self.assertLess(a2, a3)
        self.assertLess(a3, idx)

    def test_sections_nest_under_chapters(self):
        ch1 = next(c for c in self.chapters if c["title"].startswith("第一章"))
        nested = [it for it in ch1["items"] if it["kind"] == "heading" and it.get("level", 2) >= 2]
        self.assertTrue(nested, "第一章 should have nested section headings")
        self.assertTrue(any(it["text"] == "引言" for it in nested))
        # TOC is nested (many entries, but not a flat list of every heading as a chapter)
        self.assertGreater(p.toc_size(self.chapters), len(self.chapters))
        self.assertLess(len(self.chapters), 40)

    def test_false_headings_rejected_and_yinyan_kept(self):
        htexts = [it["text"] for it in self.items if it["kind"] == "heading"]
        self.assertNotIn("有一定混乱了。", htexts)
        self.assertFalse(any(t == "支撑转化为阻挡，" for t in htexts))
        self.assertNotIn("图表释义", htexts)
        self.assertNotIn("点数图技术", htexts)
        self.assertFalse(any(p.PLATE_LABEL_RE.search(t) for t in htexts))
        self.assertNotIn("逐日图表的刷新", htexts)
        yin = [it for it in self.items if it["kind"] == "heading" and it["text"] == "引言"]
        self.assertGreaterEqual(len(yin), 10, msg=f"引言 heading count={len(yin)}")


if __name__ == "__main__":
    unittest.main()
