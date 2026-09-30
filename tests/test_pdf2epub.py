#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pdf2epub  # noqa: E402

FAKE_AGENT = ROOT / "tests" / "fake_cursor_agent.py"
SAMPLE_PDF = Path("/home/ubuntu/.cursor/projects/workspace/uploads/input_5e56.pdf")
SAMPLE_JSON = Path("/home/ubuntu/.cursor/projects/workspace/uploads/mineru_basic_18d8.json")
ORIG_SCRIPT = Path("/tmp/pdf2epub_orig.py")
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01\xe5'\xde\xfc\x00\x00\x00\x00IEND\xaeB`\x82"
)


def write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(TINY_PNG)


def make_project(tmp: Path, pages: list[dict], **book_kw) -> Path:
    project = tmp / "book_work"
    project.mkdir()
    ids = []
    for i, spec in enumerate(pages, 1):
        pid = f"{i:04d}"
        ids.append(pid)
        d = project / "pages" / pid
        d.mkdir(parents=True)
        write_png(d / "page.png")
        items = spec["items"]
        for it in items:
            it.setdefault("page", i)
            if it.get("kind") == "para":
                it.setdefault("pages", [i])
            if it.get("kind") == "figure":
                src = it["src"]
                write_png(d / src)
                it.setdefault("path", str(d / src))
                it.setdefault("caption", "")
        (d / "page.md").write_text(pdf2epub.items_to_markdown(items), encoding="utf-8")
        (d / "page.ocr.md").write_text((d / "page.md").read_text(encoding="utf-8"), encoding="utf-8")
        meta = {
            "page": i,
            "continues_prev": spec.get("continues_prev", False),
            "figures": [
                {"src": it["src"], "type": it.get("type", "image"),
                 "footnote": it.get("footnote", ""), "table_html": it.get("table_html"),
                 "crop_px": it.get("crop_px")}
                for it in items if it["kind"] == "figure"
            ],
        }
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    book = {"version": 1, "title": book_kw.get("title", "测试书"), "author": book_kw.get("author", "作者"),
            "lang": "zh-CN", "pages": ids}
    (project / "book.json").write_text(json.dumps(book, ensure_ascii=False, indent=1), encoding="utf-8")
    return project


def epub_bodies(epub_path: Path) -> str:
    texts = []
    with zipfile.ZipFile(epub_path) as z:
        names = sorted(n for n in z.namelist() if n.endswith(".xhtml") and "nav" not in n.lower())
        for n in names:
            raw = z.read(n).decode("utf-8")
            texts.append(re.sub(r"<[^>]+>", "", raw))
    return "\n".join(texts)


class TestNormalizeAndMerge(unittest.TestCase):
    def test_emdash_between_cjk(self):
        s = pdf2epub.normalize_punct("任何因素—基础的")
        self.assertIn("——", s)
        self.assertNotIn("因素—基", s)

    def test_knight_ridder(self):
        s = pdf2epub.normalize_punct("Knight—Ridder")
        self.assertEqual(s, "Knight-Ridder")

    def test_heading_levels(self):
        titles = [{"_h": 20.0, "text": "大"}, {"_h": 14.0, "text": "小"}]
        pdf2epub.infer_heading_levels(titles)
        self.assertEqual(titles[0]["level"], 1)
        self.assertEqual(titles[1]["level"], 2)

    def test_cross_page_merge_continues_prev(self):
        pages = [
            {"page": 1, "continues_prev": False, "items": [
                {"kind": "para", "text": "最可能的", "page": 1, "pages": [1]},
            ]},
            {"page": 2, "continues_prev": True, "items": [
                {"kind": "para", "text": "走势，而并不是。", "page": 2, "pages": [2]},
                {"kind": "heading", "text": "下一节", "level": 2, "page": 2},
            ]},
        ]
        items = pdf2epub.merge_page_items(pages)
        paras = [i for i in items if i["kind"] == "para"]
        self.assertEqual(len(paras), 1)
        self.assertEqual(paras[0]["text"], "最可能的走势，而并不是。")
        self.assertEqual(items[1]["kind"], "heading")

    def test_cross_page_merge_heuristic(self):
        pages = [
            {"page": 1, "continues_prev": False, "items": [
                {"kind": "para", "text": "没有句号", "page": 1, "pages": [1]},
            ]},
            {"page": 2, "continues_prev": False, "items": [
                {"kind": "para", "text": "接上页。", "page": 2, "pages": [2]},
            ]},
        ]
        items = pdf2epub.merge_page_items(pages)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "没有句号接上页。")


