#!/usr/bin/env python3
"""namo — work that never landed did not happen.

An agent finishes a job on a branch, in a worktree, in a container, and
says so. The work is real. Nobody can reach it. The next morning the
person who asked opens the repository, sees yesterday, and asks whether
anything ran at all.

That is not a reporting problem. It is the last unguarded step of agent
work: the handoff from "done" to "landed".

namo is the guard for that step. It answers two questions and refuses to
let a session end until both are answered well:

    is this session's work reachable from the trunk?   (or at least pushed)
    does one line say what now exists, and where?      (MADE.md)

    namo.py check                           # both guards; exit 1 = refuse
    namo.py land                            # push the branch, open the PR
    namo.py made "thing" --path src/thing.py
    namo.py made-nothing --why "spent the night on a dead end"

MADE.md is not a status report. An entry must name something that
exists — a path on disk or a URL that answers — or it must say plainly
that nothing was made. Counts, green checks and PR states are refused on
purpose: they are what a status report says when there is no artifact.

Local, stdlib only. Network only to check a URL you asked it to check.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

VERSION = "0.1"

HOME, PENDING, STRANDED = "home", "pending", "STRANDED"


def sh(cmd, cwd=None, timeout=120):
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ds" % timeout
    except Exception as e:
        return 1, "", str(e)


def ref_exists(ref, repo):
    return sh("git rev-parse --verify --quiet %s^{commit}" % ref, cwd=repo)[0] == 0


def current_branch(repo):
    code, out, _ = sh("git rev-parse --abbrev-ref HEAD", cwd=repo)
    return out if code == 0 and out != "HEAD" else None


# ── guard one: is the work home? ─────────────────────────────────────

def landing(repo, trunk):
    """Where does this session's work actually live?

    Home is `origin/<trunk>` when a remote exists, not the local trunk.
    That distinction is the whole point: a local trunk that is ahead of
    its remote looks landed to the session and is invisible to everyone
    else. This repository ran with two divergent copies of `dawn` for
    days for exactly that reason.

    Falls back to the local trunk when there is no remote, so the tool
    still works in a repository that has never been pushed.
    """
    if not ref_exists("HEAD", repo):
        return {"state": HOME, "why": "no commits yet — nothing to strand",
                "commits": []}

    remote_trunk = "origin/%s" % trunk
    if ref_exists(remote_trunk, repo):
        home_ref, home_name = remote_trunk, remote_trunk
    elif ref_exists(trunk, repo):
        home_ref, home_name = trunk, trunk
    else:
        return {"state": STRANDED, "commits": [],
                "why": "no trunk called '%s' here — pass --trunk" % trunk}

    code, out, _ = sh("git log HEAD --not %s --format='%%h %%s'" % home_ref, cwd=repo)
    if code != 0:
        return {"state": STRANDED, "commits": [],
                "why": "cannot read git history"}

    commits = []
    for line in out.splitlines():
        sha, _, subj = line.strip().partition(" ")
        if sha:
            commits.append({"sha": sha, "subject": subj})
    if not commits:
        return {"state": HOME, "commits": [],
                "why": "every commit here is reachable from %s" % home_name}

    # Not on the trunk. Is it at least pushed somewhere anyone can fetch?
    # A branch on origin with a PR open is in review, not stranded. A
    # commit that exists only in this worktree is the actual problem.
    code, out, _ = sh("git log HEAD --not --remotes=origin --format=%H", cwd=repo)
    unpushed = [l for l in out.splitlines() if l.strip()] if code == 0 else commits

    branch = current_branch(repo)
    if unpushed:
        return {"state": STRANDED, "commits": commits, "branch": branch,
                "why": "%d commit(s) exist on no remote — only in this tree"
                       % len(unpushed)}
    return {"state": PENDING, "commits": commits, "branch": branch,
            "why": "pushed to origin but not merged into %s" % home_name}


# ── guard two: does one line say what now exists? ────────────────────

MADE_HEADER = """# MADE — what exists that did not exist yesterday

