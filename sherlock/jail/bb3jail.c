/*
 * bb3jail: network jail for the BixBench3 sandbox on Sherlock.
 *
 * Sherlock compute nodes cannot create network namespaces, so the Docker
 * "internal network" of the GCP sandbox is reproduced with seccomp + ptrace:
 *
 *   - a seccomp filter (inherited by every descendant, irrevocable) traps
 *     connect/bind/sendto(with address)/sendmsg/sendmmsg to this tracer, denies
 *     ptrace and process_vm_* (no tampering with other processes), and kills
 *     non-x86_64 / x32 syscalls;
 *   - this tracer inspects the socket address of every trapped call and only
 *     allows:   the gateway proxy (127.0.0.1:<proxy-port>),
 *               loopback ports on which a process of THIS container listens
 *               (same mount namespace: Sherlock has no PID namespaces),
 *               filesystem Unix sockets, and abstract Unix sockets owned by
 *               this container;
 *     everything else fails (ENETUNREACH for remote hosts, ECONNREFUSED for
 *     foreign loopback ports). Binds to the wildcard address are rewritten to
 *     loopback so nothing in the sandbox is reachable from the cluster network.
 *   - signals (kill/tkill/tgkill/rt_sigqueueinfo/rt_tgsigqueueinfo/pidfd_open)
 *     are trapped too: without a PID namespace the sandbox can see the host
 *     processes of the run (Inspect, gateway, collector), which run as the same
 *     user, and a `pkill python` would kill the evaluation. A signal to a
 *     process outside the container fails with ESRCH, as if it did not exist
 *     (what Docker's PID namespace gives); process-group and kill(-1) signals
 *     are delivered only to container processes. Apptainer's own helpers
 *     (starter, appinit, squashfuse, fuse-overlayfs) and bb3jail are never
 *     signalled by group/broadcast kills.
 *
 * This works for static binaries (micromamba, Go tools) too, since it acts on
 * syscalls rather than libc. If the tracer dies, PTRACE_O_EXITKILL kills the
 * traced processes and trapped syscalls fail with ENOSYS: the jail fails closed.
 *
 * Usage: bb3jail --proxy-port N [--log FILE] -- command [args...]
 * Process layout: the jail process M forks the command C and a tracer D. M waits
 * for C and returns its exit status (so `exec` returns when the command does),
 * while D keeps tracing any daemonized descendants until none are left.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <netinet/in.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/ptrace.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <sys/un.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifndef PTRACE_SEIZE
#define PTRACE_SEIZE 0x4206
#endif
#ifndef PTRACE_LISTEN
#define PTRACE_LISTEN 0x4208
#endif
#ifndef PTRACE_EVENT_STOP
#define PTRACE_EVENT_STOP 128
#endif
#ifndef PTRACE_EVENT_SECCOMP
#define PTRACE_EVENT_SECCOMP 7
#endif
#ifndef PTRACE_O_TRACESECCOMP
#define PTRACE_O_TRACESECCOMP (1 << PTRACE_EVENT_SECCOMP)
#endif
#ifndef PTRACE_O_EXITKILL
#define PTRACE_O_EXITKILL (1 << 20)
#endif
#ifndef PR_SET_PTRACER
#define PR_SET_PTRACER 0x59616d61
#endif
#ifndef PR_SET_PTRACER_ANY
#define PR_SET_PTRACER_ANY ((unsigned long)-1)
#endif
#ifndef __WALL
#define __WALL 0x40000000
#endif

#ifndef __NR_pidfd_open
#define __NR_pidfd_open 434
#endif

#define JAIL_TAG 0xB3
#define HANDLED (-1) /* decide(): syscall emulated by the tracer, return 0 to the caller */
#define X32_BIT 0x40000000

static int proxy_port = -1;
static FILE *logf;
static unsigned char *seen;
static long pid_max = 4194304;

