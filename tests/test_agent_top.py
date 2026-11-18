"""Tests for agent-top.

Run: python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import pathlib
import subprocess
import sys
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "agent-top.py"

spec = importlib.util.spec_from_file_location("agent_top", SCRIPT)
at = importlib.util.module_from_spec(spec)
spec.loader.exec_module(at)


class AgentKindTests(unittest.TestCase):
    def test_recognizes_agent_clis(self):
        self.assertEqual(at.agent_kind(["claude"]), "claude")
        self.assertEqual(at.agent_kind(["/home/u/.local/bin/claude"]), "claude")
        self.assertEqual(at.agent_kind(["codex"]), "codex")
        self.assertEqual(at.agent_kind(["pi", "-p", "hello"]), "pi")
        self.assertEqual(at.agent_kind(["node", "/x/@earendil-works/pi-coding-agent/cli.js"]), "pi")
        self.assertEqual(at.agent_kind(["node", "/x/claude-code/cli.js"]), "claude")

    def test_ignores_non_roots(self):
        # The codex sandbox helper matches "codex" as a prefix but is a child.
        self.assertIsNone(at.agent_kind(["codex-linux-sandbox", "--sandbox-policy-cwd", "/tmp"]))
        self.assertIsNone(at.agent_kind(["rustc", "--crate-name", "syn"]))
        self.assertIsNone(at.agent_kind(["/usr/bin/python3", "-c", "pass"]))
        self.assertIsNone(at.agent_kind([]))


def fake(pid, ppid, comm="x", start=1):
    return {"pid": pid, "ppid": ppid, "comm": comm, "start": start,
            "cpu": 0, "rss": 0, "threads": 1}


class AttributionTests(unittest.TestCase):
    def test_outermost_root_wins_for_nested_agents(self):
        procs = {1: fake(1, 0, "systemd"), 100: fake(100, 1, "zsh"),
                 200: fake(200, 100, "claude"), 300: fake(300, 200, "bash"),
                 400: fake(400, 300, "codex"), 500: fake(500, 400, "rustc")}
        roots = {200: "claude", 400: "codex"}
        self.assertEqual(at.outermost_root(500, procs, roots), 200)
        self.assertEqual(at.outermost_root(400, procs, roots), 200)
        self.assertEqual(at.outermost_root(200, procs, roots), 200)
        self.assertEqual(at.outermost_root(100, procs, roots), None)

    def test_survives_cycles_and_missing_parents(self):
        procs = {10: fake(10, 11, "claude"), 11: fake(11, 10, "x")}
        self.assertEqual(at.outermost_root(11, procs, {10: "claude"}), 10)
        self.assertIsNone(at.outermost_root(99, procs, {10: "claude"}))


class MathTests(unittest.TestCase):
    def test_pct_is_summed_over_cores(self):
        one_core = at.HZ  # one second of one core
        self.assertAlmostEqual(at.pct(one_core, 0, 1.0), 100.0)
        self.assertAlmostEqual(at.pct(2 * one_core, 0, 1.0), 200.0)
        self.assertAlmostEqual(at.pct(one_core, one_core // 2, 1.0), 50.0)
        self.assertEqual(at.pct(one_core, 0, 0), 0.0)

    def test_short_dir_never_exceeds_width(self):
        for path in ("/a/b/c", "/home/travers/" + "x/" * 40, ""):
            self.assertLessEqual(len(at.short_dir(path, 20)), 20, path)

    def test_human_bytes(self):
        self.assertEqual(at.human_bytes(512), "512B")
        self.assertEqual(at.human_bytes(2048), "2.0K")


class IntegrationTests(unittest.TestCase):
    """Drive the real tool against a synthetic, unmistakable agent session."""

    def test_fake_pi_session_is_attributed(self):
        # exec -a pi makes argv[0] == "pi", the same shape the real CLI has.
        # bash tolerates a rewritten argv[0]; python would mis-derive its prefix.
        proc = subprocess.Popen(
            ["bash", "-c", "exec -a pi bash -c 'sleep 30 & while :; do :; done'"],
            cwd="/tmp",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.6)
            data = None
            # A busy host can leave the burner with little scheduling grace in a
            # short window, so retry once before calling the measurement broken.
            for _ in range(2):
                data = self._run_json(["-n", "2", "--json", "--all"])
                entries = {p["pid"]: (s, p) for s in data["sessions"] for p in s["procs"]}
                if entries.get(proc.pid, (None, {}))[1].get("cpu_pct", 0) > 0:
                    break

            # The test itself may run inside a real agent session, in which case
            # the fake root is correctly folded into its owner's session.
            procs = at.snapshot()
            roots = {p: at.agent_kind(at.cached_meta(p, s["start"])["argv"])
                     for p, s in procs.items()}
            roots = {p: k for p, k in roots.items() if k}
            owner = at.outermost_root(proc.pid, procs, roots)

            entries = {p["pid"]: (s, p) for s in data["sessions"] for p in s["procs"]}
            self.assertIn(proc.pid, entries, "fake pi process was not measured at all")
            session, entry = entries[proc.pid]
            if owner is None:
                self.assertEqual(session["root_pid"], proc.pid, "fake root did not form a session")
                self.assertEqual(session["kind"], "pi")
                self.assertEqual(session["cwd"], "/tmp")
                self.assertGreaterEqual(session["proc_count"], 2, session)
            else:
                self.assertEqual(session["root_pid"], owner, "folded into the wrong session")
            self.assertGreater(entry["cpu_pct"], 10, "burner CPU not measured")
            self.assertLess(entry["cpu_pct"], 300, "CPU% is not per-core-summed")
        finally:
            proc.kill()
            proc.wait()

    def _run_json(self, argv):
        out = subprocess.run([sys.executable, str(SCRIPT)] + argv,
                             capture_output=True, text=True, timeout=180)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_human_output_has_a_session_row(self):
        out = subprocess.run(
            [sys.executable, str(SCRIPT), "-n", "0.6", "--no-color"],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("cores", out.stdout)
        self.assertIn("other processes", out.stdout)


if __name__ == "__main__":
    unittest.main()
