#!/usr/bin/env python3
"""Tests for sargas. The product is a claim about absence, so the tests
are mostly about not crying wolf and not sleeping through a real silence.

    python3 test_sargas.py
"""
import os, shutil, subprocess, sys, tempfile, time

import sargas as s

FAIL = []


def check(name, got, want):
    if got == want:
        print("  ok    %s" % name)
    else:
        print(" FAIL   %s\n          got  %r\n          want %r" % (name, got, want))
        FAIL.append(name)


print("\n-- ages parse the way a person writes them --")
check("seconds", s.age("90s"), 90)
check("minutes", s.age("45m"), 2700)
check("hours", s.age("6h"), 21600)
check("days", s.age("3d"), 259200)
try:
    s.age("6 hours"); check("bad age rejected", "accepted", "rejected")
except ValueError:
    check("bad age rejected", "rejected", "rejected")

print("\n-- freshness --")
tmp = tempfile.mkdtemp()
new = os.path.join(tmp, "fresh.md"); open(new, "w").write("x")
check("recent file is ok", s.check_fresh("fresh.md", 3600, tmp)[0], s.OK)
os.utime(new, (time.time() - 7200, time.time() - 7200))
check("stale file is overdue", s.check_fresh("fresh.md", 3600, tmp)[0], s.LATE)
check("missing is unknown, not ok",
      s.check_fresh("never-written.md", 3600, tmp)[0], s.UNKNOWN)

print("\n-- a routine that never finished is overdue, not unknown --")
check("no stamp = overdue", s.check_ran("nope", 3600, tmp)[0], s.LATE)
st = os.path.join(tmp, "done"); open(st, "w").write("x")
check("fresh stamp = ok", s.check_ran("done", 3600, tmp)[0], s.OK)
os.utime(st, (time.time() - 7200, time.time() - 7200))
check("old stamp = overdue", s.check_ran("done", 3600, tmp)[0], s.LATE)

print("\n-- unpushed work --")
r = os.path.join(tmp, "repo"); os.makedirs(r)
run = lambda c: subprocess.run(c, shell=True, cwd=r, capture_output=True)
run("git init -q -b main && git config user.email a@b && git config user.name a")
open(os.path.join(r, "f"), "w").write("1")
run("git add -A && git commit -qm one")
check("no such branch is unknown", s.check_pushed("nope", 3600, r)[0], s.UNKNOWN)
check("commit with no remote, inside window", s.check_pushed("main", 86400, r)[0], s.OK)
check("commit with no remote, past window", s.check_pushed("main", 0, r)[0], s.LATE)

print("\n-- the watchfile --")
wf = os.path.join(tmp, ".sargas")
open(wf, "w").write("# a comment\n\nfresh  a.md  6h  label here\npushed main 1d\n")
rules = s.parse(wf)
check("comments and blanks skipped", len(rules), 2)
check("label kept", rules[0]["label"], "label here")
check("label defaults to target", rules[1]["label"], "main")
for bad, why in [("bogus x 1h", "unknown check"), ("fresh a.md", "too few fields"),
                 ("fresh a.md 6 hours", "bad age")]:
    open(wf, "w").write(bad + "\n")
    try:
        s.parse(wf); check(why + " rejected", "accepted", "rejected")
    except ValueError:
        check(why + " rejected", "rejected", "rejected")

shutil.rmtree(tmp, ignore_errors=True)
print()
if FAIL:
    print("%d test(s) failed: %s" % (len(FAIL), ", ".join(FAIL))); sys.exit(1)
print("all tests pass")