/* ------------------------------------------------------------------ seccomp */
static int install_filter(void) {
    /* index: instruction (jump offsets are relative to the next instruction) */
    struct sock_filter f[] = {
        /*  0 */ BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
        /*  1 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0),
        /*  2 */ BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL),
        /*  3 */ BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        /*  4 */ BPF_JUMP(BPF_JMP | BPF_JGE | BPF_K, X32_BIT, 0, 1),
        /*  5 */ BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL),
        /*  6 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_connect, 13, 0),            /* -> 20 */
        /*  7 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_bind, 12, 0),               /* -> 20 */
        /*  8 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_sendmsg, 11, 0),            /* -> 20 */
        /*  9 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_sendmmsg, 10, 0),           /* -> 20 */
        /* 10 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_kill, 9, 0),                /* -> 20 */
        /* 11 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_tkill, 8, 0),               /* -> 20 */
        /* 12 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_tgkill, 7, 0),              /* -> 20 */
        /* 13 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_rt_sigqueueinfo, 6, 0),     /* -> 20 */
        /* 14 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_rt_tgsigqueueinfo, 5, 0),   /* -> 20 */
        /* 15 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_pidfd_open, 4, 0),          /* -> 20 */
        /* 16 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_sendto, 4, 0),              /* -> 21 */
        /* 17 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_ptrace, 8, 0),              /* -> 26 */
        /* 18 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_process_vm_writev, 7, 0),   /* -> 26 */
        /* 19 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_process_vm_readv, 6, 7),    /* -> 26 / 27 */
        /* 20 */ BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_TRACE | JAIL_TAG),
        /* 21 */ BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[4])),
        /* 22 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, 0, 0, 2),                        /* -> 23 / 25 */
        /* 23 */ BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[4]) + 4),
        /* 24 */ BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, 0, 2, 0),                        /* -> 27 / 25 */
        /* 25 */ BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_TRACE | JAIL_TAG),
        /* 26 */ BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        /* 27 */ BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog prog = {.len = (unsigned short)(sizeof f / sizeof f[0]), .filter = f};
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) return -1;
    return prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &prog);
}

/* --------------------------------------------------------------- utilities */
static void jlog(pid_t pid, const char *what, const char *detail) {
    if (!logf) return;
    time_t now = time(NULL);
    char ts[32];
    strftime(ts, sizeof ts, "%Y-%m-%dT%H:%M:%SZ", gmtime(&now));
    fprintf(logf, "%s pid=%d %s %s\n", ts, (int)pid, what, detail);
    fflush(logf);
}

static int peek(pid_t pid, unsigned long addr, void *buf, size_t len) {
    struct iovec local = {buf, len}, remote = {(void *)addr, len};
    return process_vm_readv(pid, &local, 1, &remote, 1, 0) == (ssize_t)len ? 0 : -1;
}

static int poke(pid_t pid, unsigned long addr, const void *buf, size_t len) {
    struct iovec local = {(void *)buf, len}, remote = {(void *)addr, len};
    return process_vm_writev(pid, &local, 1, &remote, 1, 0) == (ssize_t)len ? 0 : -1;
}

/*
 * Is a socket inode open in some process of this container? Sherlock disables PID
 * namespaces, so container membership is decided by the mount namespace: every process
 * of the sandbox instance shares it, while host processes (Inspect, the gateway, other
 * runs) do not.
 */
static int same_container(const char *pid) {
    static char mine[64];
    char path[64], ns[64];
    if (!mine[0]) {
        ssize_t n = readlink("/proc/self/ns/mnt", mine, sizeof mine - 1);
        if (n <= 0) return 0;
        mine[n] = 0;
    }
    snprintf(path, sizeof path, "/proc/%s/ns/mnt", pid);
    ssize_t n = readlink(path, ns, sizeof ns - 1);
    if (n <= 0) return 0;
    ns[n] = 0;
    return strcmp(ns, mine) == 0;
}

