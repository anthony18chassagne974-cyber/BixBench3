"""Input / sample manifest auditor (experiment v2d).

Runs inside the analyst sandbox before the agent starts. Standard library only.
It inventories the staged raw files and metadata tables, links raw files to samples,
summarises the experimental groups and replicate counts, and flags missing, extra or
duplicate sample identifiers. Everything is derived from the staged inputs and the
task prompt; it uses no grader or reference output and changes no scientific method.

Usage:
  python3 input_manifest_audit.py --data DATA_DIR --prompt PROMPT.md \
      --report REPORT.md --samples-table SAMPLES.tsv
Prints a one-line JSON summary on stdout.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

RAW_SUFFIXES = (".fastq.gz", ".fq.gz", ".fastq", ".fq", ".bam", ".cram", ".sra")
TABLE_SUFFIXES = (".tsv", ".csv", ".txt")
MAX_TABLE_BYTES = 2_000_000
CONTROL_WORDS = {"control", "ctrl", "vehicle", "untreated", "mock", "dmso", "wt", "wildtype", "wild-type"}
MAX_LISTED = 40


def raw_stem(name: str) -> str:
    for suffix in RAW_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def natural_key(value: str) -> tuple:
    return tuple(int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value))


def discover_raw(data: Path) -> list[dict]:
    files = []
    for path in sorted(data.rglob("*")):
        if path.is_file() and path.name.endswith(RAW_SUFFIXES):
            files.append({
                "relative": path.relative_to(data).as_posix(),
                "name": path.name,
                "stem": raw_stem(path.name),
                "bytes": path.stat().st_size,
            })
    return files


def read_table(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    delimiter = "\t" if lines[0].count("\t") >= lines[0].count(",") and "\t" in lines[0] else ","
    rows = list(csv.reader(lines, delimiter=delimiter))
    header = [column.strip() for column in rows[0]]
    if len(header) < 2 or len(set(header)) != len(header):
        return None
    body = [row for row in rows[1:] if len(row) == len(header)]
    if not body:
        return None
    columns = {name: [row[index].strip() for row in body] for index, name in enumerate(header)}
    return {"header": header, "columns": columns, "rows": len(body), "skipped_rows": len(rows) - 1 - len(body)}


def discover_tables(data: Path) -> tuple[dict[str, dict], list[dict]]:
    tables: dict[str, dict] = {}
    skipped: list[dict] = []
    for path in sorted(data.rglob("*")):
        if not path.is_file() or not path.name.endswith(TABLE_SUFFIXES):
            continue
        relative = path.relative_to(data).as_posix()
        size = path.stat().st_size
        if size > MAX_TABLE_BYTES:
            skipped.append({"relative": relative, "reason": f"large reference table ({size} bytes), not parsed"})
            continue
        table = read_table(path)
        if table is None:
            skipped.append({"relative": relative, "reason": "not a rectangular delimited table"})
        else:
            tables[relative] = table
    return tables, skipped


def unique_columns(table: dict) -> list[str]:
    return [
        name for name, values in table["columns"].items()
        if all(values) and len(set(values)) == len(values)
    ]


def design_columns(table: dict) -> tuple[list[str], dict[str, str]]:
    """Low-cardinality columns that vary are design factors; single-valued ones are constants."""
    rows = table["rows"]
    factors, constants = [], {}
    for name, values in table["columns"].items():
        levels = set(values)
        if len(levels) == 1:
            constants[name] = values[0]
        elif 2 <= len(levels) <= max(2, rows // 2) and max(len(value) for value in levels) <= 40:
            factors.append(name)
    return factors, constants


def choose_sample_table(tables: dict[str, dict]) -> str | None:
    best, best_score = None, (-1, -1, -1)
    for relative, table in tables.items():
        if not unique_columns(table):
            continue
        factors, _ = design_columns(table)
        score = (len(factors) > 0, "sample" in Path(relative).name.lower(), len(factors))
        if score > best_score:
            best, best_score = relative, score
    return best


def best_link(values_by_column: dict[str, list[str]], targets: set[str]) -> tuple[str | None, int]:
    best, best_hits = None, 0
    for name, values in values_by_column.items():
        hits = len(targets & set(values))
        if hits > best_hits:
            best, best_hits = name, hits
    return best, best_hits


def link_files_to_samples(raw: list[dict], tables: dict[str, dict], sample_table: str, sample_id: str) -> dict:
    """Map each raw file to a sample, directly or through one bridging table."""
    stems = {entry["stem"] for entry in raw}
    samples = tables[sample_table]["columns"]
    sample_ids = samples[sample_id]
    direct, direct_hits = best_link(samples, stems)
    if direct and direct_hits >= max(1, len(stems) // 2):
        mapping = dict(zip(samples[direct], sample_ids, strict=True))
        return {"via": f"`{sample_table}` column `{direct}`", "file_to_sample": mapping}
    best: dict | None = None
    for relative, table in tables.items():
        if relative == sample_table:
            continue
        run_column, run_hits = best_link(table["columns"], stems)
        if not run_column or run_hits < max(1, len(stems) // 2):
            continue
        for bridge_column, bridge_values in table["columns"].items():
            if bridge_column == run_column:
                continue
            for sample_column, sample_values in samples.items():
                hits = len(set(bridge_values) & set(sample_values))
                if hits and (best is None or hits + run_hits > best["score"]):
                    to_sample = dict(zip(sample_values, sample_ids, strict=True))
                    best = {
                        "score": hits + run_hits,
                        "via": (f"`{relative}` column `{run_column}` -> `{bridge_column}` = "
                                f"`{sample_table}` column `{sample_column}`"),
                        "file_to_sample": {
                            run: to_sample[bridge]
                            for run, bridge in zip(table["columns"][run_column], bridge_values, strict=True)
                            if bridge in to_sample
                        },
                    }
    if best is None:
        return {"via": None, "file_to_sample": {}}
    return {"via": best["via"], "file_to_sample": best["file_to_sample"]}


def is_control(level: str) -> bool:
    lowered = level.strip().lower()
    if lowered in CONTROL_WORDS:
        return True
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*[a-z%/µμ]*", lowered)
    return bool(match) and float(match.group(1)) == 0.0


def infer_contrasts(samples: dict[str, list[str]], factors: list[str]) -> dict | None:
    for treatment in factors:
        levels = sorted(set(samples[treatment]), key=natural_key)
        controls = [level for level in levels if is_control(level)]
        if len(controls) != 1:
            continue
        strata = [name for name in factors if name != treatment]
        rows = []
        index = range(len(samples[treatment]))
        stratum_of = [tuple(samples[name][i] for name in strata) for i in index]
        for stratum in sorted(set(stratum_of), key=lambda value: tuple(natural_key(part) for part in value)):
            members = [i for i in index if stratum_of[i] == stratum]
            control_n = sum(1 for i in members if samples[treatment][i] == controls[0])
            for level in levels:
                if level == controls[0]:
                    continue
                treated_n = sum(1 for i in members if samples[treatment][i] == level)
                if treated_n:
                    rows.append({"stratum": stratum, "treated": level, "treated_n": treated_n,
                                 "control": controls[0], "control_n": control_n})
        return {"treatment": treatment, "control": controls[0], "strata": strata, "rows": rows}
    return None


def prompt_facts(prompt: str, known_ids: set[str]) -> dict:
    ticked = re.findall(r"`([^`\s]+)`", prompt)
    listed = [token for token in ticked if token in known_ids]
    unknown_like = []
    if listed:
        shape = re.sub(r"\d", "0", listed[0])
        unknown_like = [token for token in ticked if token not in known_ids and re.sub(r"\d", "0", token) == shape]
    counts = []
    for match in re.finditer(r"\b(\d{1,5})\s+((?:[\w/-]+\s+){0,5}?)(FASTQs?|samples|libraries|runs)\b", prompt):
        counts.append({"count": int(match.group(1)), "phrase": " ".join(match.group(0).split())})
    return {"listed_ids": listed, "listed_but_unknown": unknown_like, "counts": counts}


def cell(value: object) -> str:
    return str(value if value not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")


def table_lines(headers: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)
    return lines


def listing(values: list[str]) -> str:
    shown = ", ".join(f"`{value}`" for value in values[:MAX_LISTED])
    return shown + (f", ... ({len(values) - MAX_LISTED} more)" if len(values) > MAX_LISTED else "")


def audit(data: Path, prompt: str) -> tuple[str, list[list[str]], dict]:
    raw = discover_raw(data)
    tables, skipped = discover_tables(data)
    flags: list[str] = []
    lines = [
        "# Input manifest audit",
        "",
        "Generated by the harness before the analysis started, from the staged files under `./data`",
        "and the task prompt only. The inferences below are heuristic: check them against the",
        "Method Guidance, which takes precedence. Resolve every item under **Flags** before analysis.",
        "",
        "## Raw files",
        "",
    ]
    by_directory: dict[str, list[dict]] = defaultdict(list)
    for entry in raw:
        by_directory[str(Path(entry["relative"]).parent.as_posix())].append(entry)
    if not raw:
        lines.append("No raw sequencing files were found under `./data`.")
        flags.append("no raw sequencing files found")
    for directory, entries in sorted(by_directory.items()):
        sizes = [entry["bytes"] for entry in entries]
        lines.append(
            f"- `data/{directory}`: {len(entries)} file(s), {sum(sizes) / 1e9:.2f} GB total, "
            f"smallest {min(sizes) / 1e6:.1f} MB, largest {max(sizes) / 1e6:.1f} MB."
        )
    empty = [entry["name"] for entry in raw if entry["bytes"] == 0]
    if empty:
        flags.append(f"{len(empty)} empty raw file(s): {listing(empty)}")
    duplicate_stems = sorted(stem for stem, count in Counter(entry["stem"] for entry in raw).items() if count > 1)
    if duplicate_stems:
        flags.append(f"raw file names repeated in several directories: {listing(duplicate_stems)}")
    paired = sorted({re.sub(r"(_R?[12])$", "", entry["stem"]) for entry in raw if re.search(r"_R?[12]$", entry["stem"])})
    lines.append(f"- Read layout from file names: {'paired-end mates detected' if paired else 'no `_1/_2` or `_R1/_R2` mates detected (single-end naming)'}.")

    lines += ["", "## Metadata tables", ""]
    lines += table_lines(
        ["Table", "Rows", "Columns"],
        [[f"`data/{relative}`", table["rows"], ", ".join(f"`{name}`" for name in table["header"])]
         for relative, table in tables.items()],
    )
    for entry in skipped:
        lines.append(f"- `data/{entry['relative']}`: {entry['reason']}.")
    for relative, table in tables.items():
        if table["skipped_rows"]:
            flags.append(f"`data/{relative}`: {table['skipped_rows']} row(s) with an unexpected number of fields")

    summary: dict = {"raw_files": len(raw), "tables": len(tables), "flags": flags}
    sample_rows: list[list[str]] = []
    sample_table = choose_sample_table(tables)
    if sample_table is None:
        lines += ["", "## Samples", "", "No table with a unique sample identifier column was found."]
        flags.append("no sample table could be identified")
        return finish(lines, flags), sample_rows, summary

    samples = tables[sample_table]["columns"]
    id_columns = unique_columns(tables[sample_table])
    known_by_column = {name: set(samples[name]) for name in id_columns}
    facts_by_column = {name: prompt_facts(prompt, known) for name, known in known_by_column.items()}
    sample_id = max(id_columns, key=lambda name: (len(facts_by_column[name]["listed_ids"]), -id_columns.index(name)))
    facts = facts_by_column[sample_id]
    sample_ids = samples[sample_id]
    factors, constants = design_columns(tables[sample_table])
    factors = [name for name in factors if name not in id_columns]

    lines += ["", "## Samples", ""]
    lines.append(f"- Sample table: `data/{sample_table}` ({len(sample_ids)} rows).")
    lines.append(f"- Sample identifier column: `{sample_id}` (unique). Other unique columns: "
                 f"{', '.join(f'`{name}`' for name in id_columns if name != sample_id) or 'none'}.")
    lines.append(f"- Varying design columns: {', '.join(f'`{name}`' for name in factors) or 'none found'}.")
    if constants:
        lines.append("- Constant columns: " + "; ".join(f"`{name}` = {value}" for name, value in constants.items()) + ".")

    link = link_files_to_samples(raw, tables, sample_table, sample_id)
    file_to_sample = link["file_to_sample"]
    files_by_sample: dict[str, list[str]] = defaultdict(list)
    unlinked = []
    for entry in raw:
        sample = file_to_sample.get(entry["stem"])
        if sample is None:
            unlinked.append(entry["name"])
        else:
            files_by_sample[sample].append(entry["relative"])
    lines += ["", "## Raw files linked to samples", ""]
    if link["via"]:
        lines.append(f"- Link used: {link['via']}.")
        lines.append(f"- {len(raw) - len(unlinked)} of {len(raw)} raw files map to a sample; "
                     f"{len(files_by_sample)} of {len(sample_ids)} samples have at least one raw file.")
    else:
        lines.append("- No column linking raw file names to sample identifiers was found.")
        flags.append("raw files could not be linked to sample identifiers")
    without_files = sorted((s for s in sample_ids if s not in files_by_sample), key=natural_key) if link["via"] else []
    several = sorted((s for s, paths in files_by_sample.items() if len(paths) > 1), key=natural_key)
    if unlinked:
        flags.append(f"{len(unlinked)} raw file(s) not linked to any sample: {listing(sorted(unlinked))}")
    if without_files:
        flags.append(f"{len(without_files)} sample(s) in the metadata without a raw file: {listing(without_files)}")
    if several and not paired:
        flags.append(f"{len(several)} sample(s) with more than one raw file: {listing(several)}")

    lines += ["", "## Expectations stated in the prompt", ""]
    for item in facts["counts"]:
        lines.append(f"- \"{item['phrase']}\"")
    stated = sorted({item["count"] for item in facts["counts"]})
    if facts["listed_ids"]:
        listed = list(dict.fromkeys(facts["listed_ids"]))
        lines.append(f"- The prompt lists {len(listed)} identifiers that match `{sample_id}`.")
        missing = sorted(set(sample_ids) - set(listed), key=natural_key)
        if missing:
            flags.append(f"{len(missing)} sample(s) in the metadata are not listed in the prompt: {listing(missing)}")
        if facts["listed_but_unknown"]:
            flags.append("identifier(s) listed in the prompt but absent from the metadata: "
                         + listing(sorted(set(facts["listed_but_unknown"]), key=natural_key)))
        duplicated = sorted(value for value, count in Counter(facts["listed_ids"]).items() if count > 1)
        if len(duplicated) > len(listed) // 2:
            duplicated = []  # identifiers legitimately repeated across prompt sections
        if duplicated:
            flags.append(f"identifier(s) listed more than once in one place of the prompt: {listing(duplicated)}")
    elif not facts["counts"]:
        lines.append("- The prompt states no explicit sample count or sample list.")
    for count in stated:
        if count not in (len(sample_ids), len(raw)):
            lines.append(f"- Note: the prompt mentions {count}, which equals neither the {len(sample_ids)} "
                         f"metadata samples nor the {len(raw)} raw files; check what it refers to.")
    if stated and len(sample_ids) not in stated and len(raw) not in stated:
        flags.append("no count stated in the prompt matches the number of metadata samples or raw files")

    lines += ["", "## Groups and replicates", ""]
    if factors:
        groups = Counter(tuple(samples[name][i] for name in factors) for i in range(len(sample_ids)))
        ordered = sorted(groups, key=lambda value: tuple(natural_key(part) for part in value))
        lines += table_lines([*factors, "samples"], [[*group, groups[group]] for group in ordered])
        sizes = Counter(groups.values())
        lines.append("")
        lines.append("- Group sizes: " + ", ".join(f"{count} group(s) with n={size}" for size, count in sorted(sizes.items())) + ".")
        small = [" / ".join(group) for group in ordered if groups[group] < 3]
        if small:
            flags.append(f"group(s) with fewer than 3 replicates: {listing(small)}")
        if len(sizes) > 1:
            lines.append("- The design is unbalanced: do not assume the same replicate count in every group.")
        summary["groups"] = len(groups)
    else:
        lines.append("No varying design column was found in the sample table.")

    contrasts = infer_contrasts(samples, factors) if factors else None
    lines += ["", "## Contrasts inferable from the metadata", ""]
    if contrasts:
        strata = contrasts["strata"]
        lines.append(f"`{contrasts['treatment']}` has one control-like level (`{contrasts['control']}`). "
                     f"Treated levels versus that control, within each level of "
                     f"{', '.join(f'`{name}`' for name in strata) or 'the whole cohort'}:")
        lines.append("")
        lines += table_lines(
            [*strata, "treated level", "n treated", "control level", "n control"],
            [[*row["stratum"], row["treated"], row["treated_n"], row["control"], row["control_n"]]
             for row in contrasts["rows"]],
        )
        lines.append("")
        lines.append(f"- {len(contrasts['rows'])} treated-versus-control contrast(s). Use the contrasts the Method "
                     "Guidance asks for; this table only shows what the metadata supports.")
        no_control = [row for row in contrasts["rows"] if row["control_n"] == 0]
        if no_control:
            flags.append(f"{len(no_control)} contrast(s) have no matched control sample")
        summary["contrasts"] = len(contrasts["rows"])
    else:
        lines.append("No single control-like level could be identified, so no contrast table is proposed.")

    duplicate_ids = sorted(value for value, count in Counter(sample_ids).items() if count > 1)
    if duplicate_ids:
        flags.append(f"duplicate sample identifiers: {listing(duplicate_ids)}")

    header = [sample_id, *[name for name in id_columns if name != sample_id], *factors, "raw_files"]
    sample_rows.append(header)
    for index in sorted(range(len(sample_ids)), key=lambda i: natural_key(sample_ids[i])):
        row = [samples[name][index] for name in header[:-1]]
        row.append(";".join(f"data/{path}" for path in sorted(files_by_sample.get(sample_ids[index], []))))
        sample_rows.append(row)
    summary.update({"samples": len(sample_ids), "sample_id_column": sample_id, "linked_files": len(raw) - len(unlinked)})
    return finish(lines, flags), sample_rows, summary


def finish(lines: list[str], flags: list[str]) -> str:
    lines += ["", "## Flags", ""]
    if flags:
        lines += [f"- {flag}" for flag in flags]
    else:
        lines.append("None: sample identifiers, raw files and groups are mutually consistent.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--prompt", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--samples-table", required=True, type=Path)
    arguments = parser.parse_args()
    prompt = arguments.prompt.read_text(encoding="utf-8") if arguments.prompt.is_file() else ""
    report, sample_rows, summary = audit(arguments.data, prompt)
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    if sample_rows:
        with arguments.samples_table.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, delimiter="\t", lineterminator="\n").writerows(sample_rows)
        report = report.replace(
            "\n## Flags\n",
            f"\n## Sample table for scripts\n\nOne row per sample with its design columns and raw files: "
            f"`./work/{arguments.samples_table.name}` (tab-separated).\n\n## Flags\n",
        )
        summary["samples_table"] = arguments.samples_table.name
    arguments.report.write_text(report, encoding="utf-8", newline="\n")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
