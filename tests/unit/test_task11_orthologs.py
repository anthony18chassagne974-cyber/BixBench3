import gzip
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fetch_orth", ROOT / "env" / "task11" / "fetch_ensembl100_orthologs.py")
fo = importlib.util.module_from_spec(spec)
sys.modules["fetch_orth"] = fo
assert spec.loader is not None
spec.loader.exec_module(fo)

SQL = """
CREATE TABLE `mmusculus_gene_ensembl__gene__main` (
  `gene_id_1020_key` int(10) NOT NULL,
  `stable_id_1023` varchar(128) DEFAULT NULL,
  `display_label_1074` varchar(128) DEFAULT NULL
) ENGINE=MyISAM;
CREATE TABLE `hsapiens_gene_ensembl__gene__main` (
  `gene_id_1020_key` int(10) NOT NULL,
  `stable_id_1023` varchar(128) DEFAULT NULL,
  `display_label_1074` varchar(128) DEFAULT NULL
) ENGINE=MyISAM;
CREATE TABLE `mmusculus_gene_ensembl__homolog_hsapiens__dm` (
  `wga_coverage_4014` float DEFAULT NULL,
  `gene_id_1020_key` int(10) NOT NULL,
  `description_4014` varchar(25) DEFAULT NULL,
  `perc_id_4015_r1` float DEFAULT NULL,
  `stable_id_4016_r2` varchar(128) DEFAULT NULL,
  `stable_id_4016_r3` varchar(128) DEFAULT NULL,
  `stable_id_4016_r1` varchar(128) DEFAULT NULL,
  `display_label_40273_r1` varchar(128) DEFAULT NULL,
  `perc_id_4015` float DEFAULT NULL,
  `is_high_confidence_4014` tinyint(1) DEFAULT NULL
) ENGINE=MyISAM;
"""
FILES = {
    "ensembl_mart_100.sql.gz": gzip.compress(SQL.encode()),
    "mmusculus_gene_ensembl__gene__main.txt.gz": gzip.compress(
        b"1\tENSMUSG00000059552\tTrp53\n2\tENSMUSG00000025194\tAbcc2\n3\tENSMUSG00000099999\t\\N\n"),
    "hsapiens_gene_ensembl__gene__main.txt.gz": gzip.compress(
        b"10\tENSG00000141510\tTP53\n11\tENSG00000023839\tABCC2\n"),
    "mmusculus_gene_ensembl__homolog_hsapiens__dm.txt.gz": gzip.compress(
        b"99.1\t1\tortholog_one2one\t78.1\tENSP00000269305\tENSMUSP00000104298\tENSG00000141510\tTP53\t77.5\t1\n"
        b"98.0\t2\tortholog_one2one\t80.2\tENSP00000360869\tENSMUSP00000026208\tENSG00000023839\tABCC2\t81.0\t0\n"
        b"\\N\t3\t\\N\t\\N\t\\N\t\\N\t\\N\t\\N\t\\N\t\\N\n"),
}


def fake_fetch(url):
    if url == fo.BASE:
        return "".join(f'<a href="{n}">{n}</a>' for n in FILES).encode()
    return FILES[url[len(fo.BASE):]]


def test_rebuilds_biomart_style_table(monkeypatch, tmp_path):
    monkeypatch.setattr(fo, "fetch", fake_fetch)
    monkeypatch.setattr(sys, "argv", ["x", "--out", str(tmp_path)])
    assert fo.main() == 0
    lines = (tmp_path / "ensembl100_mmusculus_hsapiens_orthologs.tsv").read_text().splitlines()
    assert lines[0].split("\t")[:4] == ["mouse_gene_stable_id", "mouse_gene_name", "human_gene_stable_id", "human_gene_name"]
    assert lines[1].split("\t") == ["ENSMUSG00000059552", "Trp53", "ENSG00000141510", "TP53", "ortholog_one2one", "1", "77.5", "78.1"]
    assert len(lines) == 3  # gene without homolog dropped
    prov = json.loads((tmp_path / "ensembl100_orthologs.provenance.json").read_text())
    mapping = prov["column_mapping"]["homolog"]
    assert prov["ensembl_release"] == 100 and mapping["orthology_type"] == "description_4014"
    assert mapping["homolog_stable_id"] == "stable_id_4016_r1"   # detected from ENSG values, not from r2/r3 protein ids
    assert mapping["perc_id_r1"] == "perc_id_4015_r1" and mapping["confidence"] == "is_high_confidence_4014"