static int inode_in_container(unsigned long *inodes, int count) {
    if (count == 0) return 0;
    DIR *proc = opendir("/proc");
    if (!proc) return 0;
    struct dirent *p;
    int found = 0;
    char path[300], link[600], target[64];
    while (!found && (p = readdir(proc))) {
        if (!isdigit((unsigned char)p->d_name[0])) continue;
        if (!same_container(p->d_name)) continue;
        snprintf(path, sizeof path, "/proc/%s/fd", p->d_name);
        DIR *fds = opendir(path);
        if (!fds) continue;
        struct dirent *f;
        while (!found && (f = readdir(fds))) {
            if (f->d_name[0] == '.') continue;
            snprintf(link, sizeof link, "%s/%s", path, f->d_name);
            ssize_t n = readlink(link, target, sizeof target - 1);
            if (n <= 8) continue;
            target[n] = 0;
            unsigned long ino;
            if (sscanf(target, "socket:[%lu]", &ino) != 1) continue;
            for (int i = 0; i < count; i++)
                if (inodes[i] == ino) { found = 1; break; }
        }
        closedir(fds);
    }
    closedir(proc);
    return found;
}

/* Inodes of sockets bound to local port `port` (TCP: listening only). */
static int port_inodes(const char *table, int port, int listen_only, unsigned long *out, int max, int count) {
    FILE *fp = fopen(table, "r");
    if (!fp) return count;
    char line[512];
    if (!fgets(line, sizeof line, fp)) { fclose(fp); return count; }
    while (count < max && fgets(line, sizeof line, fp)) {
        char local[128];
        unsigned state;
        unsigned long ino;
        /* sl local rem st tx:rx tr:when retrnsmt uid timeout inode */
        if (sscanf(line, " %*d: %127s %*s %x %*s %*s %*s %*d %*d %lu", local, &state, &ino) != 3) continue;
        char *colon = strrchr(local, ':');
        if (!colon) continue;
        int p = (int)strtol(colon + 1, NULL, 16);
        if (p != port) continue;
        if (listen_only && state != 0x0A) continue;
        out[count++] = ino;
    }
    fclose(fp);
    return count;
}

static int container_owns_port(int port) {
    unsigned long inodes[64];
    int n = 0;
    n = port_inodes("/proc/net/tcp", port, 1, inodes, 64, n);
    n = port_inodes("/proc/net/tcp6", port, 1, inodes, 64, n);
    n = port_inodes("/proc/net/udp", port, 0, inodes, 64, n);
    n = port_inodes("/proc/net/udp6", port, 0, inodes, 64, n);
    return inode_in_container(inodes, n);
}

static int container_owns_abstract(const char *name, size_t len) {
    /* /proc/net/unix shows abstract names with a leading '@' (NULs also as '@'). */
    char want[sizeof(((struct sockaddr_un *)0)->sun_path) + 1];
    if (len == 0 || len > sizeof want - 1) return 0;
    for (size_t i = 0; i < len; i++) want[i] = name[i] ? name[i] : '@';
    want[len] = 0;
    FILE *fp = fopen("/proc/net/unix", "r");
    if (!fp) return 0;
    char line[512];
    unsigned long inodes[64];
    int n = 0;
    if (fgets(line, sizeof line, fp)) {
        while (n < 64 && fgets(line, sizeof line, fp)) {
            unsigned long ino;
            char path[256] = "";
            if (sscanf(line, "%*s %*s %*s %*s %*s %*s %lu %255s", &ino, path) < 1) continue;
            if (strcmp(path, want) == 0) inodes[n++] = ino;
        }
    }
    fclose(fp);
    return inode_in_container(inodes, n);
}

static int is_v4_loopback(uint32_t host_order) {
    return (host_order >> 24) == 127 || host_order == 0;
}

/*
 * Decide a destination address. Returns 0 to allow, or a positive errno.
 * `for_bind`: rewrite wildcard to loopback (via *rewrite) and reject other non-loopback.
 */
