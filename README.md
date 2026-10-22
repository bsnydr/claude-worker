# claude-worker

A single-file Python launcher that runs one bounded, non-interactive
[Claude Code](https://docs.claude.com/en/docs/claude-code) job from a script or
another coding agent, and reports the outcome as one JSON object.

This is an independent community project. It is not an official Anthropic or
OpenAI product and is not affiliated with or endorsed by either company.

## Problem it addresses

When one tool (a script, or a primary coding agent) wants to hand a bounded task
to Claude Code, a bare `claude --print` call leaves several things to the caller:
which tools the worker gets, whether API keys or provider switches in the
environment silently change how it authenticates, what happens on timeout, whether two
jobs edit the same folder at once, and where the transcript and result go.
`claude-worker` wraps those concerns:

- Verifies the logged-in Claude Code account is a `claude.ai` subscription login
  before every run, and fails closed otherwise.
- Strips API-key, OAuth-token and provider-override environment variables from the child.
- Starts Claude Code with a fixed tool set (`text`, `read` or `edit` mode) and
  explicit Bash preapproval rules that you supply (see the safety section for
  what those do and do not enforce).
- Allows one job per working folder at a time.
- Enforces a timeout and terminates the whole process group.
- Saves the brief, argv, result and metadata per attempt so a job can be inspected
  and resumed.

## Requirements

- macOS or Linux. The launcher uses `fcntl` file locking and POSIX process
  groups, so **Windows is not supported**.
- Python 3.12 or newer, standard library only. This is a conservative support floor,
  not a claim that the language syntax requires it. CI is configured for Python 3.12
  and 3.14 on Ubuntu and macOS. At this edition's review on 21 October 2026, no
  remote matrix result had been verified; check the repository's Actions tab for
  current results. Local verification was on macOS with Python 3.14.6 only.
- For real use only: an installed, logged-in
  [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI. The launcher passes
  flags such as `--safe-mode`, `--permission-prompts` and `--setting-sources`; it
  needs a CLI version that accepts them. Nothing else is required, and the tests do
  not need Claude at all.

## Installation

The launcher is one executable file. Copy or symlink it into a directory on your
`PATH` that you choose:

```sh
chmod +x claude-worker
mkdir -p "$HOME/bin"                      # or any bin directory you prefer
cp claude-worker "$HOME/bin/claude-worker"
claude-worker --help
```

Or run it in place with `python3 /path/to/claude-worker ...`.

Claude CLI discovery: `--claude-bin PATH` if given; otherwise `/opt/homebrew/bin/claude`
if it exists (the default Homebrew location on Apple Silicon); otherwise the first
`claude` on `PATH`.

## Usage

The operational subcommands (`run`, `resume`, `status`, `auth`) print one JSON object
to stdout, including on failure, and short progress lines to stderr. `--help` prints
ordinary help text instead.

### Check auth (no model call)

```sh
claude-worker auth
```

Authenticate first with `claude auth login` using your Claude.ai account.

### Run a job

```sh
claude-worker run \
  --cwd /path/to/project \
  --brief brief.md \
  --model sonnet \
  --effort medium \
  --mode read \
  --timeout 900
```

`--model`, `--effort`, `--mode` and `--timeout` (seconds) are all required; nothing
is defaulted so every job states what it is asking for.

| Option | Meaning |
| --- | --- |
| `--model` | Passed through to `claude --model` (letters, digits, `.`, `_`, `-`, `[`, `]` only). |
| `--effort` | One of `low`, `medium`, `high`, `xhigh`, `max`. |
| `--mode` | `text`: no tools. `read`: Read, Glob, Grep, WebSearch, WebFetch and Agent. `edit`: also Edit and Write. |
| `--allow-command RULE` | Repeatable. Adds a `Bash(RULE)` preapproval, written as a Claude Code command pattern (a trailing `*` is a wildcard). Not allowed in `text` mode. |
| `--cwd DIR` | Working directory for the worker and the identity of the folder lock. **Not** a file-access boundary (see [SECURITY.md](SECURITY.md)). |
| `--state-dir DIR` | Where job state is stored (default below). Use a dedicated folder: an existing directory is `chmod 0700`. |
| `--claude-bin PATH` | Claude CLI to use. |

Example preapproving one literal test command (no wildcard) in a synthetic repository:

```sh
claude-worker run --cwd ~/src/example-app --brief brief.md \
  --model sonnet --effort high --mode edit --timeout 1800 \
  --allow-command 'python3 -B -m unittest -v test_example_app'
```

A rule ending in `*` (such as `python3 -m unittest *`) is a command pattern that
preapproves any arguments after the prefix.

Rules naming shells or wrappers (`bash`, `env`, `sudo`, ...), recursive agents
(`claude`, `claude-worker`, `codex`), bare wildcards, shell operators, or interpreters
with inline code (`python3 -c`, `node -e`, ...) are rejected. The `--cwd` may not be
`/`, your home directory or one of its ancestors.

### Example bounded brief

The brief is the worker's whole authorization and is sent on stdin. Keep it narrow.
A synthetic example for a made-up repository:

```markdown
# Task: fix the failing slugify test in example-app

Scope: edit only `example_app/text.py` (the test module `test_example_app.py` sits at
the repository root). Do not touch tests, other modules, or
configuration. No network access, no dependency changes.

Problem: `python3 -B -m unittest -v test_example_app` fails on
`test_slugify_collapses_spaces` (repeated spaces yield repeated hyphens).

Done when: that test passes and the rest of `test_example_app` still passes.
Run only `python3 -B -m unittest -v test_example_app`.

Report: files changed, the command you ran and its output, anything you were
unable to do.
```

The worker is asked to finish with a first line of `STATUS: complete`,
`STATUS: partial` or `STATUS: blocked`. That is the worker's own claim; review the
changes yourself.

### Status and resume

```sh
claude-worker status ~/.local/state/claude-worker/jobs/<job-id>
claude-worker resume ~/.local/state/claude-worker/jobs/<job-id> --brief followup.md
```

`status` makes no auth check and no model call. It reads the saved job, but may
create or `chmod` the local lock directory under the default state location. It
reports `interrupted` when a job is recorded as running and no process holds the lock
for its folder; a held lock identifies the folder, not necessarily that same job, so
treat `lock_held` as a hint rather than proof the job is still running.

`resume` continues the **same Claude session** with a new brief. It reuses the saved
`cwd`, `mode`, `model`, `effort` and `--allow-command` rules; they cannot be changed
on resume. Only `--timeout` and `--claude-bin` may be overridden. Auth is re-checked,
the folder lock is re-taken, and if an otherwise successful response reports a
different session id the attempt is marked `session_mismatch` rather than treated as a
continuation. Claude-reported errors remain `model_error`.

### Output, exit status and help

```sh
claude-worker --help
claude-worker run --help
```

The process exit code equals the `exit_code` field of the JSON output:

| Code | Status | Meaning |
| --- | --- | --- |
| 0 | `succeeded` | Claude finished cleanly. This is not acceptance; review the work. |
| 1 | `error` | Filesystem or unexpected error. |
| 2 | `usage` | Bad arguments or refused request. |
| 3 | `auth_failed` | Auth check did not pass. |
| 4 | `model_error` | Claude reported an error result. |
| 5 | `denied` | The worker hit permission denials. |
| 6 | `invalid_json` | Claude returned no parseable result. |
| 7 | `timeout` | Timeout reached; process group terminated. |
| 8 | `cancelled` | SIGINT/SIGTERM/SIGHUP received. |
| 9 | `busy` | Another job holds the folder lock. |
| 10 | `process_error` | Could not start Claude, or it exited non-zero after a success result. |
| 11 | `invalid_job` | Saved job metadata missing or unusable. |
| 12 | `session_mismatch` | An otherwise successful response reported a different session id. |

`result_text` in the JSON is truncated at 20,000 characters; the full text is in the
attempt's `result.md`.

## Authentication behaviour

Before `run` and `resume` (and for `auth`), the launcher executes
`claude auth status --json` with a scrubbed environment and requires **all** of:

- `loggedIn` is `true`
- `authMethod` is `claude.ai`
- `apiProvider` is `firstParty`
- `subscriptionType` is one of `max`, `pro`, `team`, `enterprise`

Anything else fails with `auth_failed` before a model process starts. The scrubbed
environment removes every variable beginning `ANTHROPIC_` or `CLAUDE_CODE_USE_`
and `CLAUDE_CODE_OAUTH_TOKEN`, so API keys, custom base URLs and Bedrock/Vertex-style
switches inherited from your shell are not passed to the child. Only the four fields
above are stored or printed from the auth output; email and organisation details are
not.

What this is and is not: it is a check of the account state the CLI reports *at launch
time*, intended to keep jobs on your logged-in subscription route instead of an API
key. It does **not** audit billing, guarantee how any plan meters or allows usage, or
protect against future changes in Claude Code's behaviour or its `auth status` output.
Whether a given plan can be used this way is between you and your Anthropic
agreement; no plan is guaranteed to work.

## Safety model: permissions, not a sandbox

Modes and `--allow-command` rules configure **Claude Code's permission system**.
They are not OS-level isolation. The Claude process runs as your user and, by
design, can do whatever those permissions plus the tools allow. If you preapprove a
test command, the test code itself runs with your privileges. Use containers, VMs or
separate accounts if you need real isolation.

`--allow-command` rules are **preapprovals, not a complete command firewall**. They
make the named commands run without a prompt (prompts are off). When Bash is enabled
(that is, when at least one rule is given), the CLI's stock permission handling may
still permit some other commands, such as certain read-only ones, and the launcher
does not try to block them. Without any rule, Bash is not enabled at all. The launcher
also rejects some risky rule shapes (shells, wrappers, inline interpreter code), but
that is a sanity check on your rules, not a guarantee about what the worker can run.

`--cwd` is a working directory and lock identity, not a file-access boundary; tools
may reach outside it, and scoping in the brief is instruction, not isolation. In
`edit` mode the worker can rewrite a preapproved test or script and then run it. See
[SECURITY.md](SECURITY.md) for details.

What the launcher sets on the Claude invocation: `--safe-mode`, no setting sources,
hooks disabled, empty strict MCP config (so user CLAUDE.md, memory, hooks, plugins,
skills and MCP servers are not loaded automatically), no permission prompts
(`--permission-prompts none`), the mode's tool list. Native subagents (`Agent`) and web research (`WebSearch`,
`WebFetch`) are enabled in `read` and `edit` modes. Their use remains subject to the
brief and installed CLI permission handling; read mode is not an offline mode.
The worker's system prompt tells it not to launch `claude`, `claude-worker` or
`codex` or other agent CLIs; native subagents are permitted. The launcher refuses to start inside another claude-worker job. These
rely on the installed CLI honouring those flags.

## Privacy and stored data

Everything is stored locally; the launcher sends nothing anywhere itself. (The Claude
CLI it starts of course sends the prompt to Anthropic under your account.)

Default state location: `~/.local/state/claude-worker/` (override job location with
`--state-dir`). Directories are created `0700` and files `0600`.

```
jobs/<job-id>/job.json            settings, status, auth summary, attempt records
jobs/<job-id>/brief.md            original brief
jobs/<job-id>/attempts/NNN/       brief.md, system-prompt.md, argv.json, auth.json,
                                  stdout.json, stderr.log, result.md, attempt.json
locks/<hash>.lock                 per-folder lock files (always under the default location)
```

**Treat all of this as sensitive.** Briefs, raw Claude output (`stdout.json`,
`stderr.log`), results, and the system prompt are saved in full and may contain
source code, paths and anything you or the model put in them. In particular:

- In `text` mode, if `~/.claude/CLAUDE.md` exists (path resolved from the current
  user's home), its contents (first 200,000 characters) are embedded into the system
  prompt, and therefore written to every attempt's `system-prompt.md`. It is also
  passed to Claude as a process argument, and a very large file may exceed the OS
  argument limit. In `read` and `edit` modes the prompt only tells the worker to read
  that file itself.
- `job.json` records `schema`, `job_id`, `job_dir`, `state_root`, `created_at`,
  `updated_at`, `cwd`, `model`, `effort`, `mode`, `allow_commands`, `timeout`,
  `claude_bin`, `session_id`, `session_confirmed`, `lock_path`, `auth` (the four
  checked fields), `status`, and `attempts` (per-attempt records).

**Retention:** nothing is ever deleted automatically. Remove job directories yourself
when you no longer need them, and do not commit or share them. The `.gitignore` here
excludes common state and output directories as a precaution only. Use a dedicated
`--state-dir`: an existing directory you point it at is `chmod 0700`. More in
[SECURITY.md](SECURITY.md).

## Concurrency, timeouts and cleanup

- **One job per folder.** A non-blocking advisory `flock` is taken per canonical
  `--cwd`. A second `run` or `resume` for the same folder exits with `busy` (code 9).
  The lock is held by the launcher and inherited by the Claude child, so a hard-killed
  launcher does not release the folder while Claude may still be running. Use a
  separate git worktree or copy for concurrent edits. Advisory locks only coordinate
  claude-worker processes; they do not stop other programs.
- **Timeout and process group.** Claude runs in its own session/process group. On
  timeout, or on SIGINT/SIGTERM/SIGHUP, the launcher sends SIGTERM to the group,
  then SIGKILL after a 5 second grace, and records the outcome. Processes that
  deliberately leave the group are not tracked.

## Offline tests

The tests use a fake `claude` script, a temporary `HOME` and temporary state
directories. They make no network or model calls and read none of your real Claude
configuration. Run them from the repository root:

```sh
python3 -B -m unittest -v test_claude_worker
```

They cover, among others: auth fail-closed cases and secret redaction, environment
scrubbing, command-rule rejection, folder locking, timeout and process-group cleanup,
resume with the same session, and rejection of tampered job metadata. The same
command is configured to run in GitHub Actions on Ubuntu and macOS
(`.github/workflows/ci.yml`). As of the 21 October 2026 review, no remote matrix
result had been verified for this edition. Check the Actions tab before assuming
cross-platform compatibility.

## Limitations

- POSIX only; no Windows.
- Tied to the Claude Code CLI flags and `auth status --json` shape the tests model.
  A different CLI version may behave differently; the fake CLI in the tests is not
  a guarantee about the real one.
- Not a security boundary (see above), and not a billing audit.

## License

MIT. See [LICENSE](LICENSE).
