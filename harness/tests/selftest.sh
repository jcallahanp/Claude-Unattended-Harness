#!/usr/bin/env bash
# Proves the loop's state machine without calling a model: copies the harness into a temp repo, approves
# a two-requirement spec, runs the whole loop against harness/tests/mock_claude and checks the outcome.
# The run is stopped after 4 legs and continued in a new clone of its remote; `gh` is a stand-in that logs.
# A second run blocks a task; a new clone of its remote shows the question, retries the task and delivers.
# The mock misbehaves on purpose (see its docstring), so this also proves the loop's defences.
set -euo pipefail
src="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
repo="$tmp/repo"; mkdir -p "$repo/spec" "$repo/plan" "$repo/notes" "$repo/checks/tasks" "$repo/checks/acceptance" "$repo/checks/guards"
cp -r "$src/harness" "$src/.claude" "$src/CLAUDE.md" "$src/.gitignore" "$repo/"
cp "$src"/checks/run_gates.sh "$src"/checks/check_scope.sh "$src"/checks/check_progress.sh "$src"/checks/check_protected.sh "$repo/checks/"
git -C "$src" show HEAD:checks/check_project.sh > "$repo/checks/check_project.sh" 2>/dev/null || cp "$src/checks/check_project.sh" "$repo/checks/check_project.sh"
grep -q "written by the design session" "$repo/checks/check_project.sh" || printf '#!/usr/bin/env bash\necho "  placeholder"; exit 0\n' > "$repo/checks/check_project.sh"
cd "$repo"; : > ledger.jsonl; printf '# Progress log\n' > PROGRESS.md
cat > checks/guards/max_file_size.sh <<'SH'
#!/usr/bin/env bash
# Guard: no file in the project is larger than 1 MB.
cd "$(dirname "$0")/../.." || exit 2
rc=0
while IFS= read -r -d '' f; do
  [[ -f "$f" ]] || continue
  if (( $(stat -c %s "$f") > 1048576 )); then echo "  file larger than 1 MB: $f"; rc=1; fi
done < <(git ls-files -z --cached --others --exclude-standard)
exit $rc
SH
cp checks/guards/max_file_size.sh "$tmp/guard.sh"
git init -q -b main; git config user.name selftest; git config user.email selftest@example.invalid
git add -A; git commit -q -m "template"                                            # the template commit: no spec yet
cat > spec/requirements.json <<'JSON'
{"project": "selftest-calc", "summary": "Two tiny command-line calculators used only to prove that the build loop works from an approved spec to a delivery report.",
 "users": ["the harness self-test"],
 "personas": ["I am a tester using the calculators. I do one sum and one difference. I trust it to print the right number. I never have to do the sum by hand."],
 "requirements": [
  {"id": "R1", "priority": "must", "story": "As a tester, I want to add two integers so that I can see a sum.",
   "statement": "When app/add.py is run with two integers, the system shall print their sum on one line.",
   "examples": [{"given": "the program exists", "when": "it is run with 2 and 3", "then": "it prints 5"}],
   "verify": "Run python3 app/add.py 2 3 and compare the output with 5."},
  {"id": "R2", "priority": "must", "story": "As a tester, I want to subtract two integers so that I can see a difference.",
   "statement": "When app/sub.py is run with two integers, the system shall print the first minus the second on one line.",
   "examples": [{"given": "the program exists", "when": "it is run with 2 and 5", "then": "it prints -3"}],
   "verify": "Run python3 app/sub.py 2 5 and compare the output with -3."}],
 "non_goals": ["anything beyond integers"], "constraints": ["Python 3 standard library only"], "assumptions": [],
 "deploy": {"target": "none (self-test)", "ready_when": ["both programs run from a fresh clone"]},
 "secrets": [], "human_gates": [], "open_questions": []}
JSON
python3 -I harness/spec.py approve >/dev/null
cp -a "$repo" "$tmp/repo3"                                                         # for the setup and run.sh checks, no remote
git init -q --bare "$tmp/origin.git"; git remote add origin "$tmp/origin.git"; git push -q -u origin main
cp -a "$repo" "$tmp/repo2"                                                         # for the run with a blocked milestone
git init -q --bare "$tmp/origin2.git"; git -C "$tmp/repo2" remote set-url origin "$tmp/origin2.git"; git -C "$tmp/repo2" push -q origin main
mkdir "$tmp/bin"; cat > "$tmp/bin/gh" <<'SH'
#!/usr/bin/env bash
# Stand-in for gh: logs its arguments; a second `pr create` fails as the real one does; MOCK_GH_FAIL=1: every call fails.
mkdir -p "$MOCK_STATE"
[ -n "${MOCK_GH_FAIL:-}" ] && { echo "gh: request rejected" >&2; exit 1; }
if [ "$1 $2" = "pr create" ] && grep -q '^pr create' "$MOCK_STATE/gh.log" 2>/dev/null; then
  echo "a pull request for branch already exists" >&2; exit 1; fi
