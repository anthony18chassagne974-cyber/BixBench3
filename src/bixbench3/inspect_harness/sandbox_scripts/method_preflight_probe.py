"""Required-method preflight probe (experiment v2c).

Runs inside the analyst sandbox before the agent starts. Standard library only.
It observes which tools, versions and resources named in the task's method guidance
are available, and writes a Markdown report. It installs nothing, changes nothing
outside the report, and never calls the web-access broker.

Usage: python3 method_preflight_probe.py --spec SPEC.json --report REPORT.md
Prints a one-line JSON summary on stdout.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

R_TIMEOUT_SECONDS = 180
NETWORK_TIMEOUT_SECONDS = 25

OK = "OK"
MISSING = "MISSING"
VERSION_MISMATCH = "VERSION MISMATCH"
BLOCKED = "BLOCKED BY PROXY"
UNREACHABLE = "UNREACHABLE"
UNKNOWN = "NOT CHECKED"


def run(argv: list[str], timeout: int) -> tuple[int, str]:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, "command not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout} s"
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def probe_r(packages: list[dict]) -> tuple[dict, list[dict]]:
    """Return R runtime facts and one row per requested R package."""
    runtime = {"r_version": None, "bioconductor_version": None, "error": None}
    names = [str(package["name"]) for package in packages]
    if shutil.which("Rscript") is None:
        runtime["error"] = "Rscript is not on PATH"
        return runtime, [
            {**package, "found_version": None, "status": MISSING, "detail": "R is not available"}
            for package in packages
        ]
    vector = ", ".join(json.dumps(name) for name in names)
    code = (
        'cat("R_VERSION", as.character(getRversion()), "\\n");'
        'bioc <- tryCatch(as.character(BiocManager::version()), error = function(e) "NA");'
        'cat("BIOC_VERSION", bioc, "\\n");'
        "ip <- installed.packages()[, \"Version\"];"
        f"for (p in c({vector})) "
        'cat("PKG", p, if (p %in% names(ip)) ip[[p]] else "NA", "\\n")'
    )
    status, output = run(["Rscript", "--vanilla", "-e", code], R_TIMEOUT_SECONDS)
    found: dict[str, str | None] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "R_VERSION":
            runtime["r_version"] = parts[1]
        elif len(parts) >= 2 and parts[0] == "BIOC_VERSION":
            runtime["bioconductor_version"] = None if parts[1] == "NA" else parts[1]
        elif len(parts) >= 3 and parts[0] == "PKG":
            found[parts[1]] = None if parts[2] == "NA" else parts[2]
    if status != 0 and runtime["r_version"] is None:
        runtime["error"] = output[-400:] or f"Rscript exited with status {status}"
    rows = []
    for package in packages:
        version = found.get(str(package["name"]))
        required = package.get("required_version")
        if runtime["error"]:
            state, detail = UNKNOWN, "R probe failed"
        elif version is None:
            state, detail = MISSING, "not installed"
        elif required and version != required:
            state, detail = VERSION_MISMATCH, f"installed {version}, guidance names {required}"
        else:
            state, detail = OK, "installed"
        rows.append({**package, "found_version": version, "status": state, "detail": detail})
    return runtime, rows


def probe_commands(commands: list[dict]) -> list[dict]:
    rows = []
    for command in commands:
        path = shutil.which(str(command["name"]))
        rows.append({
            **command,
            "status": OK if path else MISSING,
            "detail": path or "not on PATH",
        })
    return rows


def probe_url(url: str) -> tuple[str, str]:
    """Probe one URL through the sandbox proxy with a tiny ranged GET."""
    request = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "bixbench3-preflight"})
    try:
        with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
            return OK, f"HTTP {response.status}"
    except urllib.error.HTTPError as error:
        if error.code in (403, 407):
            return BLOCKED, f"HTTP {error.code} from the allow-list proxy or host"
        if error.code in (400, 405, 416):
            return OK, f"host reachable (HTTP {error.code} to a probe request)"
        return UNREACHABLE, f"HTTP {error.code}"
    except urllib.error.URLError as error:
        reason = str(error.reason)
        if "403" in reason or "407" in reason or "Forbidden" in reason:
            return BLOCKED, f"proxy refused the connection ({reason})"
        return UNREACHABLE, reason[:200]
    except Exception as error:  # a probe must never abort the report
        return UNREACHABLE, f"{type(error).__name__}: {str(error)[:200]}"


def probe_network(resources: list[dict]) -> list[dict]:
    rows = []
    for resource in resources:
        state, detail = probe_url(str(resource["url"]))
        rows.append({**resource, "status": state, "detail": detail})
    return rows


def probe_staged(entries: list[dict], workspace: Path) -> list[dict]:
    rows = []
    for entry in entries:
        path = workspace / str(entry["path"])
        pattern = entry.get("glob")
        if pattern:
            count = len(list(path.glob(str(pattern)))) if path.is_dir() else 0
            expected = entry.get("expected_count")
            if not path.is_dir():
                state, detail = MISSING, "directory not found"
            elif expected is not None and count != expected:
                state, detail = VERSION_MISMATCH, f"{count} files match {pattern}, guidance names {expected}"
            else:
                state, detail = OK, f"{count} files match {pattern}"
        else:
            state, detail = (OK, "present") if path.exists() else (MISSING, "not found")
        rows.append({**entry, "status": state, "detail": detail})
    return rows


def cell(value: object) -> str:
    return str(value if value not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")


def table(headers: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)
    return lines


def render(spec: dict, runtime: dict, packages: list[dict], commands: list[dict],
           network: list[dict], staged: list[dict]) -> tuple[str, dict]:
    problems: list[str] = []
    lines = [
        "# Required-method preflight",
        "",
        "Generated by the harness before the analysis started. It only observes the sandbox:",
        "nothing was installed or downloaded for you. The requirements come from the task's",
        "Method Guidance. Results describe the state at start; re-check after you install things.",
        "",
        "## Rules",
        "",
        "1. For every row below that is not `OK`, **repair it first**: install the named tool and",
        "   version, or obtain access to the named resource (package manager first, then",
        "   `request_web_access` with the exact URL).",
        "2. **Do not silently substitute** another tool, version, database or mapping.",
        "3. A fallback is allowed only after a real repair attempt failed. Before using it, write",
        "   down the diagnosis: what you tried, the exact error, and why the fallback is the",
        "   closest available equivalent. Record this in `outputs/METHODS.md`.",
        "",
        "## R runtime",
        "",
    ]
    r_required = spec.get("r_runtime", {}).get("required", "-")
    if runtime.get("error"):
        lines.append(f"- R: **{MISSING}** ({cell(runtime['error'])}). Guidance names: {r_required}.")
        problems.append("R runtime")
    else:
        lines.append(f"- R version found: `{runtime.get('r_version')}`. Guidance names: {r_required}.")
        lines.append(f"- Bioconductor release configured: `{runtime.get('bioconductor_version') or 'unknown'}`.")
    lines += ["", "## R / Bioconductor packages", ""]
    lines += table(
        ["Package", "Used for", "Guidance version", "Found", "Status"],
        [[f"`{p['name']}`", p.get("purpose"), p.get("required_version"), p.get("found_version"), f"**{p['status']}**"]
         for p in packages],
    )
    problems += [f"R package {p['name']} ({p['status']})" for p in packages
                 if p["status"] != OK and not p.get("optional")]
    if commands:
        lines += ["", "## Command-line tools", ""]
        lines += table(["Tool", "Used for", "Status", "Detail"],
                       [[f"`{c['name']}`", c.get("purpose"), f"**{c['status']}**", c["detail"]] for c in commands])
        problems += [f"tool {c['name']} ({c['status']})" for c in commands
                     if c["status"] != OK and not c.get("optional")]
    lines += ["", "## External resources (probed through the sandbox proxy)", ""]
    lines += table(["Resource", "Used for", "URL", "Status", "Detail"],
                   [[n["name"], n.get("purpose"), f"`{n['url']}`", f"**{n['status']}**", n["detail"]] for n in network])
    lines += [
        "",
        f"`{BLOCKED}` means the host is not on the proxy allow-list yet: request the exact URL with",
        "`request_web_access` before concluding that the resource is unavailable.",
    ]
    problems += [f"resource {n['name']} ({n['status']})" for n in network
                 if n["status"] != OK and not n.get("optional")]
    lines += ["", "## Staged inputs named by the guidance", ""]
    lines += table(["Path", "Used for", "Status", "Detail"],
                   [[f"`{s['path']}`", s.get("purpose"), f"**{s['status']}**", s["detail"]] for s in staged])
    problems += [f"staged input {s['path']} ({s['status']})" for s in staged if s["status"] != OK]
    lines += ["", "## Summary", ""]
    if problems:
        lines.append(f"{len(problems)} requirement(s) need repair before the dependent analysis step:")
        lines.append("")
        lines += [f"- {problem}" for problem in problems]
    else:
        lines.append("All probed requirements are available.")
    lines.append("")
    summary = {
        "requirements_checked": len(packages) + len(commands) + len(network) + len(staged) + 1,
        "problems": problems,
        "r_version": runtime.get("r_version"),
        "bioconductor_version": runtime.get("bioconductor_version"),
    }
    return "\n".join(lines), summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--workspace", type=Path, default=Path("/workspace"))
    arguments = parser.parse_args()
    spec = json.loads(arguments.spec.read_text(encoding="utf-8"))
    runtime, packages = probe_r(list(spec.get("r_packages", [])))
    commands = probe_commands(list(spec.get("commands", [])))
    network = probe_network(list(spec.get("network", [])))
    staged = probe_staged(list(spec.get("staged_inputs", [])), arguments.workspace)
    report, summary = render(spec, runtime, packages, commands, network, staged)
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(report, encoding="utf-8", newline="\n")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
