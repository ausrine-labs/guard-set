#!/usr/bin/env python3
"""Tests for claimcheck's detectors.

The detectors are the whole product: a missed lie is a broken promise, and
a false alarm is worse, because a checker that cries wolf gets switched off.
These tests hold both ends.

    python3 test_claimcheck.py
"""

import sys

import claimcheck as cc


FAILURES = []


def check(name, got, want):
    if got == want:
        print("  ok    %s" % name)
    else:
        print(" FAIL   %s\n          got  %r\n          want %r" % (name, got, want))
        FAILURES.append(name)


def kinds(text):
    """Which claim kinds survive masking and get detected."""
    masked = cc.mask_mentions(text)
    found = []
    for finder, _ in cc.DETECTORS:
        for kind, _quote, _arg in finder(masked):
            if kind not in found:
                found.append(kind)
    return sorted(found)


def branches(text):
    return [arg for _k, _q, arg in cc.find_pushes(cc.mask_mentions(text))]


print("\n-- a plain claim is detected --")
check("commit", kinds("committed as a1b2c3d"), ["commit"])
check("push named", kinds("pushed to dawn"), ["push"])
check("push backticked", branches("pushed to `claimcheck-hook`"), ["claimcheck-hook"])
check("push origin/", branches("pushed to origin/main"), ["main"])
check("clean tree", kinds("the working tree clean"), ["clean"])
check("tests", kinds("all tests pass"), ["tests"])

print("\n-- quoting a claim is not making it --")
check("quoted", kinds('it said "pushed; tree clean" and was wrong'), [])
check("curly quoted", kinds("it said “all tests pass” yesterday"), [])
check("italic example", kinds("*Committed as a1b2c3d. Pushed.*"), [])
check("italic over lines", kinds("*Committed as a1b2c3d.\nTests pass.*"), [])
check("fenced block", kinds("```\ncommitted as a1b2c3d\npushed to main\n```"), [])

print("\n-- emphasis of a real claim survives --")
check("bold", kinds("**pushed to dawn**"), ["push"])
check("bold branch", branches("**pushed to `dawn`**"), ["dawn"])
check("bullet list", kinds("* pushed to dawn\n* tests pass"), ["push", "tests"])

print("\n-- a push claim needs a branch to be a checkable claim --")
check("bare push", branches("pushed everything up"), [None])
check("stopword", branches("pushed to it"), [None])
check("semicolon", branches("pushed; tree clean"), [None])

print("\n-- a bare push claim asks the right question --")
import os, shutil, subprocess, tempfile
_tmp = tempfile.mkdtemp()
_r = os.path.join(_tmp, "work")
os.makedirs(_r)
_run = lambda c, d=None: subprocess.run(c, shell=True, cwd=d or _r, capture_output=True)
_run("git init -q -b main && git config user.email a@b && git config user.name a")
open(os.path.join(_r, "f"), "w").write("1")
_run("git add -A && git commit -qm one")
check("no remote at all = FALSE", cc.check_push(None, _r)[0], cc.FALSE)

_run("git init -q --bare " + os.path.join(_tmp, "origin.git"), _tmp)
_run("git remote add origin " + os.path.join(_tmp, "origin.git"))
_run("git push -q origin main")
check("pushed to origin = verified", cc.check_push(None, _r)[0], cc.TRUE)

# the bug this replaced: work on a SIDE branch is still safely on origin,
# even though the checked-out branch reads as ahead of its upstream.
_run("git checkout -q -b side && git commit -q --allow-empty -m two")
_run("git push -q origin side")
_run("git checkout -q main && git merge -q --ff-only side")
check("side-branch push counts as pushed", cc.check_push(None, _r)[0], cc.TRUE)
shutil.rmtree(_tmp, ignore_errors=True)

print("\n-- a commit this checkout cannot see is not a lie --")
# The bug (2026-08-31): check_commit asked `git cat-file`, which reads only the
# local object store, and a miss returned FALSE — the verdict that means *this
# agent lied*. Under a sandbox that cannot fetch, every checkout is behind, so
# an honest agent whose commit was safely on the remote got recorded as
# dishonest by our own tool. It did that to a true sentence.
# A check that cannot see must say so rather than guess.
import urllib.error
import urllib.request

