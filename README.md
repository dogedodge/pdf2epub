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
#   --no-punct-normalize   keep OCR punctuation as-is
```
Intermediates go to `<out>_work/` (or `--workdir`):
`pages/` 300-DPI renders, `images/` cropped figures, `layout/` pages with layout boxes drawn
(red = dropped header/footer/page no., blue = text, orange = heading, green = figure,
purple = caption), `mineru_<tier>.json` raw layout/OCR, `items.json` cleaned stream, `book.md`.

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
3. `build_document` – crop `image/chart/table/...` bodies from the 300-DPI render (normalised
   bbox + 0.8 % padding), attach captions.
4. Cleaning – drop `header/footer/page_number`, merge `continues_prev` blocks (+ fallback:
   first text on a page joins previous paragraph if it lacks terminal punctuation),
   CJK punctuation normalisation, heading levels from glyph height (largest size gap → h1).
5. `build_epub` – one XHTML per h1 chapter, h2 in nested TOC, figures with `<figcaption>`, CSS
   with 2em indent, nav + NCX, `zh-CN` metadata; optional epubcheck.
