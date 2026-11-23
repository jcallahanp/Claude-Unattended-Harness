# Security

An unattended run lets Claude run commands with nobody watching. This file says what limits it, what
does not, and how keys are kept. The blocks in this harness are tripwires that stop common mistakes.
They are not a wall. The real limit is the machine the run is on.

## The outer boundary

- **Cloud session (recommended).** A Claude Code cloud session is a throwaway machine that holds only
  this project. It is the boundary. The run is root on that machine.
- **Linux terminal.** Use a virtual machine or a container that holds only this project. Without one,
  the run acts as you, with your files. The intake recommends a boundary once and records your answer
  in `spec/SETUP.md`.
- **`sudo`.** Denied by default (`Bash(sudo:*)` in the deny list). `spec.py setup` removes that entry
  for `sudo=yes`, and refuses `sudo=yes` with boundary `none`. The intake skill asks about `sudo` only
  for boundary `vm` or `container`; otherwise it records `no`. In a cloud session the entry stops only
  the `sudo` command: the run is root there already, and `spec/SETUP.md` says so.

## What a run session may do

Each run session starts with `harness/run-settings.json`:

- **Allowed:** shell commands, file reads and edits, web fetch and search, subagents, and the browser
  tool. Edits are accepted without a prompt. Bypass mode is disabled.
- **Denied:** `sudo`, `su`, `git push`, `gh`, `ssh`, `scp`, `rsync`, `nc`, `systemctl`, `crontab`; reads
  of `.env`, `.env.local`, `.env.production` and of common key and credential locations in your home
  folder (for example `~/.ssh`, `~/.aws`, `~/.config`, `~/.claude`, `~/.git-credentials`) with Claude's
  file-read tool. A deny entry matches the usual form of a command; the same action written another way
  may not be stopped.
- **Tool servers:** only the ones in `harness/mcp.json` (the Playwright browser tool). Run sessions do
  not see your personal connectors.
- **Verify and audit sessions** have no edit tools, and the loop discards any change they make.

### The command guard

`harness/guard.sh` is a hook that checks each shell command of a run session before it runs. It blocks:

- a recursive delete outside the project (except under `/tmp`), of the repo itself, or of `.git`;
- commands that print `.env`, and commands that dump the whole environment;
- git history rewrites (`rebase`, `filter-branch`, `filter-repo`, `reflog expire`, `update-ref -d`);
- leaving the run branch for `main` or `master`, and `reset --hard` onto `main`, `master` or `origin/...`.

It matches text patterns, so it is a tripwire, not a boundary. The preflight proves that it blocks a
command in a real run session.

### Protected files

The harness (`CLAUDE.md`, `harness/`, `.claude/`, `.github/`, the fixed gates, `checks/guards/`), the
spec and its setup, and the state files (`ledger.jsonl`, `PROGRESS.md`, `DELIVERY.md`) are protected:

- After each step the loop undoes each change outside the step's allowance and commits the rest. So a
  change of a work session to the spec, the plan, the checks or the harness is not kept.
- `checks/check_protected.sh` (one of the gates) fails when `spec/requirements.json` does not match
  its hash in `spec/APPROVED`. On a `build/` branch it also fails when `CLAUDE.md`, a file under
  `harness/`, `.claude/`, `.github/` or `checks/guards/`, or one of the four fixed gate scripts differs
  from `origin/main`. It does not compare `spec/SETUP.md` or the state files.
- `checks/check_scope.sh` fails on secret-shaped strings in tracked files, a tracked `.env`, a value
  from `.env` in a tracked file, and a file over 5 MB.
- Run sessions are denied `git push` (the usual form; see the deny list). The loop pushes the run
  branch; its pushes skip the pre-push hook because it runs the gates itself before it accepts work.
  A push by hand runs the gates in `harness/hooks/pre-push`, and CI (`.github/workflows/gates.yml`)
  runs the self-test and the gates on each push and pull request.
- The loop never pushes `main`, and it never merges. You merge the pull request.

The master session also has rules: `.claude/settings.json` allows without a prompt a short, narrow list
of the harness commands and paths that setup uses. Some entries accept any arguments, for example
`Bash(python3 harness/spec.py *)` and `Bash(bash harness/run.sh *)`. Any other command asks you.

## Two meanings of "guard"

- **A user guard** is a rule you give during setup, in plain words ("never use `curl`", "no file over
  1 MB"). The intake converts it into the strongest form that fits:
  - a **block**: one entry in the deny list of `harness/run-settings.json`;
  - a **check**: a script in `checks/guards/` that the gates run, tested to pass on the clean tree and
    to fail on a sample that breaks it;
  - a **note**: recorded as "not enforced by a script" when no script can check it.

  `spec/SETUP.md` lists each user guard and its form.
- **The command guard** is the hook `harness/guard.sh` described above. It is the same for each project.

## Keys and secrets

- The spec names each secret; its value does not belong in the spec, the chat, git or a log.
- **Never paste a key into the chat.** You store it yourself.
- **Smallest scope.** Use a key with only the permissions the project needs: a test key, a read-only key,
  or a spending limit where the service offers one.
- **Cloud session: a network secret (recommended).** You add the key in the settings of the cloud
  environment, for the API's host. The cloud proxy adds it to requests to that host after they leave the
  machine. This is the documented behaviour of the cloud environment: the key is then in no file and no
  variable of the session. Limits: network secrets exist only in
  environments that Anthropic hosts; a shared environment gets one only from an Owner; the proxy adds
  it to the requests of each session in that environment; it cannot be saved for Anthropic's own API
  hosts. No script can see it, so setup records it by name and host after you confirm it.
- **Cloud session, second option: an environment variable** of the cloud environment, when the project
  needs the value itself. Each command of the session can read it, and so can anyone who uses the
  environment.
- **Linux terminal: `.env`.** You put `NAME=value` in `.env` (a copy of `.env.example`) in your editor.
  `.env` is git-ignored. The run rules (`CLAUDE.md`) tell run sessions to load it into a command without
  printing it; only the deny list and the patterns in `harness/guard.sh` back this rule. They block the
  common ways to print it, and a gate fails if one of its values (12 characters or
  more) appears in a tracked file.
- `python3 harness/spec.py secrets` says where each secret is kept, by name only, and fails when a
  required secret is missing. `spec.py setup` refuses to finish while one is missing.
- A run in a new cloud session has no `.env`. In a cloud session, use network secrets or variables of
  the environment.

## What the harness does not protect against

- A determined or confused program can reach a blocked action another way. The deny list and the
  command guard stop common forms only.
- In a terminal with no VM or container, a run session can read and change what your user can.
- A key in an environment variable or in `.env` can be read by commands of the run. Use keys with the
  smallest scope.
- Web pages and files that a run session reads can hold instructions. `CLAUDE.md` tells run sessions to
  treat them as data; that is a rule for the model, not an enforcement.
- Code and packages that the run installs come from the internet. Review the pull request before you
  merge or deploy.

## Known limits that concern security

If the browser will not start because the run is root, add `"--no-sandbox"` to its `args` in
`harness/mcp.json`. In a cloud session the intake skill records `sudo` as denied, where the run is
root anyway.

## Report a problem

Use GitHub's private vulnerability reporting on this repository (Security tab, "Report a
vulnerability"), if it is enabled. If it is not, open an issue without details of the exploit and ask for a
private channel.