_tmp = tempfile.mkdtemp()
_origin = os.path.join(_tmp, "origin.git")
_work = os.path.join(_tmp, "work")
_other = os.path.join(_tmp, "other")
_solo = os.path.join(_tmp, "solo")
_git = lambda c, d: subprocess.run(c, shell=True, cwd=d, capture_output=True, text=True)
_out = lambda c, d: _git(c, d).stdout.strip()

os.makedirs(_work)
os.makedirs(_solo)
_git("git init -q --bare " + _origin, _tmp)
_git("git init -q -b main && git config user.email a@b && git config user.name a", _work)
open(os.path.join(_work, "f"), "w").write("1")
_git("git add -A && git commit -qm one", _work)
_git("git remote add origin %s && git push -q origin main" % _origin, _work)
_first = _out("git rev-parse HEAD", _work)

# a second checkout, cloned here, which will never fetch again
_git("git clone -q %s %s" % (_origin, _other), _tmp)

# work moves on — two commits on a side branch that `other` never sees
_git("git checkout -q -b later && git commit -q --allow-empty -m two", _work)
_two = _out("git rev-parse HEAD", _work)
_git("git commit -q --allow-empty -m three && git push -q origin later", _work)
_three = _out("git rev-parse HEAD", _work)

check("commit in this checkout = verified",
      cc.check_commit(_first, _other)[0], cc.TRUE)
check("commit is a remote tip we never fetched = verified",
      cc.check_commit(_three, _other)[0], cc.TRUE)
check("commit buried in unfetched remote history = UNVERIFIABLE",
      cc.check_commit(_two, _other)[0], cc.UNVERIFIABLE)

# the test the order named: the remote exists and the checkout cannot reach it.
_git("git remote set-url origin %s" % os.path.join(_tmp, "gone.git"), _other)
check("remote unreachable = UNVERIFIABLE, not FALSE",
      cc.check_commit(_three, _other)[0], cc.UNVERIFIABLE)

# and its other half, which is what keeps the tool worth running: a checkout
# holding everything the remote holds may still call a commit false, because
# there is nowhere left for it to be.
check("reachable remote, commit nowhere = FALSE",
      cc.check_commit("deadbee", _work)[0], cc.FALSE)

_git("git init -q -b main && git config user.email a@b && git config user.name a", _solo)
_git("git commit -q --allow-empty -m only", _solo)
check("no remote at all, commit nowhere = FALSE",
      cc.check_commit("deadbee", _solo)[0], cc.FALSE)
shutil.rmtree(_tmp, ignore_errors=True)

print("\n-- a request that never arrived is not a dead link --")
# Same defect one layer up: a proxy or an offline host raised, and the URL
# claim was recorded FALSE. A server that answers 404 is evidence; a request
# that never reached a server is not.
_real_open = urllib.request.urlopen
try:
    def _blocked(*_a, **_k):
        raise urllib.error.URLError("connection denied by proxy")
    urllib.request.urlopen = _blocked
    check("blocked request = UNVERIFIABLE",
          cc.check_url("https://example.invalid/x", ".")[0], cc.UNVERIFIABLE)

    def _gone(*_a, **_k):
        raise urllib.error.HTTPError("https://example.com/x", 404, "Not Found", {}, None)
    urllib.request.urlopen = _gone
    check("server answered 404 = FALSE",
          cc.check_url("https://example.com/x", ".")[0], cc.FALSE)
finally:
    urllib.request.urlopen = _real_open


print("\n-- merged/landed are commit claims too --")
# From the [interface] shift's fix on 2026-08-31, kept: the sentence that
# started all of this ("merged as <sha>") was invisible to the detector.
check("merged as", [a for _k, _q, a in cc.find_commits("merged as abc1234")], ["abc1234"])
check("landed as", [a for _k, _q, a in cc.find_commits("landed as abc1234")], ["abc1234"])

