#!/bin/sh
# test_install.sh — prove install.sh behaves, from disposable checkouts.
#
# Every case here is one Map Room named as a must-fix on PR #44 head 6415dc9,
# plus the two failure modes the installer already claimed to handle. Each test
# builds a throwaway git repo, runs the real installer against it, and asserts
# on what actually landed on disk — not on what the script printed.
#
#   ./test_install.sh
#
# Exit 0 = every case passed. Exit 1 = at least one did not.
set -u

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
# This script travels. In the source tree the built package sits under dist/;
# in the shipped package $HERE *is* the package root. Assuming one layout means
# the customer's documented self-test fails on a cold clone — which is what
# shipped on 2026-09-08. Resolve both, and say which one we are in.
if [ -d "$HERE/dist/guard-set" ]; then
  PKG="$HERE/dist/guard-set"          # source tree, after ./package.sh
elif [ -d "$HERE/claimcheck" ] && [ -f "$HERE/install.sh" ]; then
  PKG="$HERE"                          # installed/cloned package root
else
  echo "test_install.sh: no package to test — run ./package.sh first (source tree),"
  echo "                or run this from inside an unpacked guard-set."
  exit 1
fi
WORK="${TMPDIR:-/tmp}/guardset-install-tests.$$"
PASS=0
FAILED=""