static int decide_addr(pid_t pid, unsigned long addr, unsigned long len, int for_bind, const char *call) {
    if (!addr || len < sizeof(sa_family_t)) return 0;
    struct sockaddr_storage ss;
    memset(&ss, 0, sizeof ss);
    size_t n = len < sizeof ss ? len : sizeof ss;
    if (peek(pid, addr, &ss, n) != 0) return EFAULT;
    char detail[200];
    if (ss.ss_family == AF_INET) {
        if (n < sizeof(struct sockaddr_in)) return EINVAL;
        struct sockaddr_in *a = (struct sockaddr_in *)&ss;
        uint32_t ip = ntohl(a->sin_addr.s_addr);
        int port = ntohs(a->sin_port);
        char text[INET_ADDRSTRLEN];
        inet_ntop(AF_INET, &a->sin_addr, text, sizeof text);
        if (for_bind) {
            if (ip == 0) {
                a->sin_addr.s_addr = htonl(INADDR_LOOPBACK);
                if (poke(pid, addr, a, sizeof *a) != 0) return EACCES;
                return 0;
            }
            if ((ip >> 24) == 127) return 0;
            snprintf(detail, sizeof detail, "%s %s:%d", call, text, port);
            jlog(pid, "deny", detail);
            return EADDRNOTAVAIL;
        }
        if (is_v4_loopback(ip)) {
            if (port == proxy_port || container_owns_port(port)) return 0;
            snprintf(detail, sizeof detail, "%s %s:%d (loopback port not owned by sandbox)", call, text, port);
            jlog(pid, "deny", detail);
            return ECONNREFUSED;
        }
        snprintf(detail, sizeof detail, "%s %s:%d", call, text, port);
        jlog(pid, "deny", detail);
        return ENETUNREACH;
    }
    if (ss.ss_family == AF_INET6) {
        if (n < sizeof(struct sockaddr_in6)) return EINVAL;
        struct sockaddr_in6 *a = (struct sockaddr_in6 *)&ss;
        int port = ntohs(a->sin6_port);
        const unsigned char *b = a->sin6_addr.s6_addr;
        static const unsigned char v4map[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff};
        int any = IN6_IS_ADDR_UNSPECIFIED(&a->sin6_addr);
        int loop = IN6_IS_ADDR_LOOPBACK(&a->sin6_addr) || any ||
                   (memcmp(b, v4map, 12) == 0 && (b[12] == 127 || (b[12] | b[13] | b[14] | b[15]) == 0));
        char text[INET6_ADDRSTRLEN];
        inet_ntop(AF_INET6, &a->sin6_addr, text, sizeof text);
        if (for_bind) {
            if (any) {
                a->sin6_addr = in6addr_loopback;
                if (poke(pid, addr, a, sizeof *a) != 0) return EACCES;
                return 0;
            }
            if (loop) return 0;
            snprintf(detail, sizeof detail, "%s [%s]:%d", call, text, port);
            jlog(pid, "deny", detail);
            return EADDRNOTAVAIL;
        }
        if (loop) {
            if (port == proxy_port || container_owns_port(port)) return 0;
            snprintf(detail, sizeof detail, "%s [%s]:%d (loopback port not owned by sandbox)", call, text, port);
            jlog(pid, "deny", detail);
            return ECONNREFUSED;
        }
        snprintf(detail, sizeof detail, "%s [%s]:%d", call, text, port);
        jlog(pid, "deny", detail);
        return ENETUNREACH;
    }
    if (ss.ss_family == AF_UNIX) {
        struct sockaddr_un *u = (struct sockaddr_un *)&ss;
        size_t path_len = n > offsetof(struct sockaddr_un, sun_path) ? n - offsetof(struct sockaddr_un, sun_path) : 0;
        if (for_bind || path_len == 0 || u->sun_path[0] != 0) return 0; /* filesystem sockets: container-scoped */
        if (container_owns_abstract(u->sun_path + 1, path_len - 1)) return 0;
        jlog(pid, "deny", "abstract unix socket not owned by sandbox");
        return ECONNREFUSED;
    }
    return 0; /* AF_UNSPEC (UDP disconnect), AF_NETLINK (kernel), ... */
}

static int decide_msghdr(pid_t pid, unsigned long msg_addr, const char *call) {
    struct msghdr m;
    if (peek(pid, msg_addr, &m, sizeof m) != 0) return EFAULT;
    if (!m.msg_name || m.msg_namelen == 0) return 0;
    return decide_addr(pid, (unsigned long)m.msg_name, m.msg_namelen, 0, call);
}

/* ------------------------------------------------------------------ signals */
static int in_container(long pid) {
    char buf[24];
    if (pid <= 0) return 0;
    snprintf(buf, sizeof buf, "%ld", pid);
    return same_container(buf);
}

