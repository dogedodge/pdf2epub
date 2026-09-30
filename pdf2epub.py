#!/usr/bin/env python3
"""pdf2epub.py - convert a scanned (image-only) Chinese PDF into an EPUB.

Two-stage pipeline (plus optional proofreading):

  ocr         Render pages, run MinerU locally, write an editable project dir.
  proofread   Fix confident OCR errors via the Cursor CLI (headless `agent`).
  build       Rebuild the EPUB purely from the project directory.
  all         ocr + proofread + build.

The project directory is the source of truth. Each page keeps its scan next to
editable Markdown; `build` merges cross-page paragraphs and splits chapters
from that text. Hand edits and proofread corrections therefore show up in the
EPUB.

Usage
  python pdf2epub.py ocr input.pdf --project book_work [--tier basic] [--reuse-json]
  python pdf2epub.py proofread --project book_work [--model MODEL] [--jobs N]
  python pdf2epub.py build --project book_work -o out.epub [--epubcheck]
  python pdf2epub.py all input.pdf -o out.epub --project book_work

  # backwards-compatible one-shot (ocr + build, no proofread):
  python pdf2epub.py input.pdf -o out.epub [--title T] [--author A] ...
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

DROP_TYPES = {"page_number", "header", "footer", "page_header", "page_footer",
              "aside_text", "discarded"}
FLOAT_TYPES = {"image", "chart", "table", "equation", "interline_equation",
               "seal", "figure"}
TITLE_TYPES = {"paragraph_title", "title", "doc_title"}
TERMINAL = set("。！？!?…”’」』）)》.;；:：")
CJK = r"\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef“”‘’"

COMMANDS = ("ocr", "proofread", "build", "all")
BOOK_JSON = "book.json"
PAGE_MD = "page.md"
PAGE_OCR_MD = "page.ocr.md"
PAGE_PNG = "page.png"
PAGE_META = "meta.json"
PROOFREAD_JSON = "proofread.json"

PROOFREAD_PROMPT = """You are proofreading OCR text from one scanned book page.

Read the page image once with the file-read tool (do not crop, do not call the
shell, do not run Python/PIL, do not write files, do not fetch the web):
{page_png}

The current Markdown is at {page_md} and is reproduced here:
---BEGIN PAGE.MD---
{page_text}
---END PAGE.MD---

Rules:
- Fix only confident OCR errors on characters that are clearly visible in the
  scan: a wrong glyph, or broken punctuation such as a single "—" that should
  be "——".
- If a glyph is smudged, faint, or not actually visible, do NOT guess. Leave
  the OCR text as-is. Do not insert characters to "fill a hole" (for example
  do not invent 等 vs 当).
- Do not rewrite, polish, paraphrase, or modernise the author's wording.
  Do not change 大多→大多数, 期货商→交易商, or similar.
- Keep the Markdown structure exactly (headings, paragraphs, :::figure blocks,
  blank lines, HTML footnote paragraphs).
- Do not add commentary, analysis, or a preamble in the answer.
- One image read is enough; do not over-inspect.

Reply with exactly one of:
1. The token NO_CHANGES (and nothing else) if there are no confident
   substitutions of visible glyphs / punctuation.
2. The complete page Markdown wrapped in the markers below (no code fences,
   no narration before or after the markers):

