#!/usr/bin/env python3
"""agent-top: CPU and memory breakdown by Claude / Codex / Pi agent session.

Every process on the machine is attributed to the outermost agent process in
its ancestry. Percentages come from two /proc samples (so they are *current*
usage, not the lifetime average that `ps %CPU` reports), and they are summed
over cores, so one fully busy core reads 100%.

    agent-top                 # 2s sample, single report
    agent-top -n 5 -w         # 5s sample, refresh forever
    agent-top --json          # machine-readable
    agent-top --all           # also list idle sessions
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HZ = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
HOME = os.path.expanduser("~")

# argv[0] basenames that identify an agent CLI, plus substrings of node-based
# launch paths (node <path>/cli.js, packaged codex binaries, ...).
KINDS = {
    "claude": ("claude", "claude-code"),
    "codex": ("codex",),
    "pi": ("pi",),
}
KIND_PATH_HINTS = (
    ("claude", ("claude-code",)),
    ("codex", ("@openai/codex",)),
    ("pi", ("pi-coding-agent",)),
)
# Never a session root even though the basename matches.
NOT_AGENT = ("codex-linux-sandbox", "codex-execve-wrapper")

_cache = {}


# ---------------------------------------------------------------- /proc reads


def read_stat(pid):
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    lp = raw.find(b"(")
    rp = raw.rfind(b")")
    if lp < 0 or rp < 0:
        return None
    f = raw[rp + 2:].split()
    if len(f) < 22:
        return None
    return {
        "pid": pid,
        "comm": raw[lp + 1:rp].decode("utf-8", "replace"),
        "ppid": int(f[1]),
        "cpu": int(f[11]) + int(f[12]),  # utime + stime, in ticks
        "threads": int(f[17]),
        "start": int(f[19]),
        "rss": int(f[21]) * PAGE,
    }


def read_cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def read_env(pid):
    try:
        with open("/proc/%d/environ" % pid, "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    env = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if sep:
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env


def read_cwd(pid):
    try:
        return os.readlink("/proc/%d/cwd" % pid)
    except OSError:
        return ""


SELF_NAMES = {os.path.basename(__file__), os.path.basename(sys.argv[0] or "")}


def own_tree(procs):
    """This tool's own processes: polling overhead is not user workload."""
    mine = {pid for pid, st in procs.items() if st["comm"] in SELF_NAMES}
    changed = True
    while changed:
        changed = False
        for pid, st in procs.items():
            if pid not in mine and st["ppid"] in mine:
                mine.add(pid)
                changed = True
    return mine


def snapshot():
    """{pid: stat dict} for every visible process."""
    procs = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        st = read_stat(int(name))
        if st:
            procs[st["pid"]] = st
    return procs


def cached_meta(pid, start):
    """cmdline/cwd/env reads are expensive; cache them per (pid, starttime)."""
    key = (pid, start)
    if key not in _cache:
        _cache[key] = {
            "argv": read_cmdline(pid),
            "cwd": read_cwd(pid),
            "env": read_env(pid),
        }
    return _cache[key]


# ------------------------------------------------------------ classification


def agent_kind(argv):
    if not argv:
        return None
    base = os.path.basename(argv[0])
    if base in NOT_AGENT:
        return None
    for kind, names in KINDS.items():
        if base in names:
            return kind
    head = " ".join(argv[:3])
    for kind, hints in KIND_PATH_HINTS:
        if any(h in head for h in hints):
            return kind
    return None


def outermost_root(pid, procs, roots):
    """Nearest-to-init agent ancestor of pid (pid itself counts), else None."""
    root = pid if pid in roots else None
    cur = pid
    for _ in range(64):
        st = procs.get(cur)
        if st is None:
            break
        ppid = st["ppid"]
        if ppid == cur or ppid not in procs:
            break
        cur = ppid
        if cur in roots:
            root = cur
    return root


def tmux_sockets():
    """Candidate tmux server sockets: the one we live in, then every one we own."""
    socks = []
    env_sock = os.environ.get("TMUX", "").split(",")[0]
    if env_sock:
        socks.append(env_sock)
    rundir = "/tmp/tmux-%d" % os.getuid()
    try:
        for name in sorted(os.listdir(rundir)):
            socks.append(os.path.join(rundir, name))
    except OSError:
        pass
    return socks