ok()   { PASS=$((PASS+1)); printf 'PASS  %s\n' "$1"; }
bad()  { FAILED="$FAILED
  - $1${2:+  [$2]}"; printf 'FAIL  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; }
check(){ if [ "$2" = "1" ]; then ok "$1"; else bad "$1" "${3:-}"; fi; }

# a fresh repo with the package already inside it, on branch $1
newrepo() {
  d="$WORK/r$$-$(date +%s%N 2>/dev/null || date +%s)-$RANDOM"
  mkdir -p "$d" && cd "$d" || return 1
  git init -q -b "${1:-main}" . 2>/dev/null || { git init -q .; git checkout -q -b "${1:-main}" 2>/dev/null; }
  git -c user.email=t@t.t -c user.name=t commit -q --allow-empty -m init
  cp -R "$PKG" ./guard-set
  printf '%s' "$d"
}

mkdir -p "$WORK"
echo "guard-set installer tests"
echo

# ── 1. non-main trunk (the defect: --trunk was hard-coded to main) ───────
d=$(newrepo dawn); cd "$d"
out=$(sh ./guard-set/install.sh 2>&1); code=$?
check "non-main repo: installer succeeds on a 'dawn' trunk" \
  "$([ $code -eq 0 ] && echo 1 || echo 0)" "exit $code"
check "non-main repo: resolves trunk to dawn, not main" \
  "$(printf '%s' "$out" | grep -q 'trunk resolved to: dawn' && echo 1 || echo 0)" \
  "$(printf '%s' "$out" | grep 'trunk resolved' || echo 'no trunk line')"
check "non-main repo: namo hook carries --trunk dawn" \
  "$(grep -q -- '--trunk dawn' .claude/settings.json 2>/dev/null && echo 1 || echo 0)" \
  "$(grep -o -- '--trunk [a-z]*' .claude/settings.json 2>/dev/null | head -1)"
check "non-main repo: no reference to a main branch that does not exist" \
  "$(grep -q -- '--trunk main' .claude/settings.json 2>/dev/null && echo 0 || echo 1)"

# explicit override still wins
d=$(newrepo dawn); cd "$d"
sh ./guard-set/install.sh --trunk custom-trunk >/dev/null 2>&1
check "explicit --trunk overrides detection" \
  "$(grep -q -- '--trunk custom-trunk' .claude/settings.json 2>/dev/null && echo 1 || echo 0)"

# ── 1b. feature-only branch, nothing authoritative: must REFUSE ──────────
# The defect: falling back to the current HEAD branch silently wired namo to
# whatever branch you happened to be standing on. Run once from a feature
# branch and the guard points at a ref that vanishes when its PR merges — a
# guard aimed at a dead branch fails open, which is what skydas exists to name.
d=$(newrepo feature/some-work); cd "$d"
git branch -D main master dawn trunk >/dev/null 2>&1 || true
git config --unset init.defaultBranch >/dev/null 2>&1 || true
git remote remove origin >/dev/null 2>&1 || true
branches=$(git branch --format='%(refname:short)' | tr '\n' ' ')
out=$(sh ./guard-set/install.sh 2>&1); code=$?
check "feature-only branch: installer refuses instead of guessing" \
  "$([ $code -ne 0 ] && echo 1 || echo 0)" "exit $code; branches: $branches"
check "feature-only branch: does not wire the feature branch as trunk" \
  "$(grep -q 'some-work' .claude/settings.json 2>/dev/null && echo 0 || echo 1)" \
  "$(grep -o -- '--trunk [^\"]*' .claude/settings.json 2>/dev/null | head -1)"
check "feature-only branch: writes no settings.json at all" \
  "$([ ! -f .claude/settings.json ] && echo 1 || echo 0)"
check "feature-only branch: error names --trunk as the fix" \
  "$(printf '%s' "$out" | grep -q -- '--trunk' && echo 1 || echo 0)" \
  "$(printf '%s' "$out" | tail -3 | tr '\n' ' ')"
# and the same repo installs cleanly once told
out2=$(sh ./guard-set/install.sh --trunk feature/some-work 2>&1); code2=$?
check "feature-only branch: succeeds when trunk is stated explicitly" \
  "$([ $code2 -eq 0 ] && echo 1 || echo 0)" "exit $code2"

# ── 2. unrelated hooks and settings are preserved ────────────────────────
d=$(newrepo main); cd "$d"
mkdir -p .claude
cat > .claude/settings.json <<'JSON'
{
  "hooks": {
    "Stop": [ { "hooks": [ { "type": "command", "command": "echo pre-existing" } ] } ],
    "PreToolUse": [ { "matcher": "Bash", "hooks": [ { "type": "command", "command": "echo other" } ] } ]
  },
  "unrelatedKey": "preserve-me"
}
JSON
chmod 600 .claude/settings.json
sh ./guard-set/install.sh >/dev/null 2>&1
check "existing unrelated Stop hook survives" \
  "$(grep -q 'pre-existing' .claude/settings.json && echo 1 || echo 0)"
check "unrelated hook family (PreToolUse) survives" \
  "$(grep -q 'PreToolUse' .claude/settings.json && echo 1 || echo 0)"
check "unrelated top-level key survives" \
  "$(grep -q 'preserve-me' .claude/settings.json && echo 1 || echo 0)"
mode=$(ls -l .claude/settings.json | cut -c1-10)
check "file mode preserved across the atomic write (expect rw-------)" \
  "$([ "$mode" = "-rw-------" ] && echo 1 || echo 0)" "got $mode"
check "no temp file left behind" \
  "$([ -z "$(ls -A .claude/.settings.* 2>/dev/null)" ] && echo 1 || echo 0)"

# Atomicity, tested by its observable signature rather than its side effects.
# An in-place truncate-and-write keeps the same inode; a write-to-temp-then-
# rename replaces it. The earlier "mode preserved" check passed against a
# NON-atomic implementation too, because open(path,"w") leaves an existing
# file's mode alone — it proved nothing. This distinguishes them.
d=$(newrepo main); cd "$d"
mkdir -p .claude
printf '{"unrelatedKey":"x"}\n' > .claude/settings.json
ino_before=$(ls -i .claude/settings.json | awk '{print $1}')
sh ./guard-set/install.sh >/dev/null 2>&1
ino_after=$(ls -i .claude/settings.json | awk '{print $1}')
check "settings.json is replaced atomically (inode changes, not truncated in place)" \
  "$([ "$ino_before" != "$ino_after" ] && echo 1 || echo 0)" \
  "inode $ino_before -> $ino_after"

# ── 3. partial install is repaired, not reported complete ────────────────
# The defect: MARK checked claimcheck only, so claimcheck-without-namo read as
# "already wired" and stayed broken forever.
d=$(newrepo main); cd "$d"
mkdir -p .claude
cat > .claude/settings.json <<'JSON'
{
  "hooks": {
    "Stop": [ { "hooks": [
      { "type": "command", "command": "python3 guard-set/claimcheck/claimcheck.py --file \"$JOURNAL\" --repo ." }
    ] } ]
  }
}
JSON
out=$(sh ./guard-set/install.sh 2>&1)
check "partial install: namo is detected as missing and added" \
  "$(grep -q 'guard-set/namo/namo.py' .claude/settings.json && echo 1 || echo 0)" \
  "$(printf '%s' "$out" | grep -E 'added|already' | head -1)"
check "partial install: does NOT falsely report already-complete" \
  "$(printf '%s' "$out" | grep -q 'already complete' && echo 0 || echo 1)"
n=$(grep -c 'claimcheck.py' .claude/settings.json)
check "partial install: claimcheck not duplicated while repairing" \
  "$([ "$n" -eq 1 ] && echo 1 || echo 0)" "claimcheck appears $n times"

# ── 4. idempotence ───────────────────────────────────────────────────────
d=$(newrepo main); cd "$d"
sh ./guard-set/install.sh >/dev/null 2>&1
first=$(cat .claude/settings.json)
out=$(sh ./guard-set/install.sh 2>&1)
second=$(cat .claude/settings.json)
check "second run reports already-complete" \
  "$(printf '%s' "$out" | grep -q 'already complete' && echo 1 || echo 0)" \
  "$(printf '%s' "$out" | grep -E 'added|already' | head -1)"
check "second run leaves settings.json byte-identical" \
  "$([ "$first" = "$second" ] && echo 1 || echo 0)"
c=$(grep -c 'claimcheck.py' .claude/settings.json)
m=$(grep -c 'namo.py' .claude/settings.json)
check "no duplicate hook entries after two runs" \
  "$([ "$c" -eq 1 ] && [ "$m" -eq 1 ] && echo 1 || echo 0)" "claimcheck=$c namo=$m"

# ── 5. malformed JSON leaves the original file untouched ─────────────────
d=$(newrepo main); cd "$d"
mkdir -p .claude
printf '{ "hooks": { "Stop": [ ,,, BROKEN\n' > .claude/settings.json
before=$(cat .claude/settings.json)
out=$(sh ./guard-set/install.sh 2>&1); code=$?
after=$(cat .claude/settings.json)
check "malformed JSON: installer exits non-zero" \
  "$([ $code -ne 0 ] && echo 1 || echo 0)" "exit $code"
check "malformed JSON: original file left byte-identical" \
  "$([ "$before" = "$after" ] && echo 1 || echo 0)"
check "malformed JSON: says it is refusing, not that it succeeded" \
  "$(printf '%s' "$out" | grep -qi 'refusing' && echo 1 || echo 0)" \
  "$(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
check "malformed JSON: no temp file left behind" \
  "$([ -z "$(ls -A .claude/.settings.* 2>/dev/null)" ] && echo 1 || echo 0)"

# ── 6. refuses outside a git repository ──────────────────────────────────
d="$WORK/not-a-repo"; mkdir -p "$d"; cd "$d"
out=$(sh "$PKG/install.sh" 2>&1); code=$?
check "outside a git repo: refuses with a non-zero exit" \
  "$([ $code -ne 0 ] && echo 1 || echo 0)" "exit $code"

cd /
rm -rf "$WORK"

echo
if [ -n "$FAILED" ]; then
  printf 'RESULT: %d passed, FAILURES:%s\n' "$PASS" "$FAILED"
  exit 1
fi
printf 'RESULT: %d checks — ALL PASS\n' "$PASS"
