"""Integration-style regression tests for claude-worker using a fake Claude CLI.

Run: python3 -m unittest -v test_claude_worker
"""

import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parent / "claude-worker"

# A fake `claude` that behaves like the real CLI at the boundary claude-worker uses:
# `auth status --json` reports the auth source actually selected by the environment,
# `--print` reads the prompt on stdin, keeps per-session history on disk, and prints
# one JSON result. Each invocation is logged for assertions.
FAKE_CLAUDE = textwrap.dedent(r'''
    #!{python}
    import json, os, subprocess, sys, time, uuid
    from pathlib import Path

    root = Path(os.environ["FAKE_CLAUDE_ROOT"])
    config = json.loads((root / "config.json").read_text())
    argv = sys.argv[1:]

    def log(entry):
        entry["env_keys"] = sorted(os.environ)
        entry["cwd"] = os.getcwd()
        with open(root / "invocations.jsonl", "a") as fh:
            fh.write(json.dumps(entry) + "\n")

    if argv[:2] == ["auth", "status"]:
        log({{"kind": "auth", "argv": argv}})
        if os.environ.get("ANTHROPIC_API_KEY"):
            auth = {{"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"}}
        elif os.environ.get("CLAUDE_CODE_USE_BEDROCK"):
            auth = {{"loggedIn": True, "authMethod": "third_party", "apiProvider": "bedrock"}}
        else:
            auth = config.get("auth", {{
                "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                "subscriptionType": "max", "email": "person@example.com",
                "orgId": "org-SECRET-ID", "orgName": "Secret Org"}})
        print(json.dumps(auth))
        sys.exit(config.get("auth_exit", 0 if auth.get("loggedIn") else 1))

    VALUE = {{"--output-format", "--model", "--effort", "--setting-sources", "--settings",
             "--mcp-config", "--permission-prompts", "--permission-mode", "--tools",
             "--append-system-prompt", "--session-id", "--resume"}}
    VARIADIC = {{"--allowedTools", "--disallowedTools"}}
    FLAGS = {{"--print", "--safe-mode", "--strict-mcp-config"}}
    opts, i = {{}}, 0
    while i < len(argv):
        arg = argv[i]
        if arg in VALUE:
            opts[arg] = argv[i + 1]; i += 2
        elif arg in VARIADIC:
            j = i + 1
            while j < len(argv) and not argv[j].startswith("--"):
                j += 1
            opts[arg] = argv[i + 1:j]; i = j
        elif arg in FLAGS:
            opts[arg] = True; i += 1
        else:
            print(f"error: unknown option '{{arg}}'", file=sys.stderr); sys.exit(2)

    prompt = sys.stdin.read()
    log({{"kind": "print", "argv": argv, "opts": opts, "prompt": prompt}})
    sessions = root / "sessions"; sessions.mkdir(exist_ok=True)
    if "--resume" in opts:
        sid = opts["--resume"]
        if not (sessions / sid).exists():
            print(json.dumps({{"type": "result", "subtype": "error_during_execution", "is_error": True,
                              "session_id": str(uuid.uuid4()), "result": "No conversation found"}}))
            sys.exit(1)
    else:
        sid = opts.get("--session-id") or str(uuid.uuid4())
    history = json.loads((sessions / sid).read_text()) if (sessions / sid).exists() else []
    history.append(prompt)
    (sessions / sid).write_text(json.dumps(history))

    behavior = config.get("print", "success")
    result = {{"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
              "result": f"STATUS: complete\nturns={{len(history)}}", "permission_denials": [],
              "total_cost_usd": 0.42,
              "modelUsage": {{opts.get("--model", "?"): {{"inputTokens": 10, "outputTokens": 5}}}}}}
    if behavior == "error":
        result.update(subtype="error_max_turns", is_error=True, result="")
    elif behavior == "denied":
        result.update(result="STATUS: blocked\nneeded rm", permission_denials=[
            {{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {{"command": "rm -rf build"}}}}])
    elif behavior == "wrong_session":
        result.update(session_id=str(uuid.uuid4()))
    elif behavior == "invalid":
        print("Error: something went wrong (not json)"); sys.exit(1)
    elif behavior == "hang":
        child = subprocess.Popen(["sleep", "300"])
        (root / "grandchild.pid").write_text(str(child.pid))
        (root / "fake.pid").write_text(str(os.getpid()))
        time.sleep(300)
    print(json.dumps(result))
''').lstrip()


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); check its state.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


class WorkerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cw test ")
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude" / "CLAUDE.md").write_text("GLOBAL-RULE: always cite file paths.\n")
        self.fake_root = self.tmp / "fake"
        self.fake_root.mkdir()
        self.claude = self.tmp / "bin" / "claude"
        self.claude.parent.mkdir()
        self.claude.write_text(FAKE_CLAUDE.format(python=sys.executable))
        self.claude.chmod(0o755)
        self.project = self.tmp / "project dir"
        self.project.mkdir()
        self.state = self.tmp / "state"
        self.brief = self.tmp / "brief.md"
        self.brief.write_text("Do the task.\n")
        self.configure()
        self.background = []

    def tearDown(self):
        for proc in self.background:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for stream in (proc.stdout, proc.stderr):
                stream.close()
        for name in ("fake.pid", "grandchild.pid"):
            path = self.fake_root / name
            if path.exists():
                try:
                    os.kill(int(path.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self._tmp.cleanup()

    def configure(self, **config):
        (self.fake_root / "config.json").write_text(json.dumps(config))

    def env(self, extra=None):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_WORKER_"))}
        env.update(HOME=str(self.home), FAKE_CLAUDE_ROOT=str(self.fake_root))
        env.update(extra or {})
        return env

    def run_args(self, *, cwd=None, mode="read", model="sonnet", effort="medium", timeout=30, rules=(), brief=None):
        args = ["run", "--cwd", str(cwd or self.project), "--brief", str(brief or self.brief),
                "--model", model, "--effort", effort, "--mode", mode, "--timeout", str(timeout),
                "--state-dir", str(self.state), "--claude-bin", str(self.claude)]
        for rule in rules:
            args += ["--allow-command", rule]
        return args

    def cli(self, args, extra_env=None, timeout=60):
        proc = subprocess.run([sys.executable, str(LAUNCHER), *args], capture_output=True, text=True,
                              env=self.env(extra_env), timeout=timeout)
        try:
            output = json.loads(proc.stdout)
        except ValueError:
            self.fail(f"stdout is not JSON: {proc.stdout!r}\nstderr: {proc.stderr}")
        return proc.returncode, output, proc.stderr

    def spawn(self, args):
        proc = subprocess.Popen([sys.executable, str(LAUNCHER), *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.env())
        self.background.append(proc)
        return proc

    def invocations(self, kind=None):
        path = self.fake_root / "invocations.jsonl"
        if not path.exists():
            return []
        entries = [json.loads(line) for line in path.read_text().splitlines()]
        return [entry for entry in entries if kind is None or entry["kind"] == kind]

    def wait_for(self, path, timeout=15):
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {path}")
            time.sleep(0.05)
        time.sleep(0.1)
        return int(path.read_text())

    def job(self, output):
        return json.loads((Path(output["job_dir"]) / "job.json").read_text())


class AuthTests(WorkerTestCase):
    def test_auth_reports_only_redacted_fields(self):
        code, out, err = self.cli(["auth", "--claude-bin", str(self.claude)])
        self.assertEqual(code, 0)
        self.assertEqual(out["auth"], {"loggedIn": True, "authMethod": "claude.ai",
                                       "apiProvider": "firstParty", "subscriptionType": "max"})
        for secret in ("person@example.com", "org-SECRET-ID", "Secret Org"):
            self.assertNotIn(secret, json.dumps(out) + err)

    def test_inherited_api_key_and_provider_switches_are_scrubbed(self):
        secrets = {"ANTHROPIC_API_KEY": "SYNTHETIC-FAKE-API-KEY", "ANTHROPIC_BASE_URL": "https://proxy.invalid",
                   "ANTHROPIC_AUTH_TOKEN": "SYNTHETIC-FAKE-AUTH-TOKEN", "CLAUDE_CODE_OAUTH_TOKEN": "SYNTHETIC-FAKE-OAUTH-TOKEN",
                   "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "KEEP_ME": "yes"}
        code, out, err = self.cli(self.run_args(), extra_env=secrets)
        self.assertEqual(code, 0, out)
        calls = self.invocations()
        self.assertEqual([c["kind"] for c in calls], ["auth", "print"])
        for call in calls:
            for key in secrets:
                if key == "KEEP_ME":
                    self.assertIn(key, call["env_keys"])
                else:
                    self.assertNotIn(key, call["env_keys"])
        stored = "".join(p.read_text(errors="replace") for p in self.state.rglob("*") if p.is_file())
        for value in ("SYNTHETIC-FAKE-API-KEY", "SYNTHETIC-FAKE-AUTH-TOKEN", "SYNTHETIC-FAKE-OAUTH-TOKEN", "person@example.com", "org-SECRET-ID"):
            self.assertNotIn(value, stored + json.dumps(out) + err)

    def test_api_or_console_auth_is_refused_without_model_call(self):
        for auth in ({"loggedIn": True, "authMethod": "console", "apiProvider": "firstParty"},
                     {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "vertex",
                      "subscriptionType": "max"},
                     {"loggedIn": False}):
            with self.subTest(auth=auth):
                self.configure(auth=auth)
                code, out, _ = self.cli(self.run_args())
                self.assertEqual(code, 3)
                self.assertEqual(out["status"], "auth_failed")
                self.assertIn("claude auth login", out["message"])
                self.assertEqual(self.invocations("print"), [])
                code, out, _ = self.cli(["auth", "--claude-bin", str(self.claude)])
                self.assertEqual(code, 3)

    def test_nonzero_auth_exit_is_refused_even_with_subscription_json(self):
        self.configure(auth_exit=1)  # default auth JSON is a valid max subscription
        code, out, err = self.cli(self.run_args())
        self.assertEqual((code, out["status"]), (3, "auth_failed"))
        self.assertIn("exited 1", out["message"])
        self.assertEqual(self.invocations("print"), [])
        code, out, err2 = self.cli(["auth", "--claude-bin", str(self.claude)])
        self.assertEqual((code, out["ok"]), (3, False))
        self.assertEqual(set(out["auth"]), {"loggedIn", "authMethod", "apiProvider", "subscriptionType"})
        for secret in ("person@example.com", "org-SECRET-ID", "Secret Org"):
            self.assertNotIn(secret, json.dumps(out) + err + err2)


class PermissionTests(WorkerTestCase):
    def last_opts(self):
        return self.invocations("print")[-1]["opts"]

    def test_mode_tool_sets_and_permission_modes(self):
        lead = ["WebSearch", "WebFetch", "Agent"]
        expected = {"text": ([], "dontAsk"), "read": (["Read", "Glob", "Grep", *lead], "dontAsk"),
                    "edit": (["Read", "Glob", "Grep", "Edit", "Write", *lead], "acceptEdits")}
        for mode, (tools, permission_mode) in expected.items():
            with self.subTest(mode=mode):
                code, out, _ = self.cli(self.run_args(mode=mode))
                self.assertEqual(code, 0, out)
                opts = self.last_opts()
                self.assertEqual([t for t in opts["--tools"].split(",") if t], tools)
                self.assertEqual(opts.get("--allowedTools", []), tools)
                self.assertNotIn("--disallowedTools", opts)
                self.assertEqual("subagents" in opts["--append-system-prompt"], mode != "text")
                self.assertEqual(opts["--permission-mode"], permission_mode)
                self.assertNotIn("Bash", opts["--tools"])
                self.assertTrue(opts["--safe-mode"] and opts["--strict-mcp-config"])
                self.assertEqual(json.loads(opts["--mcp-config"]), {"mcpServers": {}})
                self.assertEqual(opts["--setting-sources"], "")
                self.assertTrue(json.loads(opts["--settings"])["disableAllHooks"])
                self.assertEqual(opts["--permission-prompts"], "none")

    def test_global_instructions_inlined_only_for_text_mode_and_never_on_stdout(self):
        self.cli(self.run_args(mode="text"))
        text_prompt = self.last_opts()["--append-system-prompt"]
        code, out, _ = self.cli(self.run_args(mode="read"))
        read_prompt = self.last_opts()["--append-system-prompt"]
        self.assertIn("GLOBAL-RULE", text_prompt)
        self.assertNotIn("GLOBAL-RULE", read_prompt)
        self.assertIn(str(self.home / ".claude" / "CLAUDE.md"), read_prompt)
        self.assertNotIn("GLOBAL-RULE", json.dumps(out))

    def test_command_rules_preapprove_only_named_commands(self):
        rules = ["python3 -m unittest *", "python3 -m pytest -q tests", "python3 scripts/check.py *",
                 "python3 -B -m unittest *", "python3 -Bu scripts/check.py *", "node --test",
                 "node --test test/unit.test.js", "node scripts/build.js *", "git status"]
        code, out, _ = self.cli(self.run_args(mode="edit", rules=rules))
        self.assertEqual(code, 0, out)
        opts = self.last_opts()
        self.assertIn("Bash", opts["--tools"].split(","))
        bash_rules = [rule for rule in opts["--allowedTools"] if rule.startswith("Bash")]
        self.assertEqual(bash_rules, [f"Bash({rule})" for rule in rules])

    def test_text_mode_stays_tool_free_even_with_command_rules(self):
        code, out, _ = self.cli(self.run_args(mode="text", rules=["python3 -m unittest *"]))
        self.assertEqual((code, out["status"]), (2, "usage"))
        self.assertIn("tool-free", out["message"])
        self.assertEqual(self.invocations(), [])
        self.assertFalse((self.state / "jobs").exists())

    def test_python_rules_must_name_a_module_or_script(self):
        # Each of these would let the worker run arbitrary inline code or any module.
        for rule in ("python3 -m *", "python3 -m", "python3 -m -c *", "python3 -mpytest *",
                     "python3 -X * ", "python3 -Bc *", "python3 -", "python3.12 -m *", "python -c pass",
                     "deno eval *", "perl -E *", "node --eval *",
                     # Option values must not pass for a script name.
                     "python3 -W ignore *", "python3 -X dev *", "node -r mod *", "node --require mod *",
                     "python3 --check-hash-based-pycs always *", "perl -I lib *", "ruby -r mod *",
                     "python3 -i script.py *", "python3 -B", "node --test *", "node --no-warnings"):
            with self.subTest(rule=rule):
                code, out, _ = self.cli(self.run_args(rules=[rule]))
                self.assertEqual((code, out["status"]), (2, "usage"), out)
        self.assertEqual(self.invocations(), [])

    def test_blanket_shell_and_recursive_rules_rejected(self):
        for rule in ("*", ":*", "bash", "sh -c *", "zsh *", "env *", "python3", "python3 -c *",
                     "node -e *", "claude *", "/usr/local/bin/claude-worker run *",
                     "Bash(ls)", "ls; rm -rf ~", "echo $(id)", ""):
            with self.subTest(rule=rule):
                code, out, _ = self.cli(self.run_args(rules=[rule]))
                self.assertEqual(code, 2, out)
                self.assertEqual(out["status"], "usage")
        self.assertEqual(self.invocations(), [])
        self.assertFalse((self.state / "jobs").exists())

    def test_unknown_flags_are_not_forwarded(self):
        code, out, _ = self.cli(self.run_args() + ["--dangerously-skip-permissions"])
        self.assertEqual(code, 2)
        self.assertEqual(self.invocations(), [])

    def test_root_and_home_are_not_execution_targets(self):
        for cwd in ("/", str(self.home), str(self.tmp / "missing")):
            with self.subTest(cwd=cwd):
                code, out, _ = self.cli(self.run_args(cwd=cwd))
                self.assertEqual(code, 2, out)
        self.assertEqual(self.invocations(), [])

    def test_state_dir_refuses_root_home_and_ancestors_without_chmod(self):
        self.home.chmod(0o755)
        self.tmp.chmod(0o751)
        for state_dir in ("/", str(self.home), str(self.home.parent), "~"):
            with self.subTest(state_dir=state_dir):
                args = self.run_args()
                args[args.index("--state-dir") + 1] = state_dir
                code, out, _ = self.cli(args)
                self.assertEqual((code, out["status"]), (2, "usage"), out)
                self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(self.tmp.stat().st_mode), 0o751)
        self.assertEqual(self.invocations(), [])
        self.assertFalse((self.home / "jobs").exists())

    def test_filesystem_errors_are_reported_as_json(self):
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("file")
        args = self.run_args()
        args[args.index("--state-dir") + 1] = str(blocker)
        code, out, err = self.cli(args)
        self.assertEqual((code, out["status"], out["ok"]), (1, "error", False))
        self.assertIn(str(blocker), out["message"])
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.invocations(), [])

    def test_spaces_and_shell_syntax_are_passed_literally(self):
        weird = self.tmp / "proj $(touch PWNED1) `touch PWNED2`"
        weird.mkdir()
        brief = self.tmp / "brief with spaces.md"
        brief_text = "Run nothing. $(touch PWNED3); `touch PWNED4` && echo '$HOME'\n"
        brief.write_text(brief_text)
        code, out, _ = self.cli(self.run_args(cwd=weird, brief=brief))
        self.assertEqual(code, 0, out)
        call = self.invocations("print")[-1]
        self.assertEqual(call["prompt"], brief_text)
        self.assertEqual(call["cwd"], str(weird))
        self.assertEqual(list(self.tmp.rglob("PWNED*")), [])


class ClassificationTests(WorkerTestCase):
    def test_success_is_durable_and_separates_process_success_from_acceptance(self):
        code, out, err = self.cli(self.run_args())
        self.assertEqual(code, 0, out)
        self.assertEqual(out["status"], "succeeded")
        self.assertEqual(out["worker_reported_status"], "complete")
        self.assertTrue(out["primary_acceptance"].startswith("pending"))
        self.assertEqual(out["models_used"], ["sonnet"])
        self.assertIn("attempt 1", err)
        for path in out["artifacts"].values():
            self.assertTrue(Path(path).is_file(), path)
            self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o600, path)
        for directory in [self.state, Path(out["job_dir"]), Path(out["artifacts"]["result.md"]).parent]:
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)
        self.assertEqual(Path(out["artifacts"]["result.md"]).read_text(), "STATUS: complete\nturns=1")
        code, status, _ = self.cli(["status", out["job_dir"]])
        self.assertEqual((code, status["status"], status["session_id"]), (0, "succeeded", out["session_id"]))
        self.assertFalse(status["lock_held"])

    def test_failures_have_distinct_nonzero_statuses(self):
        cases = {"error": (4, "model_error"), "denied": (5, "denied"), "invalid": (6, "invalid_json")}
        for behavior, (exit_code, status) in cases.items():
            with self.subTest(behavior=behavior):
                self.configure(print=behavior)
                code, out, _ = self.cli(self.run_args())
                self.assertEqual((code, out["status"], out["ok"]), (exit_code, status, False))
                self.assertEqual(self.job(out)["status"], status)
                self.assertTrue(Path(out["artifacts"]["stdout.json"]).is_file())

    def test_denials_are_summarized_with_worker_self_report(self):
        self.configure(print="denied")
        code, out, _ = self.cli(self.run_args())
        self.assertEqual(code, 5)
        self.assertEqual(out["permission_denials"], [{"tool_name": "Bash", "target": "rm -rf build"}])
        self.assertEqual(out["worker_reported_status"], "blocked")

    def test_status_needs_no_claude_or_credentials(self):
        code, out, _ = self.cli(self.run_args())
        self.claude.unlink()
        before = len(self.invocations())
        code, status, _ = self.cli(["status", out["job_dir"]])
        self.assertEqual(code, 0)
        self.assertEqual(status["attempts"][0]["status"], "succeeded")
        self.assertEqual(len(self.invocations()), before)


