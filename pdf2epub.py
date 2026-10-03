#!/usr/bin/env python3
"""pdf2epub.py - convert a scanned (image-only) Chinese PDF into an EPUB.

Pipeline
  1. Inspect the PDF with PyMuPDF and render every page to PNG (default 300 DPI).
  2. Layout analysis + OCR with MinerU (runs locally; "basic" tier = ONNX
     PP-DocLayoutV2 + PP-OCRv6, "standard" tier adds the MinerU2.5 1.2B VLM).
  3. Crop illustrations / charts from the page renders using the layout
     boxes. Tables: misclassified prose → paragraphs; real tables → HTML
     (optional image crop).
  4. Clean text: drop headers/footers/page numbers, merge paragraphs split
     across pages, normalise CJK punctuation, detect chapters/headings.
  5. Build an EPUB 3 (ebooklib): one XHTML per chapter, figures in place,
     nav + NCX TOC, metadata. Optionally validate with epubcheck.

Usage
  python pdf2epub.py input.pdf -o out.epub [--tier basic|standard]
         [--title T] [--author A] [--dpi 300] [--workdir DIR]
         [--reuse-json] [--from-json mineru.json] [--chapter-regex RE]
         [--toc-file toc.txt] [--tables auto|html|image] [--table-images]
         [--table-min-quality 0.55] [--no-punct-normalize] [--epubcheck]
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path

DROP_TYPES = {"page_number", "header", "footer", "page_header", "page_footer",
              "aside_text", "discarded"}
FLOAT_TYPES = {"image", "chart", "table", "equation", "interline_equation",
               "seal", "figure"}
TITLE_TYPES = {"paragraph_title", "title", "doc_title"}
TERMINAL = set("。！？!?…”’」』）)》.;；:：")
CJK = r"\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef“”‘’"

# Chapter-level titles for Chinese books. Size/height is only a secondary signal.
CN_NUM = r"[0-9零〇一二三四五六七八九十百千两]"
DEFAULT_CHAPTER_PATTERN = (
    r"^(?:"
    rf"第{CN_NUM}+[章篇部分卷]"
    rf"|附录{CN_NUM}+"
    r"|前言|序言|自序|代序|序(?=$)"
    r"|译者的话|译者话|译序"
    r"|目录|致谢|后记|跋|索引"
    r"|关于本书|出版说明|内容提要|凡例"
    r"|全书大会串"
    r")"
)
DEFAULT_CHAPTER_RE = re.compile(DEFAULT_CHAPTER_PATTERN)
# Standalone sub-headings MinerU often emits as body text.
PROMOTE_HEADINGS = frozenset({"引言", "结论", "结语", "小结", "总结"})
SENTENCE_TAIL_RE = re.compile(r"[。；、，…;]$")
PLATE_LABEL_RE = re.compile(r"模式\s*[\d,，、—\-–]+")
CONT_PREFIXES = ("例如", "但是", "因此", "所以", "于是", "其中", "此外", "不过",
                 "然而", "其实", "当然", "总之", "可见")
_FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
TABLE_MODES = ("auto", "html", "image")
# MinerU sometimes wraps a whole text column as a 1–2 column "table".
_PROSE_AVG_CHARS = 80
_PROSE_LONG_CELL = 200
_PARA_SPLIT_RE = re.compile(
    r"(?<=[。！？…][”’」』])\s+|(?<=[。！？…])\s+"
)
_TABLE_TAGS = frozenset({
    "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "caption", "colgroup", "col", "br",
})
_TABLE_ATTRS = {
    "td": ("colspan", "rowspan", "scope"),
    "th": ("colspan", "rowspan", "scope"),
    "col": ("span",),
    "colgroup": ("span",),
}
# Quality gate: unreliable OCR HTML falls back to the cropped scan.
TABLE_EMPTY_CELL_RATIO = 0.40          # hard fail if more than this share is empty
TABLE_NOISE_CELL_RATIO = 0.50          # hard fail: fragmentary digits / symbols
TABLE_XO_CELL_RATIO = 0.25             # hard fail: point-and-figure X/O marks
TABLE_COL_INCONSISTENT_RATIO = 0.40    # hard fail: row widths disagree
TABLE_SPAN_IRREGULAR_RATIO = 0.30      # hard fail: body cells with rowspan/colspan
TABLE_FIGURE_OVERLAP = 0.30            # overlap with an image/chart box
DEFAULT_TABLE_MIN_QUALITY = 0.55       # combined score cutoff (--table-min-quality)
FIGURE_CAPTION_RE = re.compile(
    r"^(?:图|圖)\s*[\d０-９]|^(?:Figure|Fig\.?)\b",
    re.I,
)
_COMPLETE_NUM_RE = re.compile(
    r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?$"
)
_XO_MARK_RE = re.compile(r"^[XOxo○●〇x]+$")
_EMPTY_CELL_RE = re.compile(r"^[\s.\-–—_·•*]+$")
# Line-wrap after 。 is indistinguishable from a paragraph break in table HTML;
# do not split when the next span is a mid-paragraph "for example".
_PARA_NO_BREAK_PREFIXES = ("举例来说", "比如说", "也就是说", "换言之", "亦即")
FIGURE_REGION_TYPES = frozenset({"image", "chart", "figure", "seal"})


def log(msg):
    print(f"[pdf2epub {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------- step 1
def inspect_and_render(pdf: Path, pages_dir: Path, dpi: int) -> list[dict]:
    import pymupdf
    pages_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf)
    info = []
    for p in doc:
        imgs = p.get_images(full=True)
        img_area = 0.0
        for im in imgs:
            for r in p.get_image_rects(im[0]):
                img_area += abs(r & p.rect)
        # render mode 3 = invisible text (an existing OCR layer)
        spans = p.get_texttrace()
        invisible = sum(1 for s in spans if s.get("type") == 3)
        png = pages_dir / f"page_{p.number + 1:04d}.png"
        pix = p.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY if len(imgs) else None)
        pix.save(png)
        info.append({
            "page": p.number + 1, "width_pt": p.rect.width, "height_pt": p.rect.height,
            "images": len(imgs), "image_coverage": round(img_area / abs(p.rect), 3),
            "text_chars": len(p.get_text().strip()), "invisible_text_spans": invisible,
            "png": str(png), "png_size": [pix.width, pix.height],
        })
    meta = doc.metadata or {}
    doc.close()
    return info, meta


# --------------------------------------------------------------------- step 2
def run_mineru(pdf: Path, tier: str) -> dict:
    from mineru.parser import parse  # local SDK path; never uses the remote API
    res = parse(str(pdf), tier=tier, ocr_mode="ocr", page_range="all")
    return json.loads(res.to_json())


def page_info_from_mineru(mj: dict, pages_dir: Path | None = None) -> list[dict]:
    """Synthesize page_info from MinerU JSON when the PDF / page PNGs are absent."""
    layout_pages = (
        mj.get("extensions", {}).get("docvortex_layout", {}).get("pages") or []
    )
    by_idx = {p["page_idx"]: p for p in layout_pages}
    n = len(mj.get("pages", []))
    info = []
    for i in range(n):
        lay = by_idx.get(i, {})
        png = None
        if pages_dir is not None:
            cand = pages_dir / f"page_{i + 1:04d}.png"
            if cand.exists():
                png = str(cand)
        info.append({
            "page": i + 1,
            "width_pt": float(lay.get("width_pt") or 1.0),
            "height_pt": float(lay.get("height_pt") or 1.0),
            "images": 0, "image_coverage": 0.0, "text_chars": 0, "invisible_text_spans": 0,
            "png": png, "png_size": [0, 0],
        })
    return info


# --------------------------------------------------------------------- helpers
def block_text(content) -> str:
    """Flatten MinerU block content into plain text (inline formulas as $..$)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        t = content.get("type")
        c = content.get("content")
        if t in ("inline_equation", "equation_inline"):
            return f"${c}$" if isinstance(c, str) else block_text(c)
        return block_text(c)
    return "".join(block_text(x) for x in content)


