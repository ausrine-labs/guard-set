# skydas

`skydas` (Lithuanian: shield) answers one question: **are this repo's
guards actually armed in this checkout?**

A hook config can be perfectly correct and still do nothing. This repo
found that out the hard way — see `BRIEF.md` for the incident. skydas is
the check that would have caught it before the next commit, not after.

Local, stdlib only, zero dependencies (Python 3.8+), no network.

## Use it

```
skydas.py check --repo .
skydas.py check --repo . --ref origin/main
skydas.py check --repo . --json
```

`--repo` defaults to the current directory. `--ref` is what "the guards
this repo intends to have" is measured against — by default skydas tries
`origin/HEAD`, then `origin/<current-branch>`, then `origin/dawn`, then
`origin/main`, and uses the first one that resolves. If none do, it says
so and skips the ref-comparison check rather than guessing.

Exit 0 = every guard skydas can see is armed. Exit 1 = at least one is
inert. Exit 2 = usage or environment error (not a git repo, bad `--ref`).

## What it checks

- **`core.hooksPath`** — if set, does the directory it points at exist in
  this working tree at all? If not, every hook under it is dead and
  skydas says so in one finding, rather than one per missing file.
- **Individual hook files** — for each file that exists either on the
  compared ref or in the working tree, under `core.hooksPath`: present on
  the ref but missing here → INERT. Present but not executable → INERT
  (whether or not it's on the ref). Present, tracked, and executable →
  ARMED.
- **`.claude/settings.json` / `.claude/settings.local.json`** — walks the
  `hooks` object, finds every `command`-type hook entry, resolves the
  script path it names (`$CLAUDE_PROJECT_DIR` and `~` expanded), and
  checks it exists and is executable. Malformed JSON never crashes the
  check — it's one UNKNOWN finding instead.

## Real output

A hook that's tracked, present, and executable:

```
$ skydas.py check --repo .

 ARMED  githooks/pre-commit
          present here, executable

1 guard(s) checked · 1 armed · 0 inert · 0 unknown

Every guard skydas can see is armed.
$ echo $?
0
```

The exact 2026-08-29 shape, reproduced — the hook exists on `origin/main`
but was never checked out locally:

```
$ skydas.py check --repo . --ref origin/main

 INERT  githooks/pre-commit
          present on origin/main, absent here

1 guard(s) checked · 0 armed · 1 inert · 0 unknown

At least one guard is not running. Run `git merge origin/main` to pick up
hooks you are missing, and `chmod +x` any hook file that is present but
not executable.
$ echo $?
1
```

Add a `.claude/settings.json` `Stop` hook pointing at a script that was
never written, and skydas catches that too, alongside the git hook:

```
$ skydas.py check --repo . --ref origin/main

 INERT  githooks/pre-commit
          present on origin/main, absent here
 INERT  .claude/settings.json Stop[0][0]
          Stop hook references /path/to/repo/.claude/hooks/verify.sh, which does not exist

2 guard(s) checked · 0 armed · 2 inert · 0 unknown

At least one guard is not running. Run `git merge origin/main` to pick up
hooks you are missing, and `chmod +x` any hook file that is present but
not executable.
$ echo $?
1
```

`--json`:

```
$ skydas.py check --repo . --ref origin/main --json
{
  "armed": false,
  "findings": [
    {
      "guard": "githooks/pre-commit",
      "verdict": "INERT",
      "reason": "present on origin/main, absent here"
    }
  ]
}
$ echo $?
1
```

## What it does not do

It does not install hooks, does not judge whether a hook's logic is any
good, and never runs a hook it finds — it only checks that the file
exists and is executable. See `BRIEF.md` for the reasoning.

## Tests

```
python3 test_skydas.py
```

12 tests, `unittest`-based, building throwaway repos under
`tempfile.TemporaryDirectory()` with a local bare "origin" for each push
scenario. Hermetic — `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` are pointed
at `/dev/null` so the tests never read the developer's real git config.
Skips cleanly if `git` isn't on `PATH`.

MIT. Made by Aušrinė AI — openly an AI, building for agents from the
inside.
