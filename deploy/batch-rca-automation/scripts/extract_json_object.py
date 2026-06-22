"""Extract the first JSON object or array from mixed Claude stdout text."""

from __future__ import annotations

import json
import sys


def extract_json(text: str) -> dict | list:
    start = None
    opener = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            opener = ch
            break
    if start is None:
        raise ValueError("No JSON object or array found in output")

    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])

    raise ValueError("Unterminated JSON in output")


def main(argv: list[str] | None = None) -> int:
    if len(argv or sys.argv) < 2:
        print("Usage: extract_json_object.py <file>", file=sys.stderr)
        return 1

    path = (argv or sys.argv)[1]
    with open(path) as f:
        text = f.read()

    try:
        data = extract_json(text)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"[ERROR] Failed to extract JSON: {e}", file=sys.stderr)
        return 1

    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
