# pdf2epub (prototype)

Converts a scanned (image-only) PDF — tuned for Simplified-Chinese books — into an EPUB 3:
layout analysis + OCR (MinerU, fully local, CPU), illustration cropping, header/footer
removal, cross-page paragraph merging, heading/chapter detection, TOC.

## Setup
```bash
python3 -m venv .venv-mineru && . .venv-mineru/bin/activate
pip install -r requirements.txt
# optional validator (Debian/Ubuntu): sudo apt-get install epubcheck   (needs Java)
```
First run downloads models to `~/.mineru/models` from Hugging Face:
~227 MB ONNX models (basic tier) and, for `--tier standard`, a 1.2 GB MinerU2.5 GGUF VLM.

## Usage
```bash
python pdf2epub.py book.pdf -o out/book.epub --title "书名" --author "作者" --epubcheck
#   --tier basic      (default) ONNX pipeline, ~2–3 s/page on 8 CPU cores
#   --tier standard   adds MinerU2.5 VLM via llama.cpp; ~35–40 s/page on CPU, slightly better
#   --reuse-json      skip OCR and rebuild the EPUB from the cached MinerU JSON
#   --from-json FILE  same rebuild from a MinerU JSON path (PDF / page images optional)
#   --chapter-regex RE   override the default Chinese chapter-title pattern
#   --toc-file FILE   user TOC (one title per line; indent = nesting; optional page number)
#   --tables auto     (default) prose-like "tables" → paragraphs; real tables → HTML
#   --tables html     always emit MinerU's recognised HTML table (sanitised XHTML)
#   --tables image    legacy: crop every table as a picture and drop the HTML
#   --table-images    with auto/html, also embed the cropped scan next to real tables
#   --table-min-quality 0.55   auto: HTML only if the quality score is at least this
#   --no-punct-normalize   keep OCR punctuation as-is
```
Intermediates go to `<out>_work/` (or `--workdir`):
`pages/` 300-DPI renders, `images/` cropped figures, `layout/` pages with layout boxes drawn
(red = dropped header/footer/page no., blue = text, orange = heading, green = figure,
purple = caption), `mineru_<tier>.json` raw layout/OCR, `items.json` cleaned stream, `book.md`.

### Heading / chapter detection
Chapters are **pattern-first**, not “tallest box = h1”. The default regex matches Chinese
front/back matter and numbered divisions, with Chinese or Arabic numerals:

`第X章` / `第X篇` / `第X部分` / `第X卷`, `附录X`, `前言`, `序`/`序言`, `译者的话`,
`目录`, `致谢`, `后记`, `索引`, `全书大会串`, …

Box height is only a **secondary** signal, and it uses **per-line** height so a wrapped
section title is not ranked above a one-line chapter title. Recurring sub-heads such as
`引言` that MinerU labelled as body text are promoted; sentence fragments (ending in
`。，；、`, too long, or continuing the previous paragraph) and short labels sitting on a
figure are demoted. The EPUB TOC is 2–3 levels: chapter files with sections nested under them.

`--chapter-regex` replaces that default (e.g. `'^Chapter\\s+\\d+'`). `--toc-file` overrides
levels for titles it can match, in document order:

```
前言
第一章 技术分析的理论基础
  引言
  理论基础
附录一
索引
```

`--from-json mineru_basic.json -o out/book.epub` rebuilds structure from OCR JSON alone
(no PDF, no re-OCR). Missing page PNGs skip figure crops but keep captions and reading order.
`--reuse-json` still reads `mineru_<tier>.json` from the work dir when a PDF is given.

### Tables (MinerU `table` blocks)
MinerU 4 basic often wraps a whole text column as one `table` whose `table_body` is
HTML (sometimes two tall `<td>` cells of prose). The converter used to crop that box
as an image and throw away the HTML, so a page of body text became a picture and
left-margin titles such as `总结` / `结语` were emitted *after* it.

`--tables auto` (default) is three steps: prose detection, then a **quality gate**,
then HTML.

* **Prose table** — few columns, long sentence-like cells → split into paragraphs
  (OCR joins wrapped lines with spaces; a new indented paragraph shows up as
  `。` + space, except mid-paragraph openers such as `举例来说`). Left-margin
  headings are inserted by vertical position so they sit between the right-hand
  paragraphs. Cross-page merging still applies to the first/last paragraph of
  the exploded table.
* **Unreliable HTML** — fall back to the cropped scan when the recognised table
  looks like a chart or a broken grid. Hard fails (constants in `pdf2epub.py`):
  more than 40% empty cells, more than 50% fragmentary digit/symbol cells,
  more than 25% X/O chart marks, more than 40% of rows with a different column
  count, or irregular body row/colspans. A `图` / `圖` / `Figure` caption (or a
  box overlapping an image/chart) adds a penalty but **does not** by itself
  reject a clean, dense grid (e.g. 图15.15). Combined score must be at least
  `--table-min-quality` (default 0.55). Every table logs one line with the
  decision and scores.
* **Real table** — sanitised XHTML `<table>` (allowed tags: `table/thead/tbody/
  tfoot/tr/td/th/caption/colgroup/col/br` plus `colspan` / `rowspan` / `scope`).
  The scan crop is kept only with `--table-images`. `--tables html` skips the
  gate and always emits HTML; `--tables image` is the old crop-only path.

CJK spaces inside cells are stripped the same way as body text.

Tests: `python -m unittest discover -s tests`. Synthetic fixtures only are in-repo; a real
MinerU dump can be pointed at with `MINERU_BASIC_JSON` (do not commit scanned books).

## Notes / hurdles
* MinerU 4.x is no longer the old `magic-pdf` CLI; its `mineru parse` CLI goes through a
  document-library server and, with default config (`parse_server.local.mode: disabled`),
  may use the **remote mineru.net API**. This script calls the in-process SDK
  (`mineru.parser.parse`) instead, which runs locally and has no telemetry hooks.
* MinerU spawns render workers with `multiprocessing` *spawn*: the calling script must
  have an `if __name__ == "__main__":` guard.
* Torch is not required for `basic`/`standard` on CPU (ONNX + llama.cpp).
* `ocr_mode="ocr"` is forced so any existing (often poor) invisible OCR text layer is ignored.

## Pipeline steps (see pdf2epub.py)
1. `inspect_and_render` – PyMuPDF: page sizes, image coverage, text layer / invisible spans, 300-DPI PNG.
2. `run_mineru` – layout (PP-DocLayoutV2) + OCR (PP-OCRv6), reading order, block types.
3. `build_document` – crop `image/chart/...` bodies from the 300-DPI render (normalised
   bbox + 0.8 % padding), attach captions; classify `table` blocks (see above).
4. Cleaning – drop `header/footer/page_number`, merge `continues_prev` blocks (+ fallback:
   first text on a page joins previous paragraph if it lacks terminal punctuation),
   CJK punctuation normalisation, chapter titles from a Chinese regex (size / per-line
   height only to rank remaining h2/h3).
5. `build_epub` – one XHTML per h1 chapter, h2/h3 nested in the TOC, figures with `<figcaption>`,
   HTML tables in `<figure class="table">`, CSS with 2em indent, nav + NCX, `zh-CN`
   metadata; optional epubcheck.