---BEGIN PAGE.MD---
...full page.md...
---END PAGE.MD---
"""

PAGE_BEGIN = "---BEGIN PAGE.MD---"
PAGE_END = "---END PAGE.MD---"
PAGE_CLI_CONFIG = {
    "permissions": {
        "allow": ["Read(*)"],
        "deny": ["Shell(*)", "Write(*)", "WebFetch(*)", "Mcp(*:*)"],
    }
}


def log(msg):
    print(f"[pdf2epub {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------- step 1
def inspect_and_render(pdf: Path, project: Path, dpi: int) -> tuple[list[dict], dict]:
    import pymupdf
    pages_root = project / "pages"
    pages_root.mkdir(parents=True, exist_ok=True)
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
        page_dir = pages_root / f"{p.number + 1:04d}"
        page_dir.mkdir(parents=True, exist_ok=True)
        png = page_dir / PAGE_PNG
        pix = p.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY if len(imgs) else None)
        pix.save(png)
        info.append({
            "page": p.number + 1, "width_pt": p.rect.width, "height_pt": p.rect.height,
            "images": len(imgs), "image_coverage": round(img_area / abs(p.rect), 3),
            "text_chars": len(p.get_text().strip()), "invisible_text_spans": invisible,
            "png": str(png), "png_size": [pix.width, pix.height],
            "dir": str(page_dir),
        })
    meta = doc.metadata or {}
    doc.close()
    return info, meta


# --------------------------------------------------------------------- step 2
def run_mineru(pdf: Path, tier: str) -> dict:
    from mineru.parser import parse  # local SDK path; never uses the remote API
    res = parse(str(pdf), tier=tier, ocr_mode="ocr", page_range="all")
    return json.loads(res.to_json())


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


def infer_heading_levels(titles: list[dict]) -> None:
    """Assign level 1/2 from glyph height (bbox height in pt); biggest gap splits."""
    hs = sorted({round(t["_h"], 1) for t in titles})
    for t in titles:
        t["level"] = 2
    if len(hs) < 2:
        if titles:
            for t in titles:
                t["level"] = 1
        return
    best, cut = 1.0, None
    for a, b in zip(hs, hs[1:]):
        if b / a > best:
            best, cut = b / a, (a + b) / 2
    if best >= 1.12:
        for t in titles:
            t["level"] = 1 if t["_h"] > cut else 2
    else:  # all same size: treat them as chapters
        for t in titles:
            t["level"] = 1


# --------------------------------------------------------------------- extract (no cross-page merge)
def extract_pages(mj: dict, page_info: list[dict], punct: bool,
                  pad: float = 0.008) -> list[dict]:
    """Per-page items (headers/footers dropped, figures cropped). No merging."""
    from PIL import Image
    pages: list[dict] = []
    titles: list[dict] = []
    fig_no = 0
    for p in mj["pages"]:
        pidx = p["page_idx"]
        pinfo = page_info[pidx]
        page_dir = Path(pinfo["dir"])
        items: list[dict] = []
        continues_prev = False
        first_text_on_page = True
        figures_meta: list[dict] = []
        with Image.open(pinfo["png"]) as page_img:
            W, H = page_img.size
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
                    box = (max(0, int((bx0 - pad) * W)), max(0, int((by0 - pad) * H)),
                           min(W, int((bx1 + pad) * W)), min(H, int((by1 + pad) * H)))
                    name = f"fig_p{pidx + 1:04d}_{fig_no:03d}.png"
                    crop = page_img.crop(box)
                    crop.save(page_dir / name, optimize=True)
                    cap = " ".join(normalize_punct(c) if punct else c for c in caption if c)
                    fig = {"kind": "figure", "type": t, "src": name,
                           "path": str(page_dir / name),
                           "caption": cap, "footnote": " ".join(foot),
                           "table_html": table_html, "page": pidx + 1, "crop_px": list(box)}
                    items.append(fig)
                    figures_meta.append({
                        "src": name, "type": t, "footnote": fig["footnote"],
                        "table_html": table_html, "crop_px": list(box),
                    })
                    continue
                text = block_text(b.get("content"))
                text = re.sub(r"\s*\n\s*", "", text) if re.search(f"[{CJK}]", text) else text
                if punct:
                    text = normalize_punct(text)
                if not text:
                    continue
                if t in TITLE_TYPES:
                    it = {"kind": "heading", "text": text, "page": pidx + 1,
                          "_h": (y1 - y0) * pinfo["height_pt"]}
                    titles.append(it)
                    items.append(it)
                    first_text_on_page = False
                    continue
                cont = bool(b.get("continues_prev"))
                if first_text_on_page:
                    continues_prev = cont
                first_text_on_page = False
                items.append({"kind": "para", "text": text, "page": pidx + 1,
                              "pages": [pidx + 1],
                              "cls": "footnote" if "footnote" in t else None,
                              "continues_prev": cont})
        pages.append({
            "page": pidx + 1,
            "dir": str(page_dir),
            "png": pinfo["png"],
            "width_pt": pinfo["width_pt"],
            "height_pt": pinfo["height_pt"],
            "png_size": pinfo["png_size"],
            "continues_prev": continues_prev,
            "items": items,
            "figures": figures_meta,
            "inspect": {k: pinfo[k] for k in ("images", "image_coverage", "text_chars",
                                              "invisible_text_spans") if k in pinfo},
        })
    infer_heading_levels(titles)
    return pages


def merge_page_items(pages: list[dict]) -> list[dict]:
    """Join paragraphs split across pages; same rules as the original one-shot tool."""
    items: list[dict] = []
    for pidx, page in enumerate(pages):
        pending = [dict(it) for it in page["items"]]
        first_text = next((x for x in pending if x["kind"] in ("para", "heading")), None)
        if first_text and first_text["kind"] == "para" and pidx > 0:
            cont = bool(page.get("continues_prev") or first_text.get("continues_prev"))
            if not cont:
                prev = next((x for x in reversed(items) if x["kind"] in ("para", "heading")), None)
                if prev and prev["kind"] == "para" and prev["text"] and prev["text"][-1] not in TERMINAL:
                    cont = True
            if cont:
                prev = next((x for x in reversed(items) if x["kind"] == "para"), None)
                if prev is not None:
                    prev["text"] += first_text["text"]
                    prev.setdefault("pages", [prev.get("page")]).append(first_text.get("page", page["page"]))
                    pending = [x for x in pending if x is not first_text]
        for it in pending:
            it.pop("continues_prev", None)
        items.extend(pending)
    return items


# --------------------------------------------------------------------- project markdown
def items_to_markdown(items: list[dict]) -> str:
    parts = []
    for it in items:
        if it["kind"] == "heading":
            parts.append("#" * int(it.get("level") or 1) + " " + it["text"])
        elif it["kind"] == "para":
            if it.get("cls") == "footnote":
                parts.append(f'<p class="footnote">{it["text"]}</p>')
            else:
                parts.append(it["text"])
        elif it["kind"] == "figure":
            cap = it.get("caption") or ""
            if cap:
                parts.append(f":::figure {it['src']}\n{cap}\n:::")
            else:
                parts.append(f":::figure {it['src']}\n:::")
    return "\n\n".join(parts) + ("\n" if parts else "")


def _parse_text_chunk(chunk: str, page: int) -> dict | None:
    chunk = chunk.strip()
    if not chunk:
        return None
    lines = chunk.split("\n")
    hm = re.match(r"^(#{1,6})\s+(.*)$", lines[0])
    if hm and len(lines) == 1:
        return {"kind": "heading", "level": len(hm.group(1)), "text": hm.group(2).strip(), "page": page}
    fm = re.match(r"^!\[(.*?)\]\((.+?)\)\s*$", chunk, re.S)
    if fm:
        return {"kind": "figure", "src": Path(fm.group(2)).name, "caption": fm.group(1),
                "page": page, "footnote": "", "table_html": None, "type": "image"}
    fm2 = re.match(r'^<p class="footnote">(.*)</p>$', chunk, re.S)
    if fm2:
        return {"kind": "para", "text": fm2.group(1), "page": page, "pages": [page], "cls": "footnote"}
    return {"kind": "para", "text": chunk, "page": page, "pages": [page], "cls": None}


def markdown_to_items(md: str, page: int, meta: dict | None = None) -> list[dict]:
    """Parse per-page Markdown produced by items_to_markdown (plus simple hand edits)."""
    meta = meta or {}
    fig_extra = {f["src"]: f for f in meta.get("figures") or [] if f.get("src")}
    items: list[dict] = []
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    buf: list[str] = []

    def flush():
        text = "\n".join(buf).strip("\n")
        buf.clear()
        if not text.strip():
            return
        for chunk in re.split(r"\n\s*\n", text.strip()):
            it = _parse_text_chunk(chunk, page)
            if it:
                items.append(it)

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(":::figure "):
            flush()
            src = line[len(":::figure "):].strip()
            i += 1
            cap_lines: list[str] = []
            while i < len(lines) and lines[i].strip() != ":::":
                cap_lines.append(lines[i])
                i += 1
            if i < len(lines) and lines[i].strip() == ":::":
                i += 1
            extra = fig_extra.get(src, {})
            items.append({
                "kind": "figure", "src": src, "caption": "\n".join(cap_lines).strip(),
                "page": page, "type": extra.get("type", "image"),
                "footnote": extra.get("footnote") or "",
                "table_html": extra.get("table_html"),
                "crop_px": extra.get("crop_px"),
            })
            continue
        buf.append(line)
        i += 1
    flush()
    return items


def write_project(project: Path, pages: list[dict], book: dict) -> None:
    project.mkdir(parents=True, exist_ok=True)
    page_ids = []
    for pg in pages:
        pid = f"{pg['page']:04d}"
        page_ids.append(pid)
        page_dir = Path(pg["dir"])
        page_dir.mkdir(parents=True, exist_ok=True)
        md = items_to_markdown(pg["items"])
        (page_dir / PAGE_MD).write_text(md, encoding="utf-8")
        (page_dir / PAGE_OCR_MD).write_text(md, encoding="utf-8")
        meta = {
            "page": pg["page"],
            "width_pt": pg["width_pt"],
            "height_pt": pg["height_pt"],
            "png_size": pg["png_size"],
            "continues_prev": bool(pg.get("continues_prev")),
            "figures": pg.get("figures") or [],
            "inspect": pg.get("inspect") or {},
        }
        (page_dir / PAGE_META).write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
        # drop stale proofread marker on a fresh OCR
        pr = page_dir / PROOFREAD_JSON
        if pr.exists():
            pr.unlink()
    book = dict(book)
    book["version"] = 1
    book["pages"] = page_ids
    (project / BOOK_JSON).write_text(json.dumps(book, ensure_ascii=False, indent=1),
                                     encoding="utf-8")


def load_project_pages(project: Path) -> tuple[dict, list[dict]]:
    book_path = project / BOOK_JSON
    if not book_path.exists():
        raise FileNotFoundError(f"not a pdf2epub project (missing {book_path})")
    book = json.loads(book_path.read_text(encoding="utf-8"))
    pages = []
    for pid in book.get("pages") or []:
        page_dir = (project / "pages" / pid).resolve()
        meta_path = page_dir / PAGE_META
        md_path = page_dir / PAGE_MD
        if not md_path.exists():
            raise FileNotFoundError(f"missing {md_path}")
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        page_no = int(meta.get("page") or int(pid))
        md = md_path.read_text(encoding="utf-8")
        items = markdown_to_items(md, page_no, meta)
        for it in items:
            if it["kind"] == "figure":
                src = it["src"]
                path = page_dir / src
                it["path"] = str(path)
                if not it.get("caption"):
                    extra = next((f for f in meta.get("figures") or [] if f.get("src") == src), {})
                    if extra.get("caption"):
                        it["caption"] = extra["caption"]
        pages.append({
            "page": page_no,
            "id": pid,
            "dir": str(page_dir),
            "png": str(page_dir / PAGE_PNG),
            "continues_prev": bool(meta.get("continues_prev")),
            "items": items,
            "meta": meta,
            "md": md,
        })
    return book, pages


# --------------------------------------------------------------------- step 5 (epub)
CSS = """
body { font-family: serif; line-height: 1.7; margin: 0 4%; }
h1 { font-size: 1.6em; margin: 1.2em 0 0.8em; text-align: left; }
h2 { font-size: 1.25em; margin: 1.1em 0 0.5em; }
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
        if it["kind"] == "heading" and it["level"] == 1:
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
            lvl = it["level"]
            out.append(f'<h{lvl} id="{hid}">{html.escape(it["text"])}</h{lvl}>')
            if lvl == 2:
                sec_ids.append((hid, it["text"]))
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