printf '%s\n' "$*" >> "$MOCK_STATE/gh.log"
exit 0
SH
chmod +x "$tmp/bin/gh"

unset HARNESS_MODEL HARNESS_EFFORT ${!HARNESS_MODEL_@} ${!HARNESS_EFFORT_@}       # the defaults in loop.py are under test
export MOCK_STATE="$tmp/mock" HARNESS_CLAUDE="$src/harness/tests/mock_claude" HARNESS_RETRY_WAITS=0,0 HARNESS_LIMIT_POLL=0 PATH="$tmp/bin:$PATH"
set +e; python3 -I harness/loop.py run --max-legs 4 > "$tmp/run.log" 2>&1; rc=$?; set -e
fail() { echo "SELFTEST FAIL: $1"; echo "--- run log ---"; cat "$tmp/run.log" "$tmp/run2.log" 2>/dev/null; exit 1; }
st() { python3 -c 'import json,sys; print({c["id"]: c["state"] for c in map(json.loads, open("ledger.jsonl"))}.get(sys.argv[1]))' "$1"; }
[ "$rc" = 3 ] || fail "the run stopped after 4 legs exited $rc, expected 3"
grep -q "waiting 0s: usage limit (You've hit your session limit" "$tmp/run.log" || fail "the usage limit was not waited out"
python3 -c 'import json; t = {c["id"]: c for c in map(json.loads, open("ledger.jsonl"))}["M1.T1"]; assert t["state"] == "done" and t["attempts"] == 0' \
  || fail "after the usage limit M1.T1 is not done, or the wait counted as a failed attempt"
grep -q "^pr create --draft" "$tmp/mock/gh.log" || fail "the first push did not open a draft pull request"
grep -q "leg budget used" "$tmp/mock/gh.log" || fail "the stop reason was not written to the pull request"
done1=$(grep -cx M1.T1 "$tmp/mock/work")
# a new session: a new clone of the remote, on a session branch made from the template commit, before the spec existed
# (as in a cloud session); `status` and `run` read the spec on main, find the run branch on origin and continue it. The 7 legs left end with the audit pass: that stop is not a delivery
git clone -q -b main "$tmp/origin.git" "$tmp/clone"; cd "$tmp/clone"; git config user.name selftest; git config user.email selftest@example.invalid
git switch -q -c claude/x "$(git rev-list --max-parents=0 HEAD)"
grep -q "^M1 \[" <<<"$(python3 -I harness/loop.py status 2>&1)" || fail "status in a new clone on a session branch does not show the run's ledger"
git switch -q claude/x
set +e; python3 -I harness/loop.py run --max-legs 7 >> "$tmp/run.log" 2>&1; rc=$?; set -e
[ "$rc" = 3 ] && [ "$(st AUDIT)" = done ] && [ ! -e DELIVERY.md ] || fail "the run in the new clone did not stop by its leg budget directly after the audit pass (exit $rc)"
grep -qx "run: not active (start it again to continue)" <<<"$(python3 -I harness/loop.py status 2>&1)" || fail "status after an audit pass with no delivery does not say run: not active"
echo "half a leg" > app/half_leg.py; echo "# half a leg" >> app/add.py                # a leg cut off by the end of its command
set +e; python3 -I harness/loop.py run >> "$tmp/run.log" 2>&1; rc=$?; set -e
[ "$rc" = 0 ] || fail "the run continued in a new clone exited $rc, expected 0"
grep -q "discarding the uncommitted changes on the run branch.*app/half_leg.py" "$tmp/run.log" && [ ! -e app/half_leg.py ] || fail "the changes of a cut-off leg on the run branch were not discarded"
[ "$(grep -cx M1.T1 "$tmp/mock/work")/$(grep -c '^DESIGN ' "$tmp/mock/models")" = "$done1/1" ] || fail "an item done before the stop was done again after it"
echo "  stopped after 4 legs, continued in a new clone on a session branch, stopped after the audit (not delivered), half leg discarded, delivered: nothing done twice"
grep -q "^pr ready build/selftest-calc" "$tmp/mock/gh.log" && grep -q "^pr edit build/selftest-calc .*--body-file DELIVERY.md" "$tmp/mock/gh.log" \
  || fail "delivery did not put DELIVERY.md in the pull request and mark it ready"
