# Claude Unattended Harness

A template repository for Claude Code. You describe a project idea; Claude interviews you and sets up an
unattended run that builds the project, checks it against each requirement, and opens a pull request
with a delivery report. It is a loop that sets up the loop: you need not know how such runs work.

## How it works

1. **Start.** Make a repository from this template and open it in Claude Code. Describe your idea.
2. **Interview.** Claude asks one question at a time and writes the spec: requirements with examples,
   constraints, personas. You approve it.
3. **Setup.** Claude prepares the run: where it runs and its boundary, the tools, your secrets (you
   store them outside the chat), your guards, and in a cloud session an hourly wake-up. You approve one
   setup summary.
4. **Run.** A script, `harness/loop.py`, builds the project and asks nothing. Each step is a new Claude
   session that knows only its own step. Scripts decide what is done. The state goes to GitHub after
   each step, so a new session can continue the run.
5. **Stop.** The run stops when the project is delivered, or when no item is left that can run. Each
   blocked item gets one question with a recommended answer.
6. **Result.** A pull request from the branch `build/<project>` with `DELIVERY.md`: each requirement,
   its check, and the result. You merge it.

Keys stay out of the chat and out of git: a network secret in a cloud session, `.env` in a terminal. A
guard is a rule you give at setup; it becomes a block in the deny list or a check script, and a hook
(the command guard) blocks common risky commands. Details: [ARCHITECTURE.md](ARCHITECTURE.md),
[SECURITY.md](SECURITY.md).

## Start in a cloud session (recommended)

On GitHub, click **Use this template** and create a private repository. Open Claude Code on the web,
start a session on that repository, and describe your idea. The `intake` skill does the rest; you type
no terminal command. Claude starts the run as a background command and, if you agree, creates an hourly
scheduled task that wakes the session to continue the run. When `gh` cannot open the pull request,
Claude opens it with its own GitHub tool.

## Start in a Linux terminal

You need `claude` (signed in), `git`, `python3`, `jq`, `bash`, `npx` (Node.js, for the browser tool);
`tmux` and `gh` are recommended. Use a VM or a container that holds only this project. Describe your
idea; after setup the run starts in tmux. Windows: use WSL.

```bash
gh repo create my-project --private --template jcallahanp/Claude-Unattended-Harness --clone
cd my-project
claude
```

| You want to | Command |
| --- | --- |
| See where it stands | `python3 harness/loop.py status` |
| Watch it | `tmux attach` (`bash harness/run.sh` prints the session name; detach: Ctrl+b, then d) |
| Read the log | `tail -f .harness-run/run.log` |
| Start or continue the run | `bash harness/run.sh` |

To answer a stop, run `claude` in the repository. Claude shows each question with its recommended
answer, records your answer, runs `python3 harness/loop.py retry <id>`, and starts the run again.

## What stops a run

- **Delivered.** `DELIVERY.md` is committed and in the pull request.
- **Needs you.** No item can run (each open item is blocked or waits on a blocked one); the audit finds
  gaps after 3 audits; 5 steps in a row made no progress; the sign-in failed; a session kept failing.
- **Continues on a restart.** The step or time budget is used (default 400 steps, 24 hours); a usage
  limit did not clear in 8 hours; Ctrl+C.

A stop by the loop prints its reason, writes it to `.harness-run/last-stop.txt`, and shows in `status`.
A kill, a crash or a refused start writes nothing.

## Proof

- **Self-test:** `bash harness/tests/selftest.sh`. The real loop against a fake model, in about 40
  seconds, with no model calls. It proves delivery, recovery from bad model behaviour, resume from a new
  clone, blocked items, and `status` and `retry`. CI runs it on each push.
- **Preflight:** `bash harness/preflight.sh`. Runs before each run. One real run session proves that run
  sessions load the rules, that the command guard blocks, and that the browser tool opens a local page.
  In the template, before the intake, its spec and secrets lines fail (exit code 1); the `claude` lines
  still prove the three facts. `bash harness/preflight.sh tools` checks only the tools.

In a cloud session the self-test passes and the preflight proves the three facts. Limits: see
[Known limits](ARCHITECTURE.md#known-limits). License: [MIT](LICENSE).
