#!/usr/bin/env python3
"""sargas — the watchman. Notices when an agent never came back.

Every check in the agent stack reads what an agent SAID and verifies it.
That only works when the agent speaks. A hung session writes no report,
makes no claim, raises no error and produces no exit code. It is not
wrong; it is absent. Nothing notices, and the meter keeps running.

sargas watches for the absence. You declare what should be fresh and how
often, and it tells you what has gone quiet.

    sargas.py                     check the watchfile in this directory
    sargas.py --watch .sargas     check a named watchfile
    sargas.py --json              for the agent supervising the fleet
    sargas.py --quiet             print only what is overdue
    sargas.py --stamp build       a routine reporting that it finished

Exit 0 = everything reported in on time. Exit 1 = something went quiet.
Put it on a timer; silence is the one failure that never pages you.

Watchfile format — one rule per line, '#' comments:

    fresh   <path-or-glob>   <max-age>   [label]
    pushed  <branch>         <max-age>   [label]
    ran     <command>        <max-age>   [label]

    fresh   journal/*.md      26h   nightly journal
    pushed  dawn              6h    trunk backup
    ran     .sargas.d/build   25h   the build routine

Ages: 90s 45m 6h 3d. Local, stdlib only, no network.
"""

import argparse, fnmatch, glob, json, os, re, subprocess, sys, time

VERSION = "0.1"
OK, LATE, UNKNOWN = "ok", "OVERDUE", "unknown"

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def age(spec):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", spec.strip().lower())
    if not m:
        raise ValueError("bad age %r — use 90s, 45m, 6h, 3d" % spec)
    return float(m.group(1)) * UNITS[m.group(2)]


def human(sec):
    if sec < 90:
        return "%ds" % sec
    if sec < 5400:
        return "%dm" % (sec / 60)
    if sec < 172800:
        return "%.1fh" % (sec / 3600)
    return "%.1fd" % (sec / 86400)


def sh(cmd, cwd=None):
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                           text=True, timeout=60)
        return p.returncode, (p.stdout or "").strip()
    except Exception:
        return 1, ""


# ── the three watches ────────────────────────────────────────────────

def check_fresh(target, limit, repo):
    """Something should have been written recently and was not."""
    pattern = target if os.path.isabs(target) else os.path.join(repo, target)
    hits = glob.glob(os.path.expanduser(pattern))
    if not hits:
        return UNKNOWN, "nothing matches %s — it has never been written" % target
    newest = max(hits, key=lambda p: os.path.getmtime(p))
    quiet = time.time() - os.path.getmtime(newest)
    name = os.path.basename(newest.rstrip("/")) or newest
    if quiet > limit:
        return LATE, "%s is %s old, expected within %s" % (
            name, human(quiet), human(limit))
    return OK, "%s written %s ago" % (name, human(quiet))


def check_pushed(branch, limit, repo):
    """Work exists locally that no remote has seen, and it is getting old."""
    code, _ = sh("git rev-parse --verify %s" % branch, repo)
    if code != 0:
        return UNKNOWN, "no local branch %r" % branch
    code, out = sh("git log %s --not --remotes=origin --format='%%H %%ct'" % branch, repo)
    if code != 0:
        return UNKNOWN, "cannot read git history"
    rows = [l.split() for l in out.splitlines() if l.strip()]
    if not rows:
        return OK, "every commit on %s is on origin" % branch
    oldest = min(int(r[1]) for r in rows)
    stranded = time.time() - oldest
    if stranded > limit:
        return LATE, "%d commit(s) on %s unpushed for %s — oldest exceeds %s" % (
            len(rows), branch, human(stranded), human(limit))
    return OK, "%d commit(s) unpushed, oldest %s — still inside %s" % (
        len(rows), human(stranded), human(limit))


def check_ran(target, limit, repo):
    """A stamp file a routine touches when it finishes. No stamp, no run."""
    path = target if os.path.isabs(target) else os.path.join(repo, target)
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return LATE, "%s has no completion stamp — it has not finished, ever" % target
    quiet = time.time() - os.path.getmtime(path)
    if quiet > limit:
        return LATE, "last finished %s ago, expected every %s" % (
            human(quiet), human(limit))
    return OK, "finished %s ago" % human(quiet)


CHECKS = {"fresh": check_fresh, "pushed": check_pushed, "ran": check_ran}


def parse(path):
    rules = []
    for n, raw in enumerate(open(path, encoding="utf-8"), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 3:
            raise ValueError("line %d: need <kind> <target> <max-age>" % n)
        kind, target, span = parts[0], parts[1], parts[2]
        if kind not in CHECKS:
            raise ValueError("line %d: unknown check %r (use %s)"
                             % (n, kind, ", ".join(sorted(CHECKS))))
        rules.append({"kind": kind, "target": target, "limit": age(span),
                      "span": span,
                      "label": parts[3] if len(parts) > 3 else target})
    return rules


def main():
    ap = argparse.ArgumentParser(
        description="Notice when an agent, a routine or a branch goes quiet.")
    ap.add_argument("--watch", default=".sargas", help="watchfile (default: .sargas)")
    ap.add_argument("--repo", default=".", help="directory rules are relative to")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="print only what is overdue")
    ap.add_argument("--stamp", metavar="NAME",
                    help="record that NAME just finished, then exit")
    ap.add_argument("--stamp-dir", default=".sargas.d",
                    help="where stamps live (default: .sargas.d)")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args()

    if a.version:
        print("sargas " + VERSION); return

    if a.stamp:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", a.stamp):
            sys.exit("stamp names are letters, digits, dot, dash, underscore")
        d = a.stamp_dir if os.path.isabs(a.stamp_dir) else os.path.join(a.repo, a.stamp_dir)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, a.stamp)
        open(path, "w").write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
        print("stamped %s" % path)
        return

    wf = a.watch if os.path.isabs(a.watch) else os.path.join(a.repo, a.watch)
    if not os.path.exists(wf):
        sys.exit("no watchfile at %s — write one, see --help for the format" % wf)

    try:
        rules = parse(wf)
    except ValueError as e:
        sys.exit("watchfile: %s" % e)
    if not rules:
        sys.exit("watchfile has no rules — nothing is being watched")

    results = []
    for r in rules:
        state, why = CHECKS[r["kind"]](r["target"], r["limit"], a.repo)
        results.append({"kind": r["kind"], "label": r["label"],
                        "target": r["target"], "within": r["span"],
                        "state": state, "detail": why})

    late = [r for r in results if r["state"] == LATE]
    if a.json:
        print(json.dumps({"checked": len(results), "overdue": len(late),
                          "quiet": bool(late), "watches": results}, indent=2))
        sys.exit(1 if late else 0)

    mark = {OK: "  ok  ", LATE: "QUIET ", UNKNOWN: "  ?   "}
    shown = late if a.quiet else results
    if shown:
        print()
    for r in shown:
        print("%s  %-6s %s" % (mark[r["state"]], r["kind"], r["label"][:56]))
        print("          %s" % r["detail"])
    if not a.quiet:
        unknown = sum(1 for r in results if r["state"] == UNKNOWN)
        print("\n%d watch(es) · %d reporting · %d QUIET · %d unknown"
              % (len(results), len(results) - len(late) - unknown, len(late), unknown))
    if late:
        print("\nSomething stopped reporting in. That is the failure that "
              "does not raise an error.")
    sys.exit(1 if late else 0)


if __name__ == "__main__":
    main()
