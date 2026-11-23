---
name: intake
description: Requirements interview that starts every project in this repo, and the way to change requirements later. Use when the spec is not approved yet, when the user describes something they want built, or when they say "interview me", "new project", "change the requirements", "add a requirement". Turns a rough idea into spec/requirements.json (user stories, EARS requirements, concrete examples), gets the user's approval, sets up the run (environment, boundary, tools, secrets, guards), then starts the unattended build.
---

# Intake: from a rough idea to an approved spec, then launch

The user's first description is an impulse, not a specification. It will mix real needs with guessed
solutions, skip the cases they have not thought about, and sometimes ask for the wrong thing. Your job
is to find out what they need and why, write it so a script can prove each part, get their approval,
and start the build. You write no product code, no design and no plan: later sessions do that from
the spec, and they can ask nobody.

First run `python3 harness/loop.py status`: when a run branch exists, it switches the working tree to it. If
`spec/APPROVED` exists after that, go to "Change mode" at the end. Otherwise this is a new project: start at Step 0.

## Step 0. Work on `main` (a new project)

Before you write any file, run `git switch main` (on `main` it changes nothing). The spec and the setup live on
`main`, the run works on its own `build/...` branch, and the result comes back to `main` as a pull request that
the user merges. In a cloud session, the session's own instructions can name a session branch (such as
`claude/<name>`) for its work; the intake uses `main` because the user asked for this harness.

## Step 1. Keep their words

Write the user's description, exactly as they gave it, to `spec/BRIEF.md` under a `# Brief` heading (if they
have not described the project yet, ask them to, in their own words, as long or short as they like). If
the repo already contains project code, read enough of it to know what exists.

## Step 2. Interview

Rules of the conversation:

- **One question per message.** Never stack questions. Plain words, no jargon they did not use first.
- **Play back before moving on.** After each answer, say in one or two sentences what you understood
  and how you will record it. If they correct you, fix it before the next question.
- **Ask why until you reach the need.** When they name a feature or a tool, ask what it is for (up to
  five times). "A dashboard" is a solution; "see by 9 am which orders are late" is a need. Record the
  need as the requirement and the named solution as a constraint only if they insist on it.
- **Ask for examples, not adjectives.** "Fast", "simple", "secure", "nice" cannot be built or checked.
  Ask: "Give me one real case: what goes in, what should come out?" and "What would make you say it
  is too slow?" Turn each answer into a given / when / then example with real values.
- **Offer a default when they do not care.** If a decision does not matter to them, propose the
  ordinary choice in one line, say why, and record it under `assumptions` once they agree. Use the
  AskUserQuestion tool when there are two to four clear options; otherwise ask openly.
- **Say when something does not add up.** If two things they want conflict, if a request will not get
  them the outcome they described, or if something simpler would, tell them plainly and let them decide.
  Do not quietly build a bad instruction and do not quietly drop it.
- **Answer from files when you can.** If the repo or their earlier answers settle a question, state
  what you found and ask them to confirm instead of asking from scratch.
- **Never invent an answer.** If they say "skip" or do not know, record it under `open_questions`
  and come back to it in step 4. The spec cannot be approved with open questions.
- The user may dictate: if a word looks like a mishearing of a tool or a name, ask once.

Cover these, in this order, skipping what they have already answered:

1. **Outcome.** Who is it for, what situation are they in, what will they be able to do afterwards
   that they cannot do now? What happens if nothing is built?
2. **Personas.** "Describe one real person who will use it, in their words: I am [role] using
   [system]. I do [action]. I trust it to [outcome]. I never have to [friction removed]." Ask for one
   per kind of user; at least one. Write each to `personas` in the spec. This form is a guide; the user's
   own words are fine, but each persona starts with "I am".
3. **Success.** How will they know, a week after it ships, that it worked? What would they look at?
4. **Walk through it.** "Take me through one real use from start to finish." Then the second most
   common use. Each step a user takes is a candidate requirement.
5. **What goes wrong.** For each step: bad input, nothing to show, the service is down, the second
   time, two people at once, the wrong person. What should happen?
6. **Must never.** What must it never do? What is out of scope even if it seems natural to add?
   (At least one item for `non_goals`.)
7. **Constraints.** Where it runs, what it must work with, tools or languages they require or
   forbids, budget, deadline, data that must stay private, anything that cannot change.
