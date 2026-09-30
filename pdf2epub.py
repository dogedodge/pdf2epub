#!/usr/bin/env python3
"""pdf2epub.py - convert a scanned (image-only) Chinese PDF into an EPUB.

Pipeline
  1. Inspect the PDF with PyMuPDF and render every page to PNG (default 300 DPI).
  2. Layout analysis + OCR with MinerU (runs locally; "basic" tier = ONNX
     PP-DocLayoutV2 + PP-OCRv6, "standard" tier adds the MinerU2.5 1.2B VLM).
  3. Crop illustrations / charts / tables from the page renders using the
     layout boxes.
  4. Clean text: drop headers/footers/page numbers, merge paragraphs split
     across pages, normalise CJK punctuation, detect chapters/headings.
  5. Build an EPUB 3 (ebooklib): one XHTML per chapter, figures in place,
     nav + NCX TOC, metadata. Optionally validate with epubcheck.

Usage
  python pdf2epub.py input.pdf -o out.epub [--tier basic|standard]
         [--title T] [--author A] [--dpi 300] [--workdir DIR]
         [--reuse-json] [--from-json mineru.json] [--chapter-regex RE]
         [--toc-file toc.txt] [--no-punct-normalize] [--epubcheck]
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
                   chapter_max_len: int | None = 48) -> list[dict]:
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
        for b in p["blocks"]:
            t = b["type"]
            x0, y0, x1, y1 = b["bbox"]
            if t in DROP_TYPES:
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
            cont = bool(b.get("continues_prev"))
            if first_text_on_page and pidx > 0 and not cont:
                # heuristic backup: previous paragraph did not end a sentence
                prev = next((x for x in reversed(items) if x["kind"] in ("para", "heading")), None)
                if prev and prev["kind"] == "para" and prev["text"][-1] not in TERMINAL:
                    cont = True
            first_text_on_page = False
            if cont:
                prev = next((x for x in reversed(items) if x["kind"] == "para"), None)
                if prev is not None:
                    prev["text"] += text
                    prev["pages"].append(pidx + 1)
                    continue
            items.append({"kind": "para", "text": text, "page": pidx + 1, "pages": [pidx + 1],
                          "cls": "footnote" if "footnote" in t else None,
                          "bbox": [x0, y0, x1, y1],
                          "_h": (y1 - y0) * height_pt, "_w": (x1 - x0) * width_pt})
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
    log(f"wrote {out}: {len(chapters)} chapters, {nfig} figures, "
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