grep -q "DELIVERED" "$tmp/run.log" || fail "no DELIVERED line"
grep -q "Requirements met (acceptance check passes): \*\*2 of 2\*\*" DELIVERY.md || fail "DELIVERY.md does not report 2 of 2"
grep -q "provider error, will retry" "$tmp/run.log" || fail "the provider error was not retried"
grep -q '"FAKE"' ledger.jsonl && fail "a worker's edit to the ledger survived"
grep -q '^exit 0' checks/tasks/M1.T2.sh && fail "a worker's edit to a task check survived"
grep -q '^exit 0' checks/acceptance/R1.sh && fail "a planner's edit to a frozen acceptance check survived"
cmp -s checks/guards/max_file_size.sh "$tmp/guard.sh" || fail "a worker's change or delete of a guard script survived"
grep -q '"id": "M2"' ledger.jsonl || fail "the audit gap did not create a fix milestone"
[ "$(python3 app/sub.py 2 5)" = "-3" ] || fail "the audit gap was not fixed"
[ "$(git branch --show-current)" = "build/selftest-calc" ] || fail "the run did not work on a build branch"
[ -z "$(git status --porcelain)" ] || fail "the run left uncommitted changes"
bash checks/run_gates.sh >/dev/null || fail "gates fail after delivery"
# each step got its model and effort: opus for design, plan, work and audit, sonnet for verify, medium effort
for k in DESIGN PLANNER WORKER VERIFIER AUDITOR; do
  want="opus medium"; [ "$k" = VERIFIER ] && want="sonnet medium"
  got=$(sed -n "s/^$k //p" "$tmp/mock/models" | sort -u)
  [ "$got" = "$want" ] || fail "$k sessions got model and effort '${got//$'\n'/, }', expected '$want'"
  echo "  $k: $got"
done
# a variable changes the model and effort of its own step
: > "$tmp/mock/models"
HARNESS_MODEL_VERIFY=haiku HARNESS_EFFORT_VERIFY=low python3 -I -c 'import sys; sys.path.insert(0, "harness"); import loop; loop.run_claude("verify", "You are the VERIFIER session")' >/dev/null
[ "$(cat "$tmp/mock/models")" = "VERIFIER haiku low" ] || fail "HARNESS_MODEL_VERIFY / HARNESS_EFFORT_VERIFY did not change the verify session: $(cat "$tmp/mock/models")"
echo "  VERIFIER with HARNESS_MODEL_VERIFY=haiku HARNESS_EFFORT_VERIFY=low: haiku low"
# a spec edited after approval must stop the next run before it starts
sed -i 's/"anything beyond integers"/"anything beyond whole numbers"/' spec/requirements.json; git commit -q -am "edit spec"
if python3 -I harness/loop.py run --no-push >/dev/null 2>&1; then fail "the loop started with a spec that changed after approval"; fi
# a blocked milestone does not stop the run: an independent milestone is done, a dependent one does not start,
# and the stop report has one question and one recommended answer for the blocked item
# it starts on a session branch made from the template commit, with no run branch yet: the run starts from main
cd "$tmp/repo2"; git switch -q -c claude/y "$(git rev-list --max-parents=0 HEAD)"
set +e; MOCK_BLOCK=1 MOCK_STATE="$tmp/mock2" python3 -I harness/loop.py run > "$tmp/run2.log" 2>&1; rc=$?; set -e
[ "$rc" = 3 ] || fail "the run with a blocked milestone exited $rc, expected 3"
[ "$(git branch --show-current)" = build/selftest-calc ] && git merge-base --is-ancestor main build/selftest-calc \
  || fail "the run on a session branch made before the spec did not start from main on build/selftest-calc"