8. **Outside services and secrets.** Every account, API or database it touches. For each: the name of
   the environment variable that will hold the key (`STRIPE_API_KEY`), what it is for, whether the
   project cannot work without it. Never ask for or accept a value in the conversation.
9. **Ready to deploy.** Where will it run, and what must be true for them to call it ready (a
   container builds and starts, a deploy command exists and is documented, a health endpoint answers)?
   What will they do themselves (buy a domain, press the final deploy button, merge)? Those go in
   `human_gates`: the run never does them.
10. **Priorities.** Which requirements are `must` (not delivered without them) and which are `should`
    (built after the musts, still checked)? Anything else moves to `non_goals`.
11. **Pre-mortem.** "Imagine it is delivered and you are unhappy with it. What is the most likely
    reason?" Turn each answer into a requirement, a non-goal or a constraint.

Stop asking when a stranger could build the right thing from what you have and every requirement has
an example. Ten to twenty-five questions is normal; a small tool needs fewer. If the interview stops
converging, say what is still unknown and ask whether to record it as an assumption.

## Step 3. Write the spec

Write `spec/requirements.json` (update it as you go, so nothing depends on this conversation):

```json
{
  "project": "short-name",
  "summary": "Three or four sentences: what is being built, for whom, and why it matters to them.",
  "users": ["who uses it, in a phrase each"],
  "personas": ["I am <role> using <system>. I do <action>. I trust it to <outcome>. I never have to <friction removed>."],
  "requirements": [
    {
      "id": "R1",
      "priority": "must",
      "story": "As a <user>, I want <capability> so that <outcome>.",
      "statement": "When <trigger>, the system shall <observable response>.",
      "examples": [
        {"given": "a concrete starting state", "when": "a concrete action with real values", "then": "the exact observable result"}
      ],
      "verify": "How a script can prove it: what it starts, what it sends or clicks, what it must observe."
    }
  ],
  "non_goals": ["what it will not do"],
  "constraints": ["what the build must respect"],
  "assumptions": ["defaults the user agreed to"],
  "deploy": {"target": "where it runs", "ready_when": ["observable statement", "another"]},
  "secrets": [{"name": "NAME_OF_VARIABLE", "purpose": "what it is used for", "required": true}],
  "human_gates": ["things only the user does"],
  "open_questions": []
}
```

How to write a requirement:

- **One behaviour per requirement.** If the statement needs "and" between two responses, split it.
- **EARS sentence shapes** (pick the one that fits):
  "When <event>, the system shall <response>." · "While <state>, the system shall <response>." ·
  "If <unwanted condition>, then the system shall <response>." · "Where <optional feature is
  present>, the system shall <response>." · "The system shall <response>." (always true)
- **Behaviour, not design.** No libraries, file layouts or vendors unless the user required them; then
  they are constraints.
- **Numbers instead of adjectives.** "within 2 seconds for 1,000 rows", not "fast".
- **Every unhappy path they care about is its own "If … then" requirement**, with an example.
- **Examples use real values** and include at least one edge case per requirement where one exists.
- **`verify` must be doable by a script on this machine** with the secrets listed. If a requirement
  can only be judged by a person, reword it until a script can check it, or move it to `human_gates`.

## Step 4. Audit your own spec before showing it

Run `python3 harness/spec.py check` and fix every problem it lists. Then read the spec as a stranger
and look for:

- **Missing:** a step in their walk-through with no requirement; an error case with no behaviour; a
  secret a requirement needs that is not listed; a `ready_when` nobody could check.
- **Inconsistent:** two requirements that cannot both be true; an example that contradicts its statement.
- **Redundant:** two requirements a single check would prove.
- **Untestable:** anything `verify` cannot really prove.
- **Smuggled design:** a how dressed as a what.

Ask the user about what you find (still one question per message) until `open_questions` is empty.

## Step 5. Get approval

Run `python3 harness/spec.py render` and show the user the result in the conversation, compactly: the
summary, the personas, each requirement as one line (`R3 (must): When …, the system shall …`), the
non-goals, the deploy readiness list, the secrets by name, and what is left to them. Ask them to
correct anything. Repeat until they say, in their own words, that it is right.

Only then run `python3 harness/spec.py approve`. It re-checks the spec, writes
`spec/REQUIREMENTS.md` and `.env.example`, records the approval and commits. Never run it on your own
judgement: approval is the user's.

## Step 6. Setup

