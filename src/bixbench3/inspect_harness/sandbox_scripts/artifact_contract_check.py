"""Final artifact contract validator (experiment v2b).

Runs inside the analyst sandbox. Standard library only. It checks the *structure* of
the required output files against the contract stated in the task prompt: files exist,
parse as delimited tables, carry the required columns, have non-empty unique row keys,
numeric columns parse, probability-like columns lie in [0, 1], and nothing looks like a
placeholder. It never judges scientific correctness and uses no grader or reference data.

Usage:
  python3 artifact_contract_check.py --contract CONTRACT.json --workspace /workspace \
      --report REPORT.md
Prints a one-line JSON summary on stdout: {"severe": [...], "warnings": [...], ...}.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path

PROBABILITY_NAME = re.compile(r"(^|_)(p|q)(_?val(ue)?)?($|_)|padj|fdr|qvalue|p_?value", re.IGNORECASE)
PLACEHOLDERS = {"todo", "tbd", "dummy", "placeholder", "synthetic", "example", "lorem", "xxx", "fixme"}
MISSING = {"", "na", "nan", "null", "none", "n/a"}
MAX_EXAMPLES = 5
NUMERIC_MAJORITY = 0.9

csv.field_size_limit(sys.maxsize if sys.maxsize < 2**31 else 2**31 - 1)


def parse_contract_from_prompt(prompt: str) -> dict[str, dict]:
    """Extract per-artifact required columns from the prompt's Required Outputs section."""
    contract: dict[str, dict] = {}
    section = prompt.split("## Required Outputs", 1)[-1]
    for block in re.split(r"\n### ", section)[1:]:
        heading, _, body = block.partition("\n")
        artifact_id = heading.strip().strip("`")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", artifact_id):
            continue
        entry: dict = {"required_columns": [], "row_key": None, "path": None}
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("- Path:"):
                entry["path"] = line.split("`")[1] if "`" in line else None
            elif line.startswith("- Row key:"):
                entry["row_key"] = line.split("`")[1] if "`" in line else None
            elif line.startswith("- Required columns:"):
                entry["required_columns"] += re.findall(r"`([^`]+)`", line)
            elif "Required sample columns are:" in line:
                entry["required_columns"] += re.findall(r"`([^`]+)`", line.split("Required sample columns are:", 1)[1])
        contract[artifact_id] = entry
    return contract


def is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def examples(values: list[str]) -> str:
    shown = ", ".join(repr(value) for value in values[:MAX_EXAMPLES])
    return shown + (f", ... ({len(values)} total)" if len(values) > MAX_EXAMPLES else "")


def check_table(artifact: dict, path: Path) -> tuple[list[str], list[str], dict]:
    severe: list[str] = []
    warnings: list[str] = []
    stats: dict = {}
    delimiter = "\t" if str(artifact.get("format") or path.suffix.lstrip(".")).lower() == "tsv" else ","
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        warnings.append("file starts with a UTF-8 byte-order mark; write plain UTF-8")
    if b"\r\n" in raw[:100_000]:
        warnings.append("file uses Windows (CRLF) newlines; the contract asks for Unix newlines")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        return [f"file is not valid UTF-8 ({error.reason} at byte {error.start})"], warnings, stats
    try:
        rows = list(csv.reader(text.splitlines(), delimiter=delimiter))
    except csv.Error as error:
        return [f"file does not parse as {'TSV' if delimiter == chr(9) else 'CSV'}: {error}"], warnings, stats
    rows = [row for row in rows if row]
    if not rows:
        return ["file is empty"], warnings, stats
    header = [name.strip() for name in rows[0]]
    body = rows[1:]
    stats.update({"rows": len(body), "columns": len(header)})
    if not body:
        severe.append("file has a header but no data rows")
    if len(header) == 1 and delimiter == "," and ("\t" in rows[0][0] or ";" in rows[0][0]):
        severe.append("header has a single column: the file does not look comma-separated")
    duplicate_columns = sorted({name for name in header if header.count(name) > 1})
    if duplicate_columns:
        severe.append(f"duplicate column names: {examples(duplicate_columns)}")
    if any(not name for name in header):
        warnings.append("header contains an empty column name")
    ragged = [index + 2 for index, row in enumerate(body) if len(row) != len(header)]
    if ragged:
        severe.append(f"{len(ragged)} row(s) have a different number of fields than the header "
                      f"(first at line {ragged[0]})")
        body = [row for row in body if len(row) == len(header)]

    required = list(artifact.get("required_columns") or [])
    missing = [name for name in required if name not in header]
    if missing:
        close = {name.lower(): name for name in header}
        hints = [f"{name} (found {close[name.lower()]!r})" for name in missing if name.lower() in close]
        severe.append(f"missing required column(s): {examples(missing)}"
                      + (f"; case differs for: {examples(hints)}" if hints else ""))

    row_key = artifact.get("row_key")
    if row_key:
        if row_key not in header:
            severe.append(f"row-key column `{row_key}` is missing")
        else:
            if header[0] != row_key:
                warnings.append(f"row-key column `{row_key}` is not the first column")
            position = header.index(row_key)
            keys = [row[position].strip() for row in body]
            empty = sum(1 for key in keys if key.lower() in MISSING)
            if empty:
                severe.append(f"{empty} row(s) have an empty `{row_key}`")
            seen: set[str] = set()
            duplicates: list[str] = []
            for key in keys:
                if key in seen and key not in duplicates:
                    duplicates.append(key)
                seen.add(key)
            if duplicates:
                severe.append(f"{len(duplicates)} duplicate `{row_key}` value(s): {examples(duplicates)}")

    data_cells = 0
    missing_cells = 0
    for position, name in enumerate(header):
        if name == row_key:
            continue
        values = [row[position].strip() for row in body]
        present = [value for value in values if value.lower() not in MISSING]
        data_cells += len(values)
        missing_cells += len(values) - len(present)
        if values and not present:
            warnings.append(f"column `{name}` contains only missing values")
            continue
        placeholders = sorted({value for value in present if value.lower() in PLACEHOLDERS})
        if placeholders:
            warnings.append(f"column `{name}` contains placeholder-like value(s): {examples(placeholders)}")
        numeric = [value for value in present if is_number(value)]
        probability = bool(PROBABILITY_NAME.search(name))
        if probability or (present and len(numeric) / len(present) >= NUMERIC_MAJORITY):
            bad = [value for value in present if not is_number(value)]
            if bad:
                message = f"column `{name}` has {len(bad)} non-numeric value(s): {examples(sorted(set(bad)))}"
                (severe if probability else warnings).append(message)
            infinite = [value for value in numeric if math.isinf(float(value))]
            if infinite:
                warnings.append(f"column `{name}` has {len(infinite)} infinite value(s)")
        if probability:
            outside = [value for value in numeric if not 0.0 <= float(value) <= 1.0]
            if outside:
                severe.append(f"column `{name}` has {len(outside)} value(s) outside [0, 1]: "
                              f"{examples(sorted(set(outside)))}")
    if body and data_cells and missing_cells == data_cells:
        severe.append("every non-key cell is missing (NA-only table)")
    return severe, warnings, stats