One line per shift: the thing, and where it is. Not a status report.
A shift that made nothing says so, and that is the honest answer.
Newest first. Written by `namo`.
"""

# Phrasings that describe activity rather than a thing. A status report is
# what gets written when there is no artifact, so these are refused at the
# door rather than argued with later.
STATUS_PHRASES = [
    r"\ball green\b", r"\btests? (?:all )?pass", r"\bsuite is green\b",
    r"\bPR\s*#?\d+", r"\bpull request\b", r"\b(?:merged|reviewed|investigated)\b",
    r"\b(?:looked (?:at|into)|surveyed|assessed|audited|analy[sz]ed)\b",
    r"\b(?:in progress|wip|ongoing|continued work|work continues)\b",
    r"\b(?:no changes needed|nothing to do|as expected)\b",
    r"\bstatus\b", r"\bupdate[sd]?\b(?!\s+\w)",
    r"^\s*\d+\s+(?:tests?|checks?|commits?|files?|stars?|orders?|prs?)\b",
    r"\b\d+\s+(?:tests?|checks?)\s+(?:pass|passing|green)\b",
]


def reads_as_status(text):
    for pat in STATUS_PHRASES:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(0).strip()
    return None


# A third answer, for the same reason claimcheck grew one on 2026-09-01:
# "I could not check" is not "it does not exist," and a guard that reports
# the second when it means the first gets routed around.
UNVERIFIED = "unverified"

# Hosts that answer 404 for a resource that exists but is private. GitHub
# does this deliberately, so an unauthenticated 404 there carries no
# information about whether the thing is real.
MASKS_PRIVATE_AS_404 = ("github.com", "www.github.com", "api.github.com",
                        "gist.github.com", "raw.githubusercontent.com")


def masks_private(url):
    m = re.match(r"https?://([^/:]+)", url or "", re.I)
    return bool(m) and m.group(1).lower() in MASKS_PRIVATE_AS_404


def check_artifact(path, url, repo, allow_net=True):
    """An entry may only name something that exists. This is the rule that
    makes MADE.md worth opening: everything in it is clickable.

    Returns True, False, or UNVERIFIED. Only False refuses. UNVERIFIED is
    for the cases where this check cannot see: a private-masking 404, a
    request that never arrived, or --no-net, which checks nothing and used
    to say True anyway."""
    if path:
        full = path if os.path.isabs(path) else os.path.join(repo, path)
        if not os.path.exists(full):
            return False, "no such path in the repo: %s" % path
        what = "directory" if os.path.isdir(full) else "%d bytes" % os.path.getsize(full)
        return True, "%s exists (%s)" % (path, what)
    if url:
        if not allow_net:
            return UNVERIFIED, "%s (not checked at all, --no-net)" % url
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "namo/%s" % VERSION})
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                return True, "%s answers HTTP %d" % (url, r.status)
        except urllib.error.HTTPError as e:
            if e.code in (403, 405):
                return True, "%s exists (host refuses HEAD, HTTP %d)" % (url, e.code)
            if e.code == 404 and masks_private(url):
                return UNVERIFIED, (
                    "%s returns 404, which is also what this host returns for "
                    "a private resource — namo cannot tell them apart without "
                    "credentials" % url)
            return False, "%s returns HTTP %d" % (url, e.code)
        except Exception as e:
            # The request never reached a server. That is a closed door, not
            # an absent artifact.
            return UNVERIFIED, "%s gave no answer (%s)" % (url, str(e)[:40])
    return False, "name a --path or a --url; an entry must point at something"


def made_path(repo):
    return os.path.join(repo, "MADE.md")


def read_made(repo):
    p = made_path(repo)
    if not os.path.exists(p):
        return ""
    return open(p, encoding="utf-8", errors="replace").read()


def has_entry(repo, shift, day):
    """Is there already a line for this shift, on this day?"""
    text = read_made(repo)
    in_day = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_day = line[3:].strip() == day
            continue
        if in_day and re.match(r"^-\s+%s\s+·" % re.escape(shift), line.strip()):
            return line.strip()
    return None


def append_entry(repo, shift, day, entry_text):
    text = read_made(repo)
    if not text.strip():
        text = MADE_HEADER
    lines = text.rstrip("\n").split("\n")

    day_head = "## %s" % day
    line = "- %s · %s" % (shift, entry_text)

    if day_head in lines:
        i = lines.index(day_head)
        j = i + 1
        while j < len(lines) and not lines[j].startswith("## "):
            j += 1
        while j > i + 1 and not lines[j - 1].strip():
            j -= 1
        lines.insert(j, line)
    else:
        # newest day first: above the first existing day heading
        idx = next((n for n, l in enumerate(lines) if l.startswith("## ")), len(lines))
        block = [day_head, line, ""]
        if idx == len(lines):
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(block)
        else:
            lines[idx:idx] = block

    out = "\n".join(lines).rstrip("\n") + "\n"
    open(made_path(repo), "w", encoding="utf-8").write(out)
    return line


# ── commands ─────────────────────────────────────────────────────────

def cmd_check(a):
    repo, day = a.repo, a.date
    land = landing(repo, a.trunk)
    entry = has_entry(repo, a.shift, day)

    problems = []
    if land["state"] == STRANDED:
        problems.append("stranded")
    if not entry:
        problems.append("unrecorded")

    if a.json:
        print(json.dumps({"landing": land, "made_entry": entry,
                          "shift": a.shift, "date": day,
                          "ok": not problems}, indent=2))
        sys.exit(1 if problems else 0)

    print()
    mark = {HOME: "  ok  ", PENDING: "  ~   ", STRANDED: " STUCK"}[land["state"]]
    print("%s  work    %s" % (mark, land["why"]))
    for c in land["commits"][:6]:
        print("          %s  %s" % (c["sha"], c["subject"][:62]))
    if len(land["commits"]) > 6:
        print("          … and %d more" % (len(land["commits"]) - 6))

    print("%s  made    %s" % ("  ok  " if entry else " NONE ",
                              entry or "no line for '%s' on %s" % (a.shift, day)))
    print()

    if not problems:
        if land["state"] == PENDING:
            print("In review, and recorded. Fine to end.")
        else:
            print("Home, and recorded. Fine to end.")
        sys.exit(0)

    if "stranded" in problems:
        b = land.get("branch") or "this branch"
        print("This session's work exists only in this tree. Land it:")
        print("    namo.py land            # push %s and open the PR" % b)
    if "unrecorded" in problems:
        print("Nothing says what this shift made. Write the one line:")
        print('    namo.py made "<the thing>" --path <where it is>')
        print('    namo.py made-nothing --why "<what happened instead>"')
    print()
    sys.exit(1)


def cmd_land(a):
    repo = a.repo
    branch = current_branch(repo)
    if not branch:
        sys.exit("namo: detached HEAD — check out a branch first")
    if branch == a.trunk:
        print("On %s itself. Push it where it belongs:" % a.trunk)
        print("    git push origin %s" % a.trunk)
        sys.exit(1)

    code, out, err = sh("git push -u origin %s" % branch, cwd=repo)
    if code != 0:
        sys.exit("namo: push failed — %s" % (err.splitlines()[-1][:120] if err else code))
    print("pushed %s to origin" % branch)

    code, out, err = sh("gh pr view %s --json url,state" % branch, cwd=repo)
    if code == 0:
        try:
            print("PR already open: %s" % json.loads(out).get("url", "?"))
            return
        except Exception:
            pass
    code, out, err = sh('gh pr create --base %s --head %s --fill' % (a.trunk, branch),
                        cwd=repo)
    if code != 0:
        print("branch is on origin; could not open the PR automatically:")
        print("  %s" % ((err or out).splitlines()[-1][:120] if (err or out) else "no gh"))
        print("  open it by hand against %s" % a.trunk)
        return
    print(out.strip().splitlines()[-1] if out.strip() else "PR opened")


def cmd_made(a):
    repo, day = a.repo, a.date
    text = " ".join(a.what).strip()
    if not text:
        sys.exit("namo: say what now exists")

    status = reads_as_status(text)
    if status:
        sys.exit("namo: that reads as a status report (\"%s\").\n"
                 "MADE.md names a thing that exists, not what happened to it.\n"
                 "  no:  fixed the parser, all tests pass\n"
                 "  yes: namo — refuses to let a shift end with its work stranded"
                 % status)

    ok, evidence = check_artifact(a.path, a.url, repo, allow_net=not a.no_net)
    if ok is False:
        sys.exit("namo: %s\nAn entry must point at something that exists." % evidence)

    where = a.path or a.url
    existing = has_entry(repo, a.shift, day)
    if existing and not a.again:
        sys.exit("namo: %s already has a line on %s:\n  %s\n"
                 "Pass --again to add a second one." % (a.shift, day, existing))

    line = append_entry(repo, a.shift, day, "%s · %s" % (text, where))
    print("MADE.md ← %s" % line)
    print("         %s" % evidence)
    if ok is UNVERIFIED:
        print("         (recorded, but nothing here vouches for that link)")


def cmd_made_nothing(a):
    repo, day = a.repo, a.date
    why = " ".join(a.why).strip()
    if not why:
        sys.exit("namo: say what happened instead")
    existing = has_entry(repo, a.shift, day)
    if existing and not a.again:
        sys.exit("namo: %s already has a line on %s:\n  %s" % (a.shift, day, existing))
    line = append_entry(repo, a.shift, day, "made nothing — %s" % why)
    print("MADE.md ← %s" % line)


def main():
    ap = argparse.ArgumentParser(
        description="Refuse to let a session end with its work stranded.")
    ap.add_argument("--version", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--repo", default=".", help="repository (default: cwd)")
        p.add_argument("--trunk", default=os.environ.get("NAMO_TRUNK", "dawn"),
                       help="the branch that counts as home (default: dawn)")
        p.add_argument("--shift", default=os.environ.get("NAMO_SHIFT", "session"),
                       help="who is writing the line (default: session)")
        p.add_argument("--date", default=time.strftime("%Y-%m-%d"),
                       help="the day to write under (default: today)")

    c = sub.add_parser("check", help="both guards; exit 1 refuses the ending")
    common(c); c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_check)

    l = sub.add_parser("land", help="push this branch and open its PR")
    common(l); l.set_defaults(fn=cmd_land)

    m = sub.add_parser("made", help="record the one thing that now exists")
    common(m)
    m.add_argument("what", nargs="+", help="what now exists, in a few words")
    m.add_argument("--path", help="where it is, relative to the repo")
    m.add_argument("--url", help="or the URL it went live at")
    m.add_argument("--no-net", action="store_true", help="do not check a URL")
    m.add_argument("--again", action="store_true", help="allow a second line today")
    m.set_defaults(fn=cmd_made)

    n = sub.add_parser("made-nothing", help="record an honest empty night")
    common(n)
    n.add_argument("--why", nargs="+", required=True, help="what happened instead")
    n.add_argument("--again", action="store_true")
    n.set_defaults(fn=cmd_made_nothing)

    a = ap.parse_args()
    if a.version:
        print("namo " + VERSION); return
    if not getattr(a, "fn", None):
        ap.print_help(); sys.exit(2)
    a.fn(a)


if __name__ == "__main__":
    main()
