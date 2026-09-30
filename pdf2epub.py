#!/usr/bin/env python3
"""pdf2epub.py - convert a scanned (image-only) Chinese PDF into an EPUB.

Pipeline
  1. Inspect the PDF with PyMuPDF and render every page to PNG (default 300 DPI).
  2. Layout analysis + OCR with MinerU (runs locally; "basic" tier = ONNX
     PP-DocLayoutV2 + PP-OCRv6, "standard" tier adds the MinerU2.5 1.2B VLM).
  3. Crop illustrations / charts / tables from the page renders using the
     layout boxes.
  4. Clean text: drop headers/footers/page numbers, merge paragraphs split
     across pages, normalise CJK punctuation, infer heading levels.
  5. Build an EPUB 3 (ebooklib): one XHTML per chapter, figures in place,
     nav + NCX TOC, metadata. Optionally validate with epubcheck.

Usage
  python pdf2epub.py input.pdf -o out.epub [--tier basic|standard]
         [--title T] [--author A] [--dpi 300] [--workdir DIR]
         [--reuse-json] [--no-punct-normalize] [--epubcheck]
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
from pathlib import Path

DROP_TYPES = {"page_number", "header", "footer", "page_header", "page_footer",
              "aside_text", "discarded"}
FLOAT_TYPES = {"image", "chart", "table", "equation", "interline_equation",
               "seal", "figure"}
TITLE_TYPES = {"paragraph_title", "title", "doc_title"}
TERMINAL = set("。！？!?…”’」』）)》.;；:：")
CJK = r"\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef“”‘’"


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


# --------------------------------------------------------------------- step 3+4
def build_document(mj: dict, page_info: list[dict], img_dir: Path, punct: bool,
                   pad: float = 0.008) -> list[dict]:
    from PIL import Image
    img_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict] = []
    titles: list[dict] = []
    fig_no = 0
    for p in mj["pages"]:
        pidx = p["page_idx"]
        pinfo = page_info[pidx]
        page_img = Image.open(pinfo["png"])
        W, H = page_img.size
        first_text_on_page = True
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
                crop.save(img_dir / name, optimize=True)
                cap = " ".join(normalize_punct(c) if punct else c for c in caption if c)
                items.append({"kind": "figure", "type": t, "src": name, "path": str(img_dir / name),
                              "caption": cap, "footnote": " ".join(foot), "table_html": table_html,
                              "page": pidx + 1, "crop_px": box})
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
                          "cls": "footnote" if "footnote" in t else None})
    infer_heading_levels(titles)
    return items


# --------------------------------------------------------------------- step 5
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
        if secs:
            toc.append((epub.Section(ch["title"], href=c.file_name),
                        [epub.Link(f"{c.file_name}#{hid}", t, f"c{i}_{hid}") for hid, t in secs]))
        else:
            toc.append(epub.Link(c.file_name, ch["title"], f"c{i}"))
    for name in sorted(used):
        book.add_item(epub.EpubImage(uid=name, file_name=f"images/{name}", media_type="image/png",
                                     content=(img_dir / name).read_bytes()))
    book.toc = toc
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
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
        im = Image.open(page_info[p["page_idx"]]["png"]).convert("RGB")
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
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
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
    ap.add_argument("--no-punct-normalize", action="store_true")
    ap.add_argument("--epubcheck", action="store_true", help="run epubcheck if available")
    a = ap.parse_args()

    out = a.output or a.pdf.with_suffix(".epub")
    work = a.workdir or out.with_name(out.stem + "_work")
    work.mkdir(parents=True, exist_ok=True)
    timings = {}

    t = time.time()
    page_info, meta = inspect_and_render(a.pdf, work / "pages", a.dpi)
    timings["render"] = time.time() - t
    for pi in page_info:
        log(f"page {pi['page']}: {pi['width_pt']:.0f}x{pi['height_pt']:.0f}pt, images={pi['images']} "
            f"(coverage {pi['image_coverage']}), text layer chars={pi['text_chars']} "
            f"(invisible spans={pi['invisible_text_spans']}) -> {pi['png_size']}")

    jpath = work / f"mineru_{a.tier}.json"
    t = time.time()
    if a.reuse_json and jpath.exists():
        mj = json.loads(jpath.read_text())
        log(f"reused {jpath}")
    else:
        log(f"running MinerU tier={a.tier} (ocr_mode=ocr, local) ...")
        mj = run_mineru(a.pdf, a.tier)
        jpath.write_text(json.dumps(mj, ensure_ascii=False, indent=1))
    timings["ocr_layout"] = time.time() - t

    t = time.time()
    items = build_document(mj, page_info, work / "images", punct=not a.no_punct_normalize)
    draw_overlays(mj, page_info, work / "layout")
    title = a.title or (meta.get("title") or "").strip() or next(
        (i["text"] for i in items if i["kind"] == "heading" and i["level"] == 1), a.pdf.stem)
    chapters = split_chapters(items, title)
    (work / "book.md").write_text(to_markdown(items))
    (work / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=1))
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
