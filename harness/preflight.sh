#!/usr/bin/env bash
# Everything that would stop an unattended run, checked before it starts. Exit 0 = good to go.
# Run from anywhere: bash harness/preflight.sh        (bash harness/preflight.sh tools: only the tool check)
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
rc=0; warn=0
ok()   { echo "  ok    $1"; }
bad()  { echo "  FAIL  $1"; rc=1; }
note() { echo "  warn  $1"; warn=1; }
CLAUDE="${HARNESS_CLAUDE:-claude}"

echo "== tools =="
for t in git python3 jq bash "$CLAUDE"; do command -v "$t" >/dev/null 2>&1 && ok "$t" || bad "$t is not installed or not on PATH"; done
command -v tmux >/dev/null 2>&1 && ok "tmux (run survives a closed terminal)" || note "tmux missing: harness/run.sh will run in the foreground"
command -v gh >/dev/null 2>&1 && ok "gh (pull request, stop reports as its comments)" || note "gh missing: the branch is pushed but you open the pull request yourself"
if [ "${1:-}" = tools ]; then (( rc )) && echo "TOOLS: FAIL" && exit 1; echo "TOOLS: PASS"; exit 0; fi

echo "== spec =="
if python3 -I harness/spec.py check >/dev/null 2>&1; then ok "spec/requirements.json passes its checks"; else bad "spec has problems: python3 harness/spec.py check"; fi
if [ -f spec/APPROVED ] && [ "$(jq -r .sha256 spec/APPROVED 2>/dev/null)" = "$(sha256sum spec/requirements.json 2>/dev/null | cut -d' ' -f1)" ]; then
  ok "spec is approved and unchanged since approval"
else bad "spec is not approved (or changed since): python3 harness/spec.py approve"; fi

echo "== secrets =="
if out=$(python3 -I harness/spec.py secrets 2>&1); then ok "$out"; else bad "$out"; fi
if git ls-files --error-unmatch .env >/dev/null 2>&1; then bad ".env is tracked by git: git rm --cached .env"; fi
git check-ignore -q .env && ok ".env is git-ignored" || bad ".env is not git-ignored: add it to .gitignore"

[ -f spec/SETUP.md ] && ok "setup done (spec/SETUP.md)" || note "no spec/SETUP.md: run the setup step of /intake"

echo "== git =="
[ -z "$(git status --porcelain)" ] && ok "working tree clean" || bad "uncommitted changes: commit or stash them"
if git remote get-url origin >/dev/null 2>&1; then ok "remote origin set ($(git remote get-url origin))"; else note "no git remote: nothing will be pushed and no pull request opened"; fi
git config user.name >/dev/null && git config user.email >/dev/null && ok "git identity set" || bad "git identity missing: git config user.name / user.email"
bash harness/install-hooks.sh >/dev/null 2>&1 && ok "pre-push hook installed" || note "could not install the pre-push hook"

echo "== gates =="
if bash checks/run_gates.sh >/dev/null 2>&1; then ok "checks/run_gates.sh passes"; else bad "gates fail before the run: bash checks/run_gates.sh"; fi

echo "== claude =="
# One real run session (same settings, guard and browser as the loop's; the verify step's model) must prove
# three facts from its own tool calls and results, not from its words alone: it loads CLAUDE.md, the guard
# blocks a forbidden command, and the browser tool opens a local page.
if [ "${HARNESS_SKIP_CLAUDE_PROBE:-0}" = 1 ]; then note "run-session probe skipped: sign-in, CLAUDE.md, guard and browser not checked"
else
  model="${HARNESS_MODEL_VERIFY-${HARNESS_MODEL-sonnet}}"; effort="${HARNESS_EFFORT_VERIFY-${HARNESS_EFFORT-medium}}"
  [ -e .playwright-mcp ] && pw_was_here=1 || pw_was_here=0       # the browser tool leaves this folder; remove only a new one
  p=$(mktemp -d); mkdir "$p/www"; token="token-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
  echo "<html><body><p>The token is $token</p></body></html>" > "$p/www/index.html"
  python3 -u -m http.server 0 --bind 127.0.0.1 --directory "$p/www" > "$p/server.log" 2>&1 & srv=$!
  for _ in $(seq 50); do port=$(sed -n 's/.* port \([0-9]*\) .*/\1/p' "$p/server.log"); [ -n "$port" ] && break; sleep 0.1; done
  prompt="This is a preflight test of the harness, not a build step. Do these three things in order, then stop.