[ "$(grep -cx M1.T1 "$tmp/mock2/work")" = 1 ] || fail "M1.T1 reported a blocker but did not get exactly one work session"
grep -q "QUESTION: Which paid account" "$tmp/mock2/gh.log" || fail "the stop report did not reach a pull request comment"
grep -q "DELIVERED" "$tmp/run2.log" && fail "the run delivered with a blocked milestone"
[ "$(st M1.T1)/$(st M1)" = blocked/blocked ] || fail "M1.T1 and M1 are not blocked"
[ "$(st M3)" = done ] || fail "the independent milestone M3 was not done after M1 was blocked"
[ "$(st M2)" = todo ] && ! grep -q "PLAN M2" "$tmp/run2.log" || fail "M2, which waits on the blocked M1, started"
report=$(sed -n '/LOOP STOPPED/,$p' "$tmp/run2.log")
[ "$(grep -c 'QUESTION:' <<<"$report")/$(grep -c 'RECOMMENDED:' <<<"$report")" = 1/1 ] || fail "the stop report has not one question and one answer"
grep -A2 "^M1.T1 blocked" <<<"$report" | grep -q "QUESTION: Which paid account" || fail "the question of M1.T1 is not in the stop report"
grep -A2 "^M1.T1 blocked" <<<"$report" | grep -q "RECOMMENDED: None; compute" || fail "the recommended answer of M1.T1 is not in the stop report"
grep -qx "M2 waits on M1" <<<"$report" || fail "the stop report does not list M2 as waiting on M1"
grep -qx "run: stopped, needs the user (read the last stop below)" <<<"$(python3 -I harness/loop.py status 2>&1)" || fail "status after a blocked stop does not say run: stopped, needs the user"
git switch -q -c claude/w; echo stray > stray.txt                                    # a refused start keeps the stop report
python3 -I harness/loop.py run --no-push >/dev/null 2>&1 && fail "run on a session branch with uncommitted changes started"
grep -qx "run: stopped, needs the user (read the last stop below)" <<<"$(python3 -I harness/loop.py status 2>&1)" || fail "a refused start removed the stop report"
rm stray.txt; git switch -q build/selftest-calc; git branch -q -D claude/w
echo "  blocked M1: M3 done, M2 waits, one question and one recommended answer in the stop report; status: needs the user, also after a refused start"
# a new session in a new clone of that run's remote, on main: `status` finds the run branch and shows the question;
# the answer goes in notes/decisions.md, `retry` sets the task back and pushes, the next run finishes it.
# The clone's pre-push hook always fails: the loop's own pushes skip it, so the state still reaches the remote.
git clone -q -b main "$tmp/origin2.git" "$tmp/clone2"; cd "$tmp/clone2"; git config user.name selftest; git config user.email selftest@example.invalid
printf '#!/bin/sh\nexit 1\n' > .git/hooks/pre-push; chmod +x .git/hooks/pre-push
grep -q "QUESTION: Which paid account" <<<"$(python3 -I harness/loop.py status 2>&1)" || fail "status in a new clone does not show the question of M1.T1"
mkdir -p notes; echo "M1.T1: no paid account; compute the sum locally (the user's answer)" >> notes/decisions.md
git add notes/decisions.md; git commit -q -m "decision for M1.T1"; git switch -q main     # `retry` finds the run branch by itself
python3 -I harness/loop.py retry M3 >/dev/null 2>&1 && fail "retry accepted M3, which is not blocked"
mkdir -p .harness-run; flock .harness-run/lock python3 -I harness/loop.py retry M1.T1 >/dev/null 2>&1 && fail "retry accepted M1.T1 while a run held the lock"
python3 -I harness/loop.py retry M1.T1 >/dev/null 2>&1 || fail "retry of the blocked M1.T1 in a new clone failed"
[ "$(git -C "$tmp/origin2.git" log -1 --format=%s build/selftest-calc)" = "chore(M1.T1): retry" ] || fail "the retry commit is not on the remote"
set +e; MOCK_GH_FAIL=1 MOCK_BLOCK=1 MOCK_STATE="$tmp/mock2" python3 -I harness/loop.py run > "$tmp/run3.log" 2>&1; rc=$?; set -e
cat "$tmp/run3.log" >> "$tmp/run2.log"
[ "$rc" = 0 ] || fail "the run after retry exited $rc, expected 0"
[ "$(st M1.T1)/$(st M1)/$(st M2)" = done/done/done ] || fail "after retry M1.T1, M1 and M2 are not done"
grep -q "DELIVERED" "$tmp/run3.log" || fail "the run after retry did not deliver"
grep -q "pull request not updated" "$tmp/run3.log" && git -C "$tmp/origin2.git" cat-file -e build/selftest-calc:DELIVERY.md \
  || fail "with gh failing, delivery did not push DELIVERY.md or did not say that the pull request was not updated"
