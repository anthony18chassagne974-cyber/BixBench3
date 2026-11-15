"""Export a readable transcript of a running (or finished) Sherlock run, one Markdown file per run.

Reads Inspect's live sample buffer (sqlite) of the job, read-only, and writes every agent
message, tool call and tool result in order, followed by the end of the Slurm log.
Secrets are redacted. Run on the compute node of the job (ssh <node> from a login node):

  python3 export_transcript.py <job-dir: $L_SCRATCH_JOB/bb3> <slurm-log> <out.md> [title]
"""
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

SECRET = [
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "<redacted>"),
    (re.compile(r"AIza[A-Za-z0-9_-]{10,}"), "<redacted>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._-]{16,}"), r"\1<redacted>"),
    (re.compile(r"(?i)((?:api_?key|token|secret|password)\s*[=:]\s*)\S+"), r"\1<redacted>"),
]
MAX_RESULT = 6000


def redact(text):
    text = "" if text is None else str(text)
    for pattern, repl in SECRET:
        text = pattern.sub(repl, text)
    return text


def clip(text, limit=MAX_RESULT):
    text = redact(text)
    return text if len(text) <= limit else text[:limit] + f"\n... [{len(text) - limit} more characters truncated]"


def content_text(content):
    if isinstance(content, str):
        return content
    parts = []
    for item in content or []:
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "reasoning":
                summary = item.get("summary") or ""
                if summary:
                    parts.append(f"[reasoning summary] {summary}")
    return "\n".join(p for p in parts if p)


def main():
    job_dir, slurm_log, out_path = sys.argv[1:4]
    title = sys.argv[4] if len(sys.argv) > 4 else os.path.basename(out_path)
    dbs = sorted(glob.glob(os.path.join(job_dir, "xdg/inspect_ai/samplebuffer/*/*.db")), key=os.path.getmtime)
    lines = [f"# {title}", "", f"Exported {datetime.now(timezone.utc).isoformat(timespec='seconds')} (run may still be in progress)", ""]
    manifest = os.path.join(job_dir, "run/run_manifest.json")
    if os.path.exists(manifest):
        m = json.load(open(manifest))
        lines += [f"- run_id: `{m.get('run_id')}`", f"- condition: {m.get('live_tracking', {}).get('condition')}",
                  f"- branch/commit: {m.get('checkout', {}).get('branch')} @ {m.get('checkout', {}).get('commit')}",
                  f"- platform: {json.dumps(m.get('platform'))}", f"- results: {m.get('retention', {}).get('results_uri')}", ""]
    if not dbs:
        lines += ["_No live sample buffer found (run not started yet, or already finished: use the .eval log in the results folder)._"]
    else:
        conn = sqlite3.connect(f"file:{dbs[-1]}?mode=ro", uri=True, timeout=10)
        rows = conn.execute(
            "SELECT e.data FROM events e JOIN (SELECT event_id, MAX(id) AS id FROM events GROUP BY event_id) last "
            "ON last.id = e.id ORDER BY e.id"
        ).fetchall()
        conn.close()
        n_model = n_tool = 0
        lines += ["## Transcript", ""]
        for (raw,) in rows:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            kind = ev.get("event")
            ts = (ev.get("timestamp") or "")[:19]
            if kind == "model" and not ev.get("pending"):
                n_model += 1
                choices = (ev.get("output") or {}).get("choices") or []
                msg = choices[0].get("message", {}) if choices else {}
                text = content_text(msg.get("content"))
                usage = (ev.get("output") or {}).get("usage") or {}
                lines.append(f"### [{ts}] agent (model call {n_model}, out {usage.get('output_tokens', '?')} tok)")
                if text:
                    lines += ["", clip(text, 20000)]
                for call in msg.get("tool_calls") or []:
                    args = call.get("arguments")
                    lines += ["", f"**tool call `{call.get('function')}`**", "```", clip(json.dumps(args, indent=1) if not isinstance(args, str) else args, 8000), "```"]
                lines.append("")
            elif kind == "tool" and not ev.get("pending"):
                n_tool += 1
                result = ev.get("result")
                if isinstance(result, list):
                    result = content_text(result)
                err = ev.get("error")
                lines += [f"#### [{ts}] tool result `{ev.get('function')}`" + (" (ERROR)" if err else ""), "```",
                          clip(result if not err else f"{err}\n{result or ''}"), "```", ""]
        lines.insert(4, f"- model calls: {n_model}, tool results: {n_tool}")
    if os.path.exists(slurm_log):
        tail = open(slurm_log, errors="replace").read().splitlines()[-60:]
        lines += ["## End of Slurm job log", "```", clip("\n".join(tail), 20000), "```"]
    with open(out_path, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    print(out_path, os.path.getsize(out_path))


if __name__ == "__main__":
    main()