def normalize_punct(s: str) -> str:
    """Half-width -> full-width punctuation when adjacent to CJK text."""
    pairs = {",": "，", ";": "；", ":": "：", "!": "！", "?": "？", "(": "（", ")": "）"}
    out = list(s)
    for i, ch in enumerate(s):
        if ch in pairs:
            prev = s[i - 1] if i else ""
            nxt = s[i + 1] if i + 1 < len(s) else ""
            if re.match(f"[{CJK}]", prev) or re.match(f"[{CJK}]", nxt):
                out[i] = pairs[ch]
    s = "".join(out)
    # single em/en dash (or "一" misread) between CJK chars -> Chinese "——"
    s = re.sub(f"(?<=[{CJK}])[—–―](?=[{CJK}])", "——", s)
    s = re.sub(r"—{3,}", "——", s)
    # figure/table labels: "图1.1上升" -> "图 1.1 上升"
    s = re.sub(r"^(图|表|插图)\s*(\d+(?:[.．-]\d+)*)\s*", r"\1 \2 ", s)
    # Latin punctuation spacing inside English fragments
    s = re.sub(r"(?<=[A-Za-z]),(?=[A-Za-z])", ", ", s)
    s = re.sub(r"(?<=[A-Za-z])\s*[—一]\s*(?=[A-Z])", "-", s)   # Knight—Ridder -> Knight-Ridder
    s = re.sub(r"\s+(?=[）)。，])", "", s)
    # stray spaces between CJK characters
    s = re.sub(f"(?<=[\u3400-\u9fff，。；：、])\\s+(?=[\u3400-\u9fff])", "", s)
    return s.strip()


def compact_heading(text: str) -> str:
    """Whitespace-free heading key; full-width digits/colons folded to ASCII."""
    t = (text or "").translate(_FW_DIGITS)
    t = t.replace("：", ":").replace("．", ".")
    return re.sub(r"[\s\u3000\u00a0]+", "", t)


def compile_chapter_re(pattern: str | None) -> re.Pattern:
    if not pattern:
        return DEFAULT_CHAPTER_RE
    try:
        return re.compile(pattern)
    except re.error as e:
        raise SystemExit(f"invalid --chapter-regex: {e}") from e


def is_chapter_heading(text: str, chapter_re: re.Pattern | None = None,
                       max_len: int | None = 48) -> bool:
    """True when *text* is a chapter / front-or-back-matter title, not a sentence."""
    chapter_re = chapter_re or DEFAULT_CHAPTER_RE
    s = (text or "").strip()
    c = compact_heading(s)
    if not c:
        return False
    if max_len is not None and len(c) > max_len:
        return False
    m = chapter_re.search(c)
    if not m:
        m = chapter_re.search(s)
    if not m:
        return False
    rest = (c if m.string == c else s)[m.end():].strip()
    # "第一章介绍……。" is body text; "第一章 技术分析的理论基础" is a title.
    if rest and (is_sentence_like(rest) or "。" in rest or "；" in rest):
        return False
    if rest and rest.count("，") >= 1 and len(compact_heading(rest)) > 16:
        return False
    return True


def estimate_n_lines(h_pt: float, w_pt: float, text: str) -> int:
    """How many wrapped lines a heading box likely contains (CJK-aware)."""
    n_chars = max(1, len(compact_heading(text)))
    if h_pt <= 0:
        return 1
    if w_pt <= 0:
        return max(1, round(h_pt / 16.0))
    for n in range(1, 7):
        font = h_pt / n
        if font < 7:
            break
        chars_per_line = max(1.0, w_pt / font)
        if chars_per_line * n >= n_chars * 0.78:
            return n
    return max(1, min(6, round(h_pt / 16.0)))


def per_line_height(item: dict) -> float:
    h = float(item.get("_h") or 0.0)
    w = float(item.get("_w") or 0.0)
    n = estimate_n_lines(h, w, item.get("text") or "")
    item["_nlines"] = n
    return h / n if n else h


def is_sentence_like(text: str) -> bool:
    """Reject MinerU 'titles' that are really sentence fragments."""
    t = (text or "").strip()
    if not t:
        return True
    if SENTENCE_TAIL_RE.search(t):
        return True
    if t.endswith(",") and re.search(f"[{CJK}]", t):
        return True
    if len(compact_heading(t)) > 48:
        return True
    if t.startswith(CONT_PREFIXES):
        return True
    return False


def parse_toc_file(path: Path) -> list[dict]:
    """Parse a user TOC: one title per line, indent = nesting, optional page number.

    Example::

        前言
        第一章 技术分析的理论基础
          引言
          理论基础
        附录一
        索引
    """
    entries = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.lstrip(" \t")
        prefix = raw[: len(raw) - len(stripped)]
        n_tabs = prefix.count("\t")
        n_spaces = len(prefix) - n_tabs
        level = 1 + n_tabs + n_spaces // 2
        m = re.match(r"^(.*?)(?:\s{2,}|\t+)(\d+)\s*$", stripped)
        if m:
            title, page = m.group(1).strip(), int(m.group(2))
        else:
            title, page = stripped.strip(), None
        if title:
            entries.append({"title": title, "level": min(max(level, 1), 3), "page": page})
    return entries


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b or a.startswith(b) or b.startswith(a) or a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _join_heading(a: str, b: str) -> str:
    a, b = a.strip(), b.strip()
    if not a:
        return b
    if not b:
        return a
    if re.search(f"[{CJK}]$", a) or a.endswith(("，", "、", "：", ":", "；", ",", "或曰")):
        return a + b
    if re.search(f"[{CJK}]", b[:1] or ""):
        return a + b
    return a + " " + b


def _union_bbox(a, b):
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def merge_split_headings(items: list[dict], chapter_re: re.Pattern,
                         max_len: int | None) -> list[dict]:
    """Glue two-line titles that MinerU split into adjacent heading boxes."""
    out: list[dict] = []
    for it in items:
        if (
            out
            and out[-1]["kind"] == "heading"
            and it["kind"] == "heading"
            and out[-1].get("page") == it.get("page")
            and out[-1].get("bbox") and it.get("bbox")
            and (it["bbox"][1] - out[-1]["bbox"][3]) <= 0.012
            and not is_chapter_heading(out[-1]["text"], chapter_re, max_len)
            and not is_chapter_heading(it["text"], chapter_re, max_len)
        ):
            prev = out[-1]
            prev["text"] = _join_heading(prev["text"], it["text"])
            prev["bbox"] = _union_bbox(prev["bbox"], it["bbox"])
            prev["_h"] = float(prev.get("_h") or 0) + float(it.get("_h") or 0)
            prev["_w"] = max(float(prev.get("_w") or 0), float(it.get("_w") or 0))
            continue
        out.append(it)
    return out


def _in_running_band(bbox) -> bool:
    if not bbox:
        return False
    y0, y1 = bbox[1], bbox[3]
    return y0 > 0.90 or y1 > 0.93 or y1 < 0.05


def drop_running_headers(items: list[dict], chapter_re: re.Pattern,
                         max_len: int | None) -> list[dict]:
    """Drop chapter titles repeated in the header/footer band."""
    chapter_keys = [
        compact_heading(it["text"])
        for it in items
        if it["kind"] == "heading" and is_chapter_heading(it["text"], chapter_re, max_len)
    ]
    out = []
    for it in items:
        if it["kind"] != "heading" or not _in_running_band(it.get("bbox")):
            out.append(it)
            continue
        if is_chapter_heading(it["text"], chapter_re, max_len):
            out.append(it)
            continue
        c = compact_heading(it["text"])
        if c and any(c == k or (len(c) >= 4 and (c in k or k.endswith(c))) for k in chapter_keys):
            continue
        # footer-band leftovers (e.g. a chapter title without the 第X章 prefix)
        if it.get("bbox") and it["bbox"][1] > 0.90:
            continue
        out.append(it)
    return out


def is_figure_label(item: dict, items: list[dict]) -> bool:
    """Centered short labels sitting on a figure (glossary keys, plate titles)."""
    text = item.get("text") or ""
    if is_chapter_heading(text):
        return False
    if PLATE_LABEL_RE.search(text):
        return True
    bbox = item.get("bbox")
    if not bbox:
        return False
    c = compact_heading(text)
    if len(c) > 12:
        return False
    x0, y0, x1, y1 = bbox
    tcx, tw = (x0 + x1) / 2, x1 - x0
    # left-aligned body headings (typical Chinese book)
    if x0 < 0.35 and tw > 0.12 and len(c) > 6:
        return False
    page = item.get("page")
    for fig in items:
        if fig.get("kind") != "figure" or fig.get("page") != page or not fig.get("bbox"):
            continue
        fx0, fy0, fx1, fy1 = fig["bbox"]
        if not (fx0 <= tcx <= fx1 or x0 > 0.50):
            continue
        above = -0.01 <= (fy0 - y1) <= 0.04
        below_slack = 0.12 if x0 > 0.50 else 0.04
        below = -0.01 <= (y0 - fy1) <= below_slack
        beside = (y0 < fy1 and y1 > fy0) and (tcx > 0.38 or x0 > 0.50)
        if (above or below or beside) and (tw < 0.40 or x0 > 0.35):
            return True
    return False