class TestMarkdownRoundtrip(unittest.TestCase):
    def test_roundtrip(self):
        items = [
            {"kind": "heading", "level": 1, "text": "理论基础", "page": 1},
            {"kind": "para", "text": "1. 市场行为包容消化一切。", "page": 1, "pages": [1], "cls": None},
            {"kind": "heading", "level": 2, "text": "市场行为包容消化一切", "page": 1},
            {"kind": "figure", "src": "fig_p0003_001.png",
             "caption": "图 1.1 示例(Chart courtesy of CRB).", "page": 1},
            {"kind": "para", "text": "两派都试图解决同样的问题。", "page": 1, "pages": [1], "cls": None},
            {"kind": "para", "text": "脚注内容", "page": 1, "pages": [1], "cls": "footnote"},
        ]
        md = pdf2epub.items_to_markdown(items)
        back = pdf2epub.markdown_to_items(md, 1, {"figures": [{"src": "fig_p0003_001.png", "type": "chart"}]})
        self.assertEqual([i["kind"] for i in back], [i["kind"] for i in items])
        self.assertEqual(back[0]["level"], 1)
        self.assertEqual(back[2]["level"], 2)
        self.assertIn("Chart courtesy", back[3]["caption"])
        self.assertEqual(back[3]["type"], "chart")
        self.assertEqual(back[-1]["cls"], "footnote")
        self.assertIn(":::figure fig_p0003_001.png", md)


