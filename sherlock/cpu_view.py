"""Write /proc/cpuinfo and /sys/devices/system/cpu/online views limited to this job's CPUs.

Bound into the sandbox so core-counting code (R detectCores, Python os.cpu_count, BiocParallel
defaults) sees the job's 6 CPUs, as it did on the 6-vCPU GCP VM, instead of the whole node.
Usage: cpu_view.py <cpuinfo-out> <online-out>
"""
import os
import sys

n = len(os.sched_getaffinity(0))
blocks = [b for b in open("/proc/cpuinfo", encoding="utf-8").read().strip().split("\n\n") if b.strip()]
out = []
for i, block in enumerate(blocks[:n]):
    lines = []
    for line in block.split("\n"):
        key = line.split(":", 1)[0].rstrip("\t ")
        if key == "processor":
            line = f"processor\t: {i}"
        elif key in ("cpu cores", "siblings"):
            line = f"{key}\t: {n}"
        elif key in ("core id", "apicid", "initial apicid"):
            line = f"{key}\t: {i}"
        elif key == "physical id":
            line = f"{key}\t: 0"
        lines.append(line)
    out.append("\n".join(lines))
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    handle.write("\n\n".join(out) + "\n\n")
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    handle.write(f"0-{n - 1}\n")
print(n)
