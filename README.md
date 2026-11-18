# agent-top

**Which agent is eating the machine.**

`agent-top` reports CPU and memory per Claude, Codex, and Pi session on Linux.
Every process a session spawned — `rustc`, `cc1plus`, `pytest`, `node`, the
model CLI itself — is rolled into that session's row and labeled with the tmux
pane it came from. One row per session, sorted by current usage.

```text
22:07:49  machine 1200% of 12c (100% busy)  load 63.83 67.25 55.70  agents 718%  other 231%  unseen 252%

    CPU%                 RSS    N AGENT   SESSION   DIR                                  BUSIEST PROCS
   261.0 ██████████     1.9G   66 claude  app:1.1   ~/work/api-service                   runner 70%, cc1 38%, cc1 36%
   250.0 ██████████     5.2G   37 claude  app:1.2   ~/work/web-client                    claude 29%, build 20%, linter 20%
   105.5 ████······   720.5M   22 claude  app:2.1   ~/work/api-service                   runner 30%, build 17%, linter 14%
    68.5 ███·······     2.5G   33 claude  build:1.1 ~/work/data-pipeline                 pytest 36%, python 20%, claude 6%
    14.5 █·········   493.5M    6 pi      ops:1.1   ~/work/docs-site                     python3 12%, pi 2%
     4.5 ··········   617.7M   28 codex   ops:1.2   ~/work/infra                         codex 4%
     4.5 ··········   634.0M   14 claude  app:3.1   ~/work/web-client                    claude 4%
     3.5 ··········   398.4M    3 claude  build:1.2 ~/work/cli-tool                      claude 4%
     2.0 ··········   324.0M    3 claude  ops:2.1   ~/work/api-service                   claude 2%
     1.5 ··········   194.8M    3 claude  app:2.2   ~/work/web-client                    claude 2%
     1.0 ··········   629.1M    5 claude  ops:3.1   ~/work/docs-site                     claude 1%
     1.0 ··········   326.8M    3 claude  build:2.1 ~/work/docs-site                     claude 1%

  other processes (no agent ancestor): 231% across 816 procs
    22.5  668.1M 3472    chat --type=renderer
    22.0  291.3M 3683    desktop-shell --mode=ubuntu
    17.5   16.4M 3894    htop
    16.0  213.7M 4105    linter
```

> Captured with `agent-top -n 2 --top 4`. Project directories, tmux labels, and
> desktop application names are genericized; the percentages, process counts, and
> memory figures are from that run.

## Install

```sh
git clone https://github.com/Trav55555/agent-top
ln -s "$PWD/agent-top/agent-top.py" ~/.local/bin/agent-top
```

One file, standard library only, Python 3.9+ on Linux. Copying `agent-top.py`
onto your `PATH` works just as well.

## Usage

```sh
agent-top              # one report, 2s sample window
agent-top -w           # refresh until Ctrl-C
agent-top -n 5         # wider window: steadier numbers, slower to react
agent-top --sort rss   # rank by memory
agent-top --all        # include idle sessions
agent-top --json       # machine-readable
```

| Flag | Meaning |
| --- | --- |
| `-n, --interval` | Sample window in seconds (default 2, minimum 0.2) |
| `-w, --watch` | Redraw continuously |
| `--json` | Emit JSON instead of a table |
| `--sort` | `cpu` (default), `rss`, or `nproc` |
| `--all` | Show sessions under `--min-cpu` |
| `--min-cpu` | Hide sessions below this percentage (default 1) |
| `--top` | How many non-agent processes to list (default 10) |
| `--kinds` | Restrict to `claude`, `codex`, `pi` |
| `--no-color` | Plain text output |

## Reading the output

- **CPU%** is summed over cores, the way `top` reports a threaded process:
  `100%` is one fully busy core, so `410.5%` is about four cores.
- **RSS** is the summed resident memory of the session's live processes.
- **N** counts live processes. A process that exited inside the window still
  contributes the CPU it earned to the session it belonged to.
- **SESSION** is the tmux `session:window.pane` the agent was started in.
- **agents + other + unseen** adds up to the machine total.

## How the numbers are produced

CPU comes from `utime`/`stime` deltas across the sample window, which makes it
current usage. The window is polled every 200ms and accumulated, because that is
what it takes to keep a build storm in view: this class of machine forks a few
hundred processes per second, and a build's shortest compiler runs finish inside
a 2s window. Chunked polling keeps those in the picture, and a process that exits
keeps the share it already earned on its session's row.

Everything the per-process numbers collect is reported: `unseen` carries kernel
threads, irq, and processes that started and finished inside a single poll. On
an idle machine it stays near zero.

`agent-top` excludes its own process tree, so watching the machine keeps the
report about your agents.

## How attribution works

1. A **session root** is a process whose `argv[0]` is `claude`, `codex`, or `pi`
   (plus the node launch paths). Helpers such as `codex-linux-sandbox` are
   treated as part of their parent session.
2. Every other process belongs to its **outermost** root ancestor. A `codex`
   started from inside a `claude` session is part of that claude session, which
   is the useful answer when the outer one is orchestrating.
3. Percentages are per-core summed, so they can exceed 100. Memory is measured
   once at the end of the window.

## JSON

```sh
agent-top --json | jq '.sessions[] | {kind, tmux, cwd, cpu_pct, proc_count}'
```

```json
{
  "generated_at": "2026-11-17T22:00:13-0500",
  "machine_cpu_pct": 1198.4,
  "machine_busy_frac": 99.87,
  "cores": 12,
  "agent_cpu_pct": 565.0,
  "other_cpu_pct": 222.0,
  "sessions": [
    {
      "kind": "claude",
      "root_pid": 1337,
      "pane": "%72",
      "tmux": "app:1.1",
      "cwd": "/home/dev/work/api-service",
      "cpu_pct": 324.0,
      "rss_bytes": 9276813312,
      "proc_count": 58,
      "procs": [{"pid": 1340, "comm": "rustc", "cpu_pct": 63.0, "rss_bytes": 257110016}]
    }
  ]
}
```

`procs` carries the 20 busiest processes of each session.

## Notes on the numbers

- **RSS is a sum, so treat it as an upper bound.** Shared pages (`rustc`, `gcc`,
  `node`) count once per process. It compares sessions well; a byte-accurate
  figure would need PSS.
- **A reparented process stays with its session.** Agent CLIs act as child
  subreapers, so a browser launched from a session's shell keeps that ancestry
  after the shell exits, and appears with its real usage.
- **PID reuse inside a window** stays attributed to the earlier process for the
  rest of that window.
- **Local processes only.** This reads the host's `/proc`; containers and remote
  hosts are separate machines to it.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Nine cases: CLI detection, nested-root attribution, per-core percentage math,
and an end-to-end run that creates a synthetic `pi` tree burning one core and
checks both the measurement and the attribution. CI runs the suite on every
push.

## License

MIT — see [LICENSE](LICENSE).
