#!/usr/bin/env python3
"""Mock Cursor CLI `agent` for tests.

Implements the documented headless interface used by pdf2epub:

  agent --print --output-format stream-json --sandbox enabled --trust
        --workspace ABS_DIR [--model MODEL] PROMPT

Auth is via CURSOR_API_KEY in the environment, never --api-key on argv.

NDJSON events follow https://cursor.com/docs/cli/reference/output-format
(system init with model, assistant messages, tool_call, terminal result with
duration_ms + usage). Relative --workspace is resolved against cwd, matching
the real CLI.

Behaviour is selected with FAKE_AGENT_BEHAVIOR:
  ok (default)           apply deterministic OCR fixes; wrap in PAGE.MD markers
  fail                   exit 1 with an error on stderr
  timeout                sleep until the caller times out
  invalid                print non-JSON on stdout and exit 0
  drop_figure            return markdown that omits :::figure blocks
  narrate                narration before, between, and after tool calls, then
                         the corrected page (tests stream-json last-message)
  trailing_no_changes    narration ending with NO_CHANGES
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


def emit_stream(events: list[dict]) -> None:
    for ev in events:
        sys.stdout.write(json.dumps(ev, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def assistant(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "session_id": "00000000-0000-0000-0000-000000000000",
    }


def tool_read(path: str) -> list[dict]:
    sid = "00000000-0000-0000-0000-000000000000"
    return [
        {"type": "tool_call", "subtype": "started", "call_id": "t1",
         "tool_call": {"readToolCall": {"args": {"path": path}}}, "session_id": sid},
        {"type": "tool_call", "subtype": "completed", "call_id": "t1",
         "tool_call": {"readToolCall": {"args": {"path": path},
                                        "result": {"success": {"totalLines": 1}}}},
         "session_id": sid},
    ]


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("-p", "--print", action="store_true")
    ap.add_argument("--output-format")
    ap.add_argument("--model")
    ap.add_argument("--mode")
    ap.add_argument("--workspace")
    ap.add_argument("--trust", action="store_true")
    ap.add_argument("--api-key")  # real pdf2epub must NOT pass this
    ap.add_argument("--sandbox")
    ap.add_argument("-f", "--force", action="store_true")
    ap.add_argument("--yolo", action="store_true")
    args, rest = ap.parse_known_args(argv)
    behavior = os.environ.get("FAKE_AGENT_BEHAVIOR", "ok")

    log_path = os.environ.get("FAKE_AGENT_ARGV_FILE")
    if log_path:
        Path(log_path).write_text(json.dumps({
            "argv": argv, "cwd": os.getcwd(), "workspace": args.workspace,
            "api_key_flag": args.api_key, "sandbox": args.sandbox,
            "output_format": args.output_format,
            "env_has_cursor_api_key": bool(os.environ.get("CURSOR_API_KEY")),
        }, indent=1), encoding="utf-8")

    if args.api_key:
        print("api-key must not appear on the command line", file=sys.stderr)
        return 1

    if behavior == "fail":
        print("not authenticated", file=sys.stderr)
        return 1
    if behavior == "timeout":
        time.sleep(float(os.environ.get("FAKE_AGENT_SLEEP", "30")))
        return 0
    if behavior == "invalid":
        print("this is not json")
        return 0

    if not args.workspace:
        print("Error: Workspace directory does not exist: (missing)", file=sys.stderr)
        return 1
    ws = Path(args.workspace)
    if not ws.is_absolute():
        ws = Path.cwd() / ws
    if not ws.is_dir():
        print(f"Error: Workspace directory does not exist: {ws}", file=sys.stderr)
        return 1

    md_path = ws / "page.md"
    original = md_path.read_text(encoding="utf-8") if md_path.exists() else "\n".join(rest)
    if behavior == "drop_figure":
        corrected = re.sub(r":::figure .*?:::", "", original, flags=re.S).strip() + "\n"
    else:
        corrected = apply_ocr_fixes(original)

    model_name = args.model or "Composer 2.5"
    init = {
        "type": "system", "subtype": "init", "apiKeySource": "env",
        "cwd": str(ws), "session_id": "00000000-0000-0000-0000-000000000000",
        "model": model_name, "permissionMode": "default",
    }
    usage = {"inputTokens": 100, "outputTokens": 20, "cacheReadTokens": 0, "cacheWriteTokens": 0}

    def result_event(concat: str) -> dict:
        return {
            "type": "result", "subtype": "success", "is_error": False,
            "duration_ms": 1234, "duration_api_ms": 1234,
            "result": concat, "usage": usage,
            "session_id": "00000000-0000-0000-0000-000000000000",
        }

    if behavior == "trailing_no_changes":
        final = "No OCR errors found.\nNO_CHANGES"
        pre = "Comparing the OCR text against the page image…"
        mid = "Read the image."
        concat = pre + mid + final
        events = [init, assistant(pre), *tool_read("page.png"),
                  assistant(mid), *tool_read("page.md"),
                  assistant(final), result_event(concat)]
        emit_stream(events)
        return 0

    if behavior == "narrate":
        body = corrected if corrected != original else original
        wrapped = f"---BEGIN PAGE.MD---\n{body.rstrip()}\n---END PAGE.MD---"
        pre = "Comparing the OCR text against the page image…"
        mid = "Looking more closely at the scan."
        # last message also has a short preamble before the markers / heading
        final = "Everything else matches the image.\n\n" + wrapped
        concat = pre + mid + final
        events = [init, assistant(pre), *tool_read(str(ws / "page.png")),
                  assistant(mid), *tool_read(str(ws / "page.md")),
                  assistant(final), result_event(concat)]
        emit_stream(events)
        return 0

    if corrected == original:
        final = "NO_CHANGES"
    else:
        final = f"---BEGIN PAGE.MD---\n{corrected.rstrip()}\n---END PAGE.MD---"
    events = [init, assistant(final), result_event(final)]
    emit_stream(events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
