#!/bin/sh
# install.sh — wire the Guard Set into a Claude Code repo, one command.
#
# The README used to hand you a JSON snippet to edit by hand. That is the
# friction fast-growing agent tools avoid: meet the user where they are, do
# not make them hand-assemble config. This does what the snippet described,
# and tells you exactly what it changed.
#
# What it does:
#   1. Confirms it is running inside a git repository.
#   2. Resolves your trunk branch — it does NOT assume `main`, and it will
#      not guess from the branch you are standing on. If nothing
#      authoritative answers, it refuses and asks for --trunk.
#   3. Copies the guard-set/ directory into the repo root if it is not there.
#   4. Runs every guard's own test suite; refuses to wire anything if one fails.
#   5. Adds each missing hook command to .claude/settings.json, independently,
#      writing atomically so an interruption cannot truncate your config.
#
# Safe to re-run. It adds only what is missing, so a half-finished install from
# an earlier run gets repaired rather than reported as complete.
#
# HONEST ABOUT FORMATTING: this rewrites .claude/settings.json through a JSON
# parser. Your data is preserved exactly; your *formatting* is not — indentation
# is normalised to two spaces and blank-line layout is not retained. Key order
# within objects is preserved. If you hand-format that file, read the diff
# before you commit it. The file's permission mode is preserved.
#
# Usage:
#   ./guard-set/install.sh                  # resolve trunk automatically
#   ./guard-set/install.sh --trunk dawn     # state it explicitly
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

TRUNK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --trunk) shift; TRUNK="${1:-}" ;;
    --trunk=*) TRUNK="${1#--trunk=}" ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift || true
done

REPO=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "install.sh: not inside a git repository. cd into the repo you want" >&2
  echo "guarded, then run this script again." >&2
  exit 1
}