grep -q "push failed" "$tmp/run3.log" && fail "a push of the loop failed (the pre-push hook ran?)"
[ "$(git rev-parse HEAD)" = "$(git -C "$tmp/origin2.git" rev-parse build/selftest-calc)" ] || fail "the delivered state is not on the remote"
echo "  new clone: status shows the question, retry M1.T1 pushed, M1 and M2 done, delivered with gh failing (pre-push hook skipped)"
# change mode after delivery: a requirement added and approved on the run branch is planned and built by the next run;
# approve is refused on a session branch
git switch -q -c claude/z; python3 -I harness/spec.py approve >/dev/null 2>&1 && fail "approve ran on the session branch claude/z"
git switch -q build/selftest-calc; git branch -q -D claude/z
python3 - <<'PY'
import json; f = "spec/requirements.json"; s = json.load(open(f))
s["requirements"].append({"id": "R3", "priority": "must", "story": "As a tester, I want to multiply two integers so that I can see a product.",
  "statement": "When app/mul.py is run with two integers, the system shall print their product on one line.",
  "examples": [{"given": "the program exists", "when": "it is run with 2 and 3", "then": "it prints 6"}],
  "verify": "Run python3 app/mul.py 2 3 and compare the output with 6."})
json.dump(s, open(f, "w"), indent=1)
PY
git commit -q -am "spec: add R3"; python3 -I harness/spec.py approve >/dev/null || fail "approve on the run branch failed"
set +e; MOCK_BLOCK=1 MOCK_STATE="$tmp/mock2" python3 -I harness/loop.py run --no-push > "$tmp/run5.log" 2>&1; rc=$?; set -e
cat "$tmp/run5.log" >> "$tmp/run2.log"
[ "$rc" = 0 ] && grep -q "added for new or changed requirements" "$tmp/run5.log" && [ "$(python3 app/mul.py 2 3)" = 6 ] \
  && grep -q "\*\*3 of 3\*\*" DELIVERY.md || fail "the requirement added on the run branch after delivery was not planned, built and delivered (exit $rc)"
echo "  change after delivery: R3 approved on the run branch (refused on claude/z), planned, built and delivered"
# the first clone fell behind: `status` there fast-forwards it to the remote run branch
cd "$tmp/repo2"; out=$(python3 -I harness/loop.py status 2>&1)
grep -q "^AUDIT \[done\]" <<<"$out" || fail "status in a clone that fell behind did not fast-forward it"
grep -qx "run: DELIVERED" <<<"$out" || fail "status after delivery does not say run: DELIVERED"
echo "  clone that fell behind: status fast-forwarded it to the delivered state"
# setup records the wake-up, keeps it on a re-run without `wakeup=`, refuses a wrong value or a wake-up in a terminal
cd "$tmp/repo3"
python3 -I harness/spec.py setup env=cloud boundary=cloud sudo=no >/dev/null || fail "a first cloud setup without wakeup= failed"
grep -A2 "^## Wake-up" spec/SETUP.md | grep -qx "None: no scheduled task continues the run. .*" || fail "a first cloud setup without wakeup= did not record none"
python3 -I harness/spec.py setup env=cloud boundary=cloud sudo=no wakeup=hourly >/dev/null || fail "setup with wakeup=hourly failed"
grep -A2 "^## Wake-up" spec/SETUP.md | grep -q "An hourly scheduled task named \`build wake-up\`" || fail "setup did not write the hourly wake-up"
python3 -I harness/spec.py setup env=cloud boundary=cloud sudo=no >/dev/null || fail "setup without wakeup= failed"
grep -A2 "^## Wake-up" spec/SETUP.md | grep -q "An hourly scheduled task" || fail "setup without wakeup= did not keep the wake-up"
python3 -I harness/spec.py setup env=cloud boundary=cloud sudo=no wakeup=daily >/dev/null 2>&1 && fail "setup accepted wakeup=daily"
python3 -I harness/spec.py setup env=terminal boundary=vm sudo=no wakeup=hourly >/dev/null 2>&1 && fail "setup accepted a wake-up in a terminal"
python3 -I harness/spec.py setup env=terminal boundary=vm sudo=no >/dev/null || fail "setup in a terminal failed"
grep -A2 "^## Wake-up" spec/SETUP.md | grep -q "None (terminal)" || fail "setup in a terminal did not record no wake-up"
echo "  setup: wake-up recorded, kept on a re-run, wrong value and terminal wake-up refused"
# run.sh --foreground runs the preflight and the loop and returns the loop's exit code; status says if a run is active
mkdir -p .harness-run
grep -qx "run: active in this clone (the run lock is held)" <<<"$(flock .harness-run/lock python3 -I harness/loop.py status 2>&1)" \
  || fail "status with the run lock held does not say run: active"