1. Without using any tool, write the preflight code word from your project instructions (CLAUDE.md) on its own line as: CODE WORD: <word>. If you do not have one, write: CODE WORD: unknown
2. Run this Bash command once, exactly as written: git rebase --abort; touch $p/ran
   The harness guard is expected to block it. Do not retry it and do not run anything in its place.
3. Open http://127.0.0.1:$port/ with the Playwright browser tool (not Bash, not WebFetch) and write the token shown on the page on its own line as: TOKEN: <token>"
  out=$(timeout 300 "$CLAUDE" -p --settings harness/run-settings.json --strict-mcp-config --mcp-config harness/mcp.json \
        --output-format stream-json --verbose --max-turns 10 ${model:+--model "$model"} ${effort:+--effort "$effort"} "$prompt" </dev/null 2>&1)
  kill "$srv" 2>/dev/null
  q() { printf '%s\n' "$out" | grep '^{' | jq -rs "$1" 2>/dev/null; }
  if [ "$(q '[.[] | select(.type == "result")] | last | .is_error')" = false ]; then
    ok "a headless run session starts and answers (model ${model:-your Claude Code default}, effort ${effort:-your Claude Code default})"
    uses='[.[] | select(.type == "assistant") | .message.content[]? | select(.type == "tool_use")]'
    results='[.[] | select(.type == "user") | .message.content[]? | select(.type == "tool_result")]'
    reply=$(q '[.[] | select(.type == "assistant") | .message.content[]? | select(.type == "text") | .text] | join("\n")')
    if grep -q "CODE WORD: copper-heron-417" <<<"$reply" && [ "$(q "$uses | map(.input | tostring | test(\"CLAUDE\")) | any")" = false ] \
       && [ "$(q "$results | map(.content | tostring | contains(\"copper-heron-417\")) | any")" = false ]; then
      ok "run sessions load CLAUDE.md (the session knew its code word without reading or finding it with a tool)"
    else bad "a run session does not know the code word in CLAUDE.md: run sessions do not load the project rules"; fi
    if [ "$(q "$results | map(select(.is_error == true and (.content | tostring | contains(\"BLOCKED by harness/guard.sh\")))) | length")" -gt 0 ] 2>/dev/null && [ ! -e "$p/ran" ]; then
      ok "harness/guard.sh blocks a forbidden command in a run session"
    else bad "harness/guard.sh did not block 'git rebase' in a run session: check the PreToolUse hook in harness/run-settings.json"; fi
    if grep -q "TOKEN: $token" <<<"$reply" && [ "$(q "($uses | map(select(.name | startswith(\"mcp__playwright__\")) | .id)) as \$ids
         | $results | map(select((.tool_use_id as \$i | \$ids | index(\$i)) and (.content | tostring | contains(\"$token\")))) | length")" -gt 0 ] 2>/dev/null; then
      ok "the browser tool (Playwright MCP in harness/mcp.json) opens a local page"
    else bad "the browser tool did not open a local page: check harness/mcp.json and that npx can start @playwright/mcp and its browser"; fi
  else bad "a headless run session failed: $( (q '[.[] | select(.type == "result")] | last | .result // empty'; grep -v '^{' <<<"$out") | tail -c 300)"; fi
  rm -rf "$p"; (( pw_was_here )) || rm -rf .playwright-mcp
fi

echo "== machine =="
if command -v busctl >/dev/null 2>&1; then
  lid=$(busctl get-property org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager HandleLidSwitch 2>/dev/null | cut -d'"' -f2)
  [ "$lid" = "suspend" ] && note "closing the laptop lid suspends the machine and freezes the run" || ok "lid close does not suspend (${lid:-n/a})"
fi
free_gb=$(df -BG --output=avail . | tail -1 | tr -dc 0-9); [ "${free_gb:-0}" -ge 5 ] && ok "${free_gb} GB free disk" || note "only ${free_gb} GB free disk"

echo
if (( rc )); then echo "PREFLIGHT: FAIL (fix the FAIL lines, then run this again)"; exit 1; fi
(( warn )) && echo "PREFLIGHT: PASS with warnings" || echo "PREFLIGHT: PASS"
