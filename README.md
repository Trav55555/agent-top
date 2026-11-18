# agent-top

CPU and memory breakdown by agent session. Every process on the machine is
attributed to the outermost Claude, Codex, or Pi process in its ancestry, so one
row answers "which session is eating the machine".

`ps %CPU` reports a lifetime average, which hides a compile storm that started
thirty seconds ago. agent-top samples `/proc` deltas instead, so numbers are
current usage.

```sh
agent-top              # one report, 2s window
agent-top -n 5         # 5s window
agent-top -w           # refresh until Ctrl-C
agent-top --json       # machine-readable
agent-top --sort rss   # biggest memory instead of CPU
agent-top --all        # include idle sessions
```

## Output

```
21:55:57  machine 1200% of 12c (100% busy)  load 67.53 60.88 44.42  agents 862%  other 296%  unseen 42%

    CPU%                 RSS    N AGENT   SESSION   DIR                                  BUSIEST PROCS
   309.5 ██████████     5.5G   57 claude  30:10.1   ~/projects/tui-terminal              rustc 39%, rustc 28%, rustc 23%
   301.0 ██████████     2.3G   51 claude  30:8.2    ~/projects/next-os-paradigm          cc1 70%, python3 46%, glyphc 36%
```

- **CPU%** is summed over cores: 100% is one full core, so 309.5% is just over
  three cores.
- **SESSION** is the tmux `session:window.pane` the agent was started from.
- **N** counts live processes; processes that died during the window still
  contribute their CPU to the session.
- **agents + other + unseen** adds up to the machine total. `unseen` is kernel
  threads, irq, and processes born and died inside one 200 ms poll - on a
  machine running compilers, that bucket is real spending, not measurement error.

## How attribution works

1. A session root is a process whose `argv[0]` is `claude`, `codex`, or `pi`
   (plus the node-based launch paths). `codex-linux-sandbox` and other helpers
   are not roots.
2. Every other process is assigned to its outermost root ancestor. A `codex`
   started by a `claude` session is part of that claude session.
3. The window is polled every 200 ms rather than twice. A single two-point
   sample on this machine lost ~40% of the CPU, because ~380 processes per
   second fork and die inside a 2s window. Chunking keeps that loss under ~5%.
4. agent-top excludes its own process tree, so it never bills itself as load.

## Limits

- **RSS is summed, not deduplicated.** Shared pages (rustc, gcc, node) are
  counted once per process, so a large tree overstates real memory. Use it to
  compare sessions, not to predict OOM.
- **Reparented processes stay with their session.** Agent CLIs act as child
  subreapers, so a browser or GUI app launched from a session's shell can keep
  hanging off it long after the shell exited. It shows up with its real CPU.
- **PID reuse inside a window** is ignored: a reused pid keeps the earlier
  process's share for the rest of the window.
- Linux only (`/proc`), Python 3.9+, no dependencies.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The suite covers CLI detection, nested-root attribution, the per-core percent
math, and an end-to-end run against a synthetic `pi` tree that burns one core.
