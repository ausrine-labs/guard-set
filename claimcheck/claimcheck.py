#!/usr/bin/env python3
"""claimcheck — verify what another agent told you.

Agents report their own work. "Tests pass." "Committed as a1b2c3d."
"Pushed to main." "The file is there." You either believe it or you
redo the work to find out. There is no third option, and that is the
whole problem with multi-agent work today.

claimcheck is the third option. Feed it an agent's message; it finds
the checkable claims, runs the check, and tells you which ones are
true — with the evidence.

    claimcheck.py "committed as a1b2c3d and pushed to dawn; tests pass"
    claimcheck.py --file handoff.md
    echo "$AGENT_REPLY" | claimcheck.py
    claimcheck.py --json "..."          # for your agent to read

Exit 0 = every claim verified. Exit 1 = something is false or unproven.
Drop it in a hook or a handoff step and a lying report stops the line.

Local, stdlib only, no network except when a claim is about a URL.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

VERSION = "0.1"

# Extra checkouts to consult before calling a sha fabricated. Populated by
# main() from --sibling; CLAIMCHECK_SIBLINGS and the repo's own neighbours
# are merged in at lookup time. See sibling_repos().
SIBLING_HINTS = []

TRUE, FALSE, UNPROVEN = "verified", "FALSE", "unproven"
UNVERIFIABLE = "UNVERIFIABLE"


def sh(cmd, cwd=None, timeout=90):
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ds" % timeout
    except Exception as e:
        return 1, "", str(e)


# ── mention vs. assertion ────────────────────────────────────────────

def mask_mentions(text):
    """Blank the spans a report shows rather than asserts.

    A report that quotes an example — "pushed; tree clean" — or pastes
    another agent's message into a fenced block is talking *about* claims,
    not making them. Checking those raises false alarms, and a checker that
    cries wolf gets switched off, which is worse than one that misses a
    line. A miss is at least visible: the summary says how many were
    checked.

    Masked: fenced code blocks, double-quoted spans, single-asterisk
    italics. NOT masked: inline `code` — that is where real reports keep
    their real shas and real paths.
    """
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))   # keep every offset put

    text = re.sub(r"```.*?```", blank, text, flags=re.S)
    text = re.sub(r'"[^"\n]{1,300}"', blank, text)
    text = re.sub("[“‘][^”’\n]{1,300}[”’]", blank, text)
    text = re.sub(r"(?<!\*)\*(?![\s*])[^*]{1,300}\*(?!\*)", blank, text)
    return text


# ── claim detectors ──────────────────────────────────────────────────
# Each returns a list of dicts: {kind, quote, check(repo) -> (state, evidence)}

def find_commits(text):
    out = []
    for m in re.finditer(r"\b(?:commit(?:ted)?|merged|landed|sha|revision)\b[^.\n]{0,40}?\b([0-9a-f]{7,40})\b",
                         text, re.I):
        sha = m.group(1)
        out.append(("commit", m.group(0).strip(), sha))
    # bare shas in backticks
    for m in re.finditer(r"`([0-9a-f]{7,40})`", text):
        if not any(c[2] == m.group(1) for c in out):
            out.append(("commit", m.group(0), m.group(1)))
    return out


def sibling_repos(repo, hints=()):
    """Other checkouts that may hold a commit this one has never seen.

    A sha written in a sentence does not carry the name of the repo it came
    from. An agent working across two repos will quote one inside the other's
    journal, and on 2026-09-02 this tool called such a sentence FALSE: the
    commit was a real `ausrine-os` merge, quoted in the `ausrine-lab` journal,
    and every rung of the ladder below was answered by the wrong repo.

    Sources, in order, de-duplicated: explicit --sibling paths, the
    CLAIMCHECK_SIBLINGS env var (colon-separated), then the sibling
    directories of this repo that are themselves checkouts.
    """
    here = os.path.abspath(repo)
    seen, out = {here}, []

    def add(path):
        if not path:
            return
        full = os.path.abspath(os.path.expanduser(path))
        if full in seen or not os.path.exists(os.path.join(full, ".git")):
            return
        seen.add(full)
        out.append(full)

    for h in list(hints) + list(SIBLING_HINTS):
        add(h)
    for h in os.environ.get("CLAIMCHECK_SIBLINGS", "").split(":"):
        add(h.strip())

    parent = os.path.dirname(here)
    try:
        neighbours = sorted(os.listdir(parent))
    except OSError:
        neighbours = []
    for name in neighbours:
        add(os.path.join(parent, name))
    return out


def _held_next_door(sha, repo):
    """UNVERIFIABLE if some sibling checkout holds this sha, else None.

    Called before every FALSE in check_commit. A sha carries no repo name,
    so 'not in this repo' is never on its own grounds to call one invented.
    """
    short = sha[:8]
    for sib in sibling_repos(repo):
        if sh("git cat-file -e %s^{commit}" % sha, cwd=sib)[0] != 0:
            continue
        _, subj, _ = sh("git log -1 --format=%%s %s" % sha, cwd=sib)
        return UNVERIFIABLE, (
            "%s is not in this repo, but %s holds it (%s) — a sha does not "
            "carry its repo; name the repo in the sentence for a verdict"
            % (short, os.path.basename(sib), subj[:40]))
    return None


def check_commit(sha, repo):
    """Absent from this checkout is not the same as absent from the world.

    `git cat-file` reads the local object store only. It was the whole of
    this check until 2026-09-01, and under a sandbox that cannot fetch it
    made our own tool call an honest agent a liar: the commit was safely on
    the remote, this checkout had simply never seen it, and the verdict came
    back FALSE. So the check climbs, and stops as soon as it can answer:

      1. here          -> verified
      2. a remote tip  -> verified, without fetching anything
      3. cannot look   -> UNVERIFIABLE
      4. behind origin -> UNVERIFIABLE (it may be in history we lack)
      5. a sibling checkout holds it -> UNVERIFIABLE (whose sha is it?)
      6. nobody we can reach holds it -> FALSE
    """
    code, out, _ = sh("git cat-file -t %s" % sha, cwd=repo)
    if code == 0 and out == "commit":
        _, subj, _ = sh("git log -1 --format=%%s %s" % sha, cwd=repo)
        return TRUE, "commit exists: %s" % subj[:70]

    short = sha[:8]
    code, remotes, _ = sh("git remote", cwd=repo)
    if code != 0:
        return UNVERIFIABLE, "%s is not here and this is not a git repo" % short

    names = remotes.split()
    if not names:
        # No remote means this repo's store is its whole world — but a sha in
        # a sentence may have come from the checkout next door.
        elsewhere = _held_next_door(sha, repo)
        if elsewhere:
            return elsewhere
        return FALSE, "no commit %s — not here, and this repo has no remote" % short

    name = "origin" if "origin" in names else names[0]
    code, listing, err = sh("git ls-remote --heads %s" % name, cwd=repo)
    if code != 0:
        return UNVERIFIABLE, ("%s is not in this checkout and %s cannot be "
                              "reached (%s) — fetch before believing or "
                              "disbelieving this"
                              % (short, name, (err.splitlines() or [""])[0][:40]))

    tips = [ln.split()[0] for ln in listing.splitlines() if ln.strip()]
    for tip in tips:
        if tip.startswith(sha.lower()):
            return TRUE, "not fetched here, but %s holds it as a branch tip (%s)" % (
                name, tip[:8])

    missing = [t for t in tips if sh("git cat-file -e %s" % t, cwd=repo)[0] != 0]
    if missing:
        return UNVERIFIABLE, ("%s is not in this checkout, which is also missing "
                              "%d branch tip(s) from %s — it may be in history "
                              "we have never fetched" % (short, len(missing), name))

    # A sha carries no repo name. Before calling one fabricated, look next
    # door: this is the rung that was missing on 2026-09-02.
    elsewhere = _held_next_door(sha, repo)
    if elsewhere:
        return elsewhere

    return FALSE, ("no commit %s — not here, %s already holds everything this "
                   "checkout does, and no sibling checkout holds it"
                   % (short, name))


STOPWORDS = {"it", "them", "the", "up", "and", "origin", "tree", "everything",
             "this", "that", "all", "both", "clean", "live", "out", "now"}


def find_pushes(text):
    """Only count a push claim when a branch is actually named: 'pushed to X'.
    A bare 'pushed' names nothing checkable — the words after a semicolon
    belong to the next clause, not to this claim."""
    out = []
    for m in re.finditer(r"\bpush(?:ed)?\s+to\s+`?(?:origin[/ ])?([A-Za-z0-9._/-]{2,40})`?",
                         text, re.I):
        branch = m.group(1).strip(".,;:")
        if branch.lower() in STOPWORDS:
            continue
        out.append(("push", m.group(0).strip(), branch))
    if not out and re.search(r"\bpush(?:ed)?\b", text, re.I):
        out.append(("push", "claims something was pushed (no branch named)", None))
    return out


def check_push(branch, repo):
    if branch is None:
        # "ahead of upstream" is the wrong question. Work pushed to a side
        # branch is safe on origin while the local branch still reads ahead.
        # The real question is whether ANY remote has seen these commits.
        code, out, _ = sh("git log HEAD --not --remotes=origin --format=%H", cwd=repo)
        if code != 0:
            return UNVERIFIABLE, "no branch named and cannot read git history"
        stranded = [l for l in out.splitlines() if l.strip()]
        if stranded:
            return FALSE, "%d commit(s) exist on no remote — not pushed" % len(stranded)
        return TRUE, "every commit here is on origin (some branch)"
    code, out, err = sh("git ls-remote --heads origin %s" % branch, cwd=repo)
    if code != 0:
        return UNVERIFIABLE, "cannot reach remote (%s)" % (err[:60] or "no origin")
    if not out:
        return FALSE, "no branch '%s' on origin" % branch
    remote_sha = out.split()[0]
    _, local_sha, _ = sh("git rev-parse %s" % branch, cwd=repo)
    if local_sha and remote_sha.startswith(local_sha[:12]):
        return TRUE, "origin/%s == local (%s)" % (branch, remote_sha[:8])
    return TRUE, "origin/%s exists at %s" % (branch, remote_sha[:8])


def find_clean(text):
    if re.search(r"\b(tree is clean|working tree clean|nothing to commit|"
                 r"no uncommitted|everything committed|all committed)\b", text, re.I):
        return [("clean", "claims the tree is clean", None)]
    return []


def check_clean(_arg, repo):
    code, out, _ = sh("git status --porcelain", cwd=repo)
    if code != 0:
        return UNVERIFIABLE, "not a git repo — cannot see the tree"
    if out.strip():
        n = len(out.strip().splitlines())
        return FALSE, "%d uncommitted change(s): %s" % (n, out.splitlines()[0][:50])
    return TRUE, "working tree is clean"


def find_tests(text):
    if re.search(r"\b(tests? (?:all )?pass|suite (?:is )?green|all green|"
                 r"\d+\s+(?:checks?|tests?)\s+pass|passing)\b", text, re.I):
        m = re.search(r"[^.\n]*\b(?:pass|green)[^.\n]*", text, re.I)
        return [("tests", (m.group(0).strip() if m else "claims tests pass"), None)]
    return []


def check_tests(_arg, repo, cmd=None):
    if not cmd:
        for candidate, probe in [
            ("python3 test_*.py", "ls test_*.py"),
            ("python3 -m pytest -q", "ls pytest.ini setup.cfg pyproject.toml"),
            ("npm test --silent", "ls package.json"),
        ]:
            if sh(probe, cwd=repo)[0] == 0:
                cmd = candidate
                break
    if not cmd:
        return UNPROVEN, "no test command found — pass --test-cmd to check this"
    code, out, err = sh(cmd, cwd=repo, timeout=300)
    tail = (out or err).strip().splitlines()
    tail = tail[-1][:70] if tail else ""
    if code == 0:
        return TRUE, "`%s` exited 0 — %s" % (cmd, tail)
    return FALSE, "`%s` exited %d — %s" % (cmd, code, tail)


def find_files(text):
    out = []
    for m in re.finditer(r"\b(?:created|wrote|added|updated|committed|shipped|"
                         r"is (?:now )?(?:at|in))\b[^.\n]{0,40}?`([^`\s]{3,120})`", text, re.I):
        p = m.group(1)
        if "/" in p or "." in p:
            out.append(("file", m.group(0).strip(), p))
    return out


def check_file(path, repo):
    full = path if os.path.isabs(path) else os.path.join(repo, path)
    if os.path.exists(full):
        size = os.path.getsize(full) if os.path.isfile(full) else 0
        return TRUE, "exists (%d bytes)" % size if size else "exists"
    return FALSE, "no such path: %s" % path


def find_urls(text):
    return [("url", u, u) for u in
            re.findall(r"https?://[^\s`)>\]\"']+", text)]


def check_url(url, _repo):
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "claimcheck/%s" % VERSION})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return TRUE, "HTTP %d" % r.status
    except urllib.error.HTTPError as e:
        if e.code in (403, 405):          # blocks HEAD, not necessarily dead
            return UNVERIFIABLE, "HTTP %d (host refuses HEAD)" % e.code
        return FALSE, "HTTP %d" % e.code
    except Exception as e:
        # The request never reached a server. A 404 is evidence; this is
        # a closed door, and behind it the page may be perfectly fine.
        return UNVERIFIABLE, "no answer (%s)" % str(e)[:40]


def find_prs(text):
    out = []
    for m in re.finditer(r"\bPR\s*#?(\d+)\b|\bpull request\s*#?(\d+)\b", text, re.I):
        n = m.group(1) or m.group(2)
        merged = bool(re.search(r"\bmerged\b", text[max(0, m.start()-60):m.end()+60], re.I))
        out.append(("pr", m.group(0), (n, merged)))
    return out


def check_pr(arg, repo):
    n, claims_merged = arg
    code, out, err = sh("gh pr view %s --json state,mergedAt,title" % n, cwd=repo)
    if code != 0:
        return UNVERIFIABLE, "cannot query PR (%s)" % (err.splitlines()[0][:50] if err else "no gh")
    try:
        d = json.loads(out)
    except Exception:
        return UNVERIFIABLE, "unreadable gh output"
    state = d.get("state", "?")
    if claims_merged and state != "MERGED":
        return FALSE, "PR #%s is %s, not merged" % (n, state)
    return TRUE, "PR #%s is %s" % (n, state)


DETECTORS = [
    (find_commits, check_commit),
    (find_pushes, check_push),
    (find_clean, check_clean),
    (find_tests, check_tests),
    (find_files, check_file),
    (find_prs, check_pr),
    (find_urls, check_url),
]


def verify(text, repo=".", test_cmd=None, skip=()):
    results, seen = [], set()
    text = mask_mentions(text)
    for finder, checker in DETECTORS:
        for kind, quote, arg in finder(text):
            if kind in skip:
                continue
            key = (kind, str(arg))
            if key in seen:
                continue
            seen.add(key)
            if kind == "tests":
                state, evidence = check_tests(arg, repo, test_cmd)
            else:
                state, evidence = checker(arg, repo)
            results.append({"kind": kind, "claim": quote.strip(),
                            "verdict": state, "evidence": evidence})
    return results


def main():
    ap = argparse.ArgumentParser(
        description="Verify the checkable claims in an agent's report.")
    ap.add_argument("text", nargs="?", help="the agent's message (or use --file / stdin)")
    ap.add_argument("--file", help="read the message from a file")
    ap.add_argument("--repo", default=".", help="repo the claims are about (default: cwd)")
    ap.add_argument("--test-cmd", help="command that runs the tests, if claims mention them")
    ap.add_argument("--skip", default="", help="comma-separated kinds to skip (e.g. url,tests)")
    ap.add_argument("--sibling", action="append", default=[], metavar="PATH",
                    help="another checkout that may hold a quoted sha "
                         "(repeatable; also CLAIMCHECK_SIBLINGS=a:b)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args()

    if a.version:
        print("claimcheck " + VERSION); return

    if a.file:
        text = open(a.file, encoding="utf-8", errors="replace").read()
    elif a.text:
        text = a.text
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        ap.print_help(); sys.exit(2)

    results = verify(text, a.repo, a.test_cmd,
                     skip=tuple(s.strip() for s in a.skip.split(",") if s.strip()))

    if a.json:
        false_n = sum(1 for r in results if r["verdict"] == FALSE)
        blind_n = sum(1 for r in results if r["verdict"] == UNVERIFIABLE)
        print(json.dumps({"claims": results, "checked": len(results),
                          "false": false_n, "unverifiable": blind_n,
                          "trustworthy": false_n == 0 and len(results) > 0},
                         indent=2))
        sys.exit(1 if false_n else 0)

    if not results:
        print("No checkable claims found. Nothing to verify — treat the report as unverified.")
        sys.exit(1)

    mark = {TRUE: "  ok  ", FALSE: " FALSE", UNPROVEN: "  ?   ",
            UNVERIFIABLE: " BLIND"}
    print()
    for r in results:
        print("%s  %-6s %s" % (mark[r["verdict"]], r["kind"], r["claim"][:64]))
        print("          %s" % r["evidence"])
    false_n = sum(1 for r in results if r["verdict"] == FALSE)
    unproven = sum(1 for r in results if r["verdict"] == UNPROVEN)
    blind_n = sum(1 for r in results if r["verdict"] == UNVERIFIABLE)
    ok_n = len(results) - false_n - unproven - blind_n
    print()
    print("%d claim(s) checked · %d verified · %d false · %d unproven · "
          "%d unverifiable"
          % (len(results), ok_n, false_n, unproven, blind_n))
    if false_n:
        print("This report contains a false claim. Do not act on it.")
    if blind_n:
        # Deliberately not a failure. A guard that refuses what it cannot
        # justify teaches the caller to route around it, and a
        # routed-around guard is worse than none: it still looks like
        # protection. Say where the blindness is and let the work through.
        print("%d claim(s) could not be checked from here. That is this\n"
              "tool being blind, not the report being wrong — but nothing\n"
              "above vouches for them either." % blind_n)
    sys.exit(1 if false_n else 0)


if __name__ == "__main__":
    main()
