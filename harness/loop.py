#!/usr/bin/env python3
"""The build loop: a fresh Claude Code session per small step, all state in git, code decides what is done.

  loop.py run [--max-legs 400] [--max-hours 24] [--no-push]   run until delivered, until no item can run, or out of budget
  loop.py status                                              one-screen summary of the ledger and the last stop report; its line
                                                              `run:` says active, DELIVERED, needs the user, or not active (start again)
  loop.py retry <id>                                          set a blocked item back to todo once the user answered its question

Nothing lives in a session's memory. Each numbered thing below is one "leg": a brand-new session that
gets a short brief, does one job, and exits. This script checks the result itself and records it.

  1. DESIGN  once. Reads the approved spec, writes spec/DESIGN.md, plan/milestones.json (every
             requirement assigned to a milestone), checks/check_project.sh (lint/test/build gate), init.sh.
  2. PLAN    per milestone, just before it starts (when the milestones in its `depends_on` are done; no
             `depends_on`: when the one before it is done). Splits it into atomic tasks, writes one check per
             task (checks/tasks/) and one acceptance check per requirement (checks/acceptance/). This
             script rejects the plan unless every check parses and FAILS before the work exists.
  3. WORK    one task, one session. The model cannot edit the spec, the plan, the checks or the ledger:
             changes to those paths are thrown away after every leg.
  4. CHECK   this script runs the task's check and checks/run_gates.sh.
  5. VERIFY  a separate read-only session that did not do the work ends with VERDICT: PASS or FAIL.
  6. CLOSE   when a milestone's tasks are done, this script runs its requirements' acceptance checks.
             Failures send the milestone back to PLAN for more tasks.
  7. AUDIT   when every milestone is closed: all acceptance checks and gates are re-run, then a
             read-only auditor compares the product with the spec as a user would. Gaps become a new
             milestone. PASS writes DELIVERY.md, puts it in the pull request and marks the pull request ready.

A failed task is retried with the failure text (MAX_ATTEMPTS), then `blocked`; a work session that reports a
blocker (QUESTION and RECOMMENDED lines, no change) blocks its task at once. A blocked item (the design, a
milestone or a task) does not stop the run: the loop goes on with every item that does not depend on it. When
no item can run, the run stops with a stop report: one question and a recommended answer for each blocked task
and for the design or a milestone that the loop blocks itself (design or plan rejected MAX_ATTEMPTS times,
acceptance checks still failing after MAX_ROUNDS rounds); a milestone blocked by its tasks carries their
questions and has none of its own. The report also lists the items that wait on a blocked item. It does not
deliver then. `run` never retries a blocked item by itself: after the user answers, `loop.py retry <id>`
sets the item back to todo. Provider errors
(5xx, timeouts, overload) are retried with a pause and never count against a task; a usage limit is
waited out (Claude Code's "You've hit your session limit · resets 3:45pm" and its other forms); the wait does
not count as a failed attempt. Each stop the loop makes itself (no item left that can run, the leg or time budget,
FAIL_STREAK legs without progress, gaps after MAX_AUDITS audits, a usage limit that does not clear, a failed sign-in,
a session that keeps failing, Ctrl+C) prints the reason and what is needed, and is written to
.harness-run/last-stop.txt (removed when the next run has passed its start checks). The stops that a restart continues
with no person (the leg or time budget, a usage limit that does not clear, Ctrl+C) end that file with the line RESUMABLE;
`status` reports each other stop as one that needs the user. When the run pushes and gh is installed, each of these is also a comment on the pull
request (GitHub notifies the user). A stop by a signal (SIGTERM, SIGHUP, a machine that goes down) or by a crash
of the loop writes none of these. `retry` refuses while a run holds the lock.

When a git remote exists and --no-push is not set, the run branch is pushed to origin after each step (a failed
push only warns: the next push carries the commits), so a new clone continues the run: `run`, `status` and `retry`
switch to the run branch, also one that exists only on origin, from the base branch or any other branch. On another
branch whose spec is not approved (a session branch made before the spec), the slug comes from the spec on main, and with
no run branch yet they switch to main first when main holds an approved spec, so the first run starts from main; another
branch with an approved spec of its own and no run branch is itself the run branch. With a remote (`run`: unless
--no-push), they first fetch the branch and fast-forward the local one to it (the run may have gone on from another
clone); if the two have diverged, `run` and `retry` stop with a message (they never merge or force). With a remote,
`retry` pushes its commit. The first push of a run opens a draft pull request with gh, if gh is installed; when gh
fails, the run warns and goes on.

`run` first ends a session that a killed loop left running (its process id is in .harness-run/session.pid), then, on a
build/ branch, discards uncommitted changes (a leg cut off by a kill) and lists them; on any other branch it refuses them.

Exit code: 0 = delivered, 3 = stopped early (reason printed), 1 = cannot start or retry refused (reason printed).

Env: HARNESS_CLAUDE (default `claude`), HARNESS_LIMIT_HOURS (how long to wait out a usage limit,
default 8), HARNESS_LIMIT_POLL (seconds between tries during a usage limit, default 900),
HARNESS_RETRY_WAITS (comma-separated seconds between retries of a provider error).
Model and effort of each session (passed as --model and --effort): HARNESS_MODEL_<STEP> and
HARNESS_EFFORT_<STEP> for one step (STEP = DESIGN, PLAN, WORK, VERIFY or AUDIT), else HARNESS_MODEL
and HARNESS_EFFORT for every step, else the defaults in MODELS and EFFORT below (opus for design,
plan, work and audit, sonnet for verify; medium effort). An empty value omits the flag: Claude Code's own default.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
H = ROOT / "harness"
RUN = ROOT / ".harness-run"                      # git-ignored runtime files (log, lock, last stop)
CLAUDE = os.environ.get("HARNESS_CLAUDE") or "claude"
MODELS = {"design": "opus", "plan": "opus", "work": "opus", "verify": "sonnet", "audit": "opus"}   # aliases: always the latest model
EFFORT = "medium"
LIMIT_HOURS = float(os.environ.get("HARNESS_LIMIT_HOURS", "8"))
LEDGER, PROGRESS = ROOT / "ledger.jsonl", ROOT / "PROGRESS.md"
SPECJ, APPROVED = ROOT / "spec" / "requirements.json", ROOT / "spec" / "APPROVED"
MILESTONES = ROOT / "plan" / "milestones.json"

MAX_ATTEMPTS = 3            # tries per task / plan / design before it is blocked
MAX_ROUNDS = 3              # planning rounds per milestone (round 2+ = acceptance checks still failing)
MAX_AUDITS = 3              # audits per run; each failed audit adds one fix milestone
FAIL_STREAK = 5             # legs in a row without progress before the loop gives up (a new blocked item is progress)
MAX_TASKS = 25              # per planning round
MAX_MILESTONES = 12
LEG_SECONDS = {"design": 2400, "plan": 1800, "work": 2700, "verify": 900, "audit": 2400}
LEG_TURNS = {"design": "100", "plan": "100", "work": "200", "verify": "50", "audit": "100"}
RETRY_WAITS = [int(x) for x in os.environ.get("HARNESS_RETRY_WAITS", "30,120,300,900").split(",")]   # pauses before retrying a provider error
LIMIT_POLL = int(os.environ.get("HARNESS_LIMIT_POLL", "900"))                       # seconds between tries while a usage limit is active

# Who may write what. Anything a leg changes outside its allowance is put back before the commit.
HARNESS = ["CLAUDE.md", "harness", ".claude", ".github", "checks/run_gates.sh", "checks/check_scope.sh",
           "checks/check_progress.sh", "checks/check_protected.sh", "checks/guards"]
SPEC = ["spec/BRIEF.md", "spec/REQUIREMENTS.md", "spec/requirements.json", "spec/APPROVED", "spec/SETUP.md", ".env.example"]
STATE = ["ledger.jsonl", "PROGRESS.md", "DELIVERY.md"]                       # written by this script only
DESIGN_PATHS = ["spec/DESIGN.md", "plan/milestones.json", "checks/check_project.sh", "init.sh", ".gitignore", "notes/decisions.md"]
PLAN_PATHS = ["plan", "checks/tasks", "checks/acceptance", "checks/check_project.sh", "notes/decisions.md"]
WORK_FORBIDDEN = HARNESS + SPEC + STATE + ["spec/DESIGN.md", "plan", "checks"]

TRANSIENT = re.compile(r"API Error:? ?(408|409|5\d\d)|overloaded|timed? ?out|ECONNRESET|ETIMEDOUT|EAI_AGAIN|socket hang up|"
                       r"fetch failed|under capacity|connection error|network error|internal server error|bad gateway", re.I)
LIMIT = re.compile(r"(hit|reached) your [\w' -]{0,40}(limit|budget)|usage limit|limit reached|rate.?limit|API Error:? ?429|out of (extra )?usage|quota|limit will reset", re.I)
AUTH = re.compile(r"API Error:? ?40[13]|invalid api key|invalid x-api-key|authentication_error|please run /login|"
                  r"oauth token (has )?expired|not logged in", re.I)
WAITED = [0.0]                                   # seconds spent pausing for the provider (not charged to the budget)
RESUMABLE = "RESUMABLE"                          # last line of last-stop.txt for a stop that a restart continues
RUNBR = []                                       # [run branch] when the run pushes to origin; + "draft" once a pull request was asked for


class Stop(Exception):
    """Ends the run with a reason that says what is needed; Stop(reason, True): a restart continues the run."""


def sh(args, timeout=900, check=False, env=None):
    try:
        r = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(args, 124, (e.stdout or b"").decode("utf8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                                           f"timed out after {timeout}s")
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {(r.stderr or r.stdout).strip()[:400]}")
    return r


def git(*args, check=True):
    return sh(["git", *args], check=check).stdout.strip()


def say(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def pause(seconds, why):
    say(f"  waiting {int(seconds)}s: {why}")
    time.sleep(seconds)
    WAITED[0] += seconds


def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------- spec ----------
def spec():
    return json.loads(SPECJ.read_text())


def req_hash(r):
    return hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()[:12]


def spec_problem():
    """None when the spec is approved and unchanged since approval; otherwise what is wrong."""
    if not SPECJ.is_file() or not APPROVED.is_file():
        return "the spec is not approved yet. Open `claude` here and run /intake (it ends with `python3 harness/spec.py approve`)."
    try:
        want = json.loads(APPROVED.read_text()).get("sha256")
    except ValueError:
        return "spec/APPROVED is not valid JSON; run `python3 harness/spec.py approve` again."
    if hashlib.sha256(SPECJ.read_bytes()).hexdigest() != want:
        return "spec/requirements.json changed after it was approved. Review it and run `python3 harness/spec.py approve` again."
    return None


def child_env():
    """Environment for checks: the shell's, plus .env (secrets stay out of git and out of the prompts).
    A network secret is in neither: the cloud proxy adds it to requests to its host. spec.py secrets uses this too."""
    env = dict(os.environ)
    f = ROOT / ".env"
    if f.is_file():
        for line in f.read_text().splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
            if m and not line.lstrip().startswith("#"):
                v = m.group(2).strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                env[m.group(1)] = v
    return env


# ---------- ledger ----------
def load():
    return [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()] if LEDGER.is_file() else []


def save(cards):
    LEDGER.write_text("".join(json.dumps(c) + "\n" for c in cards))


def by_id(cards):
    return {c["id"]: c for c in cards}


def kids(cards, mid):
    return [c for c in cards if c.get("kind") == "task" and c.get("parent") == mid]


def deps(cards):
    """{id: ids it waits on}. A milestone without `depends_on` waits on the milestone before it (the old strict order)."""
    ms = [c for c in cards if c.get("kind") == "milestone"]
    out = {c["id"]: c.get("depends_on", []) for c in cards}
    out.update({c["id"]: c.get("depends_on", [ms[i - 1]["id"]] if i else []) for i, c in enumerate(ms)})
    return out


def block(card, note, text=""):
    """Mark an item blocked with one question for the user and a recommended answer: the session's own
    QUESTION and RECOMMENDED lines if it wrote both, else a plain default."""
    q, a = (re.findall(rf"^\s*{k}:\s*(.+)$", text, re.MULTILINE) for k in ("QUESTION", "RECOMMENDED"))
    card.update(state="blocked", note=note, question=q[-1].strip() if q and a else
                f"{card['id']} cannot pass ({' '.join(note.split())[:300]}). What must change so that it can?",
                answer=a[-1].strip() if q and a else "Read the note of this item in ledger.jsonl and correct the cause it names "
                "(the requirement, a check, or the environment).")


def stop_report(cards):
    """Each blocked item with its question and recommended answer; each other open item with what it waits on."""
    ids, dep, lines = by_id(cards), deps(cards), []
    for c in cards:
        if c["state"] == "blocked":
            lines.append(f"{c['id']} blocked: {' '.join(c['note'].split())[:300]}"
                         + (f"\n  QUESTION: {c['question']}\n  RECOMMENDED: {c['answer']}" if c.get("question") else ""))
        elif c["state"] != "done" and c.get("kind") in ("milestone", "task"):
            lines.append(f"{c['id']} waits on " + ", ".join(d for d in dep[c["id"]] if ids[d]["state"] != "done"))
    return "\n".join(lines)


def log_progress(card_id, action, check, nxt):
    head = git("rev-parse", "--short", "HEAD", check=False) or "none"
    with open(PROGRESS, "a") as f:
        f.write(f"\n## {utc()} · {card_id}\n- action: {action}\n- check: {check}\n- commit: {head}\n- next: {nxt}\n")


def commit_state(msg):
    git("add", "--", *[p for p in STATE if (ROOT / p).exists()])
    if git("status", "--porcelain", "--", *STATE):
        git("commit", "-q", "-m", msg[:100])


def record(cards, card_id, action, check, nxt, msg):
    save(cards)
    log_progress(card_id, action, check, nxt)
    commit_state(msg)
    push_run()


def push_run():
    """Put the run branch on origin after each step, so a new clone continues the run; the first push opens a draft
    pull request. A failure only warns: the next push carries the commits. Returns the failure text, or ''."""
    if not RUNBR:
        return ""
    # --no-verify: the pre-push hook runs the gates, which a state in the middle of a task may fail; the loop runs the
    # gates itself before it accepts work, and a state that is not on origin cannot be resumed from a new clone.
    p = sh(["git", "push", "-q", "--no-verify", "-u", "origin", RUNBR[0]], timeout=600)
    if p.returncode != 0:
        err = " ".join((p.stderr or p.stdout).split())[-200:] or "git push failed"
        say(f"  push failed (the next push carries these commits): {err}")
        return err
    if len(RUNBR) == 1 and shutil.which("gh"):
        RUNBR.append("draft")
        r = sh(["gh", "pr", "create", "--draft", "--base", "main", "--head", RUNBR[0], "--title", f"Deliver: {spec().get('project', ROOT.name)}"[:70],
                "--body", "Draft: the build loop is running on this branch. Each stop is a comment here; at delivery this text becomes DELIVERY.md."], timeout=120)
        if r.returncode != 0 and "already exists" not in r.stderr + r.stdout:
            say(f"  draft pull request not opened: {(r.stderr or r.stdout).strip()[-160:]}")
    return ""


def to_run_branch(create, sync):
    """Switch to the run branch build/<slug>: from any branch when it exists (local or on origin: a new clone, maybe on a
    session branch such as claude/<name>), from the base branch also with `create` a new one. The slug comes from the spec
    of the current branch when it is the base branch or holds an approved spec of its own, else from the spec on main
    (origin/main when a remote exists); with no run branch yet and an approved spec on main, it switches to main first,
    so the first run starts from main. On another branch with neither, that branch is the run branch. With `sync` and a
    remote, first fetch the branch and fast-forward the local one to origin's (the run may have gone on from another
    clone). Returns '' or why the branch could not be switched to or brought up to date."""
    branch, run = git("branch", "--show-current"), ""
    remote = "origin" in git("remote", check=False).split()
    sync, base = sync and remote, branch in ("main", "master", "")
    text = SPECJ.read_text() if SPECJ.is_file() and (base or not spec_problem()) else ""
    if not text and not base:                                      # a session branch made before the spec: read main's
        if remote:
            sh(["git", "fetch", "-q", "origin", "main"], timeout=300)
        p = sh(["git", "show", ("origin/main" if remote else "main") + ":spec/requirements.json"])
        text = p.stdout if p.returncode == 0 else ""
        approved = sh(["git", "cat-file", "-e", ("origin/main" if remote else "main") + ":spec/APPROVED"]).returncode == 0
    if text:
        slug = re.sub(r"[^a-z0-9]+", "-", str(json.loads(text).get("project", "project")).lower()).strip("-")[:40] or "project"
        run = f"build/{slug}"
        if sync:
            sh(["git", "fetch", "-q", "origin", run], timeout=300)  # fails when origin has no such branch yet: nothing to do
        if base or any(sh(["git", "rev-parse", "-q", "--verify", f"{r}/{run}"]).returncode == 0
                       for r in ("refs/heads", "refs/remotes/origin")):
            branch = run
        elif not SPECJ.is_file() or spec_problem():               # the spec came from main: the first run starts there
            if not approved:
                return ""
            if git("status", "--porcelain"):
                return "the working tree has uncommitted changes, so the branch was not switched to main"
            if sh(["git", "switch", "-q", "main"]).returncode != 0:
                return "could not switch to main, where the approved spec is"
            if remote:
                sh(["git", "merge", "-q", "--ff-only", "origin/main"])
            say("switched to main: the first run starts from the approved spec there")
            return to_run_branch(create, sync)
    elif base:
        return ""                                                  # no spec yet: no run branch either
    if (sync or git("branch", "--show-current") != branch) and git("status", "--porcelain"):
        return "the working tree has uncommitted changes, so the run branch was not switched to or brought up to date"
    if sync and branch != run:
        sh(["git", "fetch", "-q", "origin", branch], timeout=300)  # the user's own run branch
    if git("branch", "--show-current") != branch:
        if sh(["git", "switch", "-q", branch]).returncode != 0:    # a local branch, or one only on origin (a new clone)
            if not create or spec_problem():                       # never a new run branch from an unapproved spec
                return ""
            git("switch", "-q", "-c", branch)
        say(f"working on branch {branch} (never on main)")
    if sync and sh(["git", "rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}"]).returncode == 0 \
            and sh(["git", "merge", "-q", "--ff-only", f"origin/{branch}"]).returncode != 0:
        return (f"{branch} and origin/{branch} have diverged (the run went on in another clone); the loop never merges "
                f"or forces. Keep one of them (for example `git reset --hard origin/{branch}` if the local commits are not needed).")
    return ""


# ---------- one model session ----------
def run_claude(kind, prompt):
    """One fresh session. Returns {ok, text, turns, secs, denials, err} where err is None or
    'transient' | 'limit' | 'auth' | 'timeout' | 'other'."""
    cmd = [CLAUDE, "-p", "--settings", str(H / "run-settings.json"), "--strict-mcp-config", "--mcp-config", str(H / "mcp.json"),
           "--output-format", "json", "--max-turns", LEG_TURNS[kind]]
    model, effort = step_env("MODEL", kind, MODELS[kind]), step_env("EFFORT", kind, EFFORT)
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if kind in ("verify", "audit"):
        cmd += ["--disallowed-tools", "Edit", "Write", "NotebookEdit"]
    t0 = time.time()
    # Own process group + a watchdog: the whole session tree is killed at the limit, even if it hangs.
    proc = subprocess.Popen(cmd, cwd=ROOT, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    RUN.mkdir(exist_ok=True)
    (RUN / "session.pid").write_text(str(proc.pid))                 # a later `run` ends it if this loop is killed
    timed_out = []

    def stop():
        timed_out.append(1)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    dog = threading.Timer(LEG_SECONDS[kind], stop)
    dog.start()
    try:
        out, err = proc.communicate(prompt)
    finally:
        dog.cancel()
        (RUN / "session.pid").unlink(missing_ok=True)
    secs = int(time.time() - t0)
    base = {"ok": False, "text": "", "turns": 0, "secs": secs, "denials": [], "err": None}
    if timed_out:
        return {**base, "err": "timeout", "text": f"the session was stopped at its {LEG_SECONDS[kind] // 60}-minute limit: "
                "the step is too big or it got stuck. Do less in one step."}
    res = None
    for line in reversed((out or "").strip().splitlines()):
        try:
            res = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(res, dict):
        text = f"no result from the session (exit {proc.returncode}): {((err or '') + (out or ''))[-400:]}"
        return {**base, "text": text, "err": classify(text)}
    text = str(res.get("result") or "")
    denials = sorted({str(d.get("tool_name", "?")) + (": " + json.dumps(d.get("tool_input"))[:120] if d.get("tool_input") else "")
                      for d in res.get("permission_denials") or [] if isinstance(d, dict)})
    base.update(text=text, turns=res.get("num_turns", 0), denials=denials)
    if res.get("is_error") and res.get("subtype") != "error_max_turns":
        return {**base, "text": text or str(res.get("subtype")), "err": classify(text or str(res.get("subtype")))}
    return {**base, "ok": True}


def step_env(name, kind, default):
    """HARNESS_<name>_<STEP> if set, else HARNESS_<name> if set, else the default (see the docstring at the top)."""
    return os.environ.get(f"HARNESS_{name}_{kind.upper()}", os.environ.get(f"HARNESS_{name}", default))


def classify(text):
    if AUTH.search(text):
        return "auth"
    if LIMIT.search(text):
        return "limit"
    if TRANSIENT.search(text):
        return "transient"
    return "other"


def session(kind, prompt, start):
    """run_claude with patience: provider errors are retried from a clean tree, a usage limit is waited out.
    Raises Stop for errors only the user can fix."""
    waits, limit_until = list(RETRY_WAITS), None
    while True:
        r = run_claude(kind, prompt)
        if r["err"] in (None, "timeout"):
            return r
        git("reset", "-q", "--hard", start)                # a failed session leaves nothing behind
        sh(["git", "clean", "-qfd"])
        first = r["text"].strip().splitlines()[0][:200] if r["text"].strip() else "no output"
        if r["err"] == "auth":
            raise Stop(f"Claude sign-in failed ({first}). Run `claude` once in a terminal and sign in, then start the run again.")
        if r["err"] == "limit":
            limit_until = limit_until or time.time() + LIMIT_HOURS * 3600
            if time.time() > limit_until:
                raise Stop(f"the usage limit did not clear within {LIMIT_HOURS:g} h ({first}). Start the run again later; it resumes where it stopped.", True)
            pause(LIMIT_POLL, f"usage limit ({first})")
            continue
        if r["err"] == "transient" and waits:
            pause(waits.pop(0), f"provider error, will retry ({first})")
            continue
        raise Stop(f"the session kept failing: {first}. Nothing was lost; start the run again once this is fixed.")


def brief(name, **fields):
    text = (H / "prompts" / f"{name}.md").read_text()
    for k, v in fields.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def changed_paths(since):
    """Every path that differs from commit `since`, committed or not, plus untracked files."""
    out = set(git("diff", "--name-only", since).splitlines())
    out |= set(git("ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(p for p in out if p)


def under(path, roots):
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def restore(paths, since):
    """Put paths back to how they were at `since` (delete them if they did not exist then)."""
    for p in paths:
        if sh(["git", "cat-file", "-e", f"{since}:{p}"]).returncode == 0:
            git("checkout", since, "--", p)
        else:
            sh(["git", "rm", "-q", "-f", "--cached", "--ignore-unmatch", "--", p])
            (ROOT / p).unlink(missing_ok=True)


def seal(since, branch, msg, allowed=None, forbidden=None, frozen=()):
    """After a leg: get back on the run branch, undo edits the leg may not make, commit the rest.
    `allowed`: only these roots may change. `forbidden`: these roots may not. `frozen`: files that
    existed before and may not be modified. Returns (undone_paths, kept_paths)."""
    if git("branch", "--show-current") != branch:
        git("switch", "-q", "-f", branch)
    paths = changed_paths(since)
    bad = [p for p in paths if (allowed is not None and not under(p, allowed)) or (forbidden and under(p, forbidden)) or p in frozen]
    if bad:
        restore(bad, since)
    git("add", "-A")
    if git("status", "--porcelain"):
        git("commit", "-q", "-m", msg[:100])
    return bad, [p for p in paths if p not in bad]


def run_script(cmd, timeout=900):
    r = sh(["bash", "-c", cmd], timeout=timeout, env=child_env())
    return r.returncode == 0, (r.stdout + r.stderr)[-1500:].strip()


def run_gates():
    r = sh(["bash", "checks/run_gates.sh"], timeout=1800, env=child_env())
    out = "\n".join(l for l in (r.stdout + r.stderr).splitlines() if "FAIL" in l or l.startswith("  "))[-1500:]
    return r.returncode == 0, out


def acceptance(rids):
    """Run the acceptance check of each requirement. Returns {rid: output} for the ones that fail."""
    failed = {}
    for rid in rids:
        script = ROOT / "checks" / "acceptance" / f"{rid}.sh"
        if not script.is_file():
            failed[rid] = f"checks/acceptance/{rid}.sh does not exist"
            continue
        ok, out = run_script(f"bash checks/acceptance/{rid}.sh")
        if not ok:
            failed[rid] = out or "exit non-zero with no output"
    return failed


def note_denials(r):
    return ("\npermission denied during the session (allow it in harness/run-settings.json if the work needs it): "
            + "; ".join(r["denials"][:5])) if r["denials"] else ""


# ---------- legs ----------
def design_leg(card, branch):
    start = git("rev-parse", "HEAD")
    r = session("design", brief("design", FEEDBACK=card.get("note") or "none (first attempt)", MAX_MILESTONES=MAX_MILESTONES), start)
    bad, _ = seal(start, branch, "design: architecture, milestones, project gate", allowed=DESIGN_PATHS)
    if not r["ok"]:
        return False, r["text"], r
    problems = []                                  # edits outside the design files were undone by seal(); judge what is left
    reqs = {q["id"] for q in spec()["requirements"]}
    design = ROOT / "spec" / "DESIGN.md"
    if not design.is_file() or len(design.read_text()) < 800:
        problems.append("spec/DESIGN.md is missing or too thin to build from")
    for f in ("checks/check_project.sh", "init.sh"):
        if not (ROOT / f).is_file():
            problems.append(f"{f} was not written")
        elif sh(["bash", "-n", f]).returncode != 0:
            problems.append(f"{f} has a syntax error")
    ms = []
    try:
        ms = json.loads(MILESTONES.read_text())
        assert isinstance(ms, list)
    except (OSError, ValueError, AssertionError):
        problems.append("plan/milestones.json is missing or is not a JSON list")
    if ms:
        if not 1 <= len(ms) <= MAX_MILESTONES:
            problems.append(f"{len(ms)} milestones; need 1 to {MAX_MILESTONES}")
        seen, covered = set(), set()
        for i, m in enumerate(ms, 1):
            if not isinstance(m, dict) or m.get("id") != f"M{i}":
                problems.append(f"milestone {i} must have id M{i}")
                continue
            if len(str(m.get("title", ""))) < 5 or len(str(m.get("goal", ""))) < 30:
                problems.append(f"M{i}: needs a title and a goal that says what works when it is done")
            ids = m.get("requirements")
            if not isinstance(ids, list):
                problems.append(f"M{i}: `requirements` must be a list of requirement ids")
                continue
            for rid in ids:
                if rid not in reqs:
                    problems.append(f"M{i}: unknown requirement {rid}")
                if rid in seen:
                    problems.append(f"{rid} is assigned to more than one milestone")
                seen.add(rid)
            covered |= set(ids)
            if not isinstance(m.get("depends_on", []), list) or any(d not in [f"M{j}" for j in range(1, i)] for d in m.get("depends_on", [])):
                problems.append(f"M{i}: `depends_on` may name only earlier milestones (not an unknown id, not itself, not a later one)")
        missing = sorted(reqs - covered)
        if missing:
            problems.append(f"requirements not assigned to any milestone: {', '.join(missing)}")
    if not problems:
        ok, out = run_gates()
        if not ok:
            problems.append("checks/run_gates.sh fails right after the design leg (check_project.sh must skip cleanly "
                            f"for anything that does not exist yet):\n{out}")
    if problems:
        return False, "design rejected:\n- " + "\n- ".join(problems[:15]), r
    return True, f"{len(ms)} milestones" + (f"; undone edits: {', '.join(bad[:4])}" if bad else ""), r


def add_milestones(cards):
    reqs = {q["id"]: req_hash(q) for q in spec()["requirements"]}
    for m in json.loads(MILESTONES.read_text()):
        cards.append({"id": m["id"], "kind": "milestone", "title": m["title"], "goal": m["goal"], "requirements": m["requirements"],
                      "req_hash": {rid: reqs[rid] for rid in m["requirements"]}, "regen": [], "state": "todo", "rounds": 0,
                      "replan": "", "attempts": 0, "note": "", **({"depends_on": m["depends_on"]} if "depends_on" in m else {})})
    cards.append({"id": "AUDIT", "kind": "audit", "state": "todo", "rounds": 0, "note": ""})


def next_mid(cards):
    return "M" + str(1 + max([int(c["id"][1:]) for c in cards if c.get("kind") == "milestone"] or [0]))


def add_fix_milestone(cards, title, goal, rids, regen):
    reqs = {q["id"]: req_hash(q) for q in spec()["requirements"]}
    rids = [r for r in rids if r in reqs]
    m = {"id": next_mid(cards), "kind": "milestone", "title": title, "goal": goal, "requirements": rids,
         "req_hash": {rid: reqs[rid] for rid in rids}, "regen": [r for r in regen if r in rids], "state": "todo",
         "rounds": 0, "replan": "", "attempts": 0, "note": ""}
    at = next((i for i, c in enumerate(cards) if c.get("kind") == "audit"), len(cards))
    cards.insert(at, m)
    return m


def sync_spec(cards):
    """Requirements added or changed since they were last planned (re-approved spec) get a new milestone."""
    reqs = {q["id"]: req_hash(q) for q in spec()["requirements"]}
    seen = {}
    for c in cards:
        if c.get("kind") == "milestone":
            seen.update(c.get("req_hash", {}))
    new = [r for r in reqs if r not in seen]
    changed = [r for r in reqs if r in seen and seen[r] != reqs[r]]
    if not new and not changed:
        return None
    m = add_fix_milestone(cards, "Requirement changes",
                          "The spec was re-approved with changes. New requirements: " + (", ".join(new) or "none")
                          + ". Changed requirements (their acceptance checks must be rewritten to match the new text): "
                          + (", ".join(changed) or "none") + ". Make the product meet them.", new + changed, changed)
    for c in cards:
        if c.get("kind") == "audit":
            c.update(state="todo", delivered="")
    return m


def plan_leg(m, cards, branch):
    mid, rnd = m["id"], m.get("rounds", 0) + 1
    start = git("rev-parse", "HEAD")
    first_t = 1 + max([int(k["id"].split(".T")[1]) for k in kids(cards, mid)] or [0])
    plan_file = f"plan/{mid}.r{rnd}.jsonl"
    reqs = {q["id"]: q for q in spec()["requirements"]}
    req_text = "\n".join(f"- {rid}: {reqs[rid]['statement']}\n  verify: {reqs[rid].get('verify', '')}" for rid in m["requirements"] if rid in reqs) or "(none: this milestone only fixes the gaps described in the goal)"
    feedback = "\n".join(x for x in [m.get("replan"), m.get("note")] if x) or "none (first plan for this milestone)"
    frozen = [p for p in git("ls-files", "checks/acceptance").splitlines()
              if Path(p).name.split(".")[0] not in m.get("regen", []) and Path(p).name != ".gitkeep"]
    r = session("plan", brief("plan", MID=mid, TITLE=m["title"], GOAL=m["goal"], REQS=req_text, PLAN_FILE=plan_file, FIRST_T=first_t,
                              MAX_TASKS=MAX_TASKS, FEEDBACK=feedback,
                              DONE="\n".join(f"- {k['id']} {k['title']}" for k in kids(cards, mid) if k["state"] == "done") or "none"), start)
    bad, _ = seal(start, branch, f"plan({mid}): round {rnd}", allowed=PLAN_PATHS, frozen=frozen)
    if not r["ok"]:
        return False, r["text"], r
    problems = []                                  # edits outside the planner's files, or to frozen acceptance checks, were
    tasks = []                                     # undone by seal(); the plan is judged on what is left
    if not (ROOT / plan_file).is_file():
        problems.append(f"{plan_file} was not written")
    else:
        for n, line in enumerate((ROOT / plan_file).read_text().splitlines(), 1):
            if line.strip():
                try:
                    tasks.append(json.loads(line))
                except ValueError as e:
                    problems.append(f"plan line {n} is not valid JSON ({e})")
    if not problems and not 1 <= len(tasks) <= MAX_TASKS:
        problems.append(f"{len(tasks)} tasks; need 1 to {MAX_TASKS}")
    existing = {k["id"].split(".")[1] for k in kids(cards, mid)}
    local = [str(t.get("id")) for t in tasks]
    if len(set(local)) != len(local) or set(local) & existing:
        problems.append(f"task ids must be unique and start at T{first_t}")
    covered = set()
    for t in tasks:
        tid = str(t.get("id", "?"))
        if not re.fullmatch(r"T[0-9]{1,3}", tid):
            problems.append(f"{tid}: id must look like T{first_t}, T{first_t + 1}, …")
            continue
        if len(str(t.get("title", ""))) < 5 or len(str(t.get("acceptance", ""))) < 20:
            problems.append(f"{tid}: needs a title and a concrete acceptance criterion")
        if not isinstance(t.get("files"), list) or not 1 <= len(t["files"]) <= 10:
            problems.append(f"{tid}: `files` must list the 1 to 10 paths the task creates or changes (more means it is not atomic)")
        elif any(under(str(f), WORK_FORBIDDEN) for f in t["files"]):
            problems.append(f"{tid}: `files` names a path workers may not change")
        if any(d not in set(local) | existing for d in t.get("depends_on", [])):
            problems.append(f"{tid}: depends_on names an unknown task")
        covered |= set(t.get("requirements") or [])
        script = f"checks/tasks/{mid}.{tid}.sh"
        if not (ROOT / script).is_file():
            problems.append(f"{tid}: {script} is missing")
        elif sh(["bash", "-n", script]).returncode != 0:
            problems.append(f"{tid}: its check script has a syntax error")
        elif run_script(f"bash {script}", timeout=300)[0]:
            problems.append(f"{tid}: its check already passes before any work, so it proves nothing")
    for rid in m["requirements"]:
        script = f"checks/acceptance/{rid}.sh"
        if not (ROOT / script).is_file():
            problems.append(f"{rid}: {script} is missing")
        elif sh(["bash", "-n", script]).returncode != 0:
            problems.append(f"{rid}: its acceptance check has a syntax error")
        elif run_script(f"bash {script}", timeout=600)[0]:
            problems.append(f"{rid}: its acceptance check passes now, before the work, so it does not test what is missing")
        if rid not in covered:
            problems.append(f"{rid}: no task lists it under `requirements`")
    if not problems:
        ok, out = run_gates()
        if not ok:
            problems.append(f"checks/run_gates.sh fails after the plan leg:\n{out}")
    if problems:
        return False, "plan rejected:\n- " + "\n- ".join(problems[:15]), r
    at = 1 + max(i for i, c in enumerate(cards) if c["id"] == mid or c.get("parent") == mid)
    new = [{"id": f"{mid}.{t['id']}", "kind": "task", "parent": mid, "title": t["title"], "state": "todo",
            "depends_on": [f"{mid}.{d}" for d in t.get("depends_on", [])], "acceptance": t["acceptance"],
            "requirements": t.get("requirements") or [], "check": f"bash checks/tasks/{mid}.{t['id']}.sh", "files": t["files"],
            "attempts": 0, "commit": None, "checks": None, "note": ""} for t in tasks]
    cards[at:at] = new
    m.update(state="doing", rounds=rnd, replan="", note="", attempts=0)
    return True, f"{len(tasks)} atomic tasks (round {rnd})" + (f"; undone edits: {', '.join(bad[:4])}" if bad else ""), r


def work_leg(card, branch):
    start = git("rev-parse", "HEAD")
    r = session("work", brief(
        "work", TASK_ID=card["id"], TITLE=card["title"], ACCEPTANCE=card["acceptance"], CHECK=card["check"],
        REQS=", ".join(card.get("requirements") or []) or "none directly (enabling work)",
        FILES="\n".join(f"- {f}" for f in card.get("files", [])), ATTEMPT=card.get("attempts", 0) + 1,
        FEEDBACK=card.get("note") or "none (first attempt)"), start)
    bad, kept = seal(start, branch, f"work({card['id']}): {card['title']}", forbidden=WORK_FORBIDDEN)
    if not r["ok"]:
        return False, r["text"], r, start
    if not kept:
        return False, "the session changed nothing it is allowed to change" + (f" (undone: {', '.join(bad[:5])})" if bad else "") + note_denials(r), r, start
    ok, out = run_script(card["check"])
    gok, gout = run_gates()
    msg = ""
    if bad:
        msg += f"edits to protected paths were undone: {', '.join(bad[:6])}\n"
    if not ok:
        msg += f"task check `{card['check']}` failed:\n{out}\n"
    if not gok:
        msg += f"gates failed:\n{gout}\n"
    return ok and gok, (msg.strip() + note_denials(r)) or "check and gates pass", r, start


def verify_leg(card, start, branch):
    head = git("rev-parse", "HEAD")
    reqs = {q["id"]: q for q in spec()["requirements"]}
    r = session("verify", brief(
        "verify", TASK_ID=card["id"], TITLE=card["title"], ACCEPTANCE=card["acceptance"], CHECK=card["check"],
        REQS="\n".join(f"- {rid}: {reqs[rid]['statement']}" for rid in card.get("requirements") or [] if rid in reqs) or "none directly",
        BASE=start[:12], HEAD=head[:12], STAT=git("diff", "--stat", start, head)[-1500:]), head)
    if git("branch", "--show-current") != branch:
        git("switch", "-q", "-f", branch)
    git("reset", "-q", "--hard", head)                       # a verifier leaves nothing behind
    sh(["git", "clean", "-qfd"])
    if not r["ok"]:
        return False, r["text"], r
    verdicts = re.findall(r"^\s*VERDICT:\s*(PASS|FAIL)\b[ \t:—-]*(.*)$", r["text"], re.MULTILINE)
    if not verdicts:
        return False, "the verifier gave no VERDICT line (counts as FAIL)", r
    word, reason = verdicts[-1]
    return word == "PASS", reason.strip()[:600] or ("the verifier found no defect" if word == "PASS" else r["text"].strip()[-600:]), r


def audit_leg(branch):
    head = git("rev-parse", "HEAD")
    r = session("audit", brief("audit"), head)
    if git("branch", "--show-current") != branch:
        git("switch", "-q", "-f", branch)
    git("reset", "-q", "--hard", head)
    sh(["git", "clean", "-qfd"])
    if not r["ok"]:
        return None, [], r["text"], r
    gaps = [(a, b.strip()) for a, b in re.findall(r"^\s*GAP:\s*([A-Za-z0-9_-]+)\s*[-—:]\s*(.+)$", r["text"], re.MULTILINE)]
    verdicts = re.findall(r"^\s*VERDICT:\s*(PASS|FAIL)\b", r["text"], re.MULTILINE)
    if not verdicts:
        return None, gaps, "the auditor gave no VERDICT line", r
    return verdicts[-1] == "PASS" and not gaps, gaps, r["text"].strip()[-1500:], r


# ---------- delivery ----------
def deliver(cards, branch, push):
    s = spec()
    owner = {}
    for c in cards:
        if c.get("kind") == "milestone":
            for rid in c.get("requirements", []):
                owner[rid] = c["id"]
    failed = acceptance([q["id"] for q in s["requirements"]])
    audit = by_id(cards)["AUDIT"]
    lines = [f"# Delivery report: {s.get('project', ROOT.name)}", "",
             f"Generated {utc()} by harness/loop.py from the ledger and a fresh run of every check. Branch `{branch}`.", "",
             "## Result", "",
             f"- Requirements met (acceptance check passes): **{len(s['requirements']) - len(failed)} of {len(s['requirements'])}**",
             f"- Gates (`bash checks/run_gates.sh`): **{'pass' if run_gates()[0] else 'FAIL'}**",
             f"- Independent audit against the spec: **PASS** after {audit.get('rounds', 0)} audit(s)", "",
             "## Requirements", "", "| Id | Priority | Requirement | Acceptance check | Result | Milestone |", "|---|---|---|---|---|---|"]
    for q in s["requirements"]:
        lines.append(f"| {q['id']} | {q.get('priority', 'must')} | {q['statement'].replace('|', '/')} | `checks/acceptance/{q['id']}.sh` | "
                     f"{'FAIL' if q['id'] in failed else 'pass'} | {owner.get(q['id'], '-')} |")
    lines += ["", "## Ready to deploy when", ""] + [f"- {x}" for x in s.get("deploy", {}).get("ready_when", [])]
    lines += ["", f"Deploy target: {s.get('deploy', {}).get('target', 'not stated')}. The auditor checked these statements; "
              "anything that needs the user (see below) is not done by the run.", "", "## Left for the user", ""]
    lines += [f"- {x}" for x in s.get("human_gates", [])] or ["- nothing listed in the spec"]
    lines += ["", "## How to run it", "", "See `spec/DESIGN.md` (section on running and testing) and `init.sh`.", "",
              "## Work log", "", "`PROGRESS.md` has one entry per step; `ledger.jsonl` has every task with its check and commit.", ""]
    (ROOT / "DELIVERY.md").write_text("\n".join(lines))
    audit["delivered"] = utc()
    save(cards)
    git("add", "DELIVERY.md")
    commit_state("deliver: report")
    if not push:
        return "DELIVERY.md written (not pushed: --no-push)"
    if not RUNBR:
        return "DELIVERY.md written (no git remote, so nothing was pushed)"
    err = push_run()
    if err:
        return f"DELIVERY.md written; push failed: {err}"
    if not shutil.which("gh"):
        return "DELIVERY.md written and branch pushed (gh not installed: open the pull request yourself)"
    title = f"Deliver: {s.get('project', ROOT.name)}"[:70]
    pr = sh(["gh", "pr", "edit", branch, "--title", title, "--body-file", "DELIVERY.md"], timeout=120)   # the draft opened at the first push
    pr = sh(["gh", "pr", "ready", branch], timeout=120) if pr.returncode == 0 else \
        sh(["gh", "pr", "create", "--base", "main", "--head", branch, "--title", title, "--body-file", "DELIVERY.md"], timeout=120)
    return "DELIVERY.md written, branch pushed, " + ("pull request ready with the delivery report" if pr.returncode == 0
                                                     else f"pull request not updated ({(pr.stderr or pr.stdout).strip()[-160:]})")


# ---------- commands ----------
def summary(cards):
    lines = []
    for c in cards:
        if c.get("kind") == "design":
            lines.append(f"DESIGN [{c['state']}]" + (f" — {c['note'].splitlines()[0][:140]}" if c.get("note") and c["state"] != "done" else ""))
        elif c.get("kind") == "milestone":
            ks = kids(cards, c["id"])
            lines.append(f"{c['id']} [{c['state']}] {c['title']} — {sum(k['state'] == 'done' for k in ks)}/{len(ks)} tasks done"
                         + (f" — {c['note'].splitlines()[0][:140]}" if c.get("note") and c["state"] != "done" else ""))
            lines += [f"  {k['id']} [{k['state']}] {k['title']}" + (f" — {k['note'].splitlines()[0][:140]}" if k.get("note") else "")
                      for k in ks if k["state"] != "done"]
        elif c.get("kind") == "audit":
            lines.append(f"AUDIT [{c['state']}] after {c.get('rounds', 0)} audit(s)" + (f" — {c['note'].splitlines()[0][:140]}" if c.get("note") and c["state"] != "done" else ""))
    return "\n".join(lines) or "nothing in the ledger yet (the run has not started)"


def run_state(cards, last):
    """The `run:` line of `status`: what the master session (or its wake-up task) does next."""
    if (RUN / "lock").is_file():
        with open(RUN / "lock") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return "active in this clone (the run lock is held)"
    if by_id(cards).get("AUDIT", {}).get("delivered"):                 # set by deliver(); an audit pass alone is not a delivery
        return "DELIVERED"
    if last.is_file() and RESUMABLE not in last.read_text().splitlines():
        return "stopped, needs the user (read the last stop below)"
    return "not active (start it again to continue)"


def cmd_status(_):
    p = to_run_branch(create=False, sync=True)                    # a new clone is on main; the ledger is on the run branch
    if p:
        print(f"note: {p}\nThe summary below is the ledger of the current branch.")
    p = spec_problem()
    last = RUN / "last-stop.txt"
    print(("spec: approved" if not p else f"spec: {p}") + f"\nrun: {run_state(load(), last)}\n" + summary(load()))
    if last.is_file():
        print("\nlast stop: " + last.read_text().strip())
    elif any(c["state"] == "blocked" for c in load()):            # a new clone: the questions are in the ledger
        print("\nblocked items:\n" + stop_report(load()))


def cmd_retry(a):
    """Set a blocked item back to todo with no attempts, after the user answered its question. For a task, its
    milestone too if that milestone was blocked by its tasks (it has no question of its own). Starts nothing."""
    RUN.mkdir(exist_ok=True)
    lock = open(RUN / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(f"cannot retry {a.id}: a run is active in this repo; stop it first (a running loop would lose the change)")
    p = to_run_branch(create=False, sync=True)
    if p:
        sys.exit(f"cannot retry {a.id}: {p}")
    if "origin" in git("remote", check=False).split():
        RUNBR.extend([git("branch", "--show-current"), "draft"])  # record() pushes the change; the run opens the pull request
    cards = load()
    ids = by_id(cards)
    c = ids.get(a.id)
    if not c or c["state"] != "blocked":
        sys.exit(f"cannot retry {a.id}: it is not blocked (python3 harness/loop.py status lists the blocked items)")
    items = [c]
    m = ids.get(c.get("parent"))
    if m and m["state"] == "blocked" and not m.get("question"):
        items.append(m)
    for x in items:
        if x.get("kind") == "milestone" and x.get("question"):
            x["rounds_at_retry"] = x.get("rounds", 0)          # its MAX_ROUNDS planning rounds count again from here
        x.update(state="todo", attempts=0, question="", answer="")
    record(cards, a.id, "retry after the user answered: " + ", ".join(x["id"] for x in items) + " set back to todo", "-",
           "start the run again (bash harness/run.sh)", f"chore({a.id}): retry")
    print(", ".join(x["id"] for x in items) + " set back to todo. Start the run again: bash harness/run.sh")


def finish(why, legs, code, resumable=False):
    cards = load()
    text = f"{utc()} LOOP STOPPED after {legs} legs: {why}"
    RUN.mkdir(exist_ok=True)
    (RUN / "last-stop.txt").write_text(text + "\n" + (RESUMABLE + "\n" if resumable else ""))
    print(f"\n{text}\n{summary(cards)}", flush=True)
    if code and RUNBR and shutil.which("gh"):                     # the pull request is the channel: GitHub notifies the user
        r = sh(["gh", "pr", "comment", RUNBR[0], "--body", f"```\n{text}\n\n{summary(cards)}\n```"], timeout=120)
        if r.returncode != 0:
            say(f"  stop not written to the pull request: {(r.stderr or r.stdout).strip()[-160:]}")
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", f"Build loop: {ROOT.name}", why[:200]], check=False, capture_output=True)
    sys.exit(code)


def cmd_run(a):
    RUN.mkdir(exist_ok=True)
    lock = open(RUN / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("cannot start: another loop is already running in this repo")
    old = RUN / "session.pid"                                     # a session of a killed loop may still be writing
    pid = old.read_text().strip() if old.is_file() else ""
    # only a run session of this clone: its command line holds the absolute path of this repo's run settings
    if pid.isdigit() and shutil.which("ps") and str(H / "run-settings.json") in sh(["ps", "-ww", "-o", "command=", "-p", pid]).stdout:
        say(f"ending the session of an earlier loop that is still running (process {pid})")
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(int(pid), sig)
            except OSError:
                break
            for _ in range(20):
                if sh(["ps", "-p", pid]).returncode != 0:
                    break
                time.sleep(0.25)
    old.unlink(missing_ok=True)
    if git("status", "--porcelain"):
        if not git("branch", "--show-current").startswith("build/"):
            sys.exit("cannot start: the working tree has uncommitted changes; commit or stash them first")
        say("discarding the uncommitted changes on the run branch (a leg that was cut off; each leg commits when it ends): "
            + "; ".join(sh(["git", "status", "--short"]).stdout.splitlines()[:5]))
        git("reset", "-q", "--hard")                               # ignored files (.env) stay
        sh(["git", "clean", "-qfd"])
    p = to_run_branch(create=True, sync=a.push) or spec_problem()
    if p:
        sys.exit(f"cannot start: {p}")
    branch = git("branch", "--show-current")
    if a.push and git("remote", check=False):
        RUNBR.append(branch)
    if not run_gates()[0]:
        sys.exit("cannot start: gates are failing before the run starts; fix them first (bash checks/run_gates.sh)")
    (RUN / "last-stop.txt").unlink(missing_ok=True)               # a stop of an earlier run is not this run's (a refused start keeps it)
    cards = load()
    if not any(c.get("kind") == "design" for c in cards):
        cards.insert(0, {"id": "DESIGN", "kind": "design", "state": "todo", "attempts": 0, "note": ""})
        record(cards, "DESIGN", "run started from the approved spec", "spec approved and unchanged", "design leg", "chore: start run")
    deadline, legs, streak, nblocked = time.time() + a.max_hours * 3600, 0, 0, None
    stuck = "no item is left that can run: each open item is blocked or waits on a blocked one. Answer each question:\n"
    try:
        while True:
            cards = load()
            ids = by_id(cards)
            n = sum(c["state"] == "blocked" for c in cards)
            streak, nblocked = (0 if nblocked is not None and n > nblocked else streak), n
            if legs >= a.max_legs:
                finish(f"leg budget used ({a.max_legs}). Start the run again to continue.", legs, 3, True)
            if time.time() > deadline + WAITED[0]:
                finish(f"time budget used ({a.max_hours:g} h). Start the run again to continue.", legs, 3, True)
            if streak >= FAIL_STREAK:
                finish(f"{FAIL_STREAK} legs in a row made no progress. Read the notes below; the spec or a check may need the user.", legs, 3)

            # 1. design
            d = ids["DESIGN"]
            if d["state"] != "done":
                if d["state"] == "blocked":
                    finish(stuck + stop_report(cards), legs, 3)
                legs += 1
                say(f"leg {legs}: DESIGN (attempt {d.get('attempts', 0) + 1})")
                ok, msg, r = design_leg(d, branch)
                if ok:
                    streak = 0
                    d.update(state="done", note="")
                    add_milestones(cards)
                else:
                    streak += 1
                    d["attempts"] = d.get("attempts", 0) + 1
                    d["note"] = msg[:2000]
                    d["state"] = "todo"
                    if d["attempts"] >= MAX_ATTEMPTS:
                        block(d, d["note"], r["text"])
                record(cards, "DESIGN", f"design leg ({r['turns']} turns, {r['secs']}s): {msg.splitlines()[0][:200]}", "design validation -> " + ("pass" if ok else "fail"),
                       "plan the first milestone" if ok else "redo the design with the rejection reasons", "chore(DESIGN): " + ("accepted" if ok else "rejected"))
                say(f"  -> {'accepted' if ok else 'rejected'}: {' '.join(msg.split())[:220]}")
                continue

            # 2. spec changed since it was planned?
            m = sync_spec(cards)
            if m:
                record(cards, m["id"], "spec re-approved with new or changed requirements", "-", "plan this milestone", f"chore({m['id']}): requirement changes")
                say(f"{m['id']} added for new or changed requirements")
                continue

            # 3. current milestone = first one that is not done or blocked and whose `depends_on` are all done
            dep = deps(cards)
            m = next((c for c in cards if c.get("kind") == "milestone" and c["state"] not in ("done", "blocked")
                      and all(ids[d]["state"] == "done" for d in dep[c["id"]])), None)
            if not m and any(c.get("kind") == "milestone" and c["state"] != "done" for c in cards):
                finish(stuck + stop_report(cards), legs, 3)
            if m:
                mid, ks = m["id"], kids(cards, m["id"])
                open_ks = [k for k in ks if k["state"] in ("todo", "doing")]
                if not ks or m.get("replan"):
                    if m.get("rounds", 0) - m.get("rounds_at_retry", 0) >= MAX_ROUNDS:
                        block(m, f"acceptance checks still fail after {MAX_ROUNDS} planning rounds: {m.get('replan', '')[:600]}")
                        record(cards, mid, "milestone blocked", "acceptance -> fail", "next item that does not depend on it", f"chore({mid}): blocked")
                        continue
                    legs += 1
                    say(f"leg {legs}: PLAN {mid} round {m.get('rounds', 0) + 1} — {m['title']}")
                    ok, msg, r = plan_leg(m, cards, branch)
                    if ok:
                        streak = 0
                    else:
                        streak += 1
                        m["attempts"] = m.get("attempts", 0) + 1
                        m["note"] = msg[:2000]
                        if m["attempts"] >= MAX_ATTEMPTS:
                            block(m, m["note"], r["text"])
                    record(cards, mid, f"plan leg ({r['turns']} turns, {r['secs']}s): {msg.splitlines()[0][:200]}", "plan validation -> " + ("pass" if ok else "fail"),
                           "work the first task" if ok else ("blocked: next item that does not depend on it" if m["state"] == "blocked" else "re-plan with the rejection reasons"),
                           f"chore({mid}): plan " + ("accepted" if ok else "rejected"))
                    say(f"  -> {'accepted' if ok else 'rejected'}: {' '.join(msg.split())[:220]}")
                    continue
                task = next((k for k in open_ks if all(ids[x]["state"] == "done" for x in k.get("depends_on", []))), None)
                if task:
                    tid = task["id"]
                    legs += 1
                    say(f"leg {legs}: WORK {tid} (attempt {task.get('attempts', 0) + 1}) — {task['title']}")
                    ok, msg, r, start = work_leg(task, branch)
                    stage, vr = "check", None
                    if ok:
                        legs += 1
                        say(f"leg {legs}: VERIFY {tid}")
                        stage = "verify"
                        ok, msg, vr = verify_leg(task, start, branch)
                    head = git("rev-parse", "--short", "HEAD")
                    if ok:
                        streak = 0
                        task.update(state="done", commit=head, checks="pass", note=f"verified: {msg[:300]}")
                    else:
                        streak += 1
                        task["attempts"] = task.get("attempts", 0) + 1
                        task["note"] = f"attempt {task['attempts']} failed at {stage}: {msg[:1800]}"
                        task["state"] = "todo"
                        asked = msg.startswith("the session changed nothing") and all(   # a real blocker: retrying it spends usage for nothing
                            re.search(rf"^\s*{k}:", r["text"], re.MULTILINE) for k in ("QUESTION", "RECOMMENDED"))
                        if task["attempts"] >= MAX_ATTEMPTS or asked:
                            block(task, task["note"], (vr or r)["text"])
                    record(cards, tid, f"work leg ({r['turns']} turns, {r['secs']}s)" + (f" + verify leg ({vr['turns']} turns, {vr['secs']}s)" if vr else "") + f": {msg.splitlines()[0][:200]}",
                           f"{task['check']} + checks/run_gates.sh + independent verify -> " + ("pass" if ok else f"fail at {stage}"),
                           "next eligible task" if ok else ("blocked: next item that does not depend on it" if task["state"] == "blocked" else "retry with this failure as feedback"),
                           f"chore({tid}): " + ("done" if ok else task["state"]))
                    say(f"  -> {'DONE' if ok else task['state'].upper()}: {' '.join(msg.split())[:220]}")
                    continue
                if any(k["state"] == "blocked" for k in ks):
                    blocked = [k for k in ks if k["state"] == "blocked"]
                    m.update(state="blocked", note="blocked by its tasks; no question of its own, answer theirs: " + "; ".join(f"{k['id']} ({k['note'].splitlines()[0][:160]})" for k in blocked))
                    record(cards, mid, "milestone blocked by its tasks", "-", "next item that does not depend on it", f"chore({mid}): blocked")
                    continue
                # every task done: the milestone closes only if its requirements' acceptance checks pass
                say(f"CLOSE {mid}: running acceptance checks for {', '.join(m['requirements']) or 'no requirements'}")
                failed = acceptance(m["requirements"])
                gok, gout = run_gates()
                if not failed and gok:
                    streak = 0
                    m.update(state="done", note="all tasks done and verified; acceptance checks pass")
                    record(cards, mid, "milestone closed", "acceptance checks + gates -> pass", "next milestone", f"chore({mid}): done")
                    say(f"{mid} DONE")
                else:
                    streak += 1
                    m["replan"] = ("All planned tasks are done but these still fail. Plan the missing work (do not change the checks):\n"
                                   + "\n".join(f"{rid}: {out[-500:]}" for rid, out in failed.items()) + ("" if gok else f"\ngates: {gout}"))[:3000]
                    record(cards, mid, "tasks done but acceptance fails: " + ", ".join(failed) + ("" if gok else " + gates"), "acceptance -> fail",
                           "plan another round for this milestone", f"chore({mid}): acceptance failing")
                    say(f"  -> acceptance failing for {', '.join(failed) or 'gates'}: back to PLAN")
                continue

            # 4. every milestone closed: audit
            au = ids["AUDIT"]
            if au["state"] == "done":
                say("DELIVER")
                out = deliver(cards, branch, a.push)
                finish(f"DELIVERED. {out}", legs, 0)
            if au.get("rounds", 0) >= MAX_AUDITS:
                finish(f"the audit still finds gaps after {MAX_AUDITS} audits: needs the user. {au.get('note', '')[:400]}", legs, 3)
            all_ids = [q["id"] for q in spec()["requirements"]]
            failed = acceptance(all_ids)
            gok, gout = run_gates()
            if failed or not gok:
                au["rounds"] = au.get("rounds", 0) + 1
                au["note"] = "regressions: " + ", ".join(failed)
                m = add_fix_milestone(cards, "Regression fixes", ("Checks that passed when their milestone closed now fail. Fix the product (do not change the checks):\n"
                                      + "\n".join(f"{rid}: {out[-500:]}" for rid, out in failed.items()) + ("" if gok else f"\ngates: {gout}"))[:3000], list(failed), [])
                m["replan"] = m["goal"]
                record(cards, "AUDIT", "full re-run of acceptance checks found regressions: " + (", ".join(failed) or "gates"), "all acceptance + gates -> fail", f"plan {m['id']}", "chore(AUDIT): regressions")
                say(f"  -> regressions in {', '.join(failed) or 'gates'}: {m['id']} added")
                continue
            legs += 1
            say(f"leg {legs}: AUDIT {au.get('rounds', 0) + 1}")
            verdict, gaps, text, r = audit_leg(branch)
            au["rounds"] = au.get("rounds", 0) + 1
            if verdict:
                streak = 0
                au.update(state="done", note="independent audit: PASS")
                record(cards, "AUDIT", f"audit leg ({r['turns']} turns, {r['secs']}s): PASS", "all acceptance + gates + independent audit -> pass", "deliver", "chore(AUDIT): pass")
                say("  -> PASS")
                continue
            streak += 1
            au["note"] = (("gaps: " + "; ".join(f"{g}: {t}" for g, t in gaps)) if gaps else text)[:2000]
            if gaps:
                rids = [g for g, _ in gaps]
                m = add_fix_milestone(cards, "Audit fixes", ("An independent audit against the spec found these gaps. For each one, write a check that "
                                      "exposes it (it must fail now), then plan the fix:\n" + "\n".join(f"- {g}: {t}" for g, t in gaps))[:3000], rids, rids)
                nxt = f"plan {m['id']}"
            else:
                nxt = "audit again"
            record(cards, "AUDIT", f"audit leg ({r['turns']} turns, {r['secs']}s): FAIL — {au['note'][:200]}", "independent audit -> fail", nxt, "chore(AUDIT): gaps")
            say(f"  -> FAIL: {' '.join(au['note'].split())[:220]}")
    except Stop as e:
        finish(e.args[0], legs, 3, *e.args[1:])
    except KeyboardInterrupt:
        finish("interrupted. Start the run again to continue.", legs, 3, True)


def main():
    p = argparse.ArgumentParser(description="Build loop: approved spec in, verified project out")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--max-legs", type=int, default=400)
    r.add_argument("--max-hours", type=float, default=24)
    r.add_argument("--no-push", dest="push", action="store_false", help="do not push the branch or open a pull request")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    t = sub.add_parser("retry")
    t.add_argument("id", help="the id of a blocked item, as the stop report shows it")
    t.set_defaults(fn=cmd_retry)
    a = p.parse_args()
    os.chdir(ROOT)
    a.fn(a)


if __name__ == "__main__":
    main()