/* Apptainer plumbing and the jail itself: never hit by group/broadcast signals. */
static int protected_proc(long pid) {
    static const char *names[] = {"starter", "starter-suid", "appinit", "squashfuse", "squashfuse_ll",
                                  "fuse-overlayfs", "fuse2fs", "bb3jail", NULL};
    char path[64], comm[64] = "";
    snprintf(path, sizeof path, "/proc/%ld/comm", pid);
    FILE *fp = fopen(path, "r");
    if (!fp) return 0;
    if (fgets(comm, sizeof comm, fp)) comm[strcspn(comm, "\n")] = 0;
    fclose(fp);
    for (int i = 0; names[i]; i++)
        if (!strcmp(comm, names[i])) return 1;
    return 0;
}

static long proc_field(long pid, const char *file, const char *key) {
    char path[64], line[256];
    snprintf(path, sizeof path, "/proc/%ld/%s", pid, file);
    FILE *fp = fopen(path, "r");
    if (!fp) return -1;
    long value = -1;
    size_t klen = strlen(key);
    while (fgets(line, sizeof line, fp))
        if (!strncmp(line, key, klen)) { value = strtol(line + klen, NULL, 10); break; }
    fclose(fp);
    return value;
}

/* Process state letter from /proc/<pid>/stat ('Z' = zombie; 0 if gone). */
static char proc_state(long pid) {
    char path[64], buf[512];
    snprintf(path, sizeof path, "/proc/%ld/stat", pid);
    FILE *fp = fopen(path, "r");
    if (!fp) return 0;
    size_t n = fread(buf, 1, sizeof buf - 1, fp);
    fclose(fp);
    buf[n] = 0;
    char *p = strrchr(buf, ')');
    char state = 0;
    if (!p || sscanf(p + 1, " %c", &state) != 1) return 0;
    return state;
}

static long proc_pgid(long pid) {
    char path[64], buf[512];
    snprintf(path, sizeof path, "/proc/%ld/stat", pid);
    FILE *fp = fopen(path, "r");
    if (!fp) return -1;
    size_t n = fread(buf, 1, sizeof buf - 1, fp);
    fclose(fp);
    buf[n] = 0;
    char *p = strrchr(buf, ')'); /* comm may contain spaces */
    long ppid, pgid;
    char state;
    if (!p || sscanf(p + 1, " %c %ld %ld", &state, &ppid, &pgid) != 3) return -1;
    return pgid;
}

/*
 * May a single pid be signalled? Yes if it is a container process, a zombie, or gone (the
 * kernel then answers ESRCH itself). Host processes are hidden from the sandbox (/proc is
 * mounted hidepid=2 in the fakeroot user namespace) but still signallable by pid: a pid
 * that exists without being a visible container process is refused.
 */
static int target_allowed(long pid) {
    if (in_container(pid)) return 1;
    if (proc_state(pid) == 'Z') return 1;
    return kill((pid_t)pid, 0) != 0 && errno == ESRCH;
}

/*
 * kill(pid, sig) semantics restricted to the container. Returns 0 to let the kernel run
 * the call unchanged, HANDLED when the tracer delivered it itself, or a positive errno.
 */
static int decide_kill(pid_t caller, long target, int sig, const char *call) {
    char detail[160];
    if (target > 0) {
        if (target_allowed(target)) return 0;
        snprintf(detail, sizeof detail, "%s pid %ld sig %d (process outside the sandbox)", call, target, sig);
        jlog(caller, "deny", detail);
        return ESRCH;
    }
    /*
     * Process-group and broadcast signals are always delivered by the tracer, one by one, to
     * the visible container processes: a host process in the same group (e.g. the host-side
     * `apptainer exec` client) would be invisible here, so the native call is never safe.
     */
    long caller_tgid = proc_field(caller, "status", "Tgid:");
    long pgid = target == 0 ? proc_pgid(caller) : -target; /* target == -1: broadcast */
    if (target != -1 && pgid <= 0) return ESRCH;
    int delivered = 0, n = 0;
    DIR *proc = opendir("/proc");
    if (!proc) return EPERM;
    struct dirent *d;
    while ((d = readdir(proc))) {
        if (!isdigit((unsigned char)d->d_name[0])) continue;
        long p = strtol(d->d_name, NULL, 10);
        if (target == -1 ? (p == 1 || p == caller_tgid || p == getpid()) : proc_pgid(p) != pgid) continue;
        if (!in_container(p) || protected_proc(p)) continue;
        n++;
        if (sig == 0 || kill((pid_t)p, sig) == 0) delivered = 1;
    }
    closedir(proc);
    if (target == -1) {
        snprintf(detail, sizeof detail, "%s -1 sig %d: delivered to %d container process(es) only", call, sig, n);
        jlog(caller, "limit", detail);
    }
    return delivered ? HANDLED : ESRCH;
}