def build_epub(chapters, out: Path, title: str, author: str, lang: str, images: dict[str, Path],
               cover: Path | None):
    from ebooklib import epub
    book = epub.EpubBook()
    book.set_identifier(f"urn:uuid:{uuid.uuid4()}")
    book.set_title(title)
    book.set_language(lang)
    if author:
        book.add_author(author)
    book.add_metadata("DC", "description", "Generated by pdf2epub.py (MinerU OCR)")
    if cover and cover.exists():
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
        if secs:
            toc.append((epub.Section(ch["title"], href=c.file_name),
                        [epub.Link(f"{c.file_name}#{hid}", t, f"c{i}_{hid}") for hid, t in secs]))
        else:
            toc.append(epub.Link(c.file_name, ch["title"], f"c{i}"))
    for name in sorted(used):
        path = images.get(name)
        if path is None or not Path(path).exists():
            raise FileNotFoundError(f"figure {name} missing (looked at {path})")
        book.add_item(epub.EpubImage(uid=name, file_name=f"images/{name}", media_type="image/png",
                                     content=Path(path).read_bytes()))
    book.toc = toc
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    out.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out), book)


def to_markdown(items) -> str:
    md = []
    for it in items:
        if it["kind"] == "heading":
            md.append("#" * it["level"] + " " + it["text"])
        elif it["kind"] == "para":
            md.append(it["text"])
        else:
            md.append(f"![{it['caption']}](images/{it['src']})\n\n*{it['caption']}*")
    return "\n\n".join(md) + "\n"


def draw_overlays(mj, page_info, out_dir: Path):
    """Debug artefact: page renders with layout boxes (red=dropped, blue=text,
    green=figure, purple=caption, orange=heading)."""
    from PIL import Image, ImageDraw
    out_dir.mkdir(parents=True, exist_ok=True)
    col = lambda t: ("red" if t in DROP_TYPES else "green" if t in FLOAT_TYPES
                     else "orange" if t in TITLE_TYPES else "blue")
    for p in mj["pages"]:
        with Image.open(page_info[p["page_idx"]]["png"]) as src:
            im = src.convert("RGB")
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


def run_epubcheck(out: Path) -> int:
    cmd = None
    if shutil.which("epubcheck"):
        jar = Path("/usr/share/java/epubcheck.jar")
        cmd = ["java", "-jar", str(jar), str(out)] if jar.exists() else ["epubcheck", str(out)]
    if cmd:
        r = subprocess.run(cmd, capture_output=True, text=True)
        log("epubcheck:\n" + (r.stdout + r.stderr).strip())
        return r.returncode
    log("epubcheck not installed; skipped")
    return 0


