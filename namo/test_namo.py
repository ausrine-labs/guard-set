#!/usr/bin/env python3
"""Prove namo by running the failure on purpose.

Every test builds a real git repository, puts it into the exact state a
shift ends in, and checks that namo refuses or allows. The refusals are
the product, so they are what is tested hardest: a guard that has never
been watched saying no is a guard nobody should trust.

    python3 test_namo.py            # 18 checks, no network, no fixtures
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
NAMO = os.path.join(HERE, "namo.py")

PASS = FAIL = 0
FAILURES = []


def run(cmd, cwd, env=None):
    e = dict(os.environ)
    e.update(env or {})
    e.setdefault("GIT_AUTHOR_NAME", "t"); e.setdefault("GIT_AUTHOR_EMAIL", "t@t")
    e.setdefault("GIT_COMMITTER_NAME", "t"); e.setdefault("GIT_COMMITTER_EMAIL", "t@t")
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, env=e)
    return p.returncode, p.stdout, p.stderr


def namo(args, cwd, **kw):
    return run("python3 %s %s" % (NAMO, args), cwd, **kw)


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok    %s" % name)
    else:
        FAIL += 1
        FAILURES.append(name)
        print("  FAIL  %s   %s" % (name, detail))


def new_repo(with_remote=False):
    """A repo on `dawn` with one commit, optionally with a real origin."""
    d = tempfile.mkdtemp(prefix="namo-t-")
    run("git init -q -b dawn .", d)
    run("git config user.email t@t && git config user.name t", d)
    open(os.path.join(d, "seed.txt"), "w").write("seed\n")
    run("git add seed.txt && git commit -q -m seed", d)
    if with_remote:
        bare = tempfile.mkdtemp(prefix="namo-r-")
        run("git init -q --bare .", bare)
        run("git remote add origin %s" % bare, d)
        run("git push -q -u origin dawn", d)
        return d, bare
    return d, None


def work_on_branch(d, name="shift/x", fname="thing.py"):
    run("git checkout -q -b %s" % name, d)
    open(os.path.join(d, fname), "w").write("# a real artifact\n")
    run("git add %s && git commit -q -m 'made a thing'" % fname, d)


# ── guard one: stranded work ─────────────────────────────────────────
print("\nguard one — is the work home?")

d, bare = new_repo(with_remote=True)
work_on_branch(d)
code, out, _ = namo("check --shift build --json", d)
j = json.loads(out)
ok("commits only in this tree are STRANDED", j["landing"]["state"] == "STRANDED", out[:200])
ok("stranded refuses the ending (exit 1)", code == 1, "exit %d" % code)
ok("it names the commit it is worried about",
   any("made a thing" in c["subject"] for c in j["landing"]["commits"]), out[:200])

run("git push -q -u origin shift/x", d)
code, out, _ = namo("check --shift build --json", d)
j = json.loads(out)
ok("pushed but unmerged is PENDING, not stranded", j["landing"]["state"] == "pending", out[:200])

run("git checkout -q dawn && git merge -q --no-ff -m merge shift/x && git push -q origin dawn", d)
code, out, _ = namo("check --shift build --json", d)
j = json.loads(out)
ok("merged to origin/dawn is HOME", j["landing"]["state"] == "home", out[:200])
shutil.rmtree(d, ignore_errors=True); shutil.rmtree(bare, ignore_errors=True)

# the two-dawns bug: a local trunk ahead of its remote LOOKS landed
d, bare = new_repo(with_remote=True)
open(os.path.join(d, "note.md"), "w").write("local only\n")
run("git add note.md && git commit -q -m 'local trunk only'", d)
code, out, _ = namo("check --shift build --json", d)
j = json.loads(out)
ok("a local trunk ahead of origin is STRANDED, not home",
   j["landing"]["state"] == "STRANDED", out[:200])
shutil.rmtree(d, ignore_errors=True); shutil.rmtree(bare, ignore_errors=True)

# no remote at all: fall back to the local trunk so the tool still works
d, _ = new_repo(with_remote=False)
work_on_branch(d)
code, out, _ = namo("check --shift build --json", d)
ok("with no remote, an unmerged branch is still STRANDED",
   json.loads(out)["landing"]["state"] == "STRANDED", out[:200])
run("git checkout -q dawn && git merge -q --no-ff -m merge shift/x", d)
code, out, _ = namo("check --shift build --json", d)
ok("with no remote, merged into local trunk is HOME",
   json.loads(out)["landing"]["state"] == "home", out[:200])
shutil.rmtree(d, ignore_errors=True)


# ── guard two: the one line ──────────────────────────────────────────
print("\nguard two — does one line say what now exists?")

d, _ = new_repo(with_remote=False)
work_on_branch(d)
run("git checkout -q dawn && git merge -q --no-ff -m merge shift/x", d)

code, out, _ = namo("check --shift build", d)
ok("home but unrecorded still refuses", code == 1, out[-200:])

code, out, err = namo('made "all tests pass" --path thing.py --shift build', d)
ok("a status report is refused", code != 0 and "status report" in err, err[:160])

code, out, err = namo('made "landed PR #14" --path thing.py --shift build', d)
ok("a PR number is refused", code != 0, err[:160])

code, out, err = namo('made "a parser" --path nope/missing.py --shift build', d)
ok("a path that does not exist is refused", code != 0 and "no such path" in err, err[:160])

code, out, err = namo('made "a parser" --shift build', d)
ok("an entry pointing at nothing is refused", code != 0, err[:160])

code, out, err = namo('made "thing — the thing that does the thing" --path thing.py --shift build', d)
ok("a real artifact is accepted", code == 0, err[:160])
made = open(os.path.join(d, "MADE.md")).read()
ok("MADE.md names the thing and where it is",
   "thing — the thing that does the thing" in made and "thing.py" in made, made[:200])

code, out, err = namo('made "another" --path thing.py --shift build', d)
ok("a second line for the same shift is refused", code != 0, err[:160])
code, out, err = namo('made "another" --path thing.py --shift build --again', d)
ok("--again allows a deliberate second line", code == 0, err[:160])

code, out, _ = namo("check --shift build", d)
ok("home and recorded finally passes (exit 0)", code == 0, out[-200:])

code, out, _ = namo("check --shift reflect", d)
ok("a different shift is still unrecorded", code == 1, out[-200:])

code, out, err = namo('made-nothing --why "the dead end was the finding" --shift reflect', d)
ok("made-nothing is accepted", code == 0, err[:160])
ok("an empty night is visible as one",
   "made nothing" in open(os.path.join(d, "MADE.md")).read(), "")
code, out, _ = namo("check --shift reflect", d)
ok("made-nothing satisfies the guard", code == 0, out[-200:])

code, out, err = namo('made "later thing" --path thing.py --shift build --date 2026-09-02', d)
made = open(os.path.join(d, "MADE.md")).read()
i_new, i_old = made.find("## 2026-09-02"), made.find("## %s" % __import__("time").strftime("%Y-%m-%d"))
ok("a newer day is written above the older one", 0 <= i_new < i_old, made[:240])

shutil.rmtree(d, ignore_errors=True)

# ── a 404 is not always an absence ───────────────────────────────────
# Reported by the reflect shift, 2026-09-01: namo rejected every artifact
# landing in a PRIVATE repo, because GitHub answers 404 — not 403 — to an
# unauthenticated request for a private resource. The only way past was
# --no-net, which checks nothing and used to report True. A guard that can
# only be satisfied by switching it off is one that gets switched off.
sys.path.insert(0, HERE)
import namo as nm
import urllib.error, urllib.request

_real = urllib.request.urlopen
def _raise(exc):
    def f(*_a, **_k):
        raise exc
    return f

try:
    urllib.request.urlopen = _raise(
        urllib.error.HTTPError("https://github.com/x/y", 404, "Not Found", {}, None))
    ok("a github 404 is unverified, not absent",
       nm.check_artifact(None, "https://github.com/x/y", ".")[0] is nm.UNVERIFIED, "")
    ok("a 404 anywhere else is still an absence",
       nm.check_artifact(None, "https://example.com/x", ".")[0] is False, "")

    urllib.request.urlopen = _raise(urllib.error.URLError("proxy refused"))
    ok("a request that never arrived is unverified",
       nm.check_artifact(None, "https://example.com/x", ".")[0] is nm.UNVERIFIED, "")

    urllib.request.urlopen = _raise(
        urllib.error.HTTPError("https://example.com/x", 500, "Server Error", {}, None))
    ok("a server error is still an absence",
       nm.check_artifact(None, "https://example.com/x", ".")[0] is False, "")
finally:
    urllib.request.urlopen = _real

ok("--no-net no longer claims the link was checked",
   nm.check_artifact(None, "https://github.com/x/y", ".", allow_net=False)[0] is nm.UNVERIFIED, "")
ok("a real path is still plainly true",
   nm.check_artifact("namo.py", None, HERE)[0] is True, "")
ok("a missing path is still plainly false",
   nm.check_artifact("nope.py", None, HERE)[0] is False, "")


print("\n%d passed, %d failed" % (PASS, FAIL))
if FAILURES:
    print("failed: " + ", ".join(FAILURES))
sys.exit(1 if FAIL else 0)