static int decide_thread(pid_t caller, long tgid_or_tid, int sig, const char *call) {
    if (tgid_or_tid > 0 && target_allowed(tgid_or_tid)) return 0;
    char detail[160];
    snprintf(detail, sizeof detail, "%s %ld sig %d (process outside the sandbox)", call, tgid_or_tid, sig);
    jlog(caller, "deny", detail);
    return tgid_or_tid > 0 ? ESRCH : EINVAL;
}

static int decide(pid_t pid, struct user_regs_struct *r) {
    long nr = (long)r->orig_rax;
    /* pid arguments are pid_t (int): sign-extend the low 32 bits */
    if (nr == __NR_kill) return decide_kill(pid, (long)(int)r->rdi, (int)r->rsi, "kill");
    if (nr == __NR_rt_sigqueueinfo) return decide_kill(pid, (long)(int)r->rdi, (int)r->rsi, "rt_sigqueueinfo");
    if (nr == __NR_tkill) return decide_thread(pid, (long)(int)r->rdi, (int)r->rsi, "tkill");
    if (nr == __NR_tgkill) return decide_thread(pid, (long)(int)r->rdi, (int)r->rdx, "tgkill");
    if (nr == __NR_rt_tgsigqueueinfo) return decide_thread(pid, (long)(int)r->rdi, (int)r->rdx, "rt_tgsigqueueinfo");
    if (nr == __NR_pidfd_open) return decide_thread(pid, (long)(int)r->rdi, 0, "pidfd_open");
    if (nr == __NR_connect) return decide_addr(pid, r->rsi, r->rdx, 0, "connect");
    if (nr == __NR_bind) return decide_addr(pid, r->rsi, r->rdx, 1, "bind");
    if (nr == __NR_sendto) return decide_addr(pid, r->r8, r->r9, 0, "sendto");
    if (nr == __NR_sendmsg) return decide_msghdr(pid, r->rsi, "sendmsg");
    if (nr == __NR_sendmmsg) {
        unsigned long vlen = r->rdx > 1024 ? 1024 : r->rdx;
        for (unsigned long i = 0; i < vlen; i++) {
            int e = decide_msghdr(pid, r->rsi + i * sizeof(struct mmsghdr), "sendmmsg");
            if (e) return e;
        }
        return 0;
    }
    return 0;
}

/* ------------------------------------------------------------------ tracer */
static void mark(pid_t pid, int value) {
    if (pid > 0 && pid <= pid_max) seen[pid] = (unsigned char)value;
}
static int is_seen(pid_t pid) { return pid > 0 && pid <= pid_max && seen[pid]; }

static int trace_loop(void) {
    for (;;) {
        int st;
        pid_t pid = waitpid(-1, &st, __WALL);
        if (pid < 0) {
            if (errno == EINTR) continue;
            return 0; /* ECHILD: nothing left to trace */
        }
        if (WIFEXITED(st) || WIFSIGNALED(st)) { mark(pid, 0); continue; }
        if (!WIFSTOPPED(st)) continue;
        int sig = WSTOPSIG(st);
        int event = (unsigned)st >> 16;
        if (event == PTRACE_EVENT_SECCOMP) {
            unsigned long msg = 0;
            ptrace(PTRACE_GETEVENTMSG, pid, 0, &msg);
            struct user_regs_struct r;
            if (msg == JAIL_TAG && ptrace(PTRACE_GETREGS, pid, 0, &r) == 0) {
                int e = decide(pid, &r);
                if (e) {
                    r.orig_rax = (unsigned long long)-1; /* skip the syscall */
                    r.rax = e == HANDLED ? 0 : (unsigned long long)(-(long long)e);
                    ptrace(PTRACE_SETREGS, pid, 0, &r);
                }
            }
            ptrace(PTRACE_CONT, pid, 0, 0);
            continue;
        }
        if (event == PTRACE_EVENT_STOP) {
            int group = sig == SIGSTOP || sig == SIGTSTP || sig == SIGTTIN || sig == SIGTTOU;
            if (group && is_seen(pid)) ptrace(PTRACE_LISTEN, pid, 0, 0);
            else { mark(pid, 1); ptrace(PTRACE_CONT, pid, 0, 0); }
            continue;
        }
        mark(pid, 1);
        if (event) { ptrace(PTRACE_CONT, pid, 0, 0); continue; } /* fork/vfork/clone/exec */
        ptrace(PTRACE_CONT, pid, 0, sig);                       /* signal delivery */
    }
}