def validate(contract: dict, workspace: Path) -> dict:
    declared = parse_contract_from_prompt(contract.get("prompt", ""))
    results = []
    for output in contract.get("expected_outputs", []):
        artifact = {**declared.get(output["artifact_id"], {}), **{k: v for k, v in output.items() if v}}
        artifact.setdefault("required_columns", declared.get(output["artifact_id"], {}).get("required_columns", []))
        path = workspace / artifact["path"]
        entry = {"artifact_id": output["artifact_id"], "path": artifact["path"], "severe": [], "warnings": [], "stats": {}}
        if not path.is_file():
            entry["severe"].append("required output file does not exist")
        elif path.stat().st_size == 0:
            entry["severe"].append("required output file is empty (0 bytes)")
        elif str(artifact.get("format") or "").lower() in ("csv", "tsv") or path.suffix.lower() in (".csv", ".tsv"):
            entry["severe"], entry["warnings"], entry["stats"] = check_table(artifact, path)
        results.append(entry)
    methods = workspace / "outputs" / "METHODS.md"
    methods_entry = {"artifact_id": "METHODS.md", "path": "outputs/METHODS.md", "severe": [], "warnings": [], "stats": {}}
    if not methods.is_file() or methods.stat().st_size == 0:
        methods_entry["severe"].append("required file `outputs/METHODS.md` is missing or empty")
    else:
        text = methods.read_text(encoding="utf-8", errors="replace")
        absent = [o["artifact_id"] for o in contract.get("expected_outputs", []) if f"## {o['artifact_id']}" not in text]
        if absent:
            methods_entry["warnings"].append(f"no `## <artifact_id>` section for: {examples(absent)}")
    results.append(methods_entry)
    return {
        "results": results,
        "severe": [f"{r['path']}: {m}" for r in results for m in r["severe"]],
        "warnings": [f"{r['path']}: {m}" for r in results for m in r["warnings"]],
    }


def render(outcome: dict) -> str:
    lines = [
        "# Artifact contract validation",
        "",
        "Structure-only check of the required outputs against the contract in the task prompt.",
        "It does not assess whether values are scientifically correct.",
        "",
        f"- Severe structural problems: {len(outcome['severe'])}",
        f"- Warnings: {len(outcome['warnings'])}",
        "",
    ]
    for result in outcome["results"]:
        stats = result["stats"]
        shape = f" ({stats['rows']} rows x {stats['columns']} columns)" if stats else ""
        status = "SEVERE" if result["severe"] else ("WARNINGS" if result["warnings"] else "OK")
        lines.append(f"## `{result['path']}`: {status}{shape}")
        lines.append("")
        lines += [f"- SEVERE: {message}" for message in result["severe"]]
        lines += [f"- warning: {message}" for message in result["warnings"]]
        if not result["severe"] and not result["warnings"]:
            lines.append("- no structural problem found")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    arguments = parser.parse_args()
    contract = json.loads(arguments.contract.read_text(encoding="utf-8"))
    outcome = validate(contract, arguments.workspace)
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(render(outcome), encoding="utf-8", newline="\n")
    print(json.dumps({"severe": outcome["severe"], "warnings": outcome["warnings"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