# --------------------------------------------------------------------- proofread (Cursor CLI)
def now_local_iso() -> str:
    """Timezone-aware local timestamp (offset in the string; not UTC)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def find_agent_bin(explicit: str | None) -> str:
    if explicit:
        p = Path(explicit)
        if p.exists():
            return str(p.resolve())
        w = shutil.which(explicit)
        if w:
            return w
        return explicit
    env = os.environ.get("PDF2EPUB_AGENT_BIN")
    if env:
        return env
    w = shutil.which("agent")
    if w:
        return w
    home = Path.home() / ".local/bin/agent"
    if home.is_file():
        return str(home)
    raise FileNotFoundError(
        "Cursor CLI 'agent' not found. Install with: curl https://cursor.com/install -fsS | bash "
        "(see https://cursor.com/docs/cli/installation). Authenticate with `agent login` or "
        "CURSOR_API_KEY / --api-key (https://cursor.com/docs/cli/reference/authentication)."
    )


def _agent_argv(agent_bin: str) -> list[str]:
    path = Path(agent_bin)
    if path.suffix == ".py":
        return [sys.executable, str(path)]
    return [str(path)]


def ensure_page_cli_config(page_dir: Path) -> Path:
    """Project-level Cursor CLI permissions: deny shell/write; allow reads.

    https://cursor.com/docs/cli/reference/permissions
    Workspace is the page directory, so this file is <page>/.cursor/cli.json.
    """
    cfg_dir = page_dir / ".cursor"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "cli.json"
    path.write_text(json.dumps(PAGE_CLI_CONFIG, indent=2) + "\n", encoding="utf-8")
    return path


def _assistant_text(event: dict) -> str:
    msg = event.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") in (None, "text"):
            parts.append(c.get("text") or "")
        elif isinstance(c, str):
            parts.append(c)
    return "".join(parts)


def parse_stream_json(stdout: str) -> dict:
    """Parse `agent --output-format stream-json` NDJSON.

    The concatenated `result` field joins *all* assistant text (narration
    before/between tool calls included). The usable answer is the last
    assistant message after the last tool call. Model id comes from the
    system init event (what Auto actually picked).
    Docs: https://cursor.com/docs/cli/reference/output-format
    """
    model = None
    duration_ms = None
    usage = None
    is_error = False
    error_text = None
    concatenated = None
    messages: list[str] = []
    saw_result = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = ev.get("type")
        if typ == "system" and ev.get("subtype") == "init":
            model = ev.get("model") or model
        elif typ == "assistant":
            # With --stream-partial-output, timestamp_ms marks deltas/duplicates.
            # We do not pass that flag; skip those events if they appear.
            if "timestamp_ms" in ev:
                continue
            text = _assistant_text(ev)
            if text:
                messages.append(text)
        elif typ == "result":
            saw_result = True
            duration_ms = ev.get("duration_ms")
            usage = ev.get("usage")
            concatenated = ev.get("result")
            is_error = bool(ev.get("is_error"))
            if is_error:
                error_text = ev.get("result")
            model = ev.get("model") or model
            if ev.get("subtype") and ev.get("subtype") != "success":
                is_error = True
                error_text = error_text or ev.get("result")
    if is_error:
        raise RuntimeError(f"Cursor CLI reported an error: {error_text!r}")
    if not saw_result and not messages:
        # maybe a single JSON object (older json format)
        try:
            data = json.loads(stdout.strip())
        except json.JSONDecodeError as e:
            raise ValueError("Cursor CLI produced no stream-json events") from e
        if data.get("is_error"):
            raise RuntimeError(f"Cursor CLI reported an error: {data.get('result')!r}")
        return {
            "text": unwrap_agent_markdown(str(data.get("result") or "")),
            "model": data.get("model") or model,
            "duration_ms": data.get("duration_ms"),
            "usage": data.get("usage"),
            "concatenated": data.get("result"),
        }
    text = messages[-1] if messages else (concatenated or "")
    return {
        "text": unwrap_agent_markdown(str(text)),
        "model": model,
        "duration_ms": duration_ms,
        "usage": usage,
        "concatenated": concatenated,
    }


def parse_agent_json(stdout: str) -> dict:
    """Back-compat helper used by tests; prefers stream-json, then a JSON object."""
    return parse_stream_json(stdout)


def unwrap_agent_markdown(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:markdown|md)?\s*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
    return t.strip()


def looks_like_no_changes(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if t == "NO_CHANGES":
        return True
    if t.endswith("NO_CHANGES"):
        return True
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    return bool(lines) and lines[-1] == "NO_CHANGES"


def first_content_line(md: str) -> str:
    for ln in md.splitlines():
        if ln.strip():
            return ln
    return ""


def strip_trailing_commentary(text: str, original: str) -> str:
    blocks = re.split(r"\n\n+", text.strip("\n"))
    orig = original.replace("\r\n", "\n")
    while len(blocks) > 1:
        last = blocks[-1].strip()
        if last == "NO_CHANGES" or last.endswith("NO_CHANGES"):
            blocks.pop()
            continue
        if last in orig:
            break
        if not re.search(r"[\u3400-\u9fff]", last) and re.search(r"[A-Za-z]{4,}", last):
            blocks.pop()
            continue
        break
    out = "\n\n".join(blocks)
    return out + "\n" if text.endswith("\n") else out


def starts_like_original(original: str, corrected: str) -> bool:
    """True if `corrected` is page text, not a narrated preamble."""
    first = first_content_line(original)
    got = first_content_line(corrected)
    if not first:
        return True
    if not got:
        return False
    if first.lstrip().startswith("#"):
        return got.lstrip().startswith("#") and (
            got == first or got.startswith(first) or first.startswith(got)
        )
    # English narration ("Comparing the OCR…") vs CJK/body OCR
    if re.match(r"^[A-Za-z]{4,}\b", got) and not re.match(r"^[A-Za-z]{4,}\b", first):
        return False
    n = 0
    for a, b in zip(first, got):
        if a != b:
            break
        n += 1
    return n >= min(2, len(first))


def extract_page_markdown(raw: str, original: str) -> str | None:
    """Pull page Markdown out of a (possibly narrated) final assistant message.

    Returns 'NO_CHANGES', the extracted markdown, or None if it should be rejected.
    """
    t = unwrap_agent_markdown(raw)
    if PAGE_BEGIN in t and PAGE_END in t:
        t = t.split(PAGE_BEGIN, 1)[1].split(PAGE_END, 1)[0].strip("\n")
        if t and not t.endswith("\n"):
            t += "\n"
    if looks_like_no_changes(t):
        return "NO_CHANGES"
    first = first_content_line(original)
    if first:
        idx = t.find(first)
        if idx > 0:
            t = t[idx:]
        elif idx < 0 and not starts_like_original(original, t):
            return None
    t = strip_trailing_commentary(t, original)
    if looks_like_no_changes(t):
        return "NO_CHANGES"
    if not t.strip():
        return None
    if not starts_like_original(original, t):
        return None
    if not t.endswith("\n"):
        t += "\n"
    return t


def _is_punct_text(s: str) -> bool:
    if not s:
        return False
    for ch in s:
        if ch.isspace():
            continue
        if unicodedata.category(ch).startswith("P"):
            continue
        if ch in "—–―－-":
            continue
        return False
    return any(not ch.isspace() for ch in s)


def classify_and_apply(original: str, proposed: str) -> tuple[str, list[dict]]:
    """Apply punctuation fixes; hold insertions and wording changes as suggestions.

    Insertions (including smudge-fill guesses such as a missing 等) and any
    non-punctuation substitution (大多→大多数, 期货商→交易商) are recorded but
    not written to page.md.
    """
    sm = difflib.SequenceMatcher(a=original, b=proposed, autojunk=False)
    out: list[str] = []
    suggestions: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        a, b = original[i1:i2], proposed[j1:j2]
        if tag == "equal":
            out.append(a)
        elif tag == "replace":
            if _is_punct_text(a) and _is_punct_text(b):
                out.append(b)
            else:
                out.append(a)
                suggestions.append({
                    "type": "replace", "from": a, "to": b,
                    "reason": "rewrite_or_uncertain_glyph", "at": i1,
                })
        elif tag == "insert":
            if _is_punct_text(b):
                out.append(b)
            else:
                suggestions.append({
                    "type": "insert", "text": b,
                    "reason": "insertion_or_smudge", "at": i1,
                })
        elif tag == "delete":
            if _is_punct_text(a):
                pass
            else:
                out.append(a)
                suggestions.append({
                    "type": "delete", "text": a,
                    "reason": "content_deletion", "at": i1,
                })
    applied = "".join(out)
    return applied, suggestions


def run_cursor_agent(agent_bin: str, prompt: str, *, model: str | None, workspace: Path,
                     timeout: float, api_key: str | None = None) -> dict:
    """Call `agent --print --output-format stream-json` and return parsed fields.

    Interface from https://cursor.com/docs/cli/headless and
    https://cursor.com/docs/cli/reference/parameters :
      agent --print --output-format stream-json --sandbox enabled --trust
            --workspace ABS_DIR [--model MODEL] PROMPT
    Auth: CURSOR_API_KEY in the child environment only (never --api-key on argv).
    Images: include file paths in the prompt.
    """
    ws = workspace.resolve()
    ensure_page_cli_config(ws)
    cmd = _agent_argv(agent_bin) + [
        "--print",
        "--output-format", "stream-json",
        "--sandbox", "enabled",
        "--trust",
        "--workspace", str(ws),
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)
    env = os.environ.copy()
    key = api_key or env.get("CURSOR_API_KEY")
    if api_key:
        env["CURSOR_API_KEY"] = api_key
    elif key:
        env["CURSOR_API_KEY"] = key
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       cwd=str(ws), env=env)
    wall_ms = int((time.perf_counter() - t0) * 1000)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
        raise RuntimeError(f"Cursor CLI failed (exit {r.returncode}): {err[:2000]}")
    parsed = parse_stream_json(r.stdout)
    parsed["wall_ms"] = wall_ms
    parsed["requested_model"] = model
    return parsed


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def unified_diff(a: str, b: str, name: str) -> str:
    return "".join(difflib.unified_diff(
        a.splitlines(keepends=True), b.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}"))


def _correction_looks_safe(original: str, corrected: str, meta: dict) -> str | None:
    """Return an error message if the model output should be rejected, else None."""
    if not corrected.strip():
        return "empty correction"
    if not starts_like_original(original, corrected):
        return "correction does not start like the original page"
    if len(original) > 80 and len(corrected) < max(20, int(0.4 * len(original))):
        return "correction much shorter than original OCR"
    for fig in meta.get("figures") or []:
        src = fig.get("src")
        if src and src not in corrected:
            return f"correction dropped figure {src}"
    try:
        markdown_to_items(corrected, int(meta.get("page") or 1), meta)
    except Exception as e:
        return f"correction is not parseable markdown: {e}"
    return None


def _usage_summary(usage) -> str:
    if not isinstance(usage, dict) or not usage:
        return "-"
    # CLI emits camelCase inputTokens/outputTokens; some builds use snake_case.
    inp = usage.get("inputTokens", usage.get("input_tokens"))
    out = usage.get("outputTokens", usage.get("output_tokens"))
    bits = []
    if inp is not None:
        bits.append(f"in={inp}")
    if out is not None:
        bits.append(f"out={out}")
    cache_r = usage.get("cacheReadTokens", usage.get("cache_read_tokens"))
    if cache_r:
        bits.append(f"cache_read={cache_r}")
    return " ".join(bits) if bits else json.dumps(usage, ensure_ascii=False)


def proofread_one_page(page: dict, *, agent_bin: str, model: str | None, timeout: float,
                       dry_run: bool, force: bool, api_key: str | None,
                       from_ocr: bool = False) -> dict:
    page_dir = Path(page["dir"]).resolve()
    md_path = page_dir / PAGE_MD
    png_path = page_dir / PAGE_PNG
    meta = page.get("meta") or {}
    ocr_path = page_dir / PAGE_OCR_MD
    ocr_text = ocr_path.read_text(encoding="utf-8") if ocr_path.exists() else None
    on_disk = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    original = ocr_text if (from_ocr and ocr_text is not None) else on_disk
    status_path = page_dir / PROOFREAD_JSON
    pid = page.get("id") or f"{page['page']:04d}"
    stats = {"model": None, "requested_model": model, "duration_ms": None,
             "wall_ms": None, "usage": None, "time": now_local_iso()}

    if status_path.exists() and not force:
        try:
            st = json.loads(status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            st = {}
        if st.get("status") == "done":
            return {"page": pid, "status": "skipped", "reason": "already proofread", **stats}

    if not png_path.exists():
        return {"page": pid, "status": "failed", "error": f"missing {png_path}", **stats}

    if from_ocr and ocr_text is None:
        return {"page": pid, "status": "failed", "error": f"missing {ocr_path}", **stats}
    if from_ocr and ocr_text is not None and not dry_run:
        md_path.write_text(ocr_text, encoding="utf-8")

    prompt = PROOFREAD_PROMPT.format(
        page_png=str(png_path.resolve()),
        page_md=str(md_path.resolve()),
        page_text=original,
    )
    try:
        run = run_cursor_agent(agent_bin, prompt, model=model, workspace=page_dir,
                               timeout=timeout, api_key=api_key)
    except subprocess.TimeoutExpired as e:
        if not dry_run:
            md_path.write_text(on_disk if not from_ocr else original, encoding="utf-8")
            if ocr_text is not None:
                ocr_path.write_text(ocr_text, encoding="utf-8")
        wall = int((e.timeout or timeout) * 1000)
        return {"page": pid, "status": "failed", "error": f"timeout after {timeout}s",
                **{**stats, "wall_ms": wall}}
    except Exception as e:
        if not dry_run:
            md_path.write_text(on_disk if not from_ocr else original, encoding="utf-8")
            if ocr_text is not None:
                ocr_path.write_text(ocr_text, encoding="utf-8")
        return {"page": pid, "status": "failed", "error": str(e), **stats}

    stats.update({
        "model": run.get("model") or model,
        "duration_ms": run.get("duration_ms"),
        "wall_ms": run.get("wall_ms"),
        "usage": run.get("usage"),
    })

    # Discard any in-place writes the agent may have made; we own the files.
    md_path.write_text(on_disk, encoding="utf-8")
    if ocr_text is not None:
        ocr_path.write_text(ocr_text, encoding="utf-8")

    extracted = extract_page_markdown(run.get("text") or "", original)
    if extracted is None:
        return {"page": pid, "status": "failed",
                "error": "could not extract page Markdown from agent output (narration?)",
                **stats}

    if extracted == "NO_CHANGES" or extracted.replace("\r\n", "\n") == original.replace("\r\n", "\n"):
        rec = {"status": "done", "changed": False, **stats, "sha256": sha256_text(original)}
        if not dry_run:
            status_path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"page": pid, "status": "unchanged", "diff": "", "dry_run": dry_run, **stats}

    err = _correction_looks_safe(original, extracted, meta)
    if err:
        return {"page": pid, "status": "failed", "error": err, **stats}

    applied, suggestions = classify_and_apply(original, extracted)
    if not applied.endswith("\n") and original.endswith("\n"):
        applied += "\n"
    applied_changed = applied.replace("\r\n", "\n") != original.replace("\r\n", "\n")
    diff = unified_diff(original, applied, f"pages/{pid}/{PAGE_MD}") if applied_changed else ""
    sug_diff = unified_diff(original, extracted, f"pages/{pid}/{PAGE_MD}") if suggestions else ""

    if dry_run:
        status = "would_change" if applied_changed else ("would_suggest" if suggestions else "unchanged")
        return {"page": pid, "status": status, "diff": diff, "suggestions": suggestions,
                "suggestion_diff": sug_diff, "dry_run": True, **stats}

    to_write = applied if applied_changed else original
    md_path.write_text(to_write, encoding="utf-8")
    rec = {
        "status": "done",
        "changed": applied_changed,
        "suggestions": suggestions,
        "sha256": sha256_text(to_write),
        "sha256_before": sha256_text(original),
        **stats,
    }
    status_path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    if applied_changed:
        st = "changed"
    elif suggestions:
        st = "suggestions"
    else:
        st = "unchanged"
    return {"page": pid, "status": st, "diff": diff, "suggestions": suggestions,
            "suggestion_diff": sug_diff, "dry_run": False, **stats}


def _load_existing_results(project: Path) -> list[dict]:
    path = project / "proofread" / "results.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def merge_proofread_results(old: list[dict], new: list[dict]) -> list[dict]:
    by_page: dict[str, dict] = {}
    for r in old:
        pid = str(r.get("page", ""))
        if pid:
            by_page[pid] = r
    for r in new:
        pid = str(r.get("page", ""))
        if not pid:
            continue
        if r.get("status") == "skipped" and pid in by_page:
            prev = dict(by_page[pid])
            prev["skipped_this_run"] = True
            by_page[pid] = prev
        else:
            by_page[pid] = r
    return sorted(by_page.values(), key=lambda r: str(r.get("page", "")))


def write_proofread_report(project: Path, results: list[dict], *, model: str | None,
                           dry_run: bool, merge_existing: bool = True) -> Path:
    out_dir = project / "proofread"
    diffs_dir = out_dir / "diffs"
    out_dir.mkdir(parents=True, exist_ok=True)
    diffs_dir.mkdir(parents=True, exist_ok=True)
    if merge_existing:
        results = merge_proofread_results(_load_existing_results(project), results)
    this_pages = {str(r.get("page")) for r in results}
    counts: dict[str, int] = {}
    for r in results:
        st = r.get("status") or "unknown"
        counts[st] = counts.get(st, 0) + 1
        pid = str(r.get("page"))
        diff = r.get("diff") or ""
        dpath = diffs_dir / f"{pid}.diff"
        if diff:
            dpath.write_text(diff, encoding="utf-8")
        elif dpath.exists() and pid in this_pages and r.get("status") != "skipped":
            # this run replaced a previous diff with no textual change
            if r.get("status") in {"unchanged", "suggestions", "failed"}:
                if r.get("status") != "suggestions":
                    dpath.unlink()
        sug = r.get("suggestion_diff") or ""
        spath = diffs_dir / f"{pid}.suggestions.diff"
        if sug:
            spath.write_text(sug, encoding="utf-8")
        elif spath.exists() and r.get("status") != "skipped":
            spath.unlink()
    lines = [
        f"# Proofread report",
        f"",
        f"- generated (local): {now_local_iso()}",
        f"- requested model: {model or '(Cursor CLI default / Auto)'}",
        f"- dry_run: {dry_run}",
        f"- pages: {len(results)}",
        f"- changed: {counts.get('changed', 0)}",
        f"- suggestions (not auto-applied): {counts.get('suggestions', 0)}",
        f"- would_change: {counts.get('would_change', 0)}",
        f"- unchanged: {counts.get('unchanged', 0)}",
        f"- skipped (already proofread): {counts.get('skipped', 0)}",
        f"- failed: {counts.get('failed', 0)}",
        f"",
    ]
    for r in results:
        lines.append(f"## Page {r['page']} — {r['status']}")
        meta_bits = []
        if r.get("model"):
            meta_bits.append(f"model={r['model']}")
        if r.get("wall_ms") is not None:
            meta_bits.append(f"wall={r['wall_ms']}ms")
        if r.get("duration_ms") is not None:
            meta_bits.append(f"duration_ms={r['duration_ms']}")
        if r.get("usage"):
            meta_bits.append(f"usage {_usage_summary(r['usage'])}")
        if r.get("time"):
            meta_bits.append(f"at {r['time']}")
        if meta_bits:
            lines.append("\n" + ", ".join(meta_bits) + "\n")
        if r.get("error"):
            lines.append(f"\nError: {r['error']}\n")
        elif r.get("reason"):
            lines.append(f"\n{r['reason']}\n")
        if r.get("diff"):
            lines.append("\nApplied diff:\n\n```diff")
            lines.append(r["diff"].rstrip("\n"))
            lines.append("```\n")
        if r.get("suggestions"):
            lines.append("\nSuggestions (not auto-applied; needs human review):\n")
            for s in r["suggestions"]:
                if s.get("type") == "insert":
                    lines.append(f"- insert {s.get('text')!r} ({s.get('reason')})")
                elif s.get("type") == "replace":
                    lines.append(f"- replace {s.get('from')!r} → {s.get('to')!r} ({s.get('reason')})")
                elif s.get("type") == "delete":
                    lines.append(f"- delete {s.get('text')!r} ({s.get('reason')})")
                else:
                    lines.append(f"- {s}")
            lines.append("")
            if r.get("suggestion_diff"):
                lines.append("```diff")
                lines.append(r["suggestion_diff"].rstrip("\n"))
                lines.append("```\n")
        if not r.get("diff") and not r.get("suggestions") and not r.get("error") and not r.get("reason"):
            lines.append("\nNo textual changes.\n")
    report = out_dir / "report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    return report


# --------------------------------------------------------------------- stages
def resolve_project(args, pdf: Path | None = None) -> Path:
    if getattr(args, "project", None):
        p = Path(args.project)
    elif getattr(args, "output", None):
        p = Path(args.output).with_name(Path(args.output).stem + "_work")
    elif pdf is not None:
        p = Path(pdf).with_name(Path(pdf).stem + "_work")
    else:
        raise SystemExit("specify --project (editable project directory)")
    return p.expanduser().resolve()


def cmd_ocr(args) -> Path:
    pdf = Path(args.pdf)
    if not pdf.exists():
        raise SystemExit(f"PDF not found: {pdf}")
    project = resolve_project(args, pdf)
    project.mkdir(parents=True, exist_ok=True)
    timings = {}

    t = time.time()
    page_info, meta = inspect_and_render(pdf, project, args.dpi)
    timings["render"] = time.time() - t
    for pi in page_info:
        log(f"page {pi['page']}: {pi['width_pt']:.0f}x{pi['height_pt']:.0f}pt, images={pi['images']} "
            f"(coverage {pi['image_coverage']}), text layer chars={pi['text_chars']} "
            f"(invisible spans={pi['invisible_text_spans']}) -> {pi['png_size']}")

    jpath = project / f"mineru_{args.tier}.json"
    t = time.time()
    if args.reuse_json and jpath.exists():
        mj = json.loads(jpath.read_text(encoding="utf-8"))
        log(f"reused {jpath}")
    else:
        log(f"running MinerU tier={args.tier} (ocr_mode=ocr, local) ...")
        mj = run_mineru(pdf, args.tier)
        jpath.write_text(json.dumps(mj, ensure_ascii=False, indent=1), encoding="utf-8")
    timings["ocr_layout"] = time.time() - t

    t = time.time()
    pages = extract_pages(mj, page_info, punct=not args.no_punct_normalize)
    try:
        draw_overlays(mj, page_info, project / "layout")
    except Exception as e:
        log(f"layout overlays skipped: {e}")
    items_preview = merge_page_items(pages)
    title = args.title or (meta.get("title") or "").strip() or next(
        (i["text"] for i in items_preview if i["kind"] == "heading" and i.get("level") == 1),
        pdf.stem)
    cover = None
    if args.cover:
        src = Path(args.cover)
        if src.exists():
            dest = project / ("cover" + src.suffix)
            shutil.copy2(src, dest)
            cover = str(dest)
    book = {
        "title": title,
        "author": args.author or "",
        "lang": args.lang,
        "source_pdf": str(pdf.resolve()),
        "dpi": args.dpi,
        "tier": args.tier,
        "punct_normalize": not args.no_punct_normalize,
        "cover": cover,
    }
    write_project(project, pages, book)
    timings["write_project"] = time.time() - t
    nfig = sum(1 for pg in pages for i in pg["items"] if i["kind"] == "figure")
    log(f"wrote project {project}: {len(pages)} pages, {nfig} figures, "
        f"{sum(len(i['text']) for i in items_preview if i['kind'] == 'para')} body chars (after merge preview)")
    log("timings: " + ", ".join(f"{k}={v:.1f}s" for k, v in timings.items()))
    return project


def cmd_proofread(args) -> Path:
    project = resolve_project(args)
    book, pages = load_project_pages(project)
    agent_bin = find_agent_bin(getattr(args, "agent_bin", None))
    model = getattr(args, "model", None) or None
    timeout = float(getattr(args, "timeout", 300) or 300)
    jobs = max(1, int(getattr(args, "jobs", 1) or 1))
    dry_run = bool(getattr(args, "dry_run", False))
    force = bool(getattr(args, "force", False))
    from_ocr = bool(getattr(args, "from_ocr", False))
    api_key = getattr(args, "api_key", None)
    only = set(args.only_page) if getattr(args, "only_page", None) else None
    if only:
        pages = [p for p in pages if p["page"] in only or p["id"] in {f"{n:04d}" for n in only}]
        if not pages:
            raise SystemExit(f"no pages matched --only-page {sorted(only)}")
    log(f"proofread {len(pages)} page(s) via {agent_bin} "
        f"(model={model or 'default'}, jobs={jobs}, timeout={timeout}s, "
        f"dry_run={dry_run}, from_ocr={from_ocr})")
    kw = dict(agent_bin=agent_bin, model=model, timeout=timeout, dry_run=dry_run,
              force=force, api_key=api_key, from_ocr=from_ocr)
    results: list[dict] = []
    if jobs == 1:
        for p in pages:
            log(f"proofreading page {p['id']} ...")
            r = proofread_one_page(p, **kw)
            log(f"page {r['page']}: {r['status']}"
                + (f" ({r['error']})" if r.get("error") else "")
                + f" model={r.get('model') or '-'} wall={r.get('wall_ms')}ms "
                  f"duration_ms={r.get('duration_ms')} usage={_usage_summary(r.get('usage'))}")
            results.append(r)
    else:
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = {ex.submit(proofread_one_page, p, **kw): p for p in pages}
            for fut in as_completed(futs):
                r = fut.result()
                with lock:
                    results.append(r)
                    log(f"page {r['page']}: {r['status']}"
                        + (f" ({r['error']})" if r.get("error") else "")
                        + f" model={r.get('model') or '-'} wall={r.get('wall_ms')}ms "
                          f"duration_ms={r.get('duration_ms')} "
                          f"usage={_usage_summary(r.get('usage'))}")
        results.sort(key=lambda r: r["page"])
    report = write_proofread_report(project, results, model=model, dry_run=dry_run)
    nfail = sum(1 for r in results if r["status"] == "failed")
    log(f"proofread report: {report} ({nfail} failed)")
    return report


def cmd_build(args) -> Path:
    project = resolve_project(args)
    book, pages = load_project_pages(project)
    items = merge_page_items(pages)
    title = args.title or book.get("title") or "Untitled"
    if getattr(args, "author", None) in (None, ""):
        author = book.get("author") or ""
    else:
        author = args.author
    lang = args.lang or book.get("lang") or "zh-CN"
    cover = Path(args.cover) if args.cover else (Path(book["cover"]) if book.get("cover") else None)
    if args.output:
        out = Path(args.output)
    elif book.get("source_pdf"):
        out = Path(book["source_pdf"]).with_suffix(".epub")
    else:
        out = project / "book.epub"
    images = {}
    for pg in pages:
        for it in pg["items"]:
            if it["kind"] == "figure":
                images[it["src"]] = Path(it["path"])
    chapters = split_chapters(items, title)
    t = time.time()
    build_epub(chapters, out, title, author, lang, images, cover)
    nfig = sum(1 for i in items if i["kind"] == "figure")
    log(f"wrote {out}: {len(chapters)} chapters, {nfig} figures, "
        f"{sum(len(i['text']) for i in items if i['kind'] == 'para')} body chars "
        f"({time.time() - t:.1f}s)")
    if args.epubcheck:
        rc = run_epubcheck(out)
        if rc:
            raise SystemExit(rc)
    return out


def cmd_all(args) -> Path:
    project = cmd_ocr(args)
    args.project = project
    if not getattr(args, "skip_proofread", False):
        cmd_proofread(args)
    if not args.output:
        args.output = Path(args.pdf).with_suffix(".epub")
    return cmd_build(args)


# --------------------------------------------------------------------- CLI
def _add_project_arg(p):
    p.add_argument("--project", "--workdir", dest="project", type=Path,
                   help="editable project directory (source of truth for build; "
                        "alias: --workdir)")


def _add_ocr_args(p):
    p.add_argument("pdf", type=Path)
    _add_project_arg(p)
    p.add_argument("-o", "--output", type=Path, help="EPUB path (used only to default --project)")
    p.add_argument("--tier", default="basic", choices=["flash", "basic", "standard", "advanced"],
                   help="MinerU tier: basic = pipeline OCR (fast on CPU); standard = +VLM (slow on CPU)")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--title")
    p.add_argument("--author", default="")
    p.add_argument("--lang", default="zh-CN")
    p.add_argument("--cover", type=Path, help="optional cover image (copied into the project)")
    p.add_argument("--reuse-json", action="store_true",
                   help="reuse cached MinerU JSON in the project directory")
    p.add_argument("--no-punct-normalize", action="store_true")


def _add_build_args(p, author_default=None):
    _add_project_arg(p)
    p.add_argument("-o", "--output", type=Path)
    p.add_argument("--title")
    if author_default is None:
        p.add_argument("--author", default=None,
                       help="override author stored in the project")
    else:
        p.add_argument("--author", default=author_default)
    p.add_argument("--lang", default=None)
    p.add_argument("--cover", type=Path)
    p.add_argument("--epubcheck", action="store_true", help="run epubcheck if available")


def _add_proofread_args(p):
    _add_project_arg(p)
    p.add_argument("--model", help="Cursor CLI --model (see `agent --list-models` / `agent models`)")
    p.add_argument("--jobs", type=int, default=1, help="concurrent Cursor CLI processes (default 1)")
    p.add_argument("--timeout", type=float, default=300, help="per-page Cursor CLI timeout in seconds (default 300)")
    p.add_argument("--dry-run", action="store_true",
                   help="run the model and write a report, but do not modify page.md")
    p.add_argument("--force", action="store_true", help="re-proofread pages already marked done")
    p.add_argument("--from-ocr", action="store_true", dest="from_ocr",
                   help="restore page.md from page.ocr.md before proofreading "
                        "(use with --force to redo a bad correction from the original OCR)")
    p.add_argument("--agent-bin", help="Cursor CLI binary (default: agent on PATH, or $PDF2EPUB_AGENT_BIN)")
    p.add_argument("--api-key", help="set CURSOR_API_KEY for the child process (never passed on the command line)")
    p.add_argument("--only-page", type=int, action="append",
                   help="restrict to this 1-based page number (repeatable); report is merged")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p_ocr = sub.add_parser("ocr", help="render + OCR into an editable project directory")
    _add_ocr_args(p_ocr)

    p_pr = sub.add_parser("proofread", help="proofread per-page OCR via Cursor CLI")
    _add_proofread_args(p_pr)

    p_build = sub.add_parser("build", help="build EPUB from the project directory")
    _add_build_args(p_build)

    p_all = sub.add_parser("all", help="ocr + proofread + build")
    _add_ocr_args(p_all)
    p_all.add_argument("--epubcheck", action="store_true")
    p_all.add_argument("--skip-proofread", action="store_true",
                       help="ocr + build only (same as the legacy one-shot invocation)")
    p_all.add_argument("--model", help="Cursor CLI --model")
    p_all.add_argument("--jobs", type=int, default=1)
    p_all.add_argument("--timeout", type=float, default=300)
    p_all.add_argument("--dry-run", action="store_true")
    p_all.add_argument("--force", action="store_true")
    p_all.add_argument("--from-ocr", action="store_true", dest="from_ocr")
    p_all.add_argument("--agent-bin")
    p_all.add_argument("--api-key")
    p_all.add_argument("--only-page", type=int, action="append")
    return ap


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = build_parser()
    if argv and argv[0] not in COMMANDS and argv[0] not in ("-h", "--help"):
        # Legacy one-shot: pdf2epub.py input.pdf -o out.epub ...
        argv = ["all", "--skip-proofread", *argv]
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 2
    if args.cmd == "ocr":
        cmd_ocr(args)
    elif args.cmd == "proofread":
        cmd_proofread(args)
    elif args.cmd == "build":
        cmd_build(args)
    elif args.cmd == "all":
        cmd_all(args)
    else:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":  # guard required: MinerU uses spawn-based worker processes
    sys.exit(main())