class TestBuildFromProject(unittest.TestCase):
    def test_hand_edit_changes_epub(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project = make_project(tmp, [
                {"items": [
                    {"kind": "heading", "level": 1, "text": "理论基础"},
                    {"kind": "para", "text": "原文段落。"},
                    {"kind": "figure", "src": "fig_p0001_001.png", "caption": "图 1.1 示例。"},
                    {"kind": "para", "text": "图后文字。"},
                ]},
                {"continues_prev": False, "items": [
                    {"kind": "heading", "level": 2, "text": "历史会重演"},
                    {"kind": "para", "text": "第二页。"},
                ]},
            ])
            epub1 = tmp / "a.epub"
            rc = pdf2epub.main(["build", "--project", str(project), "-o", str(epub1)])
            self.assertEqual(rc, 0)
            body1 = epub_bodies(epub1)
            self.assertIn("原文段落", body1)
            self.assertIn("图 1.1 示例", body1)
            self.assertIn("第二页", body1)
            with zipfile.ZipFile(epub1) as z:
                self.assertTrue(any(n.endswith("fig_p0001_001.png") for n in z.namelist()))
                ncx = [n for n in z.namelist() if n.endswith("toc.ncx") or "ncx" in n.lower()]
                self.assertTrue(ncx)

            page_md = project / "pages" / "0001" / "page.md"
            page_md.write_text(page_md.read_text(encoding="utf-8").replace("原文段落", "手改段落"),
                               encoding="utf-8")
            epub2 = tmp / "b.epub"
            rc = pdf2epub.main(["build", "--project", str(project), "-o", str(epub2)])
            self.assertEqual(rc, 0)
            body2 = epub_bodies(epub2)
            self.assertIn("手改段落", body2)
            self.assertNotIn("原文段落", body2)
            self.assertIn("第二页", body2)

    def test_chapter_split_and_merge_from_files(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project = make_project(tmp, [
                {"items": [
                    {"kind": "heading", "level": 1, "text": "第一章"},
                    {"kind": "para", "text": "上半句"},
                ]},
                {"continues_prev": True, "items": [
                    {"kind": "para", "text": "下半句。"},
                    {"kind": "heading", "level": 1, "text": "第二章"},
                    {"kind": "para", "text": "新章。"},
                ]},
            ], title="书")
            epub = tmp / "c.epub"
            pdf2epub.main(["build", "--project", str(project), "-o", str(epub)])
            body = epub_bodies(epub)
            self.assertIn("上半句下半句。", body)
            with zipfile.ZipFile(epub) as z:
                chaps = [n for n in z.namelist() if n.endswith("chap_001.xhtml") or n.endswith("chap_002.xhtml")]
                self.assertGreaterEqual(len(chaps), 2)


class TestProofreadMock(unittest.TestCase):
    def _run(self, project: Path, extra=None, env=None):
        cmd = ["proofread", "--project", str(project), "--agent-bin", str(FAKE_AGENT),
               "--timeout", "5"]
        if extra:
            cmd.extend(extra)
        old = os.environ.copy()
        try:
            if env:
                os.environ.update(env)
            return pdf2epub.main(cmd)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_applies_fixes_and_keeps_ocr_original(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project = make_project(tmp, [
                {"items": [
                    {"kind": "heading", "level": 1, "text": "标题"},
                    {"kind": "para", "text": "两条道路分歧最大。趋势发展过一段之后，两者协调。"},
                    {"kind": "para", "text": "因素—基础的讨论。"},
                    {"kind": "para", "text": "标记OCR_ERROR_FOO结束。"},
                ]},
            ])
            ocr_before = (project / "pages/0001/page.ocr.md").read_text(encoding="utf-8")
            rc = self._run(project)
            self.assertEqual(rc, 0)
            md = (project / "pages/0001/page.md").read_text(encoding="utf-8")
            ocr_after = (project / "pages/0001/page.ocr.md").read_text(encoding="utf-8")
            self.assertEqual(ocr_before, ocr_after)
            self.assertIn("等趋势发展过一段之后", md)
            self.assertIn("因素——基础的", md)
            self.assertIn("标记校正结束", md)
            report = (project / "proofread/report.md").read_text(encoding="utf-8")
            self.assertIn("Page 0001", report)
            self.assertIn("changed", report)
            self.assertTrue((project / "proofread/diffs/0001.diff").exists())
            self.assertTrue((project / "pages/0001/proofread.json").exists())

            # resumable: second run skips
            rc = self._run(project)
            self.assertEqual(rc, 0)
            results = json.loads((project / "proofread/results.json").read_text(encoding="utf-8"))
            self.assertEqual(results[0]["status"], "skipped")

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project = make_project(tmp, [
                {"items": [{"kind": "para", "text": "标记OCR_ERROR_FOO结束。"}]},
            ])
            before = (project / "pages/0001/page.md").read_text(encoding="utf-8")
            rc = self._run(project, extra=["--dry-run"])
            self.assertEqual(rc, 0)
            after = (project / "pages/0001/page.md").read_text(encoding="utf-8")
            self.assertEqual(before, after)
            self.assertFalse((project / "pages/0001/proofread.json").exists())
            report = (project / "proofread/report.md").read_text(encoding="utf-8")
            self.assertIn("would_change", report)

    def test_failure_leaves_page_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project = make_project(tmp, [
                {"items": [{"kind": "para", "text": "标记OCR_ERROR_FOO结束。"}]},
            ])
            before = (project / "pages/0001/page.md").read_text(encoding="utf-8")
            rc = self._run(project, env={"FAKE_AGENT_BEHAVIOR": "fail"})
            self.assertEqual(rc, 0)
            after = (project / "pages/0001/page.md").read_text(encoding="utf-8")
            self.assertEqual(before, after)
            self.assertFalse((project / "pages/0001/proofread.json").exists())
            report = (project / "proofread/report.md").read_text(encoding="utf-8")
            self.assertIn("failed", report)

    def test_timeout_leaves_page_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project = make_project(tmp, [
                {"items": [{"kind": "para", "text": "标记OCR_ERROR_FOO结束。"}]},
            ])
            before = (project / "pages/0001/page.md").read_text(encoding="utf-8")
            rc = self._run(project, extra=["--timeout", "0.2"],
                           env={"FAKE_AGENT_BEHAVIOR": "timeout", "FAKE_AGENT_SLEEP": "5"})
            self.assertEqual(rc, 0)
            after = (project / "pages/0001/page.md").read_text(encoding="utf-8")
            self.assertEqual(before, after)
            report = (project / "proofread/report.md").read_text(encoding="utf-8")
            self.assertIn("timeout", report)

    def test_unsafe_correction_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project = make_project(tmp, [
                {"items": [
                    {"kind": "para", "text": "配图如下。"},
                    {"kind": "figure", "src": "fig_p0001_001.png", "caption": "图 1"},
                ]},
            ])
            before = (project / "pages/0001/page.md").read_text(encoding="utf-8")
            rc = self._run(project, env={"FAKE_AGENT_BEHAVIOR": "drop_figure"})
            self.assertEqual(rc, 0)
            after = (project / "pages/0001/page.md").read_text(encoding="utf-8")
            self.assertEqual(before, after)
            self.assertIn("dropped figure", (project / "proofread/report.md").read_text(encoding="utf-8"))


@unittest.skipUnless(SAMPLE_PDF.exists() and SAMPLE_JSON.exists(), "sample PDF/JSON not attached")
class TestSampleOcrBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.project = cls.dir / "sample_work"
        cls.project.mkdir()
        shutil_copy = SAMPLE_JSON.read_text(encoding="utf-8")
        (cls.project / "mineru_basic.json").write_text(shutil_copy, encoding="utf-8")
        cls.epub = cls.dir / "sample.epub"
        rc = pdf2epub.main([
            "ocr", str(SAMPLE_PDF), "--project", str(cls.project),
            "--reuse-json", "--title", "技术分析的理论基础", "--author", "约翰·墨菲",
        ])
        assert rc == 0, "ocr failed"
        rc = pdf2epub.main(["build", "--project", str(cls.project), "-o", str(cls.epub), "--epubcheck"])
        assert rc == 0, "build/epubcheck failed"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_project_layout(self):
        self.assertTrue((self.project / "book.json").exists())
        self.assertTrue((self.project / "pages/0001/page.png").exists())
        self.assertTrue((self.project / "pages/0001/page.md").exists())
        self.assertTrue((self.project / "pages/0003/page.md").exists())
        md3 = (self.project / "pages/0003/page.md").read_text(encoding="utf-8")
        self.assertIn(":::figure", md3)
        self.assertTrue(any(self.project.joinpath("pages/0003").glob("fig_*.png")))

    def test_cross_page_merge_in_epub(self):
        body = epub_bodies(self.epub)
        self.assertIn("让市场自己揭示它最可能的走势", body)
        self.assertIn("理论基础", body)
        self.assertIn("图 1.1", body)

    def test_hand_edit_sample(self):
        p = self.project / "pages/0001/page.md"
        orig = p.read_text(encoding="utf-8")
        p.write_text(orig.replace("技术分析有三个基本假定", "【手改】技术分析有三个基本假定"), encoding="utf-8")
        out = self.dir / "edited.epub"
        rc = pdf2epub.main(["build", "--project", str(self.project), "-o", str(out)])
        self.assertEqual(rc, 0)
        body = epub_bodies(out)
        self.assertIn("【手改】技术分析有三个基本假定", body)
        p.write_text(orig, encoding="utf-8")

    def test_equivalent_to_original_oneshot(self):
        if not ORIG_SCRIPT.exists():
            self.skipTest("original script snapshot missing")
        orig_work = self.dir / "orig_work"
        orig_epub = self.dir / "orig.epub"
        orig_work.mkdir()
        (orig_work / "mineru_basic.json").write_text(
            SAMPLE_JSON.read_text(encoding="utf-8"), encoding="utf-8")
        import subprocess
        r = subprocess.run(
            [sys.executable, str(ORIG_SCRIPT), str(SAMPLE_PDF), "-o", str(orig_epub),
             "--workdir", str(orig_work), "--reuse-json",
             "--title", "技术分析的理论基础", "--author", "约翰·墨菲"],
            cwd="/tmp", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        new_body = epub_bodies(self.epub)
        old_body = epub_bodies(orig_epub)
        # drop whitespace-only differences
        def norm(s):
            return re.sub(r"\s+", "", s)
        self.assertEqual(norm(new_body), norm(old_body))

    def test_proofread_mock_on_sample_page3(self):
        # restore a pre-punctuation dash if punct-normalize already fixed it;
        # the known missing 等 is still present in basic-tier OCR.
        md = self.project / "pages/0003/page.md"
        text = md.read_text(encoding="utf-8")
        self.assertIn("趋势发展过一段之后", text)
        rc = pdf2epub.main([
            "proofread", "--project", str(self.project), "--agent-bin", str(FAKE_AGENT),
            "--only-page", "3", "--timeout", "5", "--force",
        ])
        self.assertEqual(rc, 0)
        after = md.read_text(encoding="utf-8")
        self.assertIn("等趋势发展过一段之后", after)
        # original OCR snapshot kept
        ocr = (self.project / "pages/0003/page.ocr.md").read_text(encoding="utf-8")
        self.assertNotIn("等趋势发展过一段之后", ocr)


if __name__ == "__main__":
    unittest.main()