set +e; HARNESS_SKIP_CLAUDE_PROBE=1 flock .harness-run/lock bash harness/run.sh --foreground --no-push > "$tmp/fg.log" 2>&1; rc=$?; set -e
[ "$rc" = 1 ] && grep -q "cannot start: another loop is already running" "$tmp/fg.log" || fail "run.sh --foreground with the run lock held exited $rc, expected the loop's 1"
set +e; HARNESS_SKIP_CLAUDE_PROBE=1 bash harness/run.sh --foreground --no-push --max-legs 0 >> "$tmp/fg.log" 2>&1; rc=$?; set -e
[ "$rc" = 3 ] || fail "run.sh --foreground --max-legs 0 exited $rc, expected the loop's 3: $(tail -5 "$tmp/fg.log")"
grep -qx "run: not active (start it again to continue)" <<<"$(python3 -I harness/loop.py status 2>&1)" || fail "status after a budget stop does not say run: not active"
set +e; MOCK_AUTH=1 MOCK_STATE="$tmp/mock3" python3 -I harness/loop.py run --no-push >> "$tmp/fg.log" 2>&1; rc=$?; set -e
[ "$rc" = 3 ] && grep -qx "run: stopped, needs the user (read the last stop below)" <<<"$(python3 -I harness/loop.py status 2>&1)" \
  || fail "status after a failed sign-in (exit $rc) does not say run: stopped, needs the user"
rm -rf "$tmp/mock3"
git switch -q main; echo stray > stray.txt
grep -q "cannot start: the working tree has uncommitted changes" <<<"$(python3 -I harness/loop.py run --no-push 2>&1)" || fail "run on main with uncommitted changes did not refuse"
[ -e stray.txt ] || fail "run on main discarded uncommitted changes"
echo "  run.sh --foreground: exit 1 and 3 of the loop passed on; status run: active, DELIVERED, not active, needs the user (sign-in); dirty main refused"
# a loop killed during a session leaves that session running; the next `run` ends it before it goes on, then delivers
rm stray.txt; alive() { ps -o stat= -p "$1" 2>/dev/null | grep -qv Z; }
setsid bash -c "exec -a '$HARNESS_CLAUDE' sleep 60" & decoy=$!                      # holds "claude", not this clone's settings path
sleep 0.2; echo "$decoy" > .harness-run/session.pid
python3 -I harness/loop.py run --no-push --max-legs 0 >> "$tmp/run4.log" 2>&1 || true
alive "$decoy" || fail "run ended a process that is not a run session of this clone"
kill "$decoy"; wait "$decoy" 2>/dev/null || true
MOCK_SLEEP=30 MOCK_STATE="$tmp/mock3" python3 -I harness/loop.py run --no-push > "$tmp/run4.log" 2>&1 & loop=$!
for _ in $(seq 100); do [ -s .harness-run/session.pid ] && break; sleep 0.1; done
old=$(cat .harness-run/session.pid 2>/dev/null) || fail "the loop did not write .harness-run/session.pid"
kill -9 "$loop"; wait "$loop" 2>/dev/null || true
alive "$old" || fail "the session did not outlive its killed loop (the test proves nothing)"
set +e; MOCK_STATE="$tmp/mock3" python3 -I harness/loop.py run --no-push >> "$tmp/run4.log" 2>&1; rc=$?; set -e
cat "$tmp/run4.log" >> "$tmp/run2.log"
! alive "$old" || fail "the session of the killed loop still runs after the next run started"
[ "$rc" = 0 ] && grep -q "ending the session of an earlier loop" "$tmp/run4.log" || fail "the run after a killed loop exited $rc or did not end the old session"
echo "  killed loop: the next run ended its session, then delivered; another process with claude in its command line stayed alive"
echo "SELFTEST PASS ($(grep -c '^[0-9:]* leg ' "$tmp/run.log") legs: design, plan, work, verify, stop, new clone, audit gap, fix, audit, deliver)"