def _prev_non_figure(items: list[dict], idx: int):
    for j in range(idx - 1, -1, -1):
        if items[j]["kind"] != "figure":
            return items[j], j
    return None, None


def demote_false_headings(items: list[dict], chapter_re: re.Pattern,
                          max_len: int | None) -> list[dict]:
    """Turn sentence fragments / figure labels into body text."""
    out: list[dict] = []
    for it in items:
        if it["kind"] != "heading":
            out.append(it)
            continue
        if is_chapter_heading(it["text"], chapter_re, max_len):
            out.append(it)
            continue
        reason = None
        if is_figure_label(it, items):
            reason = "figure-label"
        elif is_sentence_like(it["text"]):
            reason = "sentence"
        else:
            prev, _ = _prev_non_figure(out, len(out))
            if (
                prev
                and prev["kind"] == "para"
                and prev.get("text")
                and prev["text"][-1] not in TERMINAL
                and prev.get("page") == it.get("page")
                and (
                    it["text"].startswith(tuple("的了着过得地而就也但并"))
                    or is_sentence_like(it["text"])
                )
            ):
                reason = "continuation"
                prev["text"] += it["text"]
                prev.setdefault("pages", [prev.get("page")]).append(it.get("page"))
                continue
        if reason:
            para = {
                "kind": "para", "text": it["text"], "page": it.get("page"),
                "pages": [it.get("page")] if it.get("page") else [],
                "cls": None, "bbox": it.get("bbox"),
            }
            prev, _ = _prev_non_figure(out, len(out))
            if (
                reason == "continuation"
                and prev
                and prev["kind"] == "para"
                and prev.get("text")
                and prev["text"][-1] not in TERMINAL
            ):
                prev["text"] += para["text"]
                continue
            out.append(para)
            continue
        out.append(it)
    return out


def promote_plain_headings(items: list[dict]) -> None:
    """MinerU often classifies recurring 引言 / 结语 as body text."""
    for it in items:
        if it.get("kind") != "para" or it.get("cls") == "footnote":
            continue
        c = compact_heading(it.get("text") or "")
        if c in PROMOTE_HEADINGS:
            it["kind"] = "heading"
            it["text"] = c
            it.pop("cls", None)


def _split_by_height_gap(titles: list[dict], attr: str, high_level: int, low_level: int,
                         min_ratio: float = 1.12) -> None:
    """Biggest relative gap on *attr* splits larger → high_level, smaller → low_level."""
    for t in titles:
        t["level"] = low_level
    hs = sorted({round(float(t.get(attr) or 0.0), 1) for t in titles})
    if len(hs) < 2:
        if titles:
            for t in titles:
                t["level"] = high_level
        return
    best, cut = 1.0, None
    for a, b in zip(hs, hs[1:]):
        if a > 0 and b / a > best:
            best, cut = b / a, (a + b) / 2
    if best >= min_ratio and cut is not None:
        for t in titles:
            t["level"] = high_level if float(t.get(attr) or 0) > cut else low_level
    else:
        for t in titles:
            t["level"] = high_level


def assign_section_levels(titles: list[dict]) -> None:
    """h2 vs h3 from per-line height when the sizes are clearly bimodal."""
    for t in titles:
        t["level"] = 2
    if len(titles) < 6:
        return
    hs = sorted(float(t.get("_line_h") or 0.0) for t in titles)
    p20 = hs[max(0, int(0.20 * (len(hs) - 1)))]
    p80 = hs[min(len(hs) - 1, int(0.80 * (len(hs) - 1)))]
    if p20 <= 0 or p80 / p20 < 1.18:
        return
    cut = (p20 + p80) / 2.0
    for t in titles:
        t["level"] = 2 if float(t.get("_line_h") or 0.0) >= cut else 3


def infer_heading_levels(titles: list[dict], chapter_re: re.Pattern | None = None,
                         max_len: int | None = 48) -> None:
    """Chapter regex first; per-line height only to split h2/h3 (or h1/h2 fallback)."""
    chapter_re = chapter_re or DEFAULT_CHAPTER_RE
    for t in titles:
        t["_line_h"] = per_line_height(t)
    if not titles:
        return
    flags = [is_chapter_heading(t["text"], chapter_re, max_len) for t in titles]
    if any(flags):
        for t, is_ch in zip(titles, flags):
            t["level"] = 1 if is_ch else 2
        assign_section_levels([t for t in titles if t["level"] != 1])
        return
    # No pattern match (e.g. English book): fall back to per-line height, not box height.
    _split_by_height_gap(titles, "_line_h", high_level=1, low_level=2)


def apply_user_toc(items: list[dict], toc_entries: list[dict]) -> None:
    """Override heading levels from a user-supplied TOC (matched in document order)."""
    headings = [it for it in items if it["kind"] == "heading"]
    used: set[int] = set()
    cursor = 0
    for ent in toc_entries:
        target = compact_heading(ent["title"])
        found = None
        for j in range(cursor, len(headings)):
            if j in used:
                continue
            h = headings[j]
            if ent.get("page") and h.get("page"):
                if abs(int(h["page"]) - int(ent["page"])) > 3:
                    continue
            hc = compact_heading(h["text"])
            if _similar(hc, target) >= 0.86:
                found = j
                break
        if found is None:
            continue
        headings[found]["level"] = int(ent["level"])
        used.add(found)
        cursor = found + 1


def refine_headings(items: list[dict], chapter_re: re.Pattern | None = None,
                    toc_entries: list[dict] | None = None,
                    max_len: int | None = 48) -> list[dict]:
    chapter_re = chapter_re or DEFAULT_CHAPTER_RE
    promote_plain_headings(items)
    items = merge_split_headings(items, chapter_re, max_len)
    items = drop_running_headers(items, chapter_re, max_len)
    items = demote_false_headings(items, chapter_re, max_len)
    titles = [it for it in items if it["kind"] == "heading"]
    infer_heading_levels(titles, chapter_re, max_len)
    if toc_entries:
        apply_user_toc(items, toc_entries)
        # TOC-assigned level 1 wins even if the regex missed it.
        for it in items:
            if it["kind"] == "heading":
                it["level"] = max(1, min(int(it.get("level") or 2), 3))
    return items


