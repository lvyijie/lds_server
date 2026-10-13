"""Build a compact server-side task catalog from the decompiled official TaskConfig."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "02-反编译源码AS3" / "反编译副本" / "config" / "scripts" / "mmo" / "config" / "task" / "TaskConfig.as"
OUTPUT = Path(__file__).resolve().parent / "data" / "task_catalog.json"


def balanced_end(text: str, start: int, opening: str, closing: str) -> int:
    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    i = start
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if line_comment:
            if ch in "\r\n":
                line_comment = False
        elif block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 1
        elif quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "/" and nxt == "/":
            line_comment = True
            i += 1
        elif ch == "/" and nxt == "*":
            block_comment = True
            i += 1
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError(f"Unclosed {opening} at offset {start}")


def parse_scalar(raw: str) -> Any:
    raw = raw.strip().rstrip(",")
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw[1:-1]
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("\\'", "'").replace("\\\\", "\\")
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def fields(block: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    # Walk only direct properties; a regex over the whole object would let
    # nested step fields (especially `ty`) overwrite task-level metadata.
    pos = 1 if block.lstrip().startswith("{") else 0
    end_block = len(block) - 1 if block.rstrip().endswith("}") else len(block)
    while pos < end_block:
        while pos < end_block and (block[pos].isspace() or block[pos] == ","):
            pos += 1
        if pos >= end_block or block[pos] in "}]":
            break
        if block[pos] in "\"'":
            q = block[pos]
            key_start = pos + 1
            pos = key_start
            while pos < end_block and block[pos] != q:
                if block[pos] == "\\":
                    pos += 1
                pos += 1
            key = block[key_start:pos]
            pos += 1
        else:
            key_start = pos
            while pos < end_block and (block[pos].isalnum() or block[pos] in "_$"):
                pos += 1
            key = block[key_start:pos]
        while pos < end_block and block[pos].isspace():
            pos += 1
        if not key or pos >= end_block or block[pos] != ":":
            pos += 1
            continue
        pos += 1
        while pos < end_block and block[pos].isspace():
            pos += 1
        if key == "stepDefines" and pos < len(block) and block[pos] == "[":
            end = balanced_end(block, pos, "[", "]")
            result[key] = block[pos:end]
            pos = end
            continue
        quote = ""
        escaped = False
        nested = 0
        end = pos
        while end < len(block):
            ch = block[end]
            if quote:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    quote = ""
            elif ch in "\"'":
                quote = ch
            elif ch in "[{(":
                nested += 1
            elif ch in "]})":
                if nested == 0:
                    break
                nested -= 1
            elif ch == "," and nested == 0:
                break
            end += 1
        result[key] = parse_scalar(block[pos:end])
        pos = end + (1 if end < end_block and block[end] == "," else 0)
    return result


def parse_steps(array_literal: str) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    pos = 1
    end = len(array_literal) - 1
    while pos < end:
        if array_literal[pos].isspace() or array_literal[pos] == ",":
            pos += 1
            continue
        if array_literal[pos] != "{":
            pos += 1
            continue
        block_end = balanced_end(array_literal, pos, "{", "}")
        steps.append(fields(array_literal[pos:block_end]))
        pos = block_end
    return steps


def main() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    marker = "TaskDatas:Object ="
    marker_pos = source.index(marker)
    outer_start = source.index("{", marker_pos)
    outer_end = balanced_end(source, outer_start, "{", "}")
    outer = source[outer_start + 1 : outer_end - 1]
    tasks: Dict[str, Dict[str, Any]] = {}
    for match in re.finditer(r"(?m)^\s*(\d+)\s*:\s*\{", outer):
        task_id = match.group(1)
        block_start = outer_start + 1 + match.end() - 1
        block_end = balanced_end(source, block_start, "{", "}")
        data = fields(source[block_start:block_end])
        steps_literal = data.pop("stepDefines", "[]")
        data["steps"] = parse_steps(steps_literal)
        tasks[task_id] = data

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(tasks)} task definitions to {OUTPUT}")


if __name__ == "__main__":
    main()
