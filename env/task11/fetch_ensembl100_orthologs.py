#!/usr/bin/env python3
"""Rebuild the Ensembl 100 mouse -> human ortholog table from the static BioMart dump.

Why: the live Ensembl 100 BioMart (https://apr2020.archive.ensembl.org/biomart/martservice,
the endpoint the task prompt asks biomaRt 2.44.1 to query) has been retired; it now
answers `301 -> https://www.ensembl.org/help/articles/archives`. The release-100
BioMart database itself is still published as a MySQL dump:
    https://ftp.ensembl.org/pub/release-100/mysql/ensembl_mart_100/
This script reads that dump (the same tables biomaRt queried) and writes the table
biomaRt would have returned for mouse genes with their human homologs.

Run at IMAGE BUILD time (network), never during a task run:
    python3 fetch_ensembl100_orthologs.py --inspect            # list tables/columns + sample rows
    python3 fetch_ensembl100_orthologs.py --out /opt/task-env/reference

Outputs (in --out):
    ensembl100_mmusculus_hsapiens_orthologs.tsv   one row per mouse gene x human homolog
    ensembl100_orthologs.provenance.json          source URLs, sha256, column mapping, row counts

Columns written (biomaRt attribute names in brackets):
    mouse_gene_stable_id      [ensembl_gene_id]
    mouse_gene_name           [external_gene_name]
    human_gene_stable_id      [hsapiens_homolog_ensembl_gene]
    human_gene_name           [hsapiens_homolog_associated_gene_name]
    orthology_type            [hsapiens_homolog_orthology_type]
    orthology_confidence      [hsapiens_homolog_orthology_confidence]
    perc_id                   [hsapiens_homolog_perc_id]
    perc_id_r1                [hsapiens_homolog_perc_id_r1]

Mart column names carry numeric suffixes (e.g. stable_id_1023). They are discovered
from the dump's .sql schema with the patterns below; if a pattern is ambiguous the
script stops and prints the candidates — rerun with the matching --col-* override.
Python stdlib only.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

BASE = "https://ftp.ensembl.org/pub/release-100/mysql/ensembl_mart_100/"
MOUSE_MAIN = "mmusculus_gene_ensembl__gene__main"
HUMAN_MAIN = "hsapiens_gene_ensembl__gene__main"
HOMOLOG_RE = re.compile(r"^mmusculus_gene_ensembl__homolog_hsap\w*__dm$")

# logical column -> regex over mart column names, per table
PATTERNS = {
    "main": {
        "key": r"^gene_id_\d+_key$",
        "stable_id": r"^stable_id_\d+$",
        "name": r"^display_label_\d+$",
    },
    "homolog": {
        "key": r"^gene_id_\d+_key$",
        # homolog_stable_id is chosen from the data (see detect_homolog_gene_column):
        # the dump has stable_id_<n>_r1/_r2/_r3 (gene / protein ids of the pair)
        "orthology_type": r"^description_\d+$",
        "confidence": r"^is_high_confidence_\d+$",
        "perc_id": r"^perc_id_\d+$",
        "perc_id_r1": r"^perc_id_\d+_r1$",          # Ensembl 100 dump: perc_id_4015_r1
        "homolog_name": r"^display_label_\d+_r1$",  # human gene name carried by the homolog table
    },
}


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "bixbench3-task11-env"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return resp.read()


def list_dir() -> list[str]:
    html = fetch(BASE).decode("utf-8", "replace")
    return sorted(set(re.findall(r'href="([^"?/][^"]*)"', html)))


def schema_columns(sql: str) -> dict[str, list[str]]:
    tables = {}
    for m in re.finditer(r"CREATE TABLE `([^`]+)` \((.*?)\n\)", sql, re.S):
        cols = re.findall(r"^\s*`([^`]+)`\s", m.group(2), re.M)
        tables[m.group(1)] = cols
    return tables


def read_table(name: str, columns: list[str]) -> tuple[list[dict], str]:
    raw = fetch(BASE + f"{name}.txt.gz")
    digest = hashlib.sha256(raw).hexdigest()
    text = gzip.decompress(raw).decode("utf-8", "replace")
    rows = []
    for line in text.splitlines():
        values = [None if v == r"\N" else v for v in line.split("\t")]
        rows.append(dict(zip(columns, values, strict=False)))
    return rows, digest


def detect_homolog_gene_column(rows: list[dict], columns: list[str]) -> tuple[str, dict]:
    """The human GENE id column is the stable_id_*_r* column whose values are ENSG ids."""
    candidates = [c for c in columns if re.match(r"^stable_id_\d+_r\d+$", c)]
    stats = {}
    for c in candidates:
        values = [r.get(c) for r in rows if r.get(c)]
        stats[c] = {"non_null": len(values),
                    "ENSG": sum(v.startswith("ENSG0") for v in values),
                    "examples": sorted(set(values))[:2]}
    best = max(candidates, key=lambda c: stats[c]["ENSG"], default=None)
    if not best or stats[best]["ENSG"] == 0:
        raise SystemExit(f"no stable_id column holds human ENSG gene ids: {stats}")
    return best, stats


def pick(columns: list[str], pattern: str, override: str | None, label: str, table: str) -> str | None:
    if override:
        if override not in columns:
            raise SystemExit(f"--col override {override!r} not in {table}: {columns}")
        return override
    hits = [c for c in columns if re.match(pattern, c)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        return None
    raise SystemExit(f"ambiguous column for {label} in {table}: {hits} — rerun with --col-{label.replace('_', '-')} <name>")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=Path("/opt/task-env/reference"))
    ap.add_argument("--inspect", action="store_true", help="print tables, columns and sample rows, write nothing")
    for logical in ("homolog-stable-id", "orthology-type", "confidence", "perc-id", "perc-id-r1"):
        ap.add_argument(f"--col-{logical}")
    args = ap.parse_args()

    files = list_dir()
    sql_name = next((f for f in files if re.fullmatch(r"ensembl_mart_100\.sql(\.gz)?", f)), None)
    if not sql_name:
        raise SystemExit(f"no ensembl_mart_100.sql(.gz) in {BASE}")
    raw_sql = fetch(BASE + sql_name)
    sql = (gzip.decompress(raw_sql) if sql_name.endswith(".gz") else raw_sql).decode("utf-8", "replace")
    tables = schema_columns(sql)
    homolog_tables = [t for t in tables if HOMOLOG_RE.match(t)]
    if len(homolog_tables) != 1:
        raise SystemExit(f"expected one mouse->human homolog table, found {homolog_tables}")
    homolog = homolog_tables[0]
    for needed in (MOUSE_MAIN, HUMAN_MAIN):
        if needed not in tables:
            raise SystemExit(f"{needed} missing from schema")

    if args.inspect:
        for t in (MOUSE_MAIN, homolog, HUMAN_MAIN):
            print(f"\n## {t}\n" + ", ".join(tables[t]))
        rows, _ = read_table(homolog, tables[homolog])
        with_homolog = [r for r in rows if any((r.get(c) or "").startswith("ENS") for c in tables[homolog] if c.startswith("stable_id"))]
        print(f"\n{homolog}: {len(rows)} rows, {len(with_homolog)} with a homolog; 3 examples:")
        for r in with_homolog[:3]:
            print({k: v for k, v in r.items() if v not in (None, "")})
        col, stats = detect_homolog_gene_column(rows, tables[homolog])
        print(f"\nhuman gene id column -> {col}")
        print(json.dumps(stats, indent=2))
        return 0

    over = {"orthology_type": args.col_orthology_type,
            "confidence": args.col_confidence, "perc_id": args.col_perc_id, "perc_id_r1": args.col_perc_id_r1}
    mcols = {k: pick(tables[MOUSE_MAIN], p, None, k, MOUSE_MAIN) for k, p in PATTERNS["main"].items()}
    hmcols = {k: pick(tables[HUMAN_MAIN], p, None, k, HUMAN_MAIN) for k, p in PATTERNS["main"].items()}
    hcols = {k: pick(tables[homolog], p, over.get(k), k, homolog) for k, p in PATTERNS["homolog"].items()}
    for label, mapping in (("mouse main", mcols), ("human main", hmcols)):
        missing = [k for k, v in mapping.items() if v is None]
        if missing:
            raise SystemExit(f"{label}: columns not found for {missing}; run --inspect")
    if hcols["key"] is None:
        raise SystemExit("homolog table: gene key column not found; run --inspect")

    mouse, mouse_sha = read_table(MOUSE_MAIN, tables[MOUSE_MAIN])
    human, human_sha = read_table(HUMAN_MAIN, tables[HUMAN_MAIN])
    homs, hom_sha = read_table(homolog, tables[homolog])
    if args.col_homolog_stable_id:
        hcols["homolog_stable_id"] = pick(tables[homolog], "", args.col_homolog_stable_id, "homolog_stable_id", homolog)
        detection = {"override": args.col_homolog_stable_id}
    else:
        hcols["homolog_stable_id"], detection = detect_homolog_gene_column(homs, tables[homolog])

    mouse_by_key = {r[mcols["key"]]: r for r in mouse}
    human_name = {r[hmcols["stable_id"]]: r[hmcols["name"]] for r in human}

    args.out.mkdir(parents=True, exist_ok=True)
    out_tsv = args.out / "ensembl100_mmusculus_hsapiens_orthologs.tsv"
    n = 0
    with out_tsv.open("w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["mouse_gene_stable_id", "mouse_gene_name", "human_gene_stable_id", "human_gene_name",
                    "orthology_type", "orthology_confidence", "perc_id", "perc_id_r1"])
        seen = set()
        for h in homs:
            hid = h.get(hcols["homolog_stable_id"])
            m = mouse_by_key.get(h.get(hcols["key"]))
            if not hid or not m or not hid.startswith("ENSG"):
                continue
            hname = human_name.get(hid) or (h.get(hcols["homolog_name"]) if hcols.get("homolog_name") else "") or ""
            row = (m[mcols["stable_id"]], m[mcols["name"]] or "", hid, hname,
                   h.get(hcols["orthology_type"]) or "" if hcols["orthology_type"] else "",
                   h.get(hcols["confidence"]) or "" if hcols["confidence"] else "",
                   h.get(hcols["perc_id"]) or "" if hcols["perc_id"] else "",
                   h.get(hcols["perc_id_r1"]) or "" if hcols["perc_id_r1"] else "")
            if row in seen:
                continue
            seen.add(row)
            w.writerow(row)
            n += 1

    provenance = {
        "ensembl_release": 100,
        "source": BASE,
        "reason": "Ensembl 100 archive BioMart (apr2020.archive.ensembl.org) retired: 301 -> /help/articles/archives",
        "tables": {MOUSE_MAIN: mouse_sha, HUMAN_MAIN: human_sha, homolog: hom_sha},
        "column_mapping": {"mouse_main": mcols, "human_main": hmcols, "homolog": hcols,
                           "homolog_gene_column_detection": detection},
        "rows": {"mouse_genes": len(mouse), "homolog_rows": len(homs), "written": n},
        "output_sha256": hashlib.sha256(out_tsv.read_bytes()).hexdigest(),
    }
    (args.out / "ensembl100_orthologs.provenance.json").write_text(json.dumps(provenance, indent=2))
    print(f"wrote {n} rows to {out_tsv}")
    print(json.dumps(provenance["column_mapping"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
