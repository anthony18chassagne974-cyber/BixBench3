#!/usr/bin/env python3
"""Generate fake snapshots for 2 runs (baseline + swarm) to try the dashboard offline."""
import json, math, random, sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

out = Path(sys.argv[1] if len(sys.argv) > 1 else "demo_data")
start = datetime(2026, 11, 14, 22, 11, 48, tzinfo=UTC)
arts = ["filtered_counts", "normalized_counts", "deg_table", "pathway_enrichment", "bmd_table", "mouse_human_map"]

def make(run_id, desc, cond, hours, speed, quality, cost_rate, baseline=None, roles=("react",)):
    d = out / run_id; d.mkdir(parents=True, exist_ok=True)
    meta = {"run_id": run_id, "task_id": "11", "paper_id": "10.1016_j.crtox.2021.01.003_v1",
            "model": "openai/gpt-5.4-mini", "effort": "xhigh", "condition": cond, "description": desc,
            "baseline_run_id": baseline, "vm": "arb-run-demo", "time_limit_s": 86400,
            "started_at": start.isoformat(), "deadline": "2026-11-15T22:00:00+00:00",
            "progress": {"label": "Échantillons Tempo-seq comptés", "glob": "counts/*", "total": 85}}
    hist = []; random.seed(run_id)
    for k in range(0, int(hours * 12) + 1):
        t = k * 300
        present = min(len(arts), int(speed * t / 3600))
        score = None if present == 0 else round(quality * present / len(arts) * (0.9 + 0.1 * math.sin(k)), 3)
        hist.append({"ts": (start + timedelta(seconds=t)).isoformat(), "elapsed_s": t, "phase": "agent_running",
                     "artifacts_present": present, "partial_score": score, "cost_usd": round(cost_rate * t / 3600, 3),
                     "n_model_calls": k * 3, "n_tool_calls": k * 5, "tokens_total": k * 400000,
                     "progress_done": min(85, int(85 * t / 3600 / 1.6))})
    last = hist[-1]
    timeline = [{"elapsed_s": h["elapsed_s"], "n_agents": 1 if len(roles) == 1 else random.choice([1, 2, 2, 3, len(roles)])} for h in hist]
    agents = [{"agent": r, "calls": random.randint(20, 200), "tokens": random.randint(1, 9) * 1_000_000,
               "cost_usd": round(random.uniform(0.2, 1.5), 2), "first_ts": hist[0]["ts"], "last_ts": last["ts"]} for r in roles]
    def checks(i):
        exp = random.randint(150, 1200); tp = int(exp * random.uniform(0.6, 1.0)); fp = int(exp * random.uniform(0, 1.2 if i == 3 else 0.4))
        prec, rec = tp / max(1, tp + fp), tp / exp
        f1 = 2 * prec * rec / max(1e-9, prec + rec); ccc = random.uniform(0.45, 0.999)
        return [{"metric": "f1", "axis": "rows", "score": round(f1, 3), "details": {"expected_count": exp, "observed_count": tp + fp,
                 "true_positive": tp, "false_positive": fp, "false_negative": exp - tp, "precision": round(prec, 4), "recall": round(rec, 4)}},
                {"metric": "ccc", "axis": "columns", "score": round(ccc, 3), "details": {"row_intersection_count": tp, "missing_requested_columns": []}}]
    latest = {**last, "status": "running", "artifacts_total": len(arts), "score_is_final": False,
              "tokens": {"input": 9_000_000, "cached": 30_000_000, "output": 1_200_000, "reasoning": 700_000},
              "idle_s": 240, "progress": {"label": "Échantillons Tempo-seq comptés", "done": last["progress_done"], "total": 85},
              "eta": {"remaining_s": 5 * 3600, "method": "artifact completion rate", "time_limit_left_s": 86400 - last["elapsed_s"]},
              "resources": {"load1": 3.2, "ncpu": 8, "mem_used_gb": 6.1, "mem_total_gb": 31.3, "disk_used_gb": 9.0, "disk_total_gb": 193},
              "last_tool": {"ts": last["ts"], "function": "bash_session", "args": "Rscript deseq2_contrasts.R --design ~dose"},
              "last_message": {"ts": last["ts"], "text": "Counts matrix done (85 samples). Running DESeq2 per chemical vs vehicle."},
              "agents": agents, "agent_timeline": timeline, "artifacts": [{"artifact_id": a, "path": f"outputs/{a}.csv", "present": i < last["artifacts_present"],
                             "mtime": last["ts"] if i < last["artifacts_present"] else None,
                             "score": round(quality * (0.7 + 0.3 * random.random()), 3) if i < last["artifacts_present"] else None,
                             "checks": checks(i) if i < last["artifacts_present"] else None}
                            for i, a in enumerate(arts)]}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    (d / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2))
    (d / "history.jsonl").write_text("\n".join(json.dumps(h) for h in hist) + "\n")

make("demo__baseline", "Baseline : harness Inspect d'origine, GPT-5.4 mini xhigh", "baseline", 3.5, 1.0, 0.45, 1.8)
make("demo__swarm-v1", "Swarm v1 : proposer/experimenter/critic, validation de chaque nœud du DAG", "swarm-v1", 2.5, 1.3, 0.62, 2.4, baseline="demo__baseline", roles=("planner", "experimenter", "critic", "contract_checker"))
print("demo data in", out)
