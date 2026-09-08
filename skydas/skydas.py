#!/usr/bin/env python3
"""skydas — shield. Answers one question: are this repo's guards actually
armed in this checkout?

On 2026-08-29 this repo added a pre-commit hook that refuses agent commits
in the shared checkout, and pointed core.hooksPath at the directory that
holds it. The hook was merged to origin/dawn. But the local dawn branch was
13 commits behind and had never checked that file out — so core.hooksPath
pointed at a directory where pre-commit did not exist in the working tree.

Git does not warn about this. It fails open, silently. For two days every
agent commit in the shared checkout was unguarded, and on 2026-08-31 two
agent sessions collided there: one session's `git add` swept another
session's in-progress file into its own commit, destroying the provenance
of that file.

The general lesson: a guard that lives in the repo is only armed on
branches that contain it. Checking out an older branch, or a branch from
before the guard existed, silently disarms it, and nothing tells you.
Every repo using core.hooksPath or a file-referencing hook config (Claude
Code's .claude/settings.json included) has this hole.

    skydas.py check --repo .
    skydas.py check --repo . --ref origin/main
    skydas.py check --json

Exit 0 = every guard skydas can see is armed. Exit 1 = at least one is
inert. Exit 2 = usage or environment error (not a git repo, etc).

Local, stdlib only, no network.
"""

import argparse
import json
import os
import subprocess
import sys

VERSION = "0.1"

ARMED, INERT, UNKNOWN = "ARMED", "INERT", "UNKNOWN"

REF_CANDIDATES_STATIC = ("origin/HEAD", "origin/dawn", "origin/main")


def sh(cmd, cwd=None, timeout=30):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ds" % timeout
    except FileNotFoundError:
        return 127, "", "git not found"


