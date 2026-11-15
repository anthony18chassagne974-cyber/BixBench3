#!/bin/bash
# Jail test suite: run inside a Slurm job on Sherlock.  Usage: test_jail.sh <sandbox.sif> <bb3jail>
set -uo pipefail
SIF=$1; JAIL=$2
HERE=$(cd "$(dirname "$0")" && pwd)
W=$(mktemp -d "${L_SCRATCH_JOB:-/tmp}/jailtest.XXXX")
free_port() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])'; }
PP=$(free_port); HP=$(free_port)
python3 "$HERE/testproxy_tcp.py" "$PP" & PROXY=$!
python3 -m http.server "$HP" --bind 127.0.0.1 --directory "$W" >/dev/null 2>&1 & HOSTSRV=$!
mkdir -p "$W/overlay" "$W/jaildir" "$W/log" "$W/wd"
cp "$JAIL" "$W/jaildir/bb3jail"
printf '127.0.0.1 localhost\n127.0.0.1 network-gateway\n::1 localhost\n' > "$W/hosts"
printf 'APT::Sandbox::User "root";\nDir::Cache::Archives "/var/cache/apt/sherlock-archives/";\n' > "$W/apt.conf"  # as run_job.sbatch
NAME=bb3test-$$
apptainer instance start --containall --cleanenv --fakeroot --ignore-fakeroot-command \
  --overlay "$W/overlay" --workdir "$W/wd" --bind "$W/jaildir:/opt/bb3jail:ro" --bind "$W/log:/opt/bb3jail-log" \
  --bind "$W/hosts:/etc/hosts:ro" --bind "$W/apt.conf:/etc/apt/apt.conf.d/99-sherlock-fakeroot:ro" "$SIF" "$NAME" || { echo "FAIL instance start"; exit 1; }
sleep 2
J() { apptainer exec --cleanenv --pwd /tmp "instance://$NAME" /opt/bb3jail/bb3jail --proxy-port "$PP" --log /opt/bb3jail-log/jail.log -- "$@"; }
PASS=0; FAIL=0
check() { local name=$1 want=$2 got=$3; if [ "$got" = "$want" ]; then echo "PASS $name"; PASS=$((PASS+1)); else echo "FAIL $name (want $want got $got)"; FAIL=$((FAIL+1)); fi; }

out=$(J python3 -c 'import socket,errno
s=socket.socket()
try: s.connect(("1.1.1.1",443)); print("connected")
except OSError as e: print(errno.errorcode.get(e.errno))' 2>&1); check "direct TCP to internet blocked" ENETUNREACH "$out"
out=$(J python3 -c 'import socket,errno
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
try: s.sendto(b"x",("8.8.8.8",53)); print("sent")
except OSError as e: print(errno.errorcode.get(e.errno))' 2>&1); check "UDP sendto internet blocked" ENETUNREACH "$out"
out=$(J python3 -c 'import socket,errno
s=socket.socket(socket.AF_INET6)
try: s.connect(("2606:4700:4700::1111",443)); print("connected")
except OSError as e: print(errno.errorcode.get(e.errno))' 2>&1); case "$out" in *ENETUNREACH*|*"Errno 97"*) out=blocked;; esac; check "direct IPv6 blocked" blocked "$out"
J curl -sS -m 15 -o /dev/null https://pypi.org/simple/ >/dev/null 2>&1; check "curl without proxy fails" 1 $(( $? != 0 ))
code=$(J curl -sS -m 30 -o /dev/null -w '%{http_code}' -x "http://network-gateway:$PP" https://pypi.org/simple/six/ 2>&1); check "curl via proxy" 200 "$code"
J env HTTPS_PROXY="http://network-gateway:$PP" python3 -c 'import urllib.request;urllib.request.urlopen("https://pypi.org/simple/six/",timeout=30)' >/dev/null 2>&1; check "python urllib via proxy" 0 $?
J micromamba search -q --json -c conda-forge zlib >/dev/null 2>&1; check "static micromamba without proxy fails" 1 $(( $? != 0 ))
J env HTTPS_PROXY="http://network-gateway:$PP" HTTP_PROXY="http://network-gateway:$PP" micromamba search -q --json -c conda-forge zlib >/dev/null 2>&1; check "static micromamba via proxy" 0 $?
J sh -c 'cd /tmp && setsid nohup python3 -m http.server 8765 --bind 127.0.0.1 >/dev/null 2>&1 & echo started' >/dev/null; sleep 3
code=$(J curl -sS -m 10 -o /dev/null -w '%{http_code}' --noproxy '*' http://127.0.0.1:8765/ 2>&1); check "loopback server owned by sandbox reachable" 200 "$code"
out=$(J python3 -c "import socket,errno
s=socket.socket()
try: s.connect(('127.0.0.1',$HP)); print('connected')
except OSError as e: print(errno.errorcode.get(e.errno))" 2>&1); check "host loopback service not reachable" ECONNREFUSED "$out"
out=$(J python3 -c 'import socket
s=socket.socket(); s.bind(("0.0.0.0",0)); print(s.getsockname()[0])' 2>&1); check "wildcard bind rewritten to loopback" 127.0.0.1 "$out"
out=$(J python3 -c 'import ctypes,ctypes.util
libc=ctypes.CDLL(ctypes.util.find_library("c"),use_errno=True)
print(libc.ptrace(0,0,0,0), ctypes.get_errno())' 2>&1); check "ptrace denied" "-1 1" "$out"
out=$(J sh -c 'grep Seccomp: /proc/self/status | tr -d "\t "' 2>&1); check "seccomp filter active" "Seccomp:2" "$out"
start=$(date +%s); J sh -c 'sleep 120 >/dev/null 2>&1 & echo ok' >/dev/null; el=$(( $(date +%s) - start ))
check "exec returns while a daemon keeps running" 1 $(( el < 20 ))
tp=$(apptainer exec "instance://$NAME" sh -c 'for p in $(pgrep -x sleep); do grep TracerPid /proc/$p/status | tr -d "\t "; done' | head -1)
check "daemon stays traced" 1 $([ -n "$tp" ] && [ "$tp" != "TracerPid:0" ] && echo 1 || echo 0)
J pip download -q --no-deps --proxy "http://network-gateway:$PP" six -d /tmp/pipdl >/dev/null 2>&1; check "pip via proxy" 0 $?
code=$(J Rscript -e "Sys.setenv(https_proxy='http://network-gateway:$PP'); x <- tryCatch(readLines(url('https://cloud.r-project.org/src/contrib/PACKAGES'), n=1), error=function(e) 'ERR'); cat(ifelse(x=='ERR','ERR','OK'))" 2>/dev/null); check "R via proxy" OK "$code"
out=$(J apt-get -o Acquire::http::Timeout=10 -o Acquire::Retries=0 update 2>&1); case "$out" in *"Failed to fetch"*|*"Temporary failure"*|*"Could not"*) out=blocked;; esac; check "apt without proxy fails" blocked "$out"
J env http_proxy="http://network-gateway:$PP" sh -c 'DEBIAN_FRONTEND=noninteractive apt-get update -q && apt-get install -y -q --no-install-recommends libuv1-dev' >/dev/null 2>&1; check "apt-get install via proxy (fakeroot apt config)" 0 $?
echo "--- jail log"; tail -5 "$W/log/jail.log"
apptainer instance stop "$NAME" >/dev/null 2>&1
kill $PROXY $HOSTSRV 2>/dev/null
rm -rf "$W"
echo "jail tests: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
