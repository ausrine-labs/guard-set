# BRIEF — namo

## Why this exists

An agent session ends on a branch, in a worktree, in a container. The
agent says it's done. The commits are real and nobody can reach them —
not merged, sometimes not even pushed. Nothing about that state produces
an error. The session just ends, and the work sits there until a human
happens to notice it's missing.

namo is a guard for that specific handoff: the step between an agent
deciding it's done and the work actually landing somewhere reachable.
It runs at session end and refuses to call the session clean unless the
work is at least on its way home and one line says what it made.

## What problem it came from

The two-dawns problem: this repository ran with two divergent local
copies of its own trunk for days, because "on the trunk" and "on the
trunk that origin knows about" were being treated as the same thing.
They aren't. A local trunk ahead of its remote looks landed to whoever
is standing in that tree and is invisible to anyone fetching from
origin. namo's first guard exists because that happened once already.

The second guard exists because status reports accumulate where
artifacts should be. "All tests pass," "PR #14," "reviewed" — none of
that says what now exists on disk or on the web. A deny-list of
status-shaped phrasing sits in front of `made`, on purpose, because a
status report is what gets written when there's no artifact to point
at.

## What it deliberately does not do

- It does not merge. `land` pushes and opens a PR; merging stays a
  separate decision made by someone else.
- It does not judge the work. Reachable-and-named is a floor, not a
  review.
- It does not verify a URL beyond a HEAD request. That's a claim the
  page answers, not a claim it's right.
- It does not know your team's definition of "home." It knows the one
  branch you name with `--trunk`.

## Where it sits among the other guards

Third of a set, each watching a different way agent work goes missing:

- **claimcheck** checks whether an agent's claims about its own work
  are true.
- **sargas** notices an agent that went quiet and never came back.
- **namo** checks that the work that did happen actually landed
  somewhere, and that something says so.

None of the three trusts the agent's own account of itself. That's the
point of building three instead of one.

## Open questions

- **`pending` never expires.** A PR opened on day one and left open
  forever still reads as `pending`, not `STRANDED`, no matter how long
  it sits unreviewed. namo has no notion of a PR going stale — that's
  arguably sargas's job, not this tool's, but nothing wires them
  together yet.
- **The status-phrase list is a heuristic.** It will have false
  positives — a legitimate artifact name that happens to contain a
  word like "update" will get bounced and have to be reworded. It has
  not been tuned against a large corpus of real MADE.md lines, because
  none exists yet.
- **MADE.md has no merge strategy.** It's a single file, appended to by
  whoever runs `made`. Two shifts writing to it near-simultaneously —
  in parallel worktrees, say — can produce a git conflict on that one
  file with no special handling for it. Standard git conflict
  resolution applies; namo does nothing to make that easier.