# That shift also asserted an unknown sha is NEVER false. We diverge here, on
# purpose. Its concern — never accuse an honest agent whose commit is on a
# remote this checkout has not fetched — is fully met by the ladder above:
# unreachable remote and behind-the-remote both return UNVERIFIABLE, and a
# remote tip returns verified without fetching. FALSE now survives in exactly
# two provable places: no remote exists at all, or this checkout already holds
# everything the remote holds. A verifier that can never say FALSE about the
# most commonly fabricated claim in agent reports is not a verifier.

_bt = tempfile.mkdtemp()
_bo = os.path.join(_bt, "o.git"); _bw = os.path.join(_bt, "w"); _behind = os.path.join(_bt, "b")
os.makedirs(_bw)
subprocess.run("git init -q --bare " + _bo, shell=True, capture_output=True)
subprocess.run("git init -q -b main && git config user.email a@b && git config user.name a",
               shell=True, cwd=_bw, capture_output=True)
subprocess.run("git commit -q --allow-empty -m one", shell=True, cwd=_bw, capture_output=True)
subprocess.run("git remote add origin %s && git push -q origin main" % _bo,
               shell=True, cwd=_bw, capture_output=True)
subprocess.run("git clone -q %s %s" % (_bo, _behind), shell=True, cwd=_bt, capture_output=True)
subprocess.run("git commit -q --allow-empty -m two && git push -q origin main",
               shell=True, cwd=_bw, capture_output=True)
check("an unfetched commit is still never FALSE",
      cc.check_commit("deadbeef", _behind)[0] == cc.FALSE, False)

shutil.rmtree(_bt, ignore_errors=True)

print("\n-- a sha carries no repo name (2026-09-02) --")
# The bug: a real `ausrine-os` merge sha, quoted in the `ausrine-lab` journal,
# was called FALSE because every rung of the ladder asked the wrong repo.
# NOTE: the two repos must differ. Two empty commits with the same message,
# author and second produce the SAME sha, which silently voids this test.
_sib = tempfile.mkdtemp()
_lab = os.path.join(_sib, "lab")
_other = os.path.join(_sib, "other")
for _d, _msg in ((_lab, "lab side"), (_other, "the neighbour commit")):
    os.makedirs(_d)
    subprocess.run("git init -q -b main && git config user.email a@b "
                   "&& git config user.name a "
                   "&& git commit -q --allow-empty -m '%s'" % _msg,
                   shell=True, cwd=_d, capture_output=True)
_next_door = subprocess.run("git rev-parse HEAD", shell=True, cwd=_other,
                            capture_output=True, text=True).stdout.strip()

check("the two repos really differ (guards this test)",
      subprocess.run("git cat-file -e " + _next_door, shell=True, cwd=_lab,
                     capture_output=True).returncode != 0, True)
check("sibling_repos finds the neighbour",
      _other in cc.sibling_repos(_lab), True)
check("sibling_repos never returns the repo itself",
      _lab not in cc.sibling_repos(_lab), True)
check("a sibling's sha is never FALSE",
      cc.check_commit(_next_door, _lab)[0] == cc.FALSE, False)
check("a sibling's sha reads UNVERIFIABLE",
      cc.check_commit(_next_door, _lab)[0], cc.UNVERIFIABLE)
check("the verdict names the repo that holds it",
      "other" in cc.check_commit(_next_door, _lab)[1], True)
check("a sha nobody holds is still FALSE",
      cc.check_commit("cafebabe1234", _lab)[0], cc.FALSE)

shutil.rmtree(_sib, ignore_errors=True)

print("\n-- offsets survive masking (a mask must not shift the text) --")
src = 'a "quoted bit" and *an italic bit* end'
check("length held", len(cc.mask_mentions(src)), len(src))
check("newlines held", cc.mask_mentions("*a\nb*").count("\n"), 1)

print()
if FAILURES:
    print("%d test(s) failed: %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all tests pass")
