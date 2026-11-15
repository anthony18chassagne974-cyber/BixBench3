#!/usr/bin/env bash
# Install the bixdash collector on a BixBench3 run VM.
#
# Safe on an ACTIVE run: it only adds files under /opt/bixdash and
# /var/lib/bixdash plus a separate systemd timer (bixdash-collect.timer).
# It does not touch /opt/bixbench3, /var/lib/bixbench3 or bixbench3-run.service.
#
# Usage (on the VM, from the folder containing collect.py):
#   sudo bash install_collector.sh \
#     --description "Baseline : harness Inspect d'origine, GPT-5.4 mini xhigh" \
#     --condition baseline \
#     [--baseline-run-id <run_id>] \
#     [--progress-label "Échantillons Tempo-seq comptés" --progress-glob "counts/*" --progress-total 85]
set -euo pipefail

DESCRIPTION=""; CONDITION="baseline"; BASELINE=""; PLABEL=""; PGLOB=""; PTOTAL=""
INTERVAL="5min"; GRADE_EVERY=900
while [ $# -gt 0 ]; do
  case "$1" in
    --description) DESCRIPTION="$2"; shift 2;;
    --condition) CONDITION="$2"; shift 2;;
    --baseline-run-id) BASELINE="$2"; shift 2;;
    --progress-label) PLABEL="$2"; shift 2;;
    --progress-glob) PGLOB="$2"; shift 2;;
    --progress-total) PTOTAL="$2"; shift 2;;
    --interval) INTERVAL="$2"; shift 2;;
    --grade-every) GRADE_EVERY="$2"; shift 2;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
install -d -m 0755 /opt/bixdash
install -m 0755 "$HERE/collect.py" /opt/bixdash/collect.py
install -d -m 0700 /var/lib/bixdash

python3 - "$DESCRIPTION" "$CONDITION" "$BASELINE" "$PLABEL" "$PGLOB" "$PTOTAL" <<'PY'
import json, sys
from pathlib import Path
desc, cond, base, plabel, pglob, ptotal = sys.argv[1:]
path = Path("/var/lib/bixdash/run_meta.json")
meta = json.loads(path.read_text()) if path.exists() else {}
if desc: meta["description"] = desc
if cond: meta["condition"] = cond
if base: meta["baseline_run_id"] = base
if pglob:
    meta["progress"] = {"label": plabel or pglob, "glob": pglob,
                        "total": int(ptotal) if ptotal else None}
path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
print("run_meta.json:", json.dumps(meta, ensure_ascii=False))
PY

cat >/etc/systemd/system/bixdash-collect.service <<UNIT
[Unit]
Description=bixdash read-only snapshot of the BixBench3 run
[Service]
Type=oneshot
Nice=19
IOSchedulingClass=idle
Environment=PATH=/usr/local/bin:/usr/bin:/bin:/root/.local/bin:/root/.cargo/bin
ExecStart=/usr/bin/python3 /opt/bixdash/collect.py --grade-every ${GRADE_EVERY}
TimeoutStartSec=1200
UNIT

cat >/etc/systemd/system/bixdash-collect.timer <<UNIT
[Unit]
Description=bixdash snapshot every ${INTERVAL}
[Timer]
OnBootSec=1min
OnUnitActiveSec=${INTERVAL}
AccuracySec=30s
[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now bixdash-collect.timer
echo "Installed. First snapshot:"
systemctl start bixdash-collect.service || true
journalctl -u bixdash-collect.service -n 5 --no-pager