/* -------------------------------------------------------------------- main */
static pid_t child_pid;
static void forward(int sig) { if (child_pid > 0) kill(child_pid, sig); }

int main(int argc, char **argv) {
    int i = 1;
    const char *log_path = NULL;
    for (; i < argc; i++) {
        if (!strcmp(argv[i], "--proxy-port") && i + 1 < argc) proxy_port = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--log") && i + 1 < argc) log_path = argv[++i];
        else if (!strcmp(argv[i], "--")) { i++; break; }
        else break;
    }
    if (i >= argc || proxy_port <= 0) {
        fprintf(stderr, "usage: bb3jail --proxy-port N [--log FILE] -- command [args...]\n");
        return 125;
    }
    FILE *pm = fopen("/proc/sys/kernel/pid_max", "r");
    if (pm) { if (fscanf(pm, "%ld", &pid_max) != 1) pid_max = 4194304; fclose(pm); }

    int go[2];
    if (pipe2(go, O_CLOEXEC) != 0) { perror("bb3jail: pipe"); return 125; }

    child_pid = fork();
    if (child_pid < 0) { perror("bb3jail: fork"); return 125; }
    if (child_pid == 0) {
        close(go[1]);
        prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0, 0, 0); /* no-op without Yama */
        char ok = 0;
        if (read(go[0], &ok, 1) != 1 || ok != 1) _exit(126); /* tracer failed: never run untraced */
        close(go[0]);
        if (install_filter() != 0) { perror("bb3jail: seccomp"); _exit(126); }
        execvp(argv[i], argv + i);
        fprintf(stderr, "bb3jail: exec %s: %s\n", argv[i], strerror(errno));
        _exit(127);
    }
    close(go[0]);

    pid_t tracer = fork();
    if (tracer < 0) { perror("bb3jail: fork tracer"); kill(child_pid, SIGKILL); return 125; }
    if (tracer == 0) {
        setsid();
        if (log_path) logf = fopen(log_path, "a");
        seen = calloc((size_t)pid_max + 1, 1);
        long opts = PTRACE_O_TRACESECCOMP | PTRACE_O_TRACEFORK | PTRACE_O_TRACEVFORK |
                    PTRACE_O_TRACECLONE | PTRACE_O_TRACEEXEC | PTRACE_O_EXITKILL;
        if (!seen || ptrace(PTRACE_SEIZE, child_pid, 0, opts) != 0) {
            perror("bb3jail: ptrace seize");
            _exit(125); /* child sees EOF on the pipe and exits 126 */
        }
        mark(child_pid, 1);
        char ok = 1;
        if (write(go[1], &ok, 1) != 1) _exit(125);
        close(go[1]);
        int devnull = open("/dev/null", O_RDWR);
        if (devnull >= 0) { dup2(devnull, 0); dup2(devnull, 1); dup2(devnull, 2); if (devnull > 2) close(devnull); }
        _exit(trace_loop());
    }
    close(go[1]);

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = forward;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);
    sigaction(SIGQUIT, &sa, NULL);

    int st;
    while (waitpid(child_pid, &st, 0) < 0) {
        if (errno != EINTR) { perror("bb3jail: wait"); return 125; }
    }
    if (WIFEXITED(st)) return WEXITSTATUS(st);
    if (WIFSIGNALED(st)) return 128 + WTERMSIG(st);
    return 125;
}