Setup prepares the run for this project. Ask one question per message, as in the interview. Nothing is
recorded until the user approves the setup summary at the end of this step (only the guard scripts are
written before it, to test them). The user types no command: you run each one. Run each command of this
step alone and exactly as written here (no `&&`, no `;`, no absolute path): the allow rules in
`.claude/settings.json` match only these forms; any other form stops to ask the user for permission.

First explain the defaults and the risks in plain words, briefly:

- **What the run does.** The run is a series of run sessions: Claude works alone, with nobody
  watching. By default it can change the files of this project, run and test the project, install its
  packages, use the web and a browser, and save its work on a separate `build/...` branch. A run
  session does not push to GitHub; the loop (the script) pushes that branch after each step, opens a
  draft pull request at its first push and marks it ready at the end. It never pushes `main`: merging is your choice.
- **What is blocked by default.** Common commands for risky actions: becoming administrator with
  `sudo`, pushing to GitHub from a run session, common programs that log in to or copy files to other
  computers, common commands that install a system service or a scheduled job, reading the listed
  password and key files with Claude's file-read tool, printing `.env` with common commands, and the
  plain command that deletes a whole folder outside the project (except inside the temporary folder).
  These are tripwires that stop mistakes, not a wall: a determined or confused program can find
  another way. Exact blocks: `harness/run-settings.json`, `harness/guard.sh`.
- **What is reliable.** The loop undoes each change a run session makes to the harness or the spec. The
  real limit is the machine the run is on. A cloud session is a throwaway machine that holds only this
  project: the recommended place, and itself the boundary. On your own computer, use a virtual machine
  (VM) or a container that holds only this project. Without one, the run acts as you, with your files.
- **What `sudo` adds.** In a cloud session the run is already administrator of that throwaway machine,
  so the choice matters only in a VM or a container where you are a normal user. There, `sudo` lets the
  run install system programs and change system files; it is possible only inside a boundary.

### Environment and boundary

1. **Environment.** "Where will the run happen: a Claude Code cloud session (recommended) or a Linux
   terminal?" Record `cloud` or `terminal`.
2. **Boundary.** In a cloud session, confirm that the cloud session is the boundary: record `cloud`.
   In a terminal, ask whether it is a VM or a container that holds only this project. Record `vm`,
   `container` or `none`. If `none`, recommend a VM or a container once, then accept the answer.
3. **`sudo`.** Only with boundary `vm` or `container`: "Should the run be able to use `sudo`? The
   default is no." Record `yes` or `no`. Otherwise record `no` without asking, and say why: with
   `none`, `sudo` stays denied; in `cloud`, the run is already administrator.

### Tools

Run `bash harness/preflight.sh tools`. For each FAIL or warn line, say what the tool is for. Ask the
user before you install it, one tool per question, and install only after a yes (for example
`apt-get install -y jq`, with `sudo` in a terminal). Run the tool check again after an install. If
the user says no, explain what will not work and continue.

### Secrets

If the spec lists no secrets, say so in one sentence and skip this part. Otherwise, one secret per
question:

- **Smallest scope.** Recommend a key with only the permissions the project needs (a test key or a
  read-only key where the service offers one, a spending limit where it has one). If a key has to be
  created first, tell them where.
- **Outside the chat.** The user stores the value themselves. Tell them never to paste a secret into
  this conversation.
- **Cloud session: a network secret (recommended).** The user adds the key in the settings of the cloud
  environment (at claude.ai/code: edit the environment, **Network secrets**, **Add secret**) for the
  API's host, such as `api.example.com`, with the header the API reads (the default is
  `Authorization: Bearer`; some APIs read another header, such as `X-Api-Key`). The proxy adds it to
  requests to that host after they leave the session's machine, so the key is in no file and no
  environment variable of the session. No script can see it: ask the user to confirm that they added
  it, and for the host. Record `NAME@host`. Network secrets exist only in cloud environments that
  Anthropic hosts, a shared environment gets one only from an Owner, and the proxy adds it to the requests of
  each session in that environment. It cannot be saved for Anthropic's own API hosts.
- **Cloud session, second option: an environment variable of the cloud environment.** Only if a
  network secret does not fit the key (the project needs the value itself). Each command of the
  session can read it, but anyone who uses the environment can read its variables.
- **Linux terminal: `.env`.** The user puts `NAME=value` in `.env` (a copy of `.env.example`) in their
  editor. `.env` is git-ignored. In the run sessions, Claude's file-read tool is denied for `.env`, and
  `harness/guard.sh` blocks common commands that print it.