# ── trunk resolution ────────────────────────────────────────────────────
# Hard-coding `main` was wrong: this lab's own trunk is `dawn`, so namo would
# have been checking a branch that does not exist. Ask git, in order of
# authority, and refuse to guess if nothing answers.
if [ -z "$TRUNK" ]; then
  TRUNK=$(git -C "$REPO" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||') || TRUNK=""
fi
if [ -z "$TRUNK" ]; then
  CAND=$(git -C "$REPO" config --get init.defaultBranch 2>/dev/null) || CAND=""
  if [ -n "$CAND" ] && git -C "$REPO" show-ref --verify --quiet "refs/heads/$CAND"; then
    TRUNK="$CAND"
  fi
fi
if [ -z "$TRUNK" ]; then
  for cand in main master dawn trunk; do
    if git -C "$REPO" show-ref --verify --quiet "refs/heads/$cand"; then
      TRUNK="$cand"; break
    fi
  done
fi
# Deliberately NO fallback to the current branch. Falling back to HEAD looks
# helpful and is the worst option available: run this once from a feature
# branch in a repo with no origin/HEAD and no conventional trunk, and namo is
# wired forever to a branch that will be deleted after its PR merges. A guard
# pointed at a branch that no longer exists fails open silently — the exact
# failure skydas was built to name. Refusing is the safe answer.
if [ -z "$TRUNK" ]; then
  echo "install.sh: could not determine this repository's trunk branch." >&2
  echo "" >&2
  echo "Nothing authoritative answered: there is no origin/HEAD, no usable" >&2
  echo "init.defaultBranch, and no branch named main, master, dawn or trunk." >&2
  echo "" >&2
  echo "This will NOT guess from the branch you happen to be standing on —" >&2
  echo "that would wire the guard to a feature branch that disappears when" >&2
  echo "its PR merges. State the trunk explicitly instead:" >&2
  echo "" >&2
  echo "    $0 --trunk <branch>" >&2
  exit 1
fi

echo "installing the Guard Set into: $REPO"
echo "  trunk resolved to: $TRUNK"

# ── 1. copy the guards in ───────────────────────────────────────────────
DEST="$REPO/guard-set"
case "$HERE" in
  "$DEST"|"$DEST"/*)
    echo "  guard-set/ already lives in this repo — leaving files in place"
    ;;
  *)
    mkdir -p "$DEST"
    for g in claimcheck sargas namo skydas; do
      mkdir -p "$DEST/$g"
      cp "$HERE/$g/"*.py "$DEST/$g/" 2>/dev/null || true
      for d in "$HERE/$g/"*.md; do [ -e "$d" ] && cp "$d" "$DEST/$g/"; done
    done
    cp "$HERE/README.md" "$HERE/LICENSE" "$DEST/" 2>/dev/null || true
    echo "  copied claimcheck, sargas, namo, skydas into guard-set/"
    ;;
esac

# ── 2. tests before wiring ──────────────────────────────────────────────
# A guard that fails its own suite must not end up in your Stop hook.
fail=0
for g in claimcheck sargas namo skydas; do
  if ( cd "$DEST/$g" && python3 "test_$g.py" >/dev/null 2>&1 ); then
    echo "  $g: tests pass"
  else
    echo "  $g: TESTS FAILED — not wiring this guard" >&2
    fail=1
  fi
done
[ "$fail" = 0 ] || {
  echo "install.sh: one or more guards failed their own tests. Fix or" >&2
  echo "re-download before wiring anything into your hooks." >&2
  exit 1
}

# ── 3. merge hooks, atomically, each command checked independently ──────
SETTINGS="$REPO/.claude/settings.json"
mkdir -p "$REPO/.claude"
python3 - "$SETTINGS" "$TRUNK" <<'PY'
import json, os, stat, sys, tempfile

path, trunk = sys.argv[1], sys.argv[2]

# Each required command, with the substring that identifies it. Checking each
# one on its own matters: an earlier version looked only for claimcheck, so a
# repo with claimcheck but no namo was reported "already wired" and left
# half-installed forever.
REQUIRED = [
    ("guard-set/claimcheck/claimcheck.py",
     'python3 guard-set/claimcheck/claimcheck.py --file "$JOURNAL" --repo .'),
    ("guard-set/namo/namo.py",
     'python3 guard-set/namo/namo.py check --repo . --trunk ' + trunk),
]

cfg, mode = {}, None
if os.path.exists(path):
    mode = stat.S_IMODE(os.stat(path).st_mode)
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if text:
        try:
            cfg = json.loads(text)
        except json.JSONDecodeError as e:
            # Refuse rather than clobber. A malformed settings.json is
            # somebody's work in progress, not ours to overwrite.
            sys.stderr.write("  .claude/settings.json is not valid JSON (%s).\n" % e)
            sys.stderr.write("  Refusing to touch it. Fix the file, then re-run.\n")
            sys.exit(3)

if not isinstance(cfg, dict):
    sys.stderr.write("  .claude/settings.json is valid JSON but not an object.\n")
    sys.stderr.write("  Refusing to touch it.\n")
    sys.exit(3)

hooks = cfg.setdefault("hooks", {})
if not isinstance(hooks, dict):
    sys.stderr.write("  'hooks' is not an object. Refusing to touch it.\n")
    sys.exit(3)
stop = hooks.setdefault("Stop", [])
if not isinstance(stop, list):
    sys.stderr.write("  'hooks.Stop' is not a list. Refusing to touch it.\n")
    sys.exit(3)


def present(mark):
    for group in stop:
        if not isinstance(group, dict):
            continue
        for h in group.get("hooks", []) or []:
            if isinstance(h, dict) and mark in (h.get("command") or ""):
                return True
    return False


missing = [(mark, cmd) for mark, cmd in REQUIRED if not present(mark)]

if not missing:
    print("  Stop hook already complete — nothing to add")
    sys.exit(0)

stop.append({"hooks": [{"type": "command", "command": cmd} for _m, cmd in missing]})

# Atomic write in the SAME directory, so an interruption cannot leave a
# truncated settings.json behind. Preserve the original file mode.
d = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(dir=d, prefix=".settings.", suffix=".json")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)
except BaseException:
    if os.path.exists(tmp):
        os.unlink(tmp)
    raise

added = ", ".join(m.split("/")[1] for m, _c in missing)
print("  added to the Stop hook: %s" % added)
PY

echo
echo "done. sargas and skydas are not wired to a hook — they answer a"
echo "different question (did the routine come back; are the guards armed"
echo "in THIS checkout) and belong on a schedule you control. See README.md."
echo
echo "what changed:"
echo "  guard-set/            (the four guards + this script)"
echo "  .claude/settings.json (hook entries added; the file is rewritten by a"
echo "                         JSON parser — data preserved, layout normalised"
echo "                         to 2-space indent, file mode preserved)"
echo
echo "verify: git diff .claude/settings.json"
