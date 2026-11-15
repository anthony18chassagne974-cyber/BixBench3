"""Experiment t9-env-reviewer: format summary of one submitted table for the artifact reviewer.

Runs inside the sandbox. Prints the shape, header, delimiter, head and tail, row-key
checks and a seeded random sample of row identifiers and columns. It reads only the
file the agent submitted.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from collections import Counter
from pathlib import Path

HEAD_ROWS = 5
TAIL_ROWS = 3
LINE_CHARS = 300
HEADER_COLUMNS = 100
DELIMITERS = {"csv": ",", "tsv": "\t"}
DELIMITER_NAMES = {",": "comma", "\t": "tab", ";": "semicolon", "|": "pipe"}


def sample_size(count: int, minimum: int) -> int:
    """sqrt(n)/10 identifiers, rounded up, never fewer than ``minimum`` (or than n)."""
    return min(count, max(minimum, math.ceil(math.sqrt(count) / 10)))


def _clip(text: str) -> str:
    return text if len(text) <= LINE_CHARS else text[:LINE_CHARS] + " <truncated>"


def _join(row: list[str], delimiter: str) -> str:
    return _clip(delimiter.join(row).replace("\t", "<TAB>"))


def _sniff(first_line: str) -> str:
    counts = {delimiter: first_line.count(delimiter) for delimiter in DELIMITER_NAMES}
    best = max(counts, key=lambda delimiter: counts[delimiter])
    return best if counts[best] else ""


def summarize(path: Path, file_format: str | None, row_key: str | None, seed: int) -> str:
    if not path.is_file():
        return f"File not found: {path}"
    raw = path.read_bytes()
    lines = [f"File: {path.name} ({len(raw)} bytes)"]
    if not raw.strip():
        return "\n".join([*lines, "The file is empty."])
    text = raw.decode("utf-8", errors="replace")
    first_line = text.splitlines()[0]
    expected = DELIMITERS.get((file_format or "").lower())
    detected = _sniff(first_line)
    lines.append(
        f"Expected format: {file_format or 'unspecified'}; delimiter detected in the header: "
        f"{DELIMITER_NAMES.get(detected, 'none')}"
    )
    issues = []
    if "�" in text:
        issues.append("not valid UTF-8 (undecodable bytes replaced)")
    if text.startswith("﻿"):
        issues.append("starts with a UTF-8 byte-order mark")
    if "\r\n" in text:
        issues.append("Windows line endings (CRLF)")
    delimiter = expected or detected or ","
    rows = list(csv.reader(text.lstrip("﻿").splitlines(), delimiter=delimiter))
    header, body = rows[0], [row for row in rows[1:] if row]
    blank = len(rows) - 1 - len(body)
    if blank:
        issues.append(f"{blank} blank lines")
    ragged = sum(1 for row in body if len(row) != len(header))
    if ragged:
        issues.append(f"{ragged} data rows do not have {len(header)} fields")
    padded = [name for name in header if name != name.strip()]
    if padded:
        issues.append(f"header names with surrounding whitespace: {padded[:5]}")
    duplicates = [name for name, count in Counter(header).items() if count > 1]
    if duplicates:
        issues.append(f"duplicate column names: {duplicates[:5]}")
    lines.append(f"Shape: {len(body)} data rows x {len(header)} columns (header row excluded)")
    shown = header if len(header) <= HEADER_COLUMNS else [*header[:HEADER_COLUMNS], "..."]
    lines.append("Header: " + " | ".join(shown))

    key_index = header.index(row_key) if row_key in header else 0
    if row_key and row_key not in header:
        issues.append(f"row key column `{row_key}` not found in the header")
    elif key_index != 0:
        issues.append(f"row key column `{row_key}` is column {key_index + 1}, not the first")
    keys = [row[key_index] if key_index < len(row) else "" for row in body]
    empty = sum(1 for key in keys if not key.strip())
    repeated = {key: count for key, count in Counter(keys).items() if count > 1 and key.strip()}
    lines.append(
        f"Row key `{header[key_index]}`: {len(set(keys))} distinct of {len(keys)}, "
        f"{empty} empty, {len(repeated)} repeated"
        + (f" (e.g. {', '.join(list(repeated)[:3])})" if repeated else "")
    )
    lines.append("Issues: " + ("; ".join(issues) if issues else "none detected"))

    lines.append(f"\nFirst {min(HEAD_ROWS, len(body))} data rows:")
    lines.extend(_join(row, delimiter) for row in body[:HEAD_ROWS])
    if len(body) > HEAD_ROWS:
        tail = body[max(HEAD_ROWS, len(body) - TAIL_ROWS):]
        lines.append(f"Last {len(tail)} data rows:")
        lines.extend(_join(row, delimiter) for row in tail)

    generator = random.Random(seed)
    if keys:
        picked = generator.sample(keys, sample_size(len(keys), 5))
        lines.append(f"\nRandom row identifiers ({len(picked)}): " + ", ".join(picked))
    if header and body:
        columns = generator.sample(range(len(header)), sample_size(len(header), 3))
        lines.append(f"Random columns ({len(columns)}) with example values:")
        examples = generator.sample(body, min(3, len(body)))
        for index in columns:
            values = [row[index] if index < len(row) else "<missing>" for row in examples]
            lines.append(_clip(f"- {header[index]}: {', '.join(values)}"))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--format", default=None)
    parser.add_argument("--row-key", default=None)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    try:
        print(summarize(args.path, args.format, args.row_key, args.seed))
    except Exception as error:
        print(f"Could not summarize {args.path}: {type(error).__name__}: {error}")
        sys.exit(0)


if __name__ == "__main__":
    main()