def tmux_labels():
    """{'%73': '30:6.2'} from every reachable tmux server."""
    exe = shutil.which("tmux")
    if not exe:
        return {}
    labels = {}
    for sock in tmux_sockets():
        try:
            out = subprocess.run(
                [exe, "-S", sock, "list-panes", "-a", "-F",
                 "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0:
            continue
        for line in out.stdout.splitlines():
            pane, _, label = line.partition("\t")
            if pane and label:
                labels.setdefault(pane, label)
    return labels


def pane_of(pid, procs, roots):
    """TMUX_PANE of pid or the nearest ancestor that has one."""
    cur, seen = pid, 0
    while cur in procs and seen < 64:
        meta = cached_meta(cur, procs[cur]["start"])
        if meta["env"].get("TMUX_PANE"):
            return meta["env"]["TMUX_PANE"]
        cur = procs[cur]["ppid"]
        seen += 1
    return ""


# ------------------------------------------------------------------ machine


def machine_ticks():
    with open("/proc/stat", "rb") as fh:
        fields = fh.readline().split()
    nums = [int(x) for x in fields[1:]]
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    return sum(nums), idle


def core_count():
    n = 0
    try:
        with open("/proc/stat", "rb") as fh:
            for line in fh:
                key = line[:3]
                if key == b"cpu" and line[3:4].isdigit():
                    n += 1
    except OSError:
        pass
    return n or 1


def load_avg():
    try:
        with open("/proc/loadavg", "rb") as fh:
            return " ".join(fh.read().decode("ascii", "replace").split()[:3])
    except OSError:
        return "?"


def pct(cur_ticks, prev_ticks, interval):
    """CPU percent summed over cores."""
    if interval <= 0:
        return 0.0
    return 100.0 * (cur_ticks - prev_ticks) / HZ / interval


# ------------------------------------------------------------------ sampling


def sample(interval, step=0.2):
    """Tree-attributed CPU/RSS over `interval`, accumulated in `step` chunks.

    A single two-point sample loses every process that forked and died inside
    the window, which on a machine running compilers is most of them. Chunking
    keeps each poll short, and processes that die mid-window keep the share
    they already earned, attributed to the session that owned them.
    """
    steps = max(1, int(round(interval / step)))
    step = interval / steps
    cum, last, prev = {}, {}, {}
    m_start = machine_ticks()
    for _ in range(steps):
        time.sleep(step)
        cur = snapshot()
        for pid, st in cur.items():
            base = prev.get(pid)
            if base and base[1] == st["start"]:
                cum[pid] = cum.get(pid, 0) + (st["cpu"] - base[0])
            else:
                cum.setdefault(pid, 0)
            last[pid] = st
        prev = {p: (s["cpu"], s["start"]) for p, s in cur.items()}

    machine, mfrac, cores = machine_cpu_window(m_start)

    for key in [k for k in _cache if k[0] not in last]:
        _cache.pop(key, None)

    meta = {}
    roots = {}
    for pid, st in last.items():
        info = cached_meta(pid, st["start"])
        meta[pid] = info
        kind = agent_kind(info["argv"])
        if kind:
            roots[pid] = kind

    skip = own_tree(last)
    sessions, others = {}, []
    for pid, st in last.items():
        if pid in skip:
            continue
        root = outermost_root(pid, last, roots)
        alive = pid in prev
        entry = {
            "pid": pid,
            "comm": st["comm"],
            "cpu": pct(cum.get(pid, 0), 0, interval),
            "rss": st["rss"] if alive else 0,
            "cwd": meta[pid]["cwd"],
            "argv": meta[pid]["argv"],
            "exited": not alive,
        }
        if root is None:
            others.append(entry)
            continue
        sess = sessions.get(root)
        if sess is None:
            sess = {
                "kind": roots[root],
                "root": root,
                "cwd": meta[root]["cwd"],
                "argv": meta[root]["argv"],
                "pane": pane_of(root, last, roots),
                "procs": [],
                "cpu": 0.0,
                "rss": 0,
            }
            sessions[root] = sess
        sess["procs"].append(entry)
        sess["cpu"] += entry["cpu"]
        sess["rss"] += entry["rss"]

    panes = tmux_labels()
    for sess in sessions.values():
        sess["procs"].sort(key=lambda p: (p["cpu"], p["rss"]), reverse=True)
        busiest = [p for p in sess["procs"] if p["cpu"] >= 1.0][:3]
        sess["top_list"] = [("%s %d%%" % (p["comm"][:18], round(p["cpu"]))) for p in busiest]
        sess["top"] = ", ".join(sess["top_list"])
        sess["nproc"] = sum(1 for p in sess["procs"] if not p["exited"])
        sess["label"] = panes.get(sess["pane"], "")
    others.sort(key=lambda p: p["cpu"], reverse=True)
    return list(sessions.values()), others, machine, mfrac, cores


def machine_cpu_window(m_start):
    """Machine busy share over the whole window, from /proc/stat deltas."""
    total2, idle2 = machine_ticks()
    dt, didle = total2 - m_start[0], idle2 - m_start[1]
    cores = core_count()
    if dt <= 0:
        return 0.0, 0.0, cores
    frac = 100.0 * (dt - didle) / dt
    return frac * cores, frac, cores


# ------------------------------------------------------------------ display


def human_bytes(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return ("%d%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024.0


def short_dir(path, width=34):
    """~-relative directory, hard-truncated from the left to exactly width."""
    if not path:
        return "?"
    if path.startswith(HOME):
        path = "~" + path[len(HOME):]
    if len(path) <= width:
        return path
    return "\u2026" + path[-(width - 1):]


def colorize(text, pct_val, enabled, scale=100.0):
    if not enabled:
        return text
    if pct_val >= 1.5 * scale:
        code = "31;1"  # red
    elif pct_val >= 0.5 * scale:
        code = "33"  # yellow
    elif pct_val > 0:
        code = "32"  # green
    else:
        code = "90"  # grey
    return "\x1b[%sm%s\x1b[0m" % (code, text)


def bar(value, peak, width=10):
    if peak <= 0:
        return ""
    filled = int(round(width * min(value, peak) / peak))
    return "\u2588" * filled + "\u00b7" * (width - filled)


def render(sessions, others, machine, machine_frac, args, cores, elapsed):
    use_color = sys.stdout.isatty() and not args.no_color
    sessions = [s for s in sessions if args.all or s["cpu"] >= args.min_cpu]
    sessions.sort(key=lambda s: s[args.sort], reverse=True)
    peak = max([s["cpu"] for s in sessions] + [args.min_cpu, 1.0])
    width = shutil.get_terminal_size((120, 40)).columns
    fixed = 53  # leading columns before DIR
    dirw = max(12, min(36, width // 3))
    topw = max(20, width - fixed - dirw)
    agent_cpu = sum(s["cpu"] for s in sessions)
    other_cpu = sum(p["cpu"] for p in others)
    unaccounted = max(0.0, machine - agent_cpu - other_cpu)

    out = [""]
    out.append(
        "%s  machine %s of %dc (%.0f%% busy)  load %s  agents %.0f%%  other %.0f%%  unseen %.0f%%"
        % (
            time.strftime("%H:%M:%S"),
            colorize("%.0f%%" % machine, machine_frac, use_color),
            cores,
            machine_frac,
            load_avg(),
            agent_cpu,
            other_cpu,
            unaccounted,
        )
    )
    out.append("")
    out.append("  %6s %-11s %7s %4s %-7s %-9s %-*s %s"
               % ("CPU%", "", "RSS", "N", "AGENT", "SESSION", dirw, "DIR", "BUSIEST PROCS"))
    if not sessions:
        out.append("  (no agent sessions above %.1f%%)" % args.min_cpu)
    for s in sessions:
        label = s.get("label") or s["pane"] or "-"
        cpu_col = "%6.1f" % s["cpu"]
        if use_color:
            cpu_col = colorize(cpu_col, s["cpu"], True)
        top = ""
        for piece in s["top_list"]:
            candidate = (top + ", " + piece) if top else piece
            if len(candidate) > topw:
                if not top:
                    top = candidate[: topw - 1] + "\u2026"
                break
            top = candidate
        out.append(
            "  %s %-11s %7s %4d %-7s %-9s %-*s %s"
            % (cpu_col, bar(s["cpu"], peak), human_bytes(s["rss"]), s["nproc"],
               s["kind"], label, dirw, short_dir(s["cwd"], dirw), top)
        )

    out.append("")
    out.append("  other processes (no agent ancestor): %.0f%% across %d procs"
               % (other_cpu, len(others)))
    shown = 0
    for p in others:
        if shown >= args.top or p["cpu"] < 0.5:
            break
        cmd = " ".join(p["argv"]) or "[%s]" % p["comm"]
        cmd = cmd.replace(HOME, "~")
        if len(cmd) > width - 30:
            cmd = cmd[: width - 31] + "\u2026"
        out.append("  %6.1f %7s %-7d %s" % (p["cpu"], human_bytes(p["rss"]), p["pid"], cmd))
        shown += 1
    out.append("")
    out.append(
        "  sampled over %.1fs, %d procs; CPU%% is summed across cores, so 100%% = one full core"
        % (elapsed, len(others) + sum(s["nproc"] for s in sessions))
    )
    out.append(
        "  unseen = kernel threads, irq, and processes born and died inside one poll interval"
    )
    return "\n".join(out)


def to_json(sessions, others, machine, machine_frac, args, cores, elapsed):
    sessions = [s for s in sessions if args.all or s["cpu"] >= args.min_cpu]
    sessions.sort(key=lambda s: -s[args.sort])
    return json.dumps(
        {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "machine_cpu_pct": round(machine, 2),
            "machine_busy_frac": round(machine_frac, 2),
            "cores": cores,
            "load": load_avg(),
            "interval_s": round(elapsed, 3),
            "agent_cpu_pct": round(sum(s["cpu"] for s in sessions), 2),
            "other_cpu_pct": round(sum(p["cpu"] for p in others), 2),
            "sessions": [
                {
                    "kind": s["kind"],
                    "root_pid": s["root"],
                    "pane": s["pane"] or None,
                    "tmux": s.get("label") or None,
                    "cwd": s["cwd"],
                    "cmdline": s["argv"],
                    "cpu_pct": round(s["cpu"], 2),
                    "rss_bytes": s["rss"],
                    "proc_count": s["nproc"],
                    "procs": [
                        {"pid": p["pid"], "comm": p["comm"], "cpu_pct": round(p["cpu"], 2),
                         "rss_bytes": p["rss"]}
                        for p in s["procs"][:20]
                    ],
                }
                for s in sessions
            ],
            "other": [
                {"pid": p["pid"], "comm": p["comm"], "cpu_pct": round(p["cpu"], 2),
                 "rss_bytes": p["rss"], "cmdline": p["argv"]}
                for p in others[: args.top]
            ],
        },
        indent=2,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="agent-top",
        description="CPU/RSS breakdown per Claude, Codex, and Pi agent session.",
    )
    ap.add_argument("-n", "--interval", type=float, default=2.0, help="sample window in seconds (default 2)")
    ap.add_argument("-w", "--watch", action="store_true", help="refresh until interrupted")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--all", action="store_true", help="include idle sessions")
    ap.add_argument("--min-cpu", type=float, default=1.0,
                    help="hide sessions below this CPU%% (default 1; use 0 to see everything)")
    ap.add_argument("--top", type=int, default=10, help="non-agent processes to list (default 10)")
    ap.add_argument("--sort", choices=("cpu", "rss", "nproc"), default="cpu")
    ap.add_argument("--kinds", default="", help="comma list: claude,codex,pi (default all)")
    ap.add_argument("--no-color", action="store_true", help="plain output")
    args = ap.parse_args(argv)

    if args.interval < 0.2:
        ap.error("--interval must be >= 0.2")

    wanted = {k.strip() for k in args.kinds.split(",") if k.strip()}
    global KINDS
    if wanted:
        KINDS = {k: v for k, v in KINDS.items() if k in wanted}

    cores = core_count()
    try:
        while True:
            started = time.monotonic()
            sessions, others, machine, frac, cores = sample(args.interval)
            elapsed = time.monotonic() - started
            if args.json:
                print(to_json(sessions, others, machine, frac, args, cores, elapsed))
            else:
                if args.watch:
                    sys.stdout.write("\x1b[H\x1b[2J")
                print(render(sessions, others, machine, frac, args, cores, elapsed))
                sys.stdout.flush()
            if not args.watch:
                return 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
