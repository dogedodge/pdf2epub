#!/usr/bin/env python3
"""Mock Cursor CLI `agent` for tests.

Implements the documented headless interface used by pdf2epub:

  agent --print --output-format json --model MODEL --trust --mode ask
        --workspace DIR [--api-key KEY] PROMPT

Success stdout is a single JSON object with a `result` field
(https://cursor.com/docs/cli/reference/output-format).

Behaviour is selected with FAKE_AGENT_BEHAVIOR:
  ok (default)  apply deterministic OCR fixes to workspace/page.md
  fail          exit 1 with an error on stderr
  timeout       sleep until the caller times out
  invalid       print non-JSON on stdout and exit 0
  drop_figure   return markdown that omits :::figure blocks (should be rejected)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path


def apply_ocr_fixes(text: str) -> str:
    text = text.replace(
        "两条道路分歧最大。趋势发展过一段之后",
        "两条道路分歧最大。等趋势发展过一段之后",
    )
    text = re.sub(r"(?<=[\u3400-\u9fff])[—–―](?=[\u3400-\u9fff])", "——", text)
    text = text.replace("OCR_ERROR_FOO", "校正")
    return text


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("-p", "--print", action="store_true")
    ap.add_argument("--output-format")
    ap.add_argument("--model")
    ap.add_argument("--mode")
    ap.add_argument("--workspace")
    ap.add_argument("--trust", action="store_true")
    ap.add_argument("--api-key")
    ap.add_argument("-f", "--force", action="store_true")
    ap.add_argument("--yolo", action="store_true")
    args, rest = ap.parse_known_args(argv)
    behavior = os.environ.get("FAKE_AGENT_BEHAVIOR", "ok")
    if behavior == "fail":
        print("not authenticated", file=sys.stderr)
        return 1
    if behavior == "timeout":
        time.sleep(float(os.environ.get("FAKE_AGENT_SLEEP", "30")))
        return 0
    if behavior == "invalid":
        print("this is not json")
        return 0

    workspace = Path(args.workspace) if args.workspace else Path(".")
    md_path = workspace / "page.md"
    if md_path.exists():
        original = md_path.read_text(encoding="utf-8")
    else:
        original = "\n".join(rest)

    if behavior == "drop_figure":
        corrected = re.sub(r":::figure .*?:::", "", original, flags=re.S).strip() + "\n"
    else:
        corrected = apply_ocr_fixes(original)

    result = "NO_CHANGES" if corrected == original else corrected
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 1,
        "duration_api_ms": 1,
        "result": result,
        "session_id": "00000000-0000-0000-0000-000000000000",
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
