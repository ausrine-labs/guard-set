# BRIEF — skydas

## The friction it comes from

On 2026-08-29 this repo added a `pre-commit` hook at `githooks/pre-commit`
that refuses agent commits in the shared checkout, and set
`core.hooksPath=githooks` so git would run it. The change was merged to
`origin/dawn`.

But the local `dawn` branch, in the shared checkout, was 13 commits behind
origin and had never checked that file out. `core.hooksPath` pointed at a
directory that existed on disk — but the `pre-commit` file inside it did
not, because the working tree simply hadn't caught up.

Git does not warn about this. There is no error, no message, nothing in
`git status`. It just quietly does not run the hook. It fails open.

For two days every agent commit made in that checkout was unguarded. On
2026-08-31 two agent sessions collided there: one session's `git add -A`
swept up another session's in-progress file and folded it into its own
commit, destroying the record of who actually wrote it — exactly the kind
of thing the missing hook existed to stop.

## The general lesson

A guard that lives in the repo is only armed on a branch that actually
contains it. Check out an older branch, or a branch cut before the guard
was added, and the guard is silently gone — not broken, not warning,
just absent. Nothing tells you.

This isn't specific to one pre-commit hook. Anything that points at a
file path as its enforcement mechanism has the same hole:
`core.hooksPath`, `.claude/settings.json` hook commands, any config that
says "run this script" rather than carrying the check inline. The config
can be present and correct while the file it names is not there.

## What skydas does

Answers one question: **for this checkout, right now, is every guard
this repo intends to have actually armed?**

It compares the working tree against a ref (default: whatever `origin`
considers current) for guards that are supposed to exist, and reports,
per guard: **ARMED**, **INERT** (with the reason), or **UNKNOWN** (it
couldn't tell). It checks `core.hooksPath` and the hooks under it, and
Claude Code's `.claude/settings.json` (and `.settings.local.json`) hook
commands.

## What it deliberately does NOT do

- **It does not install hooks.** It does not write `githooks/pre-commit`
  or wire up `core.hooksPath` for you. It only reports on what's already
  configured.
- **It does not judge whether a hook is any good.** A hook that exists,
  is executable, and always exits 0 reads as ARMED. skydas checks that
  the guard would *run*, not that it *guards* anything. Whether the
  logic inside is worth having is a code review question, not this
  tool's.
- **It does not run the hooks.** It never executes `pre-commit` or any
  script it finds — only stats the file and checks the executable bit.
  Running someone's hook as a side effect of a status check would be its
  own hazard.
- **It is not a general git-hooks manager.** No install, no uninstall, no
  template. One read-only check, one exit code.

## Done when

`skydas.py check` on a repo with the 2026-08-29 shape (hook merged to
origin, absent from the local checkout) reports INERT and exits 1; on a
repo where the hook is present, tracked, and executable, it reports ARMED
and exits 0. Both are covered by `test_skydas.py`.
