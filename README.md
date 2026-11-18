# agent-top

**Which agent is eating the machine.**

`agent-top` reports CPU and memory per Claude, Codex, and Pi session on Linux.
Every process a session spawned — `rustc`, `cc1plus`, `pytest`, `node`, the
model CLI itself — is rolled into that session's row and labeled with the tmux
pane it came from. One row per session, sorted by current usage.

```text
22:01:19  machine 1200% of 12c (100% busy)  load 50.57 55.17 46.72  agents 818%  other 180%  unseen 202%

    CPU%                 RSS    N AGENT   SESSION   DIR                                  BUSIEST PROCS
   410.5 ██████████     6.8G   54 claude  30:10.1   ~/projects/tui-terminal              rustc 36%, rustc 34%, cc1plus 32%
   287.0 ███████···     2.1G   61 claude  30:8.2    ~/projects/next-os-paradigm          test_glyph0 73%, cc1 46%, cc1 28%
    51.0 █·········     1.1G   25 claude  30:7.1    ~/projects/porygon-ai                pytest 30%, claude 16%, tea 2%
    42.5 █·········   544.8M    8 claude  30:8.1    ~/projects/next-os-paradigm          cc1 23%, cc1 8%, claude 7%
    13.5 ··········   459.3M    6 pi      30:17.1   ~/projects/c-utils                   python3 12%, pi 2%
     3.5 ··········   626.5M   28 codex   30:2.1    ~/projects/personal-agent-os         codex 4%
     3.0 ··········   630.5M    5 claude  30:6.1    ~/projects/c-utils                   claude 3%
     2.5 ··········   295.1M    3 claude  30:2.1    ~/projects/personal-agent-os         claude 2%
     1.5 ··········   194.5M    3 claude  30:3.1    ~/projects/penguin-melee             claude 2%
     1.0 ··········   665.8M   14 claude  30:10.2   ~/projects/tui-terminal              claude 1%
     1.0 ··········   327.9M    3 claude  30:6.2    ~/projects/c-utils                   claude 1%
     1.0 ··········   395.7M    3 claude  30:12.1   ~/projects                           claude 1%

  other processes (no agent ancestor): 180% across 759 procs
    23.5    1.1G 1286589 /snap/firefox/9113/usr/lib/firefox/firefox
    17.5    3.3G 1077380 /snap/firefox/9113/usr/lib/firefox/firefox -contentproc -isForBrowser -prefsHandle 0:56262 -p…
    17.0  714.8M 2847218 /app/discord/Discord --type=renderer --crashpad-handler-pid=2449 --enable-crash-reporter=e172…
    15.5   16.4M 46733   htop
```

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
      "root_pid": 4169595,
      "pane": "%72",
      "tmux": "30:10.1",
      "cwd": "/home/travers/projects/tui-terminal",
      "cpu_pct": 324.0,
      "rss_bytes": 9276813312,
      "proc_count": 58,
      "procs": [{"pid": 443474, "comm": "rustc", "cpu_pct": 63.0, "rss_bytes": 257110016}]
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