def is_executable(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


# ── repo + ref resolution ────────────────────────────────────────────

def repo_root(repo):
    code, out, err = sh(["git", "rev-parse", "--show-toplevel"], cwd=repo)
    if code != 0:
        return None, err or "not a git repository"
    return out, None


def current_branch(root):
    code, out, _ = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    if code == 0 and out and out != "HEAD":
        return out
    return None


def resolve_ref(root, explicit):
    """Pick the ref to compare the working tree against. Returns
    (ref_or_None, note) — note explains what was tried when nothing resolved,
    or which explicit ref failed."""
    if explicit:
        code, _, _ = sh(["git", "rev-parse", "--verify", "--quiet",
                         explicit + "^{commit}"], cwd=root)
        if code == 0:
            return explicit, None
        return None, "--ref %s does not resolve in this repo" % explicit

    candidates = ["origin/HEAD"]
    branch = current_branch(root)
    if branch:
        candidates.append("origin/" + branch)
    candidates += ["origin/dawn", "origin/main"]

    tried = []
    for cand in candidates:
        if cand in tried:
            continue
        tried.append(cand)
        code, _, _ = sh(["git", "rev-parse", "--verify", "--quiet",
                         cand + "^{commit}"], cwd=root)
        if code == 0:
            return cand, None
    return None, ("no ref resolved (tried %s) — skipping the ref-comparison "
                  "check" % ", ".join(tried))


# ── guard 1 + 2 + 3: hooksPath and the hooks under it ────────────────

def check_hooks_path(root, ref):
    """Returns (findings, ref_note). findings is a list of
    {guard, verdict, reason}."""
    findings = []

    code, hooks_path, _ = sh(["git", "config", "--get", "core.hooksPath"],
                             cwd=root)
    if code != 0 or not hooks_path:
        return findings, None  # no hooksPath configured — nothing to check

    hooks_dir_abs = (hooks_path if os.path.isabs(hooks_path)
                     else os.path.join(root, hooks_path))
    hooks_dir_abs = os.path.normpath(hooks_dir_abs)

    if not os.path.isdir(hooks_dir_abs):
        findings.append({
            "guard": "core.hooksPath",
            "verdict": INERT,
            "reason": ("core.hooksPath points at %s, which does not exist "
                       "in this checkout — every repo hook is silently "
                       "disabled" % hooks_path),
        })
        return findings, None

    hooks_path_rel = None if os.path.isabs(hooks_path) else hooks_path.rstrip("/")

    # names on the ref, under hooksPath
    on_ref = set()
    if ref and hooks_path_rel is not None:
        code, out, _ = sh(["git", "ls-tree", "-r", "--name-only", ref,
                           "--", hooks_path_rel], cwd=root)
        if code == 0:
            for line in out.splitlines():
                line = line.strip()
                if line:
                    on_ref.add(line)

    # names present in the working tree, under hooksPath
    in_tree = {}
    for name in sorted(os.listdir(hooks_dir_abs)):
        if name.startswith(".") or name.endswith(".sample"):
            continue
        full = os.path.join(hooks_dir_abs, name)
        if not os.path.isfile(full):
            continue
        rel = (os.path.join(hooks_path_rel, name) if hooks_path_rel is not None
               else full)
        in_tree[rel] = full

    for rel in sorted(on_ref | set(in_tree)):
        if rel in on_ref and rel not in in_tree:
            findings.append({"guard": rel, "verdict": INERT,
                             "reason": "present on %s, absent here" % ref})
        elif rel in in_tree and not is_executable(in_tree[rel]):
            findings.append({"guard": rel, "verdict": INERT,
                             "reason": "present but not executable"})
        elif rel in in_tree:
            findings.append({"guard": rel, "verdict": ARMED,
                             "reason": "present on %s and here, executable"
                             % ref if rel in on_ref else "present here, executable"})

    return findings, None


# ── guard 4: Claude Code settings.json hooks ─────────────────────────

def expand_path(token, root):
    p = token.replace("$CLAUDE_PROJECT_DIR", root)
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(root, p)
    return os.path.normpath(p)


def path_like(token):
    return "/" in token or token.startswith("~") or token.startswith("$")


def check_settings_file(root, rel_path):
    findings = []
    full = os.path.join(root, rel_path)
    if not os.path.isfile(full):
        return findings

    try:
        with open(full, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return [{"guard": rel_path, "verdict": UNKNOWN,
                 "reason": "malformed JSON: %s" % str(e)[:80]}]

    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return findings

    for event, matchers in hooks.items():
        if not isinstance(matchers, list):
            continue
        for mi, matcher in enumerate(matchers):
            if not isinstance(matcher, dict):
                continue
            entries = matcher.get("hooks")
            if not isinstance(entries, list):
                continue
            for hi, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") != "command":
                    continue
                command = entry.get("command")
                if not isinstance(command, str) or not command.strip():
                    continue
                guard = "%s %s[%d][%d]" % (rel_path, event, mi, hi)
                tokens = command.split()
                candidate = next((t for t in tokens if path_like(t)), None)
                if candidate is None:
                    findings.append({"guard": guard, "verdict": UNKNOWN,
                                     "reason": "no path-like token in command: %s"
                                     % command[:60]})
                    continue
                path = expand_path(candidate, root)
                if not os.path.exists(path):
                    findings.append({"guard": guard, "verdict": INERT,
                                     "reason": ("%s hook references %s, which "
                                               "does not exist" % (event, path))})
                elif not is_executable(path):
                    findings.append({"guard": guard, "verdict": INERT,
                                     "reason": ("%s hook references %s, which "
                                               "is not executable" % (event, path))})
                else:
                    findings.append({"guard": guard, "verdict": ARMED,
                                     "reason": "%s hook at %s is executable"
                                     % (event, path)})
    return findings


def check_settings(root):
    findings = []
    for rel in (".claude/settings.json", ".claude/settings.local.json"):
        findings.extend(check_settings_file(root, rel))
    return findings


# ── driving it ────────────────────────────────────────────────────────

def run_check(repo, explicit_ref):
    root, err = repo_root(repo)
    if err:
        return None, 2, err

    ref, ref_err = resolve_ref(root, explicit_ref)
    notes = []
    if explicit_ref and ref_err:
        return None, 2, ref_err
    if ref_err:
        notes.append(ref_err)

    findings, _ = check_hooks_path(root, ref)
    findings.extend(check_settings(root))

    inert = [f for f in findings if f["verdict"] == INERT]
    armed = not inert
    return {"armed": armed, "findings": findings, "notes": notes,
           "ref": ref, "root": root}, (1 if inert else 0), None


def main():
    ap = argparse.ArgumentParser(
        prog="skydas.py",
        description="Are this repo's guards actually armed in this checkout?")
    ap.add_argument("--version", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    check = sub.add_parser("check", help="check whether guards are armed")
    check.add_argument("--repo", default=".", help="repo to check (default: cwd)")
    check.add_argument("--ref", default=None,
                       help="ref to compare against for intended guards "
                            "(default: origin/HEAD, then origin/<branch>, "
                            "then origin/dawn, then origin/main)")
    check.add_argument("--json", action="store_true")

    a = ap.parse_args()

    if a.version:
        print("skydas " + VERSION)
        return

    if a.cmd != "check":
        ap.print_help()
        sys.exit(2)

    result, code, err = run_check(a.repo, a.ref)
    if err:
        if a.json:
            print(json.dumps({"armed": False, "findings": [],
                              "error": err}))
        else:
            print("skydas: %s" % err, file=sys.stderr)
        sys.exit(2)

    findings = result["findings"]

    if a.json:
        print(json.dumps({"armed": result["armed"], "findings": findings},
                         indent=2))
        sys.exit(code)

    mark = {ARMED: " ARMED", INERT: " INERT", UNKNOWN: "  ?   "}
    print()
    if not findings:
        print("(no guards found to check — no core.hooksPath, no "
              ".claude/settings.json)")
    for f in findings:
        print("%s  %-40s" % (mark[f["verdict"]], f["guard"][:60]))
        print("          %s" % f["reason"])
    for note in result["notes"]:
        print("\nnote: %s" % note)

    inert_n = sum(1 for f in findings if f["verdict"] == INERT)
    armed_n = sum(1 for f in findings if f["verdict"] == ARMED)
    unknown_n = sum(1 for f in findings if f["verdict"] == UNKNOWN)
    print("\n%d guard(s) checked · %d armed · %d inert · %d unknown"
          % (len(findings), armed_n, inert_n, unknown_n))

    if inert_n:
        print("\nAt least one guard is not running. Run `git merge origin/%s` "
              "to pick up hooks you are missing, and `chmod +x` any hook "
              "file that is present but not executable."
              % (result["ref"].split("/", 1)[1] if result["ref"] else "dawn"))
    else:
        print("\nEvery guard skydas can see is armed.")

    sys.exit(code)


if __name__ == "__main__":
    main()
