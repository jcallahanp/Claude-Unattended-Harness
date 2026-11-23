#!/usr/bin/env python3
"""The spec is spec/requirements.json. This script checks it, renders it for reading, and records approval.

  spec.py check      list every problem that would make the build guess (exit 1 if any)
  spec.py render     write spec/REQUIREMENTS.md from the JSON (the JSON is the only source of truth)
  spec.py approve    check + render + write .env.example + record the approval (hash) + commit. Run it on main; once a run branch exists, on the run branch build/<slug>.
  spec.py secrets    say where each secret named in the spec is kept (names only, never a value): `.env`, an
                     environment variable, or a network secret recorded in spec/SETUP.md (exit 1 if a required one is in none)
  spec.py setup env=cloud|terminal boundary=cloud|vm|container|none sudo=yes|no [network=NAME@host,NAME@host] [wakeup=hourly|none]
                     write spec/SETUP.md, allow or deny `sudo` for the run sessions in harness/run-settings.json,
                     and commit. Refuses `sudo=yes` without a boundary, and a required secret that is in none of
                     the three places. `network=` records the network secrets the user confirmed (cloud only);
                     without it, a cloud setup keeps the ones spec/SETUP.md already records. `wakeup=` records the
                     scheduled task that continues a cloud run (cloud only); without it, setup keeps the recorded one (none if nothing is recorded).
                     Run it on main, after approval.
  spec.py guard block=ENTRY text=WORDS | check=checks/guards/NAME.sh text=WORDS | note text=WORDS
                     record one guard of the user, in their words: a block adds ENTRY (such as `Bash(curl:*)`) to the
                     deny list of harness/run-settings.json; a check records a script that checks/run_gates.sh runs (it
                     must pass on the current tree); a note records "not enforced by a script". Writes the "Guards"
                     section of spec/SETUP.md (setup keeps it) and commits. The same words again replace their line.
                     Run it on main, after approval.

The build loop refuses to run unless spec/APPROVED matches the current JSON, so a spec edited after
approval has to be approved again.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECJ = ROOT / "spec" / "requirements.json"
EARS = re.compile(r"^(When|While|If|Where)\b.+\bshall\b.+|^The (system|[a-z][a-z -]{2,40}) shall\b.+", re.S)
STORY = re.compile(r"^As an? .+, I want .+ so that .+", re.S | re.I)
PERSONA = re.compile(r"^I am \S.*", re.S)
# Words that cannot be tested. A requirement that needs one needs a number or an example instead.
VAGUE = ["fast", "quick", "quickly", "easy", "easily", "simple", "user-friendly", "intuitive", "robust", "seamless", "seamlessly",
         "appropriate", "appropriately", "properly", "as needed", "etc", "and so on", "nice", "modern", "scalable", "efficient",
         "efficiently", "flexible", "reasonable", "good", "clean", "smooth", "minimal", "some", "various", "should"]


def load():
    try:
        return json.loads(SPECJ.read_text())
    except OSError:
        sys.exit("spec/requirements.json does not exist yet: run /intake in `claude` first")
    except ValueError as e:
        sys.exit(f"spec/requirements.json is not valid JSON: {e}")


def problems(s):
    out = []
    for key, kind in (("project", str), ("summary", str), ("users", list), ("personas", list), ("non_goals", list),
                      ("constraints", list), ("assumptions", list), ("deploy", dict), ("secrets", list), ("human_gates", list),
                      ("open_questions", list), ("requirements", list)):
        if not isinstance(s.get(key), kind):
            out.append(f"`{key}` is missing or is not a {kind.__name__}")
    if out:
        return out
    if len(s["summary"]) < 80:
        out.append("`summary` must say in a few sentences what is being built, for whom and why")
    if not s["users"]:
        out.append("`users`: name at least one kind of user")
    if not s["personas"]:
        out.append("`personas`: write at least one persona (ask the user to describe one real user)")
    for x in s["personas"]:
        if not PERSONA.match(str(x)):
            out.append(f"persona {str(x)[:40]!r}: must start with \"I am\", for example \"I am <role> using <system>. "
                       "I do <action>. I trust it to <outcome>. I never have to <friction removed>.\"")
    if not s["non_goals"]:
        out.append("`non_goals`: name at least one thing this project will not do (ask the user for anti-goals)")
    if s["open_questions"]:
        out.append(f"{len(s['open_questions'])} open question(s) left: ask the user, or record their decision to drop them")
    if not str(s["deploy"].get("target", "")).strip() or not s["deploy"].get("ready_when"):
        out.append("`deploy` needs a `target` and at least one observable `ready_when` statement")
    for x in s["secrets"]:
        if not isinstance(x, dict) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", str(x.get("name", ""))) or not x.get("purpose"):
            out.append(f"secret {x!r}: needs a NAME_LIKE_THIS and a purpose (never the value)")
        elif any(k not in ("name", "purpose", "required") for k in x):
            out.append(f"secret {x['name']}: only name, purpose, required are allowed (never a value)")
    if not s["requirements"]:
        out.append("no requirements yet")
    seen = set()
    for n, r in enumerate(s["requirements"], 1):
        rid = str(r.get("id", f"#{n}"))
        if not re.fullmatch(r"R[0-9]{1,3}", rid):
            out.append(f"{rid}: id must look like R1, R2, …")
        if rid in seen:
            out.append(f"{rid}: duplicate id")
        seen.add(rid)
        if r.get("priority") not in ("must", "should"):
            out.append(f"{rid}: priority must be `must` or `should` (a `could` or `won't` belongs in non_goals)")
        if not STORY.match(str(r.get("story", ""))):
            out.append(f"{rid}: story must read \"As a <user>, I want <capability> so that <outcome>.\"")
        st = str(r.get("statement", ""))
        if not EARS.match(st):
            out.append(f"{rid}: statement must be one EARS sentence: \"When/While/If/Where <condition>, the system shall <response>.\" "
                       "or \"The system shall <response>.\"")
        ex = r.get("examples")
        if not isinstance(ex, list) or not ex or any(not isinstance(e, dict) or any(len(str(e.get(k, ""))) < 4 for k in ("given", "when", "then")) for e in ex):
            out.append(f"{rid}: needs at least one concrete example with given / when / then (real values, not placeholders)")
            ex = []
        if len(str(r.get("verify", ""))) < 20:
            out.append(f"{rid}: `verify` must say how a script can prove it (what it runs, what it observes)")
        text = " ".join([st] + [str(e.get("then", "")) for e in ex]).lower()
        hits = [w for w in VAGUE if re.search(rf"(?<![a-z-]){re.escape(w)}(?![a-z-])", text)]
        if hits:
            out.append(f"{rid}: untestable wording ({', '.join(hits)}): replace with a number, a limit or an example")
    return out


def render(s):
    L = [f"# Requirements: {s['project']}", "",
         "Generated from `spec/requirements.json` by `python3 harness/spec.py render`. Edit the JSON, not this file.", "",
         "## What and why", "", s["summary"], "", "## Users", ""] + [f"- {u}" for u in s["users"]]
    L += ["", "## Personas", ""] + [f"- {p}" for p in s["personas"]]
    L += ["", "## Requirements", ""]
    for r in s["requirements"]:
        L += [f"### {r['id']} ({r['priority']})", "", f"**Story:** {r['story']}", "", f"**Requirement:** {r['statement']}", "", "**Examples:**", ""]
        L += [f"- Given {e['given']}, when {e['when']}, then {e['then']}" for e in r["examples"]]
        L += ["", f"**How it is proven:** {r['verify']}", ""]
    for title, key in (("Will not do", "non_goals"), ("Constraints", "constraints"), ("Assumptions", "assumptions"),
                       ("Only the user does these", "human_gates")):
        L += [f"## {title}", ""] + ([f"- {x}" for x in s[key]] or ["- none"]) + [""]
    L += ["## Deploy", "", f"Target: {s['deploy']['target']}", "", "Ready to deploy when:", ""] + [f"- {x}" for x in s["deploy"]["ready_when"]]
    L += ["", "## Secrets (names only; where each is kept: `spec/SETUP.md`)", ""]
    L += [f"- `{x['name']}`{'' if x.get('required', True) else ' (optional)'}: {x['purpose']}" for x in s["secrets"]] or ["- none"]
    return "\n".join(L) + "\n"


def network_secrets():
    """The network secrets the user confirmed, as spec/SETUP.md records them: {NAME: host}. A script cannot see their values."""
    f = ROOT / "spec" / "SETUP.md"
    return dict(re.findall(r"^- `([A-Z][A-Z0-9_]+)`: a network secret for `([^`]+)`", f.read_text(), re.M)) if f.is_file() else {}


def secret_places(s, net):
    """Where each secret of the spec is kept (None: nowhere), and the required and optional names that are nowhere.
    `.env` and environment variables are read the way the loop reads them for the run (loop.child_env)."""
    sys.path.insert(0, str(ROOT / "harness"))
    from loop import child_env
    env = child_env()
    places = {}
    for x in s.get("secrets", []):
        n, v = x["name"], env.get(x["name"])
        places[n] = ("`.env`" if v and v != os.environ.get(n) else "an environment variable" if v
                     else f"a network secret for `{net[n]}`" if n in net else None)
    missing = [x["name"] for x in s.get("secrets", []) if places[x["name"]] is None and x.get("required", True)]
    optional = [x["name"] for x in s.get("secrets", []) if places[x["name"]] is None and not x.get("required", True)]
    return places, missing, optional


MISSING = "missing (not in .env, not in the environment, not a network secret recorded in spec/SETUP.md): "


ENVS = {"cloud": "A Claude Code cloud session.", "terminal": "A Linux terminal."}
BOUNDARIES = {"cloud": "The cloud session. The run happens in a disposable cloud machine that holds only this project.",
              "vm": "A virtual machine that holds only this project.",
              "container": "A container that holds only this project.",
              "none": "None. The run sessions act as your user on this machine and can reach what your user can reach."}
SUDO_DENY = '"Bash(sudo:*)", '
WAKEUPS = {"hourly": "An hourly scheduled task named `build wake-up` fires into the master session and continues the run. "
                     "It is removed at DELIVERED and at a stop that needs the user.",
           "terminal": "None (terminal): the run in tmux needs no wake-up.",
           "none": "None: no scheduled task continues the run. The run goes on while its background command runs; if the "
                   "machine is replaced or the command's time ends, the run waits until the user sends the master session a message."}


def on_main_approved(cmd, approved=True):
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=ROOT, text=True, capture_output=True).stdout.strip()
    if cmd == "approve" and branch not in ("main", "master") and not branch.startswith("build/"):
        sys.exit("refused: approve runs on main, or on the run branch build/<slug> once one exists "
                 "(python3 harness/loop.py status switches to the run branch when one exists)")
    if cmd != "approve" and branch not in ("main", "master"):
        sys.exit(f"refused: {cmd} runs on main, before the run (switch to main first)")
    if not approved:
        return
    sys.path.insert(0, str(ROOT / "harness"))
    from loop import spec_problem
    problem = spec_problem()
    if problem:
        sys.exit(problem)


GUARDS_HEAD = ["## Guards", "", "Constraints of the user, in their words, and where each is enforced. A block is a deny entry in "
               "`harness/run-settings.json`: a tripwire that stops the usual command, not each possible way. A check is a "
               "script in `checks/guards/` that `checks/run_gates.sh` runs.", ""]


def guard_lines():
    """The guard lines spec/SETUP.md records: ["- words — where", ...]."""
    f = ROOT / "spec" / "SETUP.md"
    text = f.read_text() if f.is_file() else ""
    part = text.split("\n## Guards\n", 1)[1] if "\n## Guards\n" in text else ""
    return [x for x in part.splitlines() if x.startswith("- ")]


def guard(args):
    """Record one guard: a block (deny entry), a check (script in checks/guards/) or a note (not enforced by a script)."""
    a = dict(x.split("=", 1) for x in args if "=" in x)
    words = " ".join(a.get("text", "").split())
    kinds = [k for k in ("block", "check") if k in a] + (["note"] if "note" in args else [])
    if not words or len(kinds) != 1:
        sys.exit("usage: spec.py guard block=ENTRY text=WORDS | check=checks/guards/NAME.sh text=WORDS | note text=WORDS")
    on_main_approved("guard")
    paths = ["spec/SETUP.md"]
    if kinds == ["block"]:
        entry = a["block"]
        if not re.fullmatch(r"[A-Z][A-Za-z]*(\(\S[^()]*\))?|mcp__[A-Za-z0-9_-]+", entry):
            sys.exit("a block is one deny entry for a command or a tool, such as Bash(curl:*), WebSearch or mcp__server")
        where = f"block: the deny entry `{entry}` in `harness/run-settings.json`"
    elif kinds == ["check"]:
        script = a["check"]
        if not re.fullmatch(r"checks/guards/[A-Za-z0-9_.-]+\.sh", script) or not (ROOT / script).is_file():
            sys.exit("a check is an existing script checks/guards/NAME.sh")
        where = f"check: `{script}`, run by `checks/run_gates.sh`"
    else:
        where = "not enforced by a script"
    line, lines = f"- {words} — {where}", guard_lines()
    old = [x for x in lines if x.rpartition(" — ")[0] == "- " + words]
    if old and old[0] != line:
        sys.exit(f"refused: these words are recorded already with another form or target: {old[0]}")
    if kinds == ["block"]:
        rs = ROOT / "harness" / "run-settings.json"
        text = rs.read_text()
        if entry not in json.loads(text)["permissions"]["deny"]:
            text = re.sub(r'("deny": \[\s*)', lambda m: m.group(1) + json.dumps(entry) + ", ", text, count=1)
            try:
                ok = entry in json.loads(text)["permissions"]["deny"]
            except ValueError:
                ok = False
            if not ok:
                sys.exit("could not add the block to harness/run-settings.json: edit its deny list by hand")
            rs.write_text(text)
        paths.append("harness/run-settings.json")
    elif kinds == ["check"]:
        r = subprocess.run(["bash", script], cwd=ROOT, text=True, capture_output=True)
        if r.returncode != 0:
            sys.exit(f"refused: {script} fails on the current tree; fix the script or the tree first:\n{r.stdout}{r.stderr}")
        paths.append(script)
    if not old:
        lines.append(line)
    f = ROOT / "spec" / "SETUP.md"
    text = f.read_text() if f.is_file() else "# Setup\n"
    text = text.split("\n## Guards\n", 1)[0].rstrip("\n") + "\n\n" + "\n".join(GUARDS_HEAD + lines) + "\n"
    f.write_text(text)
    subprocess.run(["git", "add", "--", *paths], cwd=ROOT, check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet", "--", *paths], cwd=ROOT).returncode == 0:
        print(f"guard unchanged: {where}")
        return
    subprocess.run(["git", "commit", "-q", "-m", f"guard: {words[:60]}"], cwd=ROOT, check=False)
    print(f"recorded guard: {where}")


def setup(args):
    """Write spec/SETUP.md and apply the `sudo` choice to harness/run-settings.json. Commits both."""
    a = dict(x.split("=", 1) for x in args if "=" in x)
    env, boundary, sudo = a.get("env"), a.get("boundary"), a.get("sudo")
    items = [x for x in a.get("network", "").split(",") if x]
    wakeup = a.get("wakeup")
    if env not in ENVS or boundary not in BOUNDARIES or sudo not in ("yes", "no") or wakeup not in (None, "hourly", "none") or any(
            not re.fullmatch(r"[A-Z][A-Z0-9_]{2,}@(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+", x) for x in items):
        sys.exit("usage: spec.py setup env=cloud|terminal boundary=cloud|vm|container|none sudo=yes|no [network=NAME@host,NAME@host] [wakeup=hourly|none]")
    if items and env != "cloud":
        sys.exit("refused: network secrets exist only in a cloud environment; in a terminal use .env")
    if wakeup == "hourly" and env != "cloud":
        sys.exit("refused: a wake-up task is for a cloud session; in a terminal the run in tmux needs none")
    if (env == "cloud") != (boundary == "cloud"):
        sys.exit("a cloud session has the boundary `cloud`; a Linux terminal has `vm`, `container` or `none`")
    if sudo == "yes" and boundary == "none":
        sys.exit("refused: `sudo` is allowed only inside a boundary (a cloud session, a VM or a container)")
    on_main_approved("setup")
    net = dict(x.split("@", 1) for x in items) if "network" in a else network_secrets() if env == "cloud" else {}
    s = load()
    unknown = sorted(set(net) - {x["name"] for x in s["secrets"]})
    if unknown:
        sys.exit("refused: not a secret of the spec: " + ", ".join(unknown))
    places, missing, _ = secret_places(s, net)
    if missing:
        sys.exit(MISSING + ", ".join(missing))
    old = (ROOT / "spec" / "SETUP.md").read_text() if (ROOT / "spec" / "SETUP.md").is_file() else ""
    kept = old.split("\n## Wake-up\n\n", 1)[1].split("\n", 1)[0] if "\n## Wake-up\n\n" in old else ""
    wake = (WAKEUPS["terminal"] if env == "terminal" else WAKEUPS[wakeup] if wakeup
            else kept if kept and kept != WAKEUPS["terminal"] else WAKEUPS["none"])
    rs = ROOT / "harness" / "run-settings.json"
    text = rs.read_text()
    if sudo == "yes":
        text = text.replace(SUDO_DENY, "")
    elif SUDO_DENY not in text:
        text = text.replace('"Bash(su:*)"', SUDO_DENY + '"Bash(su:*)"', 1)
    if ("Bash(sudo:*)" in json.loads(text)["permissions"]["deny"]) != (sudo == "no"):
        sys.exit("could not apply the sudo choice to harness/run-settings.json: edit its deny list by hand")
    rs.write_text(text)
    sudo_text = ("Allowed. The run sessions can use `sudo` (root inside the boundary): install system packages, change any "
                 "file of the machine. `Bash(sudo:*)` is not in the deny list of `harness/run-settings.json`."
                 if sudo == "yes" else
                 "Denied (the default). `Bash(sudo:*)` is in the deny list of `harness/run-settings.json`.")
    if boundary == "cloud" and sudo == "no":
        sudo_text += " In a cloud session the run is already administrator of that machine: this entry stops only the `sudo` command."
    L = ["# Setup", "", "Written by `python3 harness/spec.py setup`. Run it again to change a choice.", "",
         "## Environment", "", ENVS[env], "", "## Outer boundary", "", BOUNDARIES[boundary], "", "## sudo", "", sudo_text, "",
         "## Wake-up", "", wake, "",
         "## Secrets", "", "Where each secret of the spec is kept. Names only; no value is ever written here.", ""]
    L += [f"- `{x['name']}`: {places[x['name']] or 'not set'}{'' if x.get('required', True) else ' (optional)'}" for x in s["secrets"]] or ["- none"]
    L += [""]
    guards = guard_lines()
    if guards:
        L += GUARDS_HEAD + guards + [""]
    (ROOT / "spec" / "SETUP.md").write_text("\n".join(L))
    subprocess.run(["git", "add", "spec/SETUP.md", "harness/run-settings.json"], cwd=ROOT, check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet", "--", "spec/SETUP.md", "harness/run-settings.json"], cwd=ROOT).returncode == 0:
        print(f"setup unchanged: environment {env}, boundary {boundary}, sudo {'allowed' if sudo == 'yes' else 'denied'}")
        return
    subprocess.run(["git", "commit", "-q", "-m", f"setup: {env}, boundary {boundary}, sudo {sudo}"], cwd=ROOT, check=False)
    print(f"wrote spec/SETUP.md: environment {env}, boundary {boundary}, sudo {'allowed' if sudo == 'yes' else 'denied'}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("check", "render", "approve", "secrets", "setup", "guard"):
        sys.exit(__doc__)
    s = load()
    if cmd == "setup":
        return setup(sys.argv[2:])
    if cmd == "guard":
        return guard(sys.argv[2:])
    probs = problems(s)
    if cmd == "secrets":
        places, missing, optional = secret_places(s, network_secrets())
        found = "; ".join(f"{n} in {p}" for n, p in places.items() if p)
        need = sum(x.get("required", True) for x in s.get("secrets", []))
        print(MISSING + ", ".join(missing) if missing else
              f"secrets: all {need} required present" + (f" ({found})" if found else ""))
        if optional:
            print("optional secrets not set: " + ", ".join(optional))
        sys.exit(1 if missing else 0)
    if probs:
        print(f"{len(probs)} problem(s) in spec/requirements.json:")
        print("\n".join(f"- {p}" for p in probs))
        sys.exit(1)
    if cmd == "check":
        print(f"spec ok: {len(s['requirements'])} requirements, {len(s['secrets'])} secrets, no open questions")
        return
    if cmd == "approve":
        on_main_approved("approve", approved=False)
    (ROOT / "spec" / "REQUIREMENTS.md").write_text(render(s))
    if cmd == "render":
        print("wrote spec/REQUIREMENTS.md")
        return
    lines = ["# Secrets this project needs. Copy to .env and fill in the values. .env is never committed.", ""]
    for x in s["secrets"]:
        lines += [f"# {x['purpose']}" + ("" if x.get("required", True) else " (optional)"), f"{x['name']}=", ""]
    (ROOT / ".env.example").write_text("\n".join(lines))
    (ROOT / "spec" / "APPROVED").write_text(json.dumps({
        "sha256": hashlib.sha256(SPECJ.read_bytes()).hexdigest(), "approved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requirements": len(s["requirements"])}, indent=1) + "\n")
    subprocess.run(["git", "add", "spec", ".env.example"], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"spec: approved ({len(s['requirements'])} requirements)"], cwd=ROOT, check=False)
    print(f"approved: {len(s['requirements'])} requirements. Next: the setup step of /intake, then `bash harness/run.sh`.")


if __name__ == "__main__":
    main()
