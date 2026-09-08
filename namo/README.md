# namo

An agent finishes a job on a branch, in a worktree, in a container, and
says so. The work is real. Nobody else can reach it. The next morning
someone opens the repository, sees yesterday, and asks whether anything
ran at all.

namo is a guard for that handoff — the moment between "done" and
"landed." Run it at the end of a session. It checks two things and
refuses to let the session end clean until both hold: the work is
reachable from the trunk (or at least on its way there), and one line
somewhere says what now exists.

## Install

One file, Python 3, no dependencies.

```
cp namo.py ~/bin/namo.py   # or wherever you keep scripts
python3 namo.py --version
```

## The four commands

**`check`** — run both guards. Exit 0 if the session is clean to end,
exit 1 if it refuses.

```
python3 namo.py check --repo ~/project --shift build
```

**`land`** — push the current branch to origin and open a PR against
the trunk. Never merges anything itself.

```
python3 namo.py land --repo ~/project
```

**`made`** — write the one line saying what now exists.

```
python3 namo.py made "the retry queue" --path src/queue.py --shift build
python3 namo.py made "the docs site" --url https://example.com/docs --shift build
```

**`made-nothing`** — write the one line saying nothing did.

```
python3 namo.py made-nothing --why "spent the night on a dead end in the parser" --shift build
```

## The three landing states

| state | means | does `check` refuse? |
|---|---|---|
| `home` | every commit is reachable from `origin/<trunk>` | no |
| `pending` | pushed to origin, PR open, not yet merged | no — it's in review |
| `STRANDED` | commits exist on no remote, only in this working tree | yes |

"Home" is `origin/<trunk>`, not your local trunk. A local trunk that's
ahead of its remote looks landed to you and is invisible to everyone
else — that gap is exactly what `home` is measured against.

## What MADE.md looks like

`made` and `made-nothing` write to a single `MADE.md` in the repo root,
newest day first:

```
# MADE — what exists that did not exist yesterday

## 2026-08-31
- build · the retry queue · src/queue.py

## 2026-08-30
- build · made nothing — spent the night on a dead end in the parser
- reflect · the shift-handoff doc · docs/handoff.md
```

Each line names a thing and where it is, or says plainly that nothing
was made. It does not say "all tests pass" or "PR #14" — see below.

## Wire it as a hook

A Claude Code `Stop` hook that refuses to let a session end without
both guards passing:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/namo.py check --repo /path/to/project --shift build"
          }
        ]
      }
    ]
  }
}
```

Exit 1 is what sends it back.

## What it does not do

- It never merges anything. `land` opens a PR; a human or a separate
  process decides when it goes in.
- It cannot tell you whether the work is good. It only tells you
  whether it is reachable and named.
- Checking a `--url` is one HTTP HEAD request. A 200 means the URL
  answered, not that the page behind it is correct.
- It only knows "home" as the trunk you name with `--trunk` (default
  `dawn`, or `$NAMO_TRUNK`). It has no idea what your team means by
  landed beyond that.

## Tested

23 tests in `test_namo.py`, all passing. Each one builds a real
throwaway git repository and puts it in the exact state a shift ends
in — stranded, pushed-but-unmerged, merged, no remote at all, recorded,
unrecorded — rather than mocking git.

`mutate.sh` injects 10 deliberate breakages into `namo.py`, one at a
time — a stranded-work check that always passes, a status phrase that
slips through, a path check that never checks — and reruns the suite
against each. All 10 are caught.
