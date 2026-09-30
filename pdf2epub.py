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
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
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

Read the page image at this path (use your file-read tool):
{page_png}

The current Markdown for this page is also at:
{page_md}

It is reproduced here:
---BEGIN PAGE.MD---
{page_text}
---END PAGE.MD---

Rules:
- Fix only confident OCR errors: wrong or missing characters, broken punctuation
  such as a single "—" that should be "——".
- Do not rewrite, polish, paraphrase, or modernise the author's wording.
- Keep the Markdown structure exactly (headings, paragraphs, :::figure blocks,
  blank lines, HTML footnote paragraphs).
- Do not add commentary, analysis, or a preamble.
- If there are no confident corrections, reply with exactly: NO_CHANGES
- Otherwise reply with the complete corrected Markdown only (no code fences).
"""


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
        page_dir = project / "pages" / pid
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


def parse_agent_json(stdout: str) -> dict:
    raw = stdout.strip()
    if not raw:
        raise ValueError("Cursor CLI produced empty stdout")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
        raise


def unwrap_agent_markdown(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:markdown|md)?\s*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
    return t.strip()


def run_cursor_agent(agent_bin: str, prompt: str, *, model: str | None, workspace: Path,
                     timeout: float, api_key: str | None = None) -> str:
    """Call `agent --print --output-format json` and return the result text.

    Interface from https://cursor.com/docs/cli/headless and
    https://cursor.com/docs/cli/reference/parameters :
      agent -p/--print --output-format json --model MODEL --trust --workspace DIR
            --mode ask  PROMPT
    Auth: CURSOR_API_KEY or --api-key (https://cursor.com/docs/cli/reference/authentication).
    JSON shape: {type: result, subtype: success, result: "<text>", ...}
      (https://cursor.com/docs/cli/reference/output-format)
    Images: include file paths in the prompt; the agent reads them via tools
      (https://cursor.com/docs/cli/headless#working-with-images).
    """
    cmd = _agent_argv(agent_bin) + [
        "--print",
        "--output-format", "json",
        "--trust",
        "--mode", "ask",
        "--workspace", str(workspace),
    ]
    if model:
        cmd.extend(["--model", model])
    key = api_key or os.environ.get("CURSOR_API_KEY")
    if key and "CURSOR_API_KEY" not in os.environ:
        cmd.extend(["--api-key", key])
    cmd.append(prompt)
    env = os.environ.copy()
    if api_key:
        env["CURSOR_API_KEY"] = api_key
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       cwd=str(workspace), env=env)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
        raise RuntimeError(f"Cursor CLI failed (exit {r.returncode}): {err[:2000]}")
    data = parse_agent_json(r.stdout)
    if data.get("is_error"):
        raise RuntimeError(f"Cursor CLI reported an error: {data.get('result')!r}")
    if data.get("type") not in (None, "result"):
        # still accept if `result` is present
        if "result" not in data:
            raise RuntimeError(f"unexpected Cursor CLI JSON: {list(data)[:8]}")
    result = data.get("result")
    if result is None:
        raise RuntimeError("Cursor CLI JSON missing 'result'")
    return unwrap_agent_markdown(str(result))


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


def proofread_one_page(page: dict, *, agent_bin: str, model: str | None, timeout: float,
                       dry_run: bool, force: bool, api_key: str | None) -> dict:
    page_dir = Path(page["dir"])
    md_path = page_dir / PAGE_MD
    png_path = page_dir / PAGE_PNG
    meta = page.get("meta") or {}
    original = md_path.read_text(encoding="utf-8")
    ocr_path = page_dir / PAGE_OCR_MD
    ocr_text = ocr_path.read_text(encoding="utf-8") if ocr_path.exists() else None
    status_path = page_dir / PROOFREAD_JSON
    pid = page.get("id") or f"{page['page']:04d}"

    if status_path.exists() and not force:
        try:
            st = json.loads(status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            st = {}
        if st.get("status") == "done":
            return {"page": pid, "status": "skipped", "reason": "already proofread"}

    if not png_path.exists():
        return {"page": pid, "status": "failed", "error": f"missing {png_path}"}

    prompt = PROOFREAD_PROMPT.format(
        page_png=str(png_path.resolve()),
        page_md=str(md_path.resolve()),
        page_text=original,
    )
    try:
        result = run_cursor_agent(agent_bin, prompt, model=model, workspace=page_dir,
                                  timeout=timeout, api_key=api_key)
    except subprocess.TimeoutExpired:
        md_path.write_text(original, encoding="utf-8")
        if ocr_text is not None:
            ocr_path.write_text(ocr_text, encoding="utf-8")
        return {"page": pid, "status": "failed", "error": f"timeout after {timeout}s"}
    except Exception as e:
        md_path.write_text(original, encoding="utf-8")
        if ocr_text is not None:
            ocr_path.write_text(ocr_text, encoding="utf-8")
        return {"page": pid, "status": "failed", "error": str(e)}

    # Discard any in-place writes the agent may have made; we own the files.
    md_path.write_text(original, encoding="utf-8")
    if ocr_text is not None:
        ocr_path.write_text(ocr_text, encoding="utf-8")

    if result == "NO_CHANGES" or result.replace("\r\n", "\n") == original.replace("\r\n", "\n"):
        rec = {"status": "done", "changed": False, "model": model,
               "time": datetime.now(timezone.utc).isoformat(),
               "sha256": sha256_text(original)}
        diff = ""
        if not dry_run:
            status_path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"page": pid, "status": "unchanged", "diff": diff, "dry_run": dry_run}

    err = _correction_looks_safe(original, result, meta)
    if err:
        return {"page": pid, "status": "failed", "error": err}

    if not result.endswith("\n"):
        result += "\n"
    diff = unified_diff(original, result, f"pages/{pid}/{PAGE_MD}")
    if dry_run:
        return {"page": pid, "status": "would_change", "diff": diff, "dry_run": True}

    md_path.write_text(result, encoding="utf-8")
    rec = {"status": "done", "changed": True, "model": model,
           "time": datetime.now(timezone.utc).isoformat(),
           "sha256": sha256_text(result), "sha256_before": sha256_text(original)}
    status_path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"page": pid, "status": "changed", "diff": diff, "dry_run": False}


def write_proofread_report(project: Path, results: list[dict], *, model: str | None,
                           dry_run: bool) -> Path:
    out_dir = project / "proofread"
    diffs_dir = out_dir / "diffs"
    out_dir.mkdir(parents=True, exist_ok=True)
    diffs_dir.mkdir(parents=True, exist_ok=True)
    counts = {"changed": 0, "unchanged": 0, "skipped": 0, "failed": 0, "would_change": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        diff = r.get("diff") or ""
        if diff:
            (diffs_dir / f"{r['page']}.diff").write_text(diff, encoding="utf-8")
    lines = [
        f"# Proofread report",
        f"",
        f"- generated: {datetime.now(timezone.utc).isoformat()}",
        f"- model: {model or '(Cursor CLI default)'}",
        f"- dry_run: {dry_run}",
        f"- pages: {len(results)}",
        f"- changed: {counts.get('changed', 0)}",
        f"- would_change: {counts.get('would_change', 0)}",
        f"- unchanged: {counts.get('unchanged', 0)}",
        f"- skipped (already proofread): {counts.get('skipped', 0)}",
        f"- failed: {counts.get('failed', 0)}",
        f"",
    ]
    for r in results:
        lines.append(f"## Page {r['page']} — {r['status']}")
        if r.get("error"):
            lines.append(f"\nError: {r['error']}\n")
        elif r.get("reason"):
            lines.append(f"\n{r['reason']}\n")
        elif r.get("diff"):
            lines.append("\n```diff")
            lines.append(r["diff"].rstrip("\n"))
            lines.append("```\n")
        else:
            lines.append("\nNo textual changes.\n")
    report = out_dir / "report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    return report


# --------------------------------------------------------------------- stages
def resolve_project(args, pdf: Path | None = None) -> Path:
    if getattr(args, "project", None):
        return Path(args.project)
    out = getattr(args, "output", None)
    if out:
        return Path(out).with_name(Path(out).stem + "_work")
    if pdf is not None:
        return Path(pdf).with_name(Path(pdf).stem + "_work")
    raise SystemExit("specify --project (editable project directory)")


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
    timeout = float(getattr(args, "timeout", 180) or 180)
    jobs = max(1, int(getattr(args, "jobs", 1) or 1))
    dry_run = bool(getattr(args, "dry_run", False))
    force = bool(getattr(args, "force", False))
    api_key = getattr(args, "api_key", None)
    only = set(args.only_page) if getattr(args, "only_page", None) else None
    if only:
        pages = [p for p in pages if p["page"] in only or p["id"] in {f"{n:04d}" for n in only}]
        if not pages:
            raise SystemExit(f"no pages matched --only-page {sorted(only)}")
    log(f"proofread {len(pages)} page(s) via {agent_bin} "
        f"(model={model or 'default'}, jobs={jobs}, timeout={timeout}s, dry_run={dry_run})")
    results: list[dict] = []
    if jobs == 1:
        for p in pages:
            log(f"proofreading page {p['id']} ...")
            r = proofread_one_page(p, agent_bin=agent_bin, model=model, timeout=timeout,
                                   dry_run=dry_run, force=force, api_key=api_key)
            log(f"page {r['page']}: {r['status']}" + (f" ({r['error']})" if r.get("error") else ""))
            results.append(r)
    else:
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = {ex.submit(proofread_one_page, p, agent_bin=agent_bin, model=model,
                              timeout=timeout, dry_run=dry_run, force=force,
                              api_key=api_key): p for p in pages}
            for fut in as_completed(futs):
                r = fut.result()
                with lock:
                    results.append(r)
                    log(f"page {r['page']}: {r['status']}"
                        + (f" ({r['error']})" if r.get("error") else ""))
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
    p.add_argument("--timeout", type=float, default=180, help="per-page Cursor CLI timeout in seconds")
    p.add_argument("--dry-run", action="store_true",
                   help="run the model and write a report, but do not modify page.md")
    p.add_argument("--force", action="store_true", help="re-proofread pages already marked done")
    p.add_argument("--agent-bin", help="Cursor CLI binary (default: agent on PATH, or $PDF2EPUB_AGENT_BIN)")
    p.add_argument("--api-key", help="passed as --api-key; otherwise CURSOR_API_KEY is used")
    p.add_argument("--only-page", type=int, action="append",
                   help="restrict to this 1-based page number (repeatable)")


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
    p_all.add_argument("--timeout", type=float, default=180)
    p_all.add_argument("--dry-run", action="store_true")
    p_all.add_argument("--force", action="store_true")
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
