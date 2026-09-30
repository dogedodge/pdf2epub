# pdf2epub

Converts a scanned (image-only) PDF — tuned for Simplified-Chinese books — into an EPUB 3:
layout analysis + OCR (MinerU, fully local, CPU), illustration cropping, header/footer
removal, cross-page paragraph merging, heading/chapter detection, TOC.

The conversion is a **two-stage pipeline** with an editable project directory as the
source of truth. An optional **proofread** stage calls the [Cursor CLI](https://cursor.com/docs/cli/overview)
in headless print mode to fix confident OCR errors using your Cursor subscription.

Licensed under the MIT License. See [LICENSE](LICENSE). Copyright 2026 dogedodge.

## Setup
```bash
python3 -m venv .venv-mineru && . .venv-mineru/bin/activate
pip install -r requirements.txt
# optional validator (Debian/Ubuntu): sudo apt-get install epubcheck   (needs Java)
```
First run downloads models to `~/.mineru/models` from Hugging Face:
~227 MB ONNX models (basic tier) and, for `--tier standard`, a 1.2 GB MinerU2.5 GGUF VLM.

### Cursor CLI (for `proofread`)
Official docs: [Installation](https://cursor.com/docs/cli/installation),
[Authentication](https://cursor.com/docs/cli/reference/authentication),
[Headless / print mode](https://cursor.com/docs/cli/headless),
[Parameters](https://cursor.com/docs/cli/reference/parameters),
[Output format](https://cursor.com/docs/cli/reference/output-format).

```bash
# macOS, Linux, WSL
curl https://cursor.com/install -fsS | bash
# put ~/.local/bin on PATH, then:
agent --version

# authenticate (browser) or use an API key from https://cursor.com/dashboard/api
agent login
# or:
export CURSOR_API_KEY=your_api_key_here

# models available on your account
agent models          # or: agent --list-models
```

pdf2epub invokes the documented non-interactive interface:

```text
agent --print --output-format stream-json --sandbox enabled --trust
      --workspace <absolute-page-dir> [--model MODEL] "<prompt>"
```

`--print` (`-p`) is headless print mode. `--output-format stream-json` is used
instead of `json` because the JSON `result` field concatenates **all** assistant
text, including narration before and between tool calls. pdf2epub keeps only the
last assistant message after the last tool call, then extracts Markdown between
`---BEGIN PAGE.MD---` / `---END PAGE.MD---` markers (or treats a reply that
**ends** in `NO_CHANGES` as unchanged).

`--mode ask` is **not** a sandbox: the agent can still run shell commands and
write files. pdf2epub therefore passes `--sandbox enabled` and writes a per-page
`.cursor/cli.json` that **denies** `Shell(*)`, `Write(*)`, `WebFetch(*)`, and
MCP tools, while allowing `Read(*)` so the page image can be opened. pdf2epub
applies accepted corrections itself.

`--workspace` is always an absolute path (a relative `--project` is resolved
first). Auth is `CURSOR_API_KEY` in the environment; `--api-key` on pdf2epub
is copied into that env and is **never** placed on the child command line.
The binary name is `agent`. Page images are passed as file paths in the prompt
([Working with images](https://cursor.com/docs/cli/headless.md)).

## Usage

```bash
# 1. OCR → editable project (per-page Markdown + images)
python pdf2epub.py ocr book.pdf --project book_work --title "书名" --author "作者"

# 2. Optional: proofread each page with the Cursor CLI (resumable)
python pdf2epub.py proofread --project book_work --model claude-sonnet-5-thinking-high --jobs 2
# other ids from `agent models`, e.g. gpt-5.6-sol-high, composer-2.5

# 3. Build EPUB purely from the project directory (hand edits honoured)
python pdf2epub.py build --project book_work -o out/book.epub --epubcheck

# Convenience: ocr + proofread + build
python pdf2epub.py all book.pdf -o out/book.epub --project book_work --title "书名"

# Backwards-compatible one-shot (ocr + build, no proofread):
python pdf2epub.py book.pdf -o out/book.epub --title "书名" --author "作者" --epubcheck
```

Useful flags:

```text
--tier basic|standard   MinerU tier (default basic)
--reuse-json            skip MinerU and reuse mineru_<tier>.json in the project
--no-punct-normalize    keep OCR punctuation as-is at OCR time
--workdir DIR           alias for --project
--epubcheck             run epubcheck if installed

proofread:
  --model NAME          Cursor CLI --model (`agent models`; e.g. claude-sonnet-5-thinking-high,
                        gpt-5.6-sol-high, composer-2.5). Auto records the resolved id.
  --jobs N              concurrent agent processes (default 1)
  --timeout SEC         per-page timeout (default 300); on timeout the page is left unchanged
  --dry-run             call the model and write a report, do not modify page.md
  --force               re-proofread pages already marked done (uses current page.md)
  --from-ocr            restore page.md from page.ocr.md first (with --force, redo a bad correction)
  --agent-bin PATH      default: `agent` on PATH, ~/.local/bin/agent, or $PDF2EPUB_AGENT_BIN
  --api-key KEY         set CURSOR_API_KEY for the child (not passed on argv)
  --only-page N         repeatable; proofread a subset of pages (report/results are merged)
```

`--reuse-json` still needs the PDF (pages are re-rendered). MinerU is not called.

## Project directory (source of truth)

`--project` (default: `<pdf-or-output-stem>_work/`) is what `build` reads. Edit
`pages/NNNN/page.md` next to `page.png`; rebuild the EPUB and your edits appear.

```text
book_work/
  book.json                 metadata, page order, title/author/lang
  mineru_basic.json         cached MinerU output (optional; --reuse-json)
  layout/                   debug overlays (not used by build)
  pages/
    0001/
      page.png              300 DPI scan
      page.md               editable text (build reads this)
      page.ocr.md           original OCR snapshot (never overwritten by proofread)
      meta.json             continues_prev, figure files, crop boxes
      fig_p0003_001.png     cropped illustrations, if any
      proofread.json        written after a successful proofread (skip/resume)
    0002/
      ...
  proofread/
    report.md               summary + per-page diffs
    diffs/0003.diff
    results.json
```

### Per-page Markdown

Headings use ATX (`#` chapter, `##` section). Body paragraphs are blank-line
separated. Figures use a fence so captions may contain parentheses:

```markdown
# 理论基础

技术分析有三个基本假定或者说前提条件:

## 市场行为包容消化一切

段落……

:::figure fig_p0003_001.png
图 1.1 上升趋势的示例。……
:::
```

`meta.json` `continues_prev` marks a page whose **first paragraph** is the
second half of a paragraph split at a page break. `build` concatenates that
fragment with the previous page (and still applies the original fallback:
if the previous paragraph has no terminal punctuation, the next page’s leading
paragraph is joined). Chapter splits follow `#` headings, same as before.

Proofread writes punctuation-level corrections into `page.md`, leaves
`page.ocr.md` intact, and appends a reviewable report (per-page diff, wall time,
CLI `duration_ms`, token `usage`, and the model Auto actually picked). Timestamps
are local time with an offset. Already-proofread pages are skipped unless
`--force`. `--force` re-sends the **current** `page.md` (a bad correction would
snowball); `--force --from-ocr` restores `page.ocr.md` first. Failures and
timeouts leave `page.md` unchanged and are retried on the next run. `--dry-run`
still calls the model and writes `proofread/report.md` but does not mark pages
done or edit `page.md`. `--only-page` merges into the existing report instead of
replacing it.

The proofreader is instructed to fix only confident substitutions of **visible**
glyphs and broken punctuation (`—` → `——`), not to rewrite or modernise the
author, and not to guess smudged/missing characters (e.g. 等 vs 当). Insertions
and other non-punctuation edits are **suggestions** in the report for human
review; they are not auto-applied. One image read is enough; do not over-inspect.

## Notes / hurdles
* MinerU 4.x is no longer the old `magic-pdf` CLI; its `mineru parse` CLI goes through a
  document-library server and, with default config (`parse_server.local.mode: disabled`),
  may use the **remote mineru.net API**. This script calls the in-process SDK
  (`mineru.parser.parse`) instead, which runs locally and has no telemetry hooks.
* MinerU spawns render workers with `multiprocessing` *spawn*: the calling script must
  have an `if __name__ == "__main__":` guard.
* Torch is not required for `basic`/`standard` on CPU (ONNX + llama.cpp).
* `ocr_mode="ocr"` is forced so any existing (often poor) invisible OCR text layer is ignored.

## Pipeline
1. `ocr` / `inspect_and_render` – PyMuPDF: page sizes, image coverage, 300-DPI PNG per page.
2. `ocr` / `run_mineru` – layout (PP-DocLayoutV2) + OCR (PP-OCRv6), reading order, block types.
3. `ocr` / `extract_pages` – drop `header/footer/page_number`, crop `image/chart/table/...`
   bodies (normalised bbox + 0.8 % padding), CJK punctuation normalisation, heading
   levels from glyph height. Writes per-page Markdown; **does not** merge across pages.
4. `proofread` (optional) – Cursor CLI headless `agent --print` on each page image + `page.md`.
5. `build` – load every `page.md`, merge `continues_prev` / unpunctuated splits, split
   `#` chapters, one XHTML per chapter, h2 in nested TOC, figures with `<figcaption>`,
   CSS with 2em indent, nav + NCX, `zh-CN` metadata; optional epubcheck.

## Tests
```bash
python3 -m unittest tests.test_pdf2epub
```
Proofread tests use `tests/fake_cursor_agent.py`, a stand-in for `agent` that
speaks the documented stream-json print-mode interface (including narration
around tool calls). They do not require a Cursor login.