class LifecycleTests(WorkerTestCase):
    def test_timeout_kills_process_group_and_records_status(self):
        self.configure(print="hang")
        started = time.monotonic()
        code, out, _ = self.cli(self.run_args(timeout=2))
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual((code, out["status"]), (7, "timeout"))
        self.assertFalse(pid_alive(int((self.fake_root / "grandchild.pid").read_text())))
        self.assertFalse(pid_alive(int((self.fake_root / "fake.pid").read_text())))
        attempt = json.loads(Path(out["artifacts"]["attempt.json"]).read_text())
        self.assertEqual(attempt["status"], "timeout")

    def test_sigterm_cancels_and_reaps_descendants(self):
        self.configure(print="hang")
        proc = self.spawn(self.run_args(timeout=120))
        grandchild = self.wait_for(self.fake_root / "grandchild.pid")
        proc.send_signal(signal.SIGTERM)
        stdout, _ = proc.communicate(timeout=30)
        out = json.loads(stdout)
        self.assertEqual((proc.returncode, out["status"]), (8, "cancelled"))
        self.assertFalse(pid_alive(grandchild))
        self.assertEqual(self.job(out)["status"], "cancelled")

    def wait_lock_free(self, path, timeout=15):
        deadline = time.monotonic() + timeout
        fd = os.open(path, os.O_RDONLY)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    return
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        self.fail(f"lock {path} still held")
                    time.sleep(0.05)
        finally:
            os.close(fd)

    def test_held_lock_rejects_second_job_until_claude_child_exits(self):
        self.configure(print="hang")
        first = self.spawn(self.run_args(timeout=120))
        self.wait_for(self.fake_root / "grandchild.pid")
        fake_pid = self.wait_for(self.fake_root / "fake.pid")
        self.configure()
        code, out, _ = self.cli(self.run_args())
        self.assertEqual((code, out["status"]), (9, "busy"))
        self.assertEqual(out["holder_pid"], first.pid)
        # A different project folder is not blocked.
        other = self.tmp / "other"
        other.mkdir()
        self.assertEqual(self.cli(self.run_args(cwd=other))[0], 0)
        # Hard-kill the launcher. The orphaned Claude child still holds the inherited lock descriptor.
        first.kill()
        first.wait()
        self.assertTrue(pid_alive(fake_pid))
        code, out, _ = self.cli(self.run_args())
        self.assertEqual((code, out["status"]), (9, "busy"), out)
        hung = [job for job in (self.state / "jobs").iterdir()
                if json.loads((job / "job.json").read_text())["cwd"] == str(self.project)]
        self.assertEqual(len(hung), 1)
        code, status, _ = self.cli(["status", str(hung[0])])
        self.assertEqual((status["status"], status["lock_held"]), ("running", True))
        process = status["attempts"][-1]["process"]
        self.assertEqual(process, {"launcher_pid": first.pid, "pid": fake_pid, "pgid": fake_pid})
        # The parent ends the recorded group itself; then the retained lockfile is free again.
        os.killpg(process["pgid"], signal.SIGKILL)
        self.assertTrue(Path(out["lock_path"]).exists())
        self.wait_lock_free(out["lock_path"])
        code, status, _ = self.cli(["status", str(hung[0])])
        self.assertEqual((status["status"], status["lock_held"]), ("interrupted", False))
        code, out, _ = self.cli(self.run_args())
        self.assertEqual(code, 0, out)

    def test_lock_is_shared_across_state_dirs(self):
        self.configure(print="hang")
        first = self.spawn(self.run_args(timeout=120))
        self.wait_for(self.fake_root / "grandchild.pid")
        self.configure()
        args = self.run_args()
        args[args.index("--state-dir") + 1] = str(self.tmp / "other state")
        code, out, _ = self.cli(args)
        self.assertEqual((code, out["status"]), (9, "busy"), out)
        self.assertEqual(Path(out["lock_path"]).parent,
                         self.home / ".local" / "state" / "claude-worker" / "locks")
        self.assertEqual(list((self.tmp / "other state" / "jobs").iterdir()), [])
        self.assertFalse((self.state / "locks").exists())
        first.send_signal(signal.SIGTERM)
        stdout, _ = first.communicate(timeout=30)
        self.assertEqual(json.loads(stdout)["status"], "cancelled")
        self.assertEqual(self.cli(args)[0], 0)

    def test_resume_preserves_identity_settings_and_history(self):
        code, first, _ = self.cli(self.run_args(mode="edit", model="opus", effort="high",
                                                rules=["python3 -m unittest *"]))
        self.assertEqual(code, 0, first)
        first_attempt = Path(first["artifacts"]["result.md"]).read_text()
        brief2 = self.tmp / "follow up.md"
        brief2.write_text("Now fix the failing test.\n")
        code, second, _ = self.cli(["resume", first["job_dir"], "--brief", str(brief2), "--timeout", "30"])
        self.assertEqual(code, 0, second)
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(second["result_text"], "STATUS: complete\nturns=2")
        run_call, resume_call = self.invocations("print")
        self.assertEqual(resume_call["opts"]["--resume"], first["session_id"])
        self.assertNotIn("--session-id", resume_call["opts"])
        for flag in ("--model", "--effort", "--tools", "--permission-mode", "--allowedTools"):
            self.assertEqual(resume_call["opts"][flag], run_call["opts"][flag], flag)
        self.assertEqual(resume_call["cwd"], str(self.project))
        self.assertEqual(Path(first["artifacts"]["result.md"]).read_text(), first_attempt)
        self.assertNotEqual(first["artifacts"]["result.md"], second["artifacts"]["result.md"])
        history = json.loads((self.fake_root / "sessions" / first["session_id"]).read_text())
        self.assertEqual(history, ["Do the task.\n", "Now fix the failing test.\n"])
        job = self.job(second)
        self.assertEqual([a["kind"] for a in job["attempts"]], ["run", "resume"])
        self.assertEqual(job["session_id"], first["session_id"])

    def assert_job_kept(self, job, first, settings, kinds):
        self.assertEqual(job["session_id"], first["session_id"])
        self.assertEqual({key: job[key] for key in settings}, settings)
        self.assertEqual([a["kind"] for a in job["attempts"]], kinds)
        self.assertEqual(job["attempts"][0], json.loads(Path(first["artifacts"]["attempt.json"]).read_text()))
        self.assertNotIn("session_history", job)

    def test_failed_resume_keeps_original_session_and_settings(self):
        code, first, _ = self.cli(self.run_args(mode="edit", model="opus", effort="high",
                                                rules=["python3 -m unittest *"]))
        self.assertEqual(code, 0, first)
        settings = {key: self.job(first)[key] for key in ("cwd", "mode", "model", "effort", "allow_commands")}
        session_file = self.fake_root / "sessions" / first["session_id"]
        saved = session_file.read_text()
        session_file.unlink()  # the fake now answers 'No conversation found' with a fresh uuid
        code, failed, _ = self.cli(["resume", first["job_dir"], "--brief", str(self.brief)])
        self.assertEqual((code, failed["status"]), (4, "model_error"), failed)
        self.assertEqual(failed["session_id"], first["session_id"])
        self.assertNotIn(failed["reported_session_id"], (None, first["session_id"]))
        job = self.job(failed)
        self.assert_job_kept(job, first, settings, ["run", "resume"])
        self.assertEqual(job["attempts"][1]["session_id"], first["session_id"])
        self.assertEqual(job["attempts"][1]["reported_session_id"], failed["reported_session_id"])
        self.assertNotIn(failed["reported_session_id"], json.dumps({k: v for k, v in job.items() if k != "attempts"}))
        # The next resume still targets the original session.
        session_file.write_text(saved)
        code, again, _ = self.cli(["resume", first["job_dir"], "--brief", str(self.brief)])
        self.assertEqual(code, 0, again)
        self.assertEqual(self.invocations("print")[-1]["opts"]["--resume"], first["session_id"])
        self.assertEqual((again["session_id"], again["reported_session_id"]), (first["session_id"],) * 2)
        self.assert_job_kept(self.job(again), first, settings, ["run", "resume", "resume"])

    def test_success_under_another_session_id_is_a_mismatch_error(self):
        code, first, _ = self.cli(self.run_args(mode="edit", rules=["python3 -m unittest *"]))
        self.assertEqual(code, 0, first)
        settings = {key: self.job(first)[key] for key in ("cwd", "mode", "model", "effort", "allow_commands")}
        self.configure(print="wrong_session")
        code, out, _ = self.cli(["resume", first["job_dir"], "--brief", str(self.brief)])
        self.assertEqual((code, out["status"], out["ok"]), (12, "session_mismatch", False), out)
        self.assertEqual(out["session_id"], first["session_id"])
        self.assertNotIn(out["reported_session_id"], (None, first["session_id"]))
        self.assertIn("not a same-session continuation", out["message"])
        job = self.job(out)
        self.assert_job_kept(job, first, settings, ["run", "resume"])
        self.assertEqual(job["attempts"][1]["reported_session_id"], out["reported_session_id"])
        code, status, _ = self.cli(["status", first["job_dir"]])
        self.assertEqual((status["status"], status["session_id"]), ("session_mismatch", first["session_id"]))
        # A fresh run reporting a different id is refused the same way.
        other = self.tmp / "other"
        other.mkdir()
        code, run, _ = self.cli(self.run_args(cwd=other))
        self.assertEqual((code, run["status"]), (12, "session_mismatch"))
        self.assertEqual(self.job(run)["session_id"], self.invocations("print")[-1]["opts"]["--session-id"])
        self.configure()
        code, again, _ = self.cli(["resume", first["job_dir"], "--brief", str(self.brief)])
        self.assertEqual((code, again["session_id"]), (0, first["session_id"]), again)

    def test_resume_rechecks_auth(self):
        code, first, _ = self.cli(self.run_args())
        self.configure(auth={"loggedIn": True, "authMethod": "console", "apiProvider": "firstParty"})
        code, out, _ = self.cli(["resume", first["job_dir"], "--brief", str(self.brief)])
        self.assertEqual((code, out["status"]), (3, "auth_failed"))
        self.assertEqual(len(self.invocations("print")), 1)

    def test_resume_rejects_tampered_settings_before_granting_tools(self):
        code, first, _ = self.cli(self.run_args(mode="text"))
        self.assertEqual(code, 0, first)
        job_path = Path(first["job_dir"]) / "job.json"
        original = json.loads(job_path.read_text())
        tampering = {"text mode with commands": {"allow_commands": ["python3 -m unittest *"]},
                     "blanket shell rule": {"mode": "read", "allow_commands": ["bash"]},
                     "unknown mode": {"mode": "yolo"},
                     "bad model": {"model": "--dangerously-skip-permissions"},
                     "foreign state root": {"state_root": str(self.tmp / "elsewhere")},
                     "cwd not a string": {"cwd": ["/"]},
                     "attempts not a list": {"attempts": "x"}}
        for label, change in tampering.items():
            with self.subTest(label):
                job_path.write_text(json.dumps({**original, **change}))
                code, out, err = self.cli(["resume", first["job_dir"], "--brief", str(self.brief)])
                self.assertEqual((code, out["status"]), (11, "invalid_job"), out)
                self.assertEqual(out["job_dir"], first["job_dir"])
                self.assertNotIn("Traceback", err)
        self.assertEqual(len(self.invocations("print")), 1)
        self.assertEqual(len(self.invocations("auth")), 1)

    def test_malformed_or_missing_job_metadata_returns_json_error(self):
        code, first, _ = self.cli(self.run_args())
        job_path = Path(first["job_dir"]) / "job.json"
        for content in ("{not json", "[]", json.dumps({"schema": 1})):
            with self.subTest(content=content):
                job_path.write_text(content)
                for args in (["status", first["job_dir"]],
                             ["resume", first["job_dir"], "--brief", str(self.brief)]):
                    code, out, err = self.cli(args)
                    self.assertEqual((code, out["status"]), (11, "invalid_job"), out)
                    self.assertEqual(out["job_dir"], first["job_dir"])
                    self.assertNotIn("Traceback", err)
        code, out, _ = self.cli(["status", str(self.tmp / "no such job")])
        self.assertEqual((code, out["status"]), (11, "invalid_job"))
        self.assertEqual(len(self.invocations("print")), 1)

    def test_launcher_refuses_to_run_inside_a_worker(self):
        code, out, _ = self.cli(self.run_args(), extra_env={"CLAUDE_WORKER_ACTIVE": "1"})
        self.assertEqual(code, 2)
        self.assertEqual(self.invocations(), [])


if __name__ == "__main__":
    unittest.main()
