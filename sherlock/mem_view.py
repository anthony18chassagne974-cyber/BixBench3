"""Write a /proc/meminfo view limited to this job's Slurm memory allocation.

Bound into the sandbox so memory-sizing code (free, R/Bioconductor and Python heuristics,
`psutil.virtual_memory()`) sees the job's limit, as the agent saw the VM's RAM on GCP, instead
of the whole node (128-384 GB on Sherlock) while the cgroup kills at the job limit.
The view is static: MemFree/MemAvailable report the full allocation.
Usage: mem_view.py <meminfo-out>   (reads SLURM_MEM_PER_NODE, in MB)
"""
import os
import sys

limit_kb = int(os.environ["SLURM_MEM_PER_NODE"]) * 1024
FULL = {"MemTotal", "MemFree", "MemAvailable", "CommitLimit"}
ZERO = {"Buffers", "Cached", "SwapCached", "SwapTotal", "SwapFree", "Shmem", "SReclaimable", "SUnreclaim",
        "Slab", "Active", "Inactive", "Active(anon)", "Inactive(anon)", "Active(file)", "Inactive(file)",
        "AnonPages", "Mapped", "Dirty", "Writeback", "Committed_AS", "KReclaimable"}
out = []
for line in open("/proc/meminfo", encoding="utf-8"):
    key, rest = line.split(":", 1)
    parts = rest.split()
    if len(parts) == 2 and parts[1] == "kB":
        value = int(parts[0])
        if key in FULL:
            value = limit_kb
        elif key in ZERO:
            value = 0
        else:
            value = min(value, limit_kb)
        line = f"{key + ':':<16}{value:>8} kB\n"
    out.append(line)
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    handle.writelines(out)
print(limit_kb // 1024 // 1024)