# --------------------------------------------------------------------- tables
class _TableWalker(HTMLParser):
    """Collect cell texts and rebuild a small sanitised XHTML table."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.cell_meta: list[list[dict]] = []
        self.n_cols = 0
        self._row: list[str] | None = None
        self._row_meta: list[dict] | None = None
        self._cell: list[str] | None = None
        self._cell_span = 1
        self._cell_rowspan = 1
        self._row_cols = 0
        self._parts: list[str] = []
        self._stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        if tag == "tr":
            self._row = []
            self._row_meta = []
            self._row_cols = 0
        elif tag in ("td", "th"):
            self._cell = []
            self._cell_span = 1
            self._cell_rowspan = 1
            for k, v in attrs:
                if k.lower() == "colspan":
                    try:
                        self._cell_span = max(1, int(v))
                    except (TypeError, ValueError):
                        self._cell_span = 1
                elif k.lower() == "rowspan":
                    try:
                        self._cell_rowspan = max(1, int(v))
                    except (TypeError, ValueError):
                        self._cell_rowspan = 1
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")
        if tag not in _TABLE_TAGS:
            return
        if tag == "br":
            self._parts.append("<br/>")
            return
        allowed = _TABLE_ATTRS.get(tag, ())
        adict = {str(k).lower(): v for k, v in attrs}
        attr_s = ""
        for k in allowed:
            if k not in adict or adict[k] is None:
                continue
            v = re.sub(r"[^0-9A-Za-z_\-]", "", str(adict[k]))
            if v:
                attr_s += f' {k}="{html.escape(v, quote=True)}"'
        if tag == "col":
            self._parts.append(f"<col{attr_s}/>")
            return
        self._parts.append(f"<{tag}{attr_s}>")
        self._stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if (tag or "").lower() not in ("br", "col"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if tag in ("td", "th") and self._cell is not None:
            text = "".join(self._cell)
            if self._row is not None:
                self._row.append(text)
                if self._row_meta is not None:
                    self._row_meta.append({
                        "text": text, "colspan": self._cell_span, "rowspan": self._cell_rowspan,
                    })
                self._row_cols += self._cell_span
                self.n_cols = max(self.n_cols, self._row_cols)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self.cell_meta.append(self._row_meta or [])
            self.n_cols = max(self.n_cols, self._row_cols, len(self._row))
            self._row = None
            self._row_meta = None
        if tag in _TABLE_TAGS and tag not in ("br", "col"):
            if self._stack and self._stack[-1] == tag:
                self._stack.pop()
                self._parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        if self._stack:
            self._parts.append(html.escape(data))

    def finish(self):
        if self._cell is not None:
            self.handle_endtag("td" if not (self._stack and self._stack[-1] == "th") else "th")
        if self._row is not None:
            self.handle_endtag("tr")
        while self._stack:
            self._parts.append(f"</{self._stack.pop()}>")


def _walk_table(raw: str | None) -> _TableWalker | None:
    if not raw or "<table" not in raw.lower():
        return None
    walker = _TableWalker()
    try:
        walker.feed(raw)
        walker.close()
        walker.finish()
    except Exception:
        return None
    return walker


def parse_table_html(raw: str | None) -> tuple[list[list[str]], int, str]:
    """Return (rows of cell texts, n_cols, sanitised XHTML)."""
    walker = _walk_table(raw)
    if walker is None:
        return [], 0, ""
    xhtml = "".join(walker._parts).strip()
    if "<table" not in xhtml.lower():
        xhtml = ""
    return walker.rows, walker.n_cols, xhtml


def parse_table_analysis(raw: str | None) -> dict:
    """Rows, column count, sanitised XHTML, and per-cell span metadata."""
    walker = _walk_table(raw)
    if walker is None:
        return {"rows": [], "n_cols": 0, "xhtml": "", "cell_meta": []}
    xhtml = "".join(walker._parts).strip()
    if "<table" not in xhtml.lower():
        xhtml = ""
    return {
        "rows": walker.rows, "n_cols": walker.n_cols, "xhtml": xhtml,
        "cell_meta": walker.cell_meta,
    }


def table_html_from_rows(rows: list[list[str]]) -> str:
    out = ["<table>"]
    for row in rows:
        out.append("<tr>")
        for cell in row:
            out.append(f"<td>{html.escape(cell)}</td>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def sanitize_table_html(raw: str | None) -> str:
    rows, _, xhtml = parse_table_html(raw)
    if xhtml:
        return xhtml
    if rows:
        return table_html_from_rows(rows)
    return ""


def cell_plain_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def is_prose_table(rows: list[list[str]], n_cols: int) -> bool:
    """True when MinerU labelled flowing paragraphs as a table."""
    cells = [c for row in rows for c in row]
    if not cells:
        return False
    lengths = [cell_plain_len(c) for c in cells]
    total = sum(lengths)
    avg = total / len(cells)
    sent = sum(1 for c in cells if re.search(r"[。！？]", c or ""))
    sent_frac = sent / len(cells)
    if total < 80:
        return False
    cols = n_cols or max((len(r) for r in rows), default=0)
    if cols <= 1 and avg >= 60 and sent >= 1:
        return True
    if cols <= 2 and avg >= _PROSE_AVG_CHARS and sent_frac >= 0.4:
        return True
    if avg >= _PROSE_LONG_CELL and sent >= 1:
        return True
    return False


def is_figure_style_caption(text: str) -> bool:
    """True for 图 / 圖 / Figure captions (not 表, which is a real table)."""
    t = (text or "").strip()
    if not t:
        return False
    return bool(FIGURE_CAPTION_RE.match(t))


def cell_is_empty(text: str) -> bool:
    t = (text or "").strip()
    return (not t) or bool(_EMPTY_CELL_RE.fullmatch(t))


def cell_is_noise(text: str) -> bool:
    """Fragmentary digits/symbols, not a complete number or a real word."""
    t = re.sub(r"\s+", "", text or "")
    if not t or cell_is_empty(t):
        return False
    if _COMPLETE_NUM_RE.fullmatch(t):
        return False
    if _XO_MARK_RE.fullmatch(t):
        return True
    if re.search(r"[\u3400-\u9fffA-Za-z]{2,}", t):
        return False
    if len(t) <= 2 and not t.isalnum():
        return True
    if len(t) <= 2 and t.isdigit():
        return False
    if len(t) <= 3 and re.fullmatch(r"[.\-–—_/\\|~`'\"]+", t):
        return True
    if len(t) <= 4 and re.search(r"\d", t) and re.search(r"[^\d.,%\-+\u2212]", t):
        return True
    if t.endswith(".") and t[:-1].isdigit() and len(t) <= 4:
        return True
    return False


def cell_is_xo(text: str) -> bool:
    t = re.sub(r"\s+", "", text or "")
    return bool(t and _XO_MARK_RE.fullmatch(t))


def _row_width_inconsistency(widths: list[int]) -> float:
    if len(widths) < 2:
        return 0.0
    ordered = sorted(widths)
    med = ordered[len(ordered) // 2]
    if med <= 0:
        return 1.0
    return sum(1 for w in widths if w != med) / len(widths)


def bbox_overlap_ratio(a, b) -> float:
    """Intersection over area of *a* (0 if either box is missing)."""
    if not a or not b or len(a) < 4 or len(b) < 4:
        return 0.0
    ix0, iy0 = max(float(a[0]), float(b[0])), max(float(a[1]), float(b[1]))
    ix1, iy1 = min(float(a[2]), float(b[2])), min(float(a[3]), float(b[3]))
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    area = (float(a[2]) - float(a[0])) * (float(a[3]) - float(a[1]))
    if area <= 0:
        return 0.0
    return ((ix1 - ix0) * (iy1 - iy0)) / area


def _bboxes_near(a, b, gap: float = 0.08) -> bool:
    if bbox_overlap_ratio(a, b) > 0:
        return True
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    horiz = not (float(b[2]) < float(a[0]) or float(b[0]) > float(a[2]))
    if not horiz:
        return False
    if float(b[3]) <= float(a[1]) + 0.02 and float(a[1]) - float(b[3]) <= gap:
        return True
    if float(a[3]) <= float(b[1]) + 0.02 and float(b[1]) - float(a[3]) <= gap:
        return True
    return False


def collect_figure_signals(table_bbox, page_blocks, own_captions) -> tuple[bool, float]:
    """Figure-style caption (own or nearby) and max overlap with an image/chart."""
    captions = [c for c in (own_captions or []) if c]
    overlap = 0.0
    for b in page_blocks or []:
        if not isinstance(b, dict):
            continue
        bb = b.get("bbox")
        bt = b.get("type") or ""
        if bt in FIGURE_REGION_TYPES and bb:
            overlap = max(overlap, bbox_overlap_ratio(table_bbox, bb))
        texts: list[str] = []
        if bt in TITLE_TYPES or bt in ("text", "paragraph_title") or bt.endswith("caption"):
            texts.append(block_text(b.get("content")))
        if isinstance(b.get("content"), list):
            for sub in b["content"]:
                if not isinstance(sub, dict):
                    continue
                st = str(sub.get("type") or "")
                if st.endswith("_caption"):
                    cap = block_text(sub.get("content"))
                    texts.append(cap)
                    if sub.get("bbox") and table_bbox and _bboxes_near(table_bbox, sub["bbox"]):
                        captions.append(cap)
        for t in texts:
            if not is_figure_style_caption(t):
                continue
            if bb and table_bbox and _bboxes_near(table_bbox, bb):
                captions.append(t)
            elif not bb and not table_bbox:
                captions.append(t)
    fig_cap = any(is_figure_style_caption(t) for t in captions)
    return fig_cap, overlap


def table_quality(rows: list[list[str]], cell_meta: list[list[dict]] | None,
                  captions: list[str] | None = None, table_bbox=None,
                  page_blocks=None, min_quality: float = DEFAULT_TABLE_MIN_QUALITY) -> dict:
    """Score recognised table HTML; ok=False means fall back to the image crop."""
    meta = cell_meta or [[{"text": c, "colspan": 1, "rowspan": 1} for c in row] for row in rows]
    cells = [c for row in rows for c in row]
    n = len(cells)
    fig_cap, overlap = collect_figure_signals(table_bbox, page_blocks, captions)
    if n == 0:
        return {
            "ok": False, "reason": "no-cells",
            "empty": 1.0, "noise": 1.0, "xo": 0.0, "cols": 1.0, "span": 0.0,
            "caption": fig_cap, "overlap": overlap, "score": 0.0, "dense": False,
        }
    empty_n = sum(1 for c in cells if cell_is_empty(c))
    empty_ratio = empty_n / n
    nonempty = [c for c in cells if not cell_is_empty(c)]
    noise_n = sum(1 for c in nonempty if cell_is_noise(c))
    noise_ratio = noise_n / len(nonempty) if nonempty else 1.0
    xo_n = sum(1 for c in nonempty if cell_is_xo(c))
    xo_ratio = xo_n / len(nonempty) if nonempty else 0.0
    if meta and any(meta):
        widths = [sum(int(m.get("colspan") or 1) for m in row) for row in meta]
    else:
        widths = [len(r) for r in rows]
    col_inconsist = _row_width_inconsistency(widths)
    body = [m for i, row in enumerate(meta) for m in row if i > 0]
    span_n = sum(1 for m in body if int(m.get("colspan") or 1) > 1 or int(m.get("rowspan") or 1) > 1)
    span_ratio = span_n / len(body) if body else 0.0
    dense = (
        empty_ratio <= 0.20 and noise_ratio <= 0.25 and col_inconsist <= 0.20
        and xo_ratio <= 0.10 and n >= 4
    )
    reasons: list[str] = []
    if empty_ratio > TABLE_EMPTY_CELL_RATIO:
        reasons.append("empty")
    if noise_ratio > TABLE_NOISE_CELL_RATIO:
        reasons.append("noise")
    if xo_ratio > TABLE_XO_CELL_RATIO:
        reasons.append("chart-marks")
    if col_inconsist > TABLE_COL_INCONSISTENT_RATIO:
        reasons.append("scrambled-cols")
    if span_ratio > TABLE_SPAN_IRREGULAR_RATIO:
        reasons.append("irregular-spans")
    score = 1.0
    score -= 0.9 * max(0.0, empty_ratio - 0.10) / 0.50
    score -= 0.9 * max(0.0, noise_ratio - 0.15) / 0.55
    score -= 0.8 * col_inconsist
    score -= 0.6 * span_ratio
    score -= 0.8 * xo_ratio
    if fig_cap:
        score -= 0.08 if dense else 0.28
    if overlap >= TABLE_FIGURE_OVERLAP:
        score -= 0.08 if dense else 0.25
    score = max(0.0, min(1.0, score))
    if reasons:
        reason = "+".join(reasons)
        if fig_cap:
            reason = "figure-caption+" + reason
        elif overlap >= TABLE_FIGURE_OVERLAP:
            reason = "figure-overlap+" + reason
        ok = False
    elif score < min_quality:
        ok = False
        reason = "figure-caption+low-score" if fig_cap else "low-score"
    else:
        ok = True
        reason = "quality-ok"
    return {
        "ok": ok, "reason": reason, "empty": empty_ratio, "noise": noise_ratio,
        "xo": xo_ratio, "cols": col_inconsist, "span": span_ratio,
        "caption": fig_cap, "overlap": overlap, "score": score, "dense": dense,
    }


def format_table_decision_log(page: int, info: dict) -> str:
    s = info.get("scores") or info
    if info.get("decision") == "prose":
        return (
            f"page {page}: table → prose ({info.get('reason')}; "
            f"cells={s.get('cells', '?')} cols={s.get('cols', '?')} avg={s.get('avg', 0):.0f})"
        )
    return (
        f"page {page}: table → {info.get('decision')} ({info.get('reason')}; "
        f"empty={s.get('empty', 0):.2f} noise={s.get('noise', 0):.2f} "
        f"xo={s.get('xo', 0):.2f} cols={s.get('cols', 0):.2f} "
        f"span={s.get('span', 0):.2f} cap={int(bool(s.get('caption')))} "
        f"ov={s.get('overlap', 0):.2f} score={s.get('score', 0):.2f})"
    )


def split_ocr_paragraphs(text: str) -> list[str]:
    """Split a cell whose OCR joined lines with spaces into paragraphs.

    In these scans a new indented paragraph becomes ``。 `` (terminator +
    space) while sentences inside the same paragraph have no space after
    ``。``. Line-wrap spaces sit between CJK letters and are stripped later
    by ``normalize_punct``.
    """
    s = re.sub(r"\s*\n\s*", " ", text or "")
    s = re.sub(r"[ \t]+", " ", s).strip()
    if not s:
        return []
    parts = [p.strip() for p in _PARA_SPLIT_RE.split(s) if p and p.strip()]
    merged: list[str] = []
    for part in parts:
        if merged and part.startswith(_PARA_NO_BREAK_PREFIXES):
            merged[-1] += part
        else:
            merged.append(part)
    return merged or [s]


def _distribute_vertical_bboxes(bbox, weights) -> list[list[float]]:
    x0, y0, x1, y1 = bbox
    total = float(sum(weights)) or 1.0
    out, cur, h = [], y0, y1 - y0
    for w in weights:
        dh = h * (float(w) / total)
        out.append([x0, cur, x1, cur + dh])
        cur += dh
    if out:
        out[-1][3] = y1
    return out


def prose_table_to_paras(rows: list[list[str]], table_bbox, page: int,
                         punct: bool, height_pt: float, width_pt: float) -> list[dict]:
    """Explode a prose table into paragraph items with estimated y-ranges."""
    row_paras: list[list[str]] = []
    for row in rows:
        paras: list[str] = []
        for cell in row:
            chunks = split_ocr_paragraphs(cell)
            for ch in chunks:
                text = normalize_punct(ch) if punct else re.sub(
                    r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", ch
                ).strip()
                if text:
                    paras.append(text)
        row_paras.append(paras)
    if not any(row_paras):
        return []
    # Rows are visual bands from the layout model; keep them equal height.
    # Character weight is only used to place paragraphs inside a row.
    row_weights = [1 if paras else 0 for paras in row_paras]
    if not any(row_weights):
        return []
    row_boxes = _distribute_vertical_bboxes(table_bbox, row_weights)
    items: list[dict] = []
    for paras, rb in zip(row_paras, row_boxes):
        if not paras:
            continue
        p_boxes = _distribute_vertical_bboxes(rb, [max(1, len(p)) for p in paras])
        for text, bb in zip(paras, p_boxes):
            x0, y0, x1, y1 = bb
            items.append({
                "kind": "para", "text": text, "page": page, "pages": [page],
                "cls": None, "bbox": list(bb),
                "_h": (y1 - y0) * height_pt, "_w": (x1 - x0) * width_pt,
            })
    return items


def extract_table_payload(block: dict) -> dict:
    body = list(block.get("bbox") or [0, 0, 1, 1])
    caption, foot, table_html, plain = [], [], None, []
    if isinstance(block.get("content"), list):
        for sub in block["content"]:
            if not isinstance(sub, dict):
                continue
            st = sub.get("type", "")
            if st.endswith("_body"):
                if sub.get("bbox"):
                    body = list(sub["bbox"])
                c = sub.get("content")
                if isinstance(c, str) and "<table" in c.lower():
                    table_html = c
                elif isinstance(c, str) and c.strip():
                    plain.append(c)
                else:
                    raw = block_text(c)
                    if raw and "<table" in raw.lower():
                        table_html = raw
                    elif raw.strip():
                        plain.append(raw)
            elif st.endswith("_caption"):
                caption.append(block_text(sub.get("content")))
            elif st.endswith("_footnote"):
                foot.append(block_text(sub.get("content")))
    if table_html is None and plain:
        table_html = table_html_from_rows([[p] for p in plain])
    return {"body": body, "caption": caption, "footnote": foot, "html": table_html}


def classify_table_decision(payload: dict, table_mode: str,
                            page_blocks=None, table_bbox=None,
                            min_quality: float = DEFAULT_TABLE_MIN_QUALITY) -> dict:
    """Decide prose / html / image. Auto: prose, then quality gate, then HTML."""
    mode = table_mode if table_mode in TABLE_MODES else "auto"
    try:
        min_quality = float(min_quality)
    except (TypeError, ValueError):
        min_quality = DEFAULT_TABLE_MIN_QUALITY
    min_quality = max(0.0, min(1.0, min_quality))
    raw = payload.get("html")
    analysis = parse_table_analysis(raw)
    rows, n_cols = analysis["rows"], analysis["n_cols"]
    q = table_quality(
        rows, analysis.get("cell_meta"),
        captions=payload.get("caption") or [],
        table_bbox=table_bbox or payload.get("body"),
        page_blocks=page_blocks,
        min_quality=min_quality,
    )
    scores = {k: q[k] for k in (
        "empty", "noise", "xo", "cols", "span", "caption", "overlap", "score", "dense",
    )}
    if mode == "image" or not raw:
        return {"decision": "image", "reason": "mode=image" if mode == "image" else "no-html",
                "scores": scores, "analysis": analysis}
    if mode == "html":
        return {"decision": "html", "reason": "forced-html", "scores": scores, "analysis": analysis}
    if is_prose_table(rows, n_cols):
        cells = [c for row in rows for c in row]
        total = sum(cell_plain_len(c) for c in cells) if cells else 0
        avg = total / len(cells) if cells else 0
        return {
            "decision": "prose",
            "reason": "long sentence-like cells",
            "scores": {"cells": len(cells), "cols": n_cols, "avg": avg},
            "analysis": analysis,
        }
    if not q["ok"]:
        return {"decision": "image", "reason": q["reason"], "scores": scores, "analysis": analysis}
    return {"decision": "html", "reason": q["reason"], "scores": scores, "analysis": analysis}


def _is_margin_heading(it: dict) -> bool:
    """Left-gutter titles (e.g. 总结 / 结语) printed beside the text column."""
    if it.get("kind") != "heading":
        return False
    bbox = it.get("bbox")
    if not bbox or len(bbox) < 4:
        return False
    x0, _, x1, _ = bbox
    return x1 <= 0.28 and (x1 - x0) <= 0.22 and x0 < 0.18


def _item_mid_y(it: dict) -> float | None:
    bbox = it.get("bbox")
    if not bbox or len(bbox) < 4:
        return None
    return (float(bbox[1]) + float(bbox[3])) / 2.0


def interleave_margin_headings(items: list[dict]) -> list[dict]:
    """Insert left-margin headings by vertical position among body items."""
    if len(items) < 2:
        return items
    margin, body = [], []
    for it in items:
        if _is_margin_heading(it):
            margin.append(it)
        else:
            body.append(it)
    if not margin:
        return items
    for h in sorted(margin, key=lambda it: (it.get("bbox") or [0, 0])[1]):
        hy = _item_mid_y(h)
        if hy is None:
            hy = 0.0
        idx = len(body)
        for i, it in enumerate(body):
            if it.get("kind") in ("figure", "table"):
                continue
            mid = _item_mid_y(it)
            if mid is not None and mid > hy:
                idx = i
                break
        body.insert(idx, h)
    return body


def is_running_band_text(it: dict) -> bool:
    """Short footer-band leftovers MinerU labelled as body text (e.g. 道氏理论)."""
    if it.get("kind") != "para" or not _in_running_band(it.get("bbox")):
        return False
    t = (it.get("text") or "").strip()
    if not t or SENTENCE_TAIL_RE.search(t):
        return False
    return len(compact_heading(t)) <= 8


def _prev_body_para(items: list[dict]):
    for x in reversed(items):
        if x["kind"] == "para" and not is_running_band_text(x):
            return x
    return None


def _continues_from_prev_page(items: list[dict], first_text_on_page: bool,
                              pidx: int, explicit: bool = False) -> bool:
    if explicit:
        return True
    if not (first_text_on_page and pidx > 0):
        return False
    for x in reversed(items):
        if x["kind"] == "heading":
            return False
        if x["kind"] != "para" or is_running_band_text(x):
            continue
        return bool(x.get("text") and x["text"][-1] not in TERMINAL)
    return False


# --------------------------------------------------------------------- step 3+4
def _dummy_page_info(page: int, width_pt: float = 1.0, height_pt: float = 1.0) -> dict:
    return {
        "page": page, "width_pt": width_pt, "height_pt": height_pt,
        "images": 0, "image_coverage": 0.0, "text_chars": 0, "invisible_text_spans": 0,
        "png": None, "png_size": [0, 0],
    }


def build_document(mj: dict, page_info: list[dict] | None, img_dir: Path | None,
                   punct: bool, pad: float = 0.008,
                   chapter_re: re.Pattern | None = None,
                   toc_entries: list[dict] | None = None,
                   chapter_max_len: int | None = 48,
                   table_mode: str = "auto",
                   table_images: bool = False,
                   table_min_quality: float = DEFAULT_TABLE_MIN_QUALITY) -> list[dict]:
    """Build the linear document stream. Figure crops are skipped when PNGs are missing."""
    if page_info is None:
        page_info = page_info_from_mineru(mj)
    Image = None
    if img_dir is not None:
        img_dir.mkdir(parents=True, exist_ok=True)
        try:
            from PIL import Image as _Image
            Image = _Image
        except ImportError:
            log("Pillow not available; skipping figure crops")
    items: list[dict] = []
    fig_no = 0
    for p in mj["pages"]:
        pidx = p["page_idx"]
        pinfo = page_info[pidx] if pidx < len(page_info) else _dummy_page_info(pidx + 1)
        page_img = None
        png = pinfo.get("png")
        if Image is not None and png:
            try:
                page_img = Image.open(png)
            except Exception as e:
                log(f"page {pidx + 1}: cannot open {png} ({e}); skipping crops")
                page_img = None
        W = H = None
        if page_img is not None:
            W, H = page_img.size
        first_text_on_page = True
        height_pt = float(pinfo.get("height_pt") or 1.0)
        width_pt = float(pinfo.get("width_pt") or 1.0)
        page_start = len(items)
        for b in p["blocks"]:
            t = b["type"]
            x0, y0, x1, y1 = b["bbox"]
            if t in DROP_TYPES:
                continue
            if t == "table":
                payload = extract_table_payload(b)
                info = classify_table_decision(
                    payload, table_mode, page_blocks=p.get("blocks"),
                    table_bbox=b.get("bbox"), min_quality=table_min_quality,
                )
                log(format_table_decision_log(pidx + 1, info))
                analysis = info.get("analysis") or parse_table_analysis(payload.get("html"))
                rows, xhtml = analysis.get("rows") or [], analysis.get("xhtml") or ""
                decision = info["decision"]
                if table_mode != "image" and decision == "prose" and rows:
                    exploded = prose_table_to_paras(
                        rows, payload.get("body") or b["bbox"], pidx + 1,
                        punct, height_pt, width_pt,
                    )
                    for it in exploded:
                        cont = _continues_from_prev_page(
                            items, first_text_on_page, pidx,
                            explicit=bool(b.get("continues_prev")) and first_text_on_page,
                        )
                        first_text_on_page = False
                        if cont:
                            prev = _prev_body_para(items)
                            if prev is not None:
                                prev["text"] += it["text"]
                                prev.setdefault("pages", []).append(pidx + 1)
                                continue
                        items.append(it)
                    continue
                if table_mode != "image" and decision == "html":
                    table_xhtml = xhtml or sanitize_table_html(payload.get("html"))
                    if table_xhtml:
                        fig_no += 1
                        name = f"fig_p{pidx + 1:04d}_{fig_no:03d}.png"
                        box = None
                        if table_images and page_img is not None and img_dir is not None and W and H:
                            bx0, by0, bx1, by1 = payload.get("body") or b["bbox"]
                            box = (max(0, int((bx0 - pad) * W)), max(0, int((by0 - pad) * H)),
                                   min(W, int((bx1 + pad) * W)), min(H, int((by1 + pad) * H)))
                            page_img.crop(box).save(img_dir / name, optimize=True)
                        cap = " ".join(normalize_punct(c) if punct else c
                                       for c in payload.get("caption") or [] if c)
                        items.append({
                            "kind": "table", "type": t, "html": table_xhtml,
                            "src": name if box else None,
                            "show_image": bool(box),
                            "path": str((img_dir / name) if img_dir and box else name),
                            "caption": cap,
                            "footnote": " ".join(payload.get("footnote") or []),
                            "page": pidx + 1, "crop_px": box, "bbox": list(b["bbox"]),
                        })
                        continue
            if t in FLOAT_TYPES:
                fig_no += 1
                body = b["bbox"]
                caption, foot, table_html = [], [], None
                if isinstance(b.get("content"), list):
                    for sub in b["content"]:
                        st = sub.get("type", "")
                        if st.endswith("_body"):
                            body = sub.get("bbox", body)
                            c = sub.get("content")
                            if isinstance(c, str) and "<table" in c:
                                table_html = c
                        elif st.endswith("_caption"):
                            caption.append(block_text(sub.get("content")))
                        elif st.endswith("_footnote"):
                            foot.append(block_text(sub.get("content")))
                bx0, by0, bx1, by1 = body
                name = f"fig_p{pidx + 1:04d}_{fig_no:03d}.png"
                box = None
                if page_img is not None and W and H:
                    box = (max(0, int((bx0 - pad) * W)), max(0, int((by0 - pad) * H)),
                           min(W, int((bx1 + pad) * W)), min(H, int((by1 + pad) * H)))
                    crop = page_img.crop(box)
                    crop.save(img_dir / name, optimize=True)
                cap = " ".join(normalize_punct(c) if punct else c for c in caption if c)
                items.append({"kind": "figure", "type": t, "src": name,
                              "path": str((img_dir / name) if img_dir else name),
                              "caption": cap, "footnote": " ".join(foot), "table_html": table_html,
                              "page": pidx + 1, "crop_px": box, "bbox": list(b["bbox"])})
                continue
            text = block_text(b.get("content"))
            text = re.sub(r"\s*\n\s*", "", text) if re.search(f"[{CJK}]", text) else text
            if punct:
                text = normalize_punct(text)
            if not text:
                continue
            if t in TITLE_TYPES:
                it = {"kind": "heading", "text": text, "page": pidx + 1,
                      "bbox": [x0, y0, x1, y1],
                      "_h": (y1 - y0) * height_pt, "_w": (x1 - x0) * width_pt}
                items.append(it)
                first_text_on_page = False
                continue
            # body text / list / footnote etc.
            cont = _continues_from_prev_page(
                items, first_text_on_page, pidx, explicit=bool(b.get("continues_prev")),
            )
            first_text_on_page = False
            if cont:
                prev = _prev_body_para(items)
                if prev is not None:
                    prev["text"] += text
                    prev.setdefault("pages", []).append(pidx + 1)
                    continue
            items.append({"kind": "para", "text": text, "page": pidx + 1, "pages": [pidx + 1],
                          "cls": "footnote" if "footnote" in t else None,
                          "bbox": [x0, y0, x1, y1],
                          "_h": (y1 - y0) * height_pt, "_w": (x1 - x0) * width_pt})
        tail = [it for it in items[page_start:] if not is_running_band_text(it)]
        items[page_start:] = interleave_margin_headings(tail)
    return refine_headings(items, chapter_re, toc_entries, chapter_max_len)


# --------------------------------------------------------------------- step 5
CSS = """
body { font-family: serif; line-height: 1.7; margin: 0 4%; }
h1 { font-size: 1.6em; margin: 1.2em 0 0.8em; text-align: left; }
h2 { font-size: 1.25em; margin: 1.1em 0 0.5em; }
h3 { font-size: 1.1em; margin: 0.9em 0 0.4em; }
p { text-indent: 2em; margin: 0 0 0.35em; text-align: justify; }
p.noindent { text-indent: 0; }
p.footnote { font-size: 0.85em; text-indent: 0; }
figure { margin: 1em 0; text-align: center; page-break-inside: avoid; }
figure img { max-width: 100%; height: auto; }
figcaption { font-size: 0.9em; text-indent: 0; text-align: left; margin-top: 0.4em; }
figure.table { text-align: left; }
figure.table table { width: 100%; border-collapse: collapse; font-size: 0.92em; margin: 0.2em 0; }
figure.table th, figure.table td {
  border: 1px solid #666; padding: 0.25em 0.45em; vertical-align: top; text-align: left;
}
figure.table th { font-weight: bold; }
figure.table img { margin-bottom: 0.4em; }
"""


def split_chapters(items: list[dict], fallback_title: str) -> list[dict]:
    chapters, cur = [], None
    for it in items:
        if it["kind"] == "heading" and int(it.get("level") or 2) == 1:
            cur = {"title": it["text"], "items": [it]}
            chapters.append(cur)
            continue
        if cur is None:
            cur = {"title": fallback_title, "items": []}
            chapters.append(cur)
        cur["items"].append(it)
    return chapters


def render_xhtml(ch: dict, sec_ids: list) -> str:
    out = []
    n = 0
    for it in ch["items"]:
        if it["kind"] == "heading":
            n += 1
            hid = f"h{n}"
            lvl = max(1, min(int(it.get("level") or 2), 3))
            out.append(f'<h{lvl} id="{hid}">{html.escape(it["text"])}</h{lvl}>')
            if lvl >= 2:
                sec_ids.append((lvl, hid, it["text"]))
        elif it["kind"] == "para":
            cls = it.get("cls")
            if not cls and re.match(r"^(\d+|[一二三四五六七八九十]+)[.、．]", it["text"]) and len(it["text"]) < 60:
                cls = "noindent"
            c = f' class="{cls}"' if cls else ""
            out.append(f"<p{c}>{html.escape(it['text'])}</p>")
        elif it["kind"] == "table":
            cap = f"<figcaption>{html.escape(it['caption'])}</figcaption>" if it.get("caption") else ""
            bits = []
            if it.get("src") and it.get("show_image"):
                alt = html.escape((it.get("caption") or "")[:80] or "table", quote=True)
                bits.append(f'<img src="images/{it["src"]}" alt="{alt}"/>')
            bits.append(it["html"])
            if cap:
                bits.append(cap)
            out.append(f'<figure class="table">{"".join(bits)}</figure>')
        elif it["kind"] == "figure":
            alt = html.escape(it["caption"][:80] or "illustration", quote=True)
            cap = f"<figcaption>{html.escape(it['caption'])}</figcaption>" if it["caption"] else ""
            out.append(f'<figure><img src="images/{it["src"]}" alt="{alt}"/>{cap}</figure>')
    return "\n".join(out)


def _nested_chapter_toc(ch_title: str, file_name: str, i: int, secs: list):
    """secs: list of (level, hid, text) with level >= 2."""
    from ebooklib import epub
    if not secs:
        return epub.Link(file_name, ch_title, f"c{i}")
    nodes = []
    for lvl, hid, text in secs:
        link = epub.Link(f"{file_name}#{hid}", text, f"c{i}_{hid}")
        if lvl <= 2 or not nodes:
            nodes.append(link)
            continue
        last = nodes[-1]
        if isinstance(last, tuple):
            last[1].append(link)
        else:
            nodes[-1] = (last, [link])
    return (epub.Section(ch_title, href=file_name), nodes)


def build_epub(chapters, out: Path, title: str, author: str, lang: str, img_dir: Path,
               cover: Path | None):
    from ebooklib import epub
    book = epub.EpubBook()
    book.set_identifier(f"urn:uuid:{uuid.uuid4()}")
    book.set_title(title)
    book.set_language(lang)
    if author:
        book.add_author(author)
    book.add_metadata("DC", "description", "Generated by pdf2epub.py (MinerU OCR)")
    if cover:
        book.set_cover("cover" + cover.suffix, cover.read_bytes())
    css = epub.EpubItem(uid="style", file_name="style/main.css", media_type="text/css", content=CSS)
    book.add_item(css)
    used = set()
    toc, spine = [], ["nav"]
    for i, ch in enumerate(chapters, 1):
        secs = []
        body = render_xhtml(ch, secs)
        c = epub.EpubHtml(title=ch["title"], file_name=f"chap_{i:03d}.xhtml", lang=lang)
        c.content = f"<html><head><title>{html.escape(ch['title'])}</title></head><body>{body}</body></html>"
        c.add_item(css)
        book.add_item(c)
        spine.append(c)
        used.update(re.findall(r'src="images/([^"]+)"', body))
        toc.append(_nested_chapter_toc(ch["title"], c.file_name, i, secs))
    for name in sorted(used):
        pth = img_dir / name
        if not pth.exists():
            log(f"skip missing image {name}")
            continue
        book.add_item(epub.EpubImage(uid=name, file_name=f"images/{name}", media_type="image/png",
                                     content=pth.read_bytes()))
    book.toc = toc
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(out), book)


def to_markdown(items) -> str:
    md = []
    for it in items:
        if it["kind"] == "heading":
            md.append("#" * int(it.get("level") or 2) + " " + it["text"])
        elif it["kind"] == "para":
            md.append(it["text"])
        elif it["kind"] == "table":
            cap = it.get("caption") or ""
            md.append((f"**{cap}**\n\n" if cap else "") + (it.get("html") or ""))
        else:
            md.append(f"![{it['caption']}](images/{it['src']})\n\n*{it['caption']}*")
    return "\n\n".join(md) + "\n"


def toc_size(chapters: list[dict]) -> int:
    """Number of nav entries: one per chapter plus nested h2/h3."""
    n = 0
    for ch in chapters:
        n += 1
        n += sum(1 for it in ch["items"] if it["kind"] == "heading" and int(it.get("level") or 2) >= 2)
    return n


def structure_report(chapters: list[dict]) -> list[dict]:
    rows = []
    for i, ch in enumerate(chapters, 1):
        page = next((it.get("page") for it in ch["items"] if it.get("page")), None)
        n2 = sum(1 for it in ch["items"] if it["kind"] == "heading" and int(it.get("level") or 2) == 2)
        n3 = sum(1 for it in ch["items"] if it["kind"] == "heading" and int(it.get("level") or 2) >= 3)
        rows.append({"n": i, "title": ch["title"], "page": page, "h2": n2, "h3": n3})
    return rows


def draw_overlays(mj, page_info, out_dir: Path):
    """Debug artefact: page renders with layout boxes (red=dropped, blue=text,
    green=figure, purple=caption, orange=heading)."""
    from PIL import Image, ImageDraw
    out_dir.mkdir(parents=True, exist_ok=True)
    col = lambda t: ("red" if t in DROP_TYPES else "green" if t in FLOAT_TYPES
                     else "orange" if t in TITLE_TYPES else "blue")
    for p in mj["pages"]:
        pidx = p["page_idx"]
        if pidx >= len(page_info):
            continue
        png = page_info[pidx].get("png")
        if not png or not Path(png).exists():
            continue
        im = Image.open(png).convert("RGB")
        W, H = im.size
        d = ImageDraw.Draw(im)
        for b in p["blocks"]:
            x0, y0, x1, y1 = b["bbox"]
            d.rectangle((x0 * W, y0 * H, x1 * W, y1 * H), outline=col(b["type"]), width=6)
            d.text((x0 * W + 8, y0 * H + 4), f'{b.get("index", "")}:{b["type"]}', fill=col(b["type"]))
            if isinstance(b.get("content"), list):
                for sub in b["content"]:
                    if isinstance(sub, dict) and sub.get("bbox") and sub.get("type", "").endswith(("_caption", "_footnote")):
                        sx0, sy0, sx1, sy1 = sub["bbox"]
                        d.rectangle((sx0 * W, sy0 * H, sx1 * W, sy1 * H), outline="purple", width=5)
        im.save(out_dir / f"layout_p{p['page_idx'] + 1:04d}.png")


# --------------------------------------------------------------------- main
def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="?", type=Path, help="input PDF (optional with --from-json)")
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("--workdir", type=Path, help="intermediate files (default: <output stem>_work)")
    ap.add_argument("--tier", default="basic", choices=["flash", "basic", "standard", "advanced"],
                    help="MinerU tier: basic = pipeline OCR (fast on CPU); standard = +VLM (slow on CPU)")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--title")
    ap.add_argument("--author", default="")
    ap.add_argument("--lang", default="zh-CN")
    ap.add_argument("--cover", type=Path, help="optional cover image")
    ap.add_argument("--reuse-json", action="store_true", help="reuse cached MinerU JSON in workdir")
    ap.add_argument("--from-json", type=Path, dest="from_json",
                    help="MinerU JSON (skip OCR; PDF/page images optional)")
    ap.add_argument("--chapter-regex", dest="chapter_regex",
                    help="regex matching chapter-level headings (overrides the Chinese default)")
    ap.add_argument("--toc-file", type=Path, dest="toc_file",
                    help="user TOC: one title per line, indent for nesting, optional page number")
    ap.add_argument("--tables", choices=TABLE_MODES, default="auto",
                    help="table handling: auto = prose→paragraphs, real tables→HTML; "
                         "html = always emit recognised HTML; image = crop only (legacy)")
    ap.add_argument("--table-images", action="store_true", dest="table_images",
                    help="also embed the cropped scan of real tables next to the HTML")
    ap.add_argument("--table-min-quality", type=float, default=DEFAULT_TABLE_MIN_QUALITY,
                    dest="table_min_quality",
                    help="auto mode: emit HTML only when the quality score is at least this "
                         f"(default {DEFAULT_TABLE_MIN_QUALITY}); lower keeps more HTML")
    ap.add_argument("--no-punct-normalize", action="store_true")
    ap.add_argument("--epubcheck", action="store_true", help="run epubcheck if available")
    a = ap.parse_args(argv)

    if a.pdf is None and a.from_json is None:
        ap.error("provide a PDF, or --from-json for a JSON-only rebuild")
    if a.pdf is None and a.output is None:
        ap.error("-o/--output is required when no PDF is given")

    out = a.output or a.pdf.with_suffix(".epub")
    work = a.workdir or out.with_name(out.stem + "_work")
    work.mkdir(parents=True, exist_ok=True)
    timings = {}
    meta = {}
    chapter_re = compile_chapter_re(a.chapter_regex)
    chapter_max_len = 48 if a.chapter_regex is None else None
    toc_entries = parse_toc_file(a.toc_file) if a.toc_file else None

    page_info = None
    if a.pdf:
        t = time.time()
        page_info, meta = inspect_and_render(a.pdf, work / "pages", a.dpi)
        timings["render"] = time.time() - t
        for pi in page_info:
            log(f"page {pi['page']}: {pi['width_pt']:.0f}x{pi['height_pt']:.0f}pt, images={pi['images']} "
                f"(coverage {pi['image_coverage']}), text layer chars={pi['text_chars']} "
                f"(invisible spans={pi['invisible_text_spans']}) -> {pi['png_size']}")

    jpath = work / f"mineru_{a.tier}.json"
    t = time.time()
    if a.from_json:
        mj = json.loads(a.from_json.read_text(encoding="utf-8"))
        log(f"loaded {a.from_json}")
    elif a.reuse_json and jpath.exists():
        mj = json.loads(jpath.read_text(encoding="utf-8"))
        log(f"reused {jpath}")
    else:
        if a.pdf is None:
            ap.error("--reuse-json needs a cached JSON in workdir, or pass --from-json")
        log(f"running MinerU tier={a.tier} (ocr_mode=ocr, local) ...")
        mj = run_mineru(a.pdf, a.tier)
        jpath.write_text(json.dumps(mj, ensure_ascii=False, indent=1), encoding="utf-8")
    timings["ocr_layout"] = time.time() - t

    if page_info is None:
        page_info = page_info_from_mineru(mj, work / "pages")
        log(f"page info from JSON ({len(page_info)} pages); figure crops skipped if PNGs are missing")

    t = time.time()
    items = build_document(
        mj, page_info, work / "images", punct=not a.no_punct_normalize,
        chapter_re=chapter_re, toc_entries=toc_entries, chapter_max_len=chapter_max_len,
        table_mode=a.tables, table_images=a.table_images,
        table_min_quality=a.table_min_quality,
    )
    try:
        draw_overlays(mj, page_info, work / "layout")
    except Exception as e:
        log(f"layout overlays skipped ({e})")
    title = a.title or (meta.get("title") or "").strip() or next(
        (i["text"] for i in items if i["kind"] == "heading" and i.get("level") == 1), 
        (a.pdf.stem if a.pdf else out.stem))
    chapters = split_chapters(items, title)
    (work / "book.md").write_text(to_markdown(items), encoding="utf-8")
    (work / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    for row in structure_report(chapters):
        log(f"chapter {row['n']:02d} p{row['page'] or '?'}: {row['title']} "
            f"(h2={row['h2']}, h3={row['h3']})")
    log(f"TOC entries (nested): {toc_size(chapters)}")
    build_epub(chapters, out, title, a.author, a.lang, work / "images", a.cover)
    timings["build"] = time.time() - t
    nfig = sum(1 for i in items if i["kind"] == "figure")
    ntab = sum(1 for i in items if i["kind"] == "table")
    log(f"wrote {out}: {len(chapters)} chapters, {nfig} figures, {ntab} HTML tables, "
        f"{sum(len(i['text']) for i in items if i['kind'] == 'para')} body chars")
    log("timings: " + ", ".join(f"{k}={v:.1f}s" for k, v in timings.items()))

    if a.epubcheck:
        cmd = None
        if shutil.which("epubcheck"):
            jar = Path("/usr/share/java/epubcheck.jar")
            cmd = ["java", "-jar", str(jar), str(out)] if jar.exists() else ["epubcheck", str(out)]
        if cmd:
            r = subprocess.run(cmd, capture_output=True, text=True)
            log("epubcheck:\n" + (r.stdout + r.stderr).strip())
        else:
            log("epubcheck not installed; skipped")


if __name__ == "__main__":  # guard required: MinerU uses spawn-based worker processes
    sys.exit(main())