Run `python3 harness/spec.py secrets` to check `.env` and the environment (it prints names, never a
value). It reports a network secret as missing until the setup command below records it; that is
expected. The setup command checks again, with the network secrets the user confirmed, and refuses
if a required secret is in none of the three places. Do not start the run with one missing.

### Guards

A guard is a rule of the user for the run. Explain in one or two sentences: a guard becomes a block or
a check. A block is an entry in the deny list of `harness/run-settings.json`: it stops the usual
command (for example `curl ...`), but it is a tripwire, not a wall, so the same action written another
way may not be stopped. A check is a short script in `checks/guards/` that the gates run: a result that
breaks it is not accepted.

Ask: "Is there something the run must never do, or a rule each result must meet?" One guard per
question; play it back; ask again until the user has no more. Convert each guard into the strongest
form that fits:

- **Block**, if it is one command or one tool: a deny entry in the form of the existing ones, such as
  `Bash(curl:*)` for "never use `curl`", or `WebSearch` for a tool. If the rule needs more than one
  entry can say, make it a check or a note.
- **Check**, if a script can test it on the files of the project: write `checks/guards/<name>.sh`.
  Test it now: it must pass on the clean tree (`bash checks/guards/<name>.sh`) and fail on a sample
  that breaks it. Make the sample `guard-sample` in the project root (with the Write tool, or
  `truncate -s 101M guard-sample` for a size limit up to 100 MB), run the script, then
  `rm guard-sample`. Write each script like this one (run from the repo root, check the files of the
  project, print the reason, exit 1 on a failure):

  ```bash
  #!/usr/bin/env bash
  # Guard: no file in the project is larger than 1 MB.
  cd "$(dirname "$0")/../.." || exit 2
  rc=0
  while IFS= read -r -d '' f; do
    [[ -f "$f" ]] || continue
    if (( $(stat -c %s "$f") > 1048576 )); then echo "  file larger than 1 MB: $f"; rc=1; fi
  done < <(git ls-files -z --cached --others --exclude-standard)
  exit $rc
  ```

- **Note**, if no script can check it (for example "keep the messages friendly"): tell the user that
  no script can enforce it, and that it is recorded as "not enforced by a script".

### Wake-up

In a Linux terminal, skip this part: the run in tmux keeps going by itself.

In a cloud session, tell the user, in these words or close to them: "In a cloud session, the machine is put
away a few minutes after I stop working, so I start the run in a way that keeps the machine working. As a
safety net, a scheduled task can wake this session once an hour to check the run and continue it. It
removes itself when the project is delivered or needs you. Each wake-up uses a little of your usage." Then
ask: "Should I create this hourly wake-up? (recommended)"

For you, not for the user (measured in a cloud session): a process that the session does not track was gone
about 4 to 6 minutes after the turn ended (one sample: between 3 min 44 s and 5 min 46 s), a background
command of the session's shell tool kept the machine alive for the 12 minutes measured, and a one-time
scheduled task that fired into the same session started a turn on a new machine, with the repo present at
the last pushed commit.

- **Yes, and you have a tool that creates a scheduled task that fires into this same session** (for
  example `create_trigger` today): after the approval of the setup summary, create one task named
  `build wake-up` that runs every hour and fires into this session (not into a new session). The user may
  see one approval prompt for it. Its prompt, exactly:

  ```text
  Wake-up of the build run. Do these steps in order, then end your turn.
  1. Run `python3 harness/loop.py status` and read its line `run:`.
  2. If it says `active`, or a background command of yours that runs `bash harness/run.sh` still runs: do nothing more.
  3. Otherwise do what section B of CLAUDE.md, item 6, says for that line. The scheduled task `build wake-up` that it may tell you to delete is this task: delete it with your scheduling tool (if you cannot, ask the user to delete it).
  ```

  Record `wakeup=hourly`.
- **No such tool, or the user says no**: record `wakeup=none`. Tell the user: "The run goes on while its
  background command runs. If the machine is replaced or the command's time ends, the run waits until you
  send this session a message, for example "continue the run"."

### Setup summary

Show one short summary: environment, boundary, `sudo` (allowed or denied), the tools (present,
installed, missing), the secrets by name with where each is kept, the wake-up (cloud: the task and its
interval, or none), and each guard in the user's words with its form (the block entry, the check script,
or "not enforced by a script"). Ask the user to approve it or correct it. After the approval, run:

`python3 harness/spec.py setup env=<cloud|terminal> boundary=<cloud|vm|container|none> sudo=<yes|no>`

In a cloud session with network secrets, add `network=NAME@host,NAME@host` (names and hosts only). In a
cloud session, add `wakeup=<hourly|none>`; then create the hourly task, if the user chose it.
It writes `spec/SETUP.md` (with where each secret is kept, by name, and the wake-up), removes `Bash(sudo:*)` from the
deny list of `harness/run-settings.json` only for `sudo=yes` inside a boundary (it refuses `sudo=yes`
without one), and commits. Then record each guard, with the user's words:

`python3 harness/spec.py guard block='<entry>' text='<words>'` ·
`python3 harness/spec.py guard check=checks/guards/<name>.sh text='<words>'` ·
`python3 harness/spec.py guard note text='<words>'`

Keep the single quotes, as in `block='Bash(curl:*)'`. If the words hold an apostrophe, put them in
double quotes and write no backtick, `$` or `"` in them. It adds the deny entry, or commits the script (it
refuses a script that fails now), and writes the guard to the "Guards" section of `spec/SETUP.md`. The
same guard again changes nothing, except that a check script is tested again and a change to it is
committed. To change a
setup choice later, run `setup` again with the new values, on `main`, before a run; `setup` keeps the
guards. A recorded guard is not changed by a second command: `guard` refuses the same words with another
form or target. To change or remove one, with the user, remove its line in `spec/SETUP.md` and its deny
entry or its script, commit, then record the new guard, if any. After `setup` and the guards, and after each later
change of them, push `main` before the run starts, always in a cloud session and in a terminal when the repo has a
remote: `git push origin main` (a wake-up or a new machine reads the spec, `spec/SETUP.md` and the guards from the
remote `main`, and on a build branch the gates compare the harness with `origin/main`). If the push is refused, tell
the user and stop: do not work around it.

## Step 7. Launch

1. If this repo has no GitHub remote yet and the user wants one, create a private repository with
   `gh repo create <name> --private --source . --remote origin --push` (ask before creating it).
2. Run `bash harness/preflight.sh` and fix or explain every FAIL line.
3. Start the run as `spec/SETUP.md` (section "Environment") says. The loop works on a `build/...`
   branch, never on `main`.
   - Linux terminal: run `bash harness/run.sh`. It starts the build loop in a tmux session that survives
     a closed terminal.
   - Cloud session: run `bash harness/run.sh --foreground --max-hours 1` with your shell tool's
     run-in-background option and the longest time the tool allows (for example `run_in_background` with
     `timeout` 7200000 on the Bash tool). It runs the preflight, then the loop, and ends with the loop's
     exit code. The loop stops itself between steps after about one hour of work, so it usually ends
     before the tool's limit; then you start it again (`CLAUDE.md`, section B).
4. Tell the user, briefly: how to watch (terminal: `tmux attach -t <name>`; both: `bash harness/run.sh
   status`), that the run stops by itself with a reason if it needs them, and that it ends with
   `DELIVERY.md` and a pull request they merge. Then end your turn. Do not start building in this session.

## Change mode (the spec is already approved)

Start with `python3 harness/loop.py status` (the check at the top ran it already): when a run branch `build/...`
exists, the working tree is now on it, and the change is made there, so the run gets it. With no run branch yet,
make the change on `main` (`git switch main`).

1. If a run is in progress, stop it first (terminal: `tmux attach -t <name>`, then Ctrl+C; cloud
   session: delete the scheduled task `build wake-up`, then stop the run's background command with your
   shell tool): a running loop puts the spec back to its approved state after every step. Then ask what should change and why,
   with the same interview rules, as briefly as the change allows.
2. Edit `spec/requirements.json`: change the requirement in place (keep its id), or add a new one with
   the next free id. Never renumber. Move a dropped requirement's intent to `non_goals`.
3. Run steps 4 and 5 again for the changed parts, then `python3 harness/spec.py approve`.
4. If this change answers a question of a stopped run, stop here: `CLAUDE.md` (section B) runs
   `python3 harness/loop.py retry <id>` for each answered item and then starts the run once. Otherwise
   start the run again as `CLAUDE.md` (section B, "Start the run") says: it adds a milestone for the new and changed
   requirements by itself (a run that has not started yet designs them with the rest) and audits the whole product
   again at the end.
