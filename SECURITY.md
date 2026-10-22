# Security and privacy notes

claude-worker configures Claude Code's permission system and stores job data
locally. It is not a sandbox. This page lists the caveats that matter most; the
[README](README.md) has the overview.

## `--cwd` is not a file-access boundary

`--cwd` is the worker's working directory and the identity used for the per-folder
lock. Claude Code's tools may reach paths outside it (absolute paths, `..`, symlinks,
anything your user account can access, subject to Claude Code's own permission
handling). Scoping in the brief ("edit only `src/foo.py`") is an instruction to the
model, not isolation.

## Permissions do not make arbitrary code safe

- Modes (`text`, `read`, `edit`) and `--allow-command` rules are permission
  configuration for the Claude CLI, not OS isolation. `read` and `edit` include
  WebSearch, WebFetch and native subagents (Agent), so read mode is not offline.
  Keep network and subagent scope explicit in the brief; the installed CLI
  controls tool permission handling.
- `--allow-command` rules are preapprovals, not a complete command firewall. With
  Bash enabled, the CLI's stock handling may still permit some other commands.
- In `edit` mode the worker can rewrite files, including a test or script that you
  preapproved, and then run it. A preapproved command is only as trustworthy as the
  files it executes. Review changes before running anything the worker touched.
- For real isolation use a container, VM or separate OS account, and a disposable
  worktree.

## Authentication checks

The launcher requires `claude auth status --json` to report a logged-in `claude.ai`
`firstParty` subscription (`max`, `pro`, `team` or `enterprise`) and strips
`ANTHROPIC_*`, `CLAUDE_CODE_USE_*` and `CLAUDE_CODE_OAUTH_TOKEN` from the child
environment. This is a launch-time check of what the CLI reports. It does not audit
billing or guard against future CLI behaviour.

## Stored data

State lives under `~/.local/state/claude-worker/` by default, or under `--state-dir`.
Directories are `0700` and files `0600`. Briefs, system prompts, raw output, results
and metadata are kept in full and are never deleted automatically. Treat them as
sensitive and do not commit or share them.

- **`--state-dir` should be a dedicated folder.** If it already exists, it is
  `chmod 0700`, not only created that way. The launcher refuses `/`, your home
  directory and its ancestors, but any other existing directory you point it at
  will have its permissions changed.
- **Global instructions in `text` mode.** If `~/.claude/CLAUDE.md` exists, its
  contents (first 200,000 characters) are placed in the system prompt. That text is
  saved in each attempt's `system-prompt.md` and is also passed to the Claude process
  as a command-line argument, so it can be visible to other local users or tools that
  can list process arguments. A very large instructions file may exceed the operating
  system's argument size limit and prevent the process from starting. In `read` and
  `edit` modes only the file's path is mentioned and the worker is asked to read it.
- **`status` side effects.** `status` makes no auth or model call, but it may create
  or `chmod` the local lock directory. A held lock identifies a folder, not
  necessarily the job being inspected.

## Reporting issues

Report vulnerabilities privately to the maintainer, using the hosting platform's
private security reporting where available. Do not include credentials or sensitive
job artifacts in a public issue.
