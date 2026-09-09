#!/usr/bin/env python3
"""guard-mcp — the Guard Set as an MCP server, so an agent can check another
agent's work without a human in the loop.

Why this exists rather than just the repo: a GitHub repo is something a human
finds, reads, and installs. An agent does not browse. It queries a registry,
reads a capability schema, and calls a tool. Same four guards, in the shape
the other side of the market can actually reach.

Four tools, and each answers a different question about an agent's own claims:

  verify_claim        is what this agent just told you true?
  check_routine_ran   did the scheduled job come back, or only start?
  check_work_landed   did the work reach somewhere a human will find it?
  check_guards_armed  are these checks even switched on in this checkout?

The fourth is the one nobody else ships, and it is the one that matters most:
a hook that is configured but not checked out fails open, silently, and looks
exactly like protection.

No dependencies. Standard library only, JSON-RPC 2.0 over stdio. Runs the
same guard scripts the repo ships, so there is one implementation and no
second copy to drift.

    python3 guard_mcp.py

MIT. Made by an AI agent, openly and by design.
"""

import json
import os
import subprocess
import sys

VERSION = "0.1.0"
PROTOCOL = "2024-11-05"

# The guards live one directory up. Resolve once, honestly, and fail loudly
# rather than silently degrading if they are missing — a verification tool
# that cannot find its own verifiers must not report success.
HERE = os.path.dirname(os.path.abspath(__file__))

# Two layouts are both legitimate and both happen:
#   shipped package   guard-set/{mcp,claimcheck,sargas,...}   -> guards are siblings
#   source tree       goods/{guard-set/mcp, claimcheck, ...}  -> guards are one level up
# Resolve against every candidate rather than assuming one. GUARD_SET_HOME
# overrides, for anyone who installs the guards somewhere else entirely.
_ROOTS = [
    os.environ.get("GUARD_SET_HOME"),
    os.path.dirname(HERE),                     # shipped: guard-set/
    os.path.dirname(os.path.dirname(HERE)),    # source:  goods/
]


def guard_path(name, script):
    """Absolute path to a guard, or the best-guess path if it is truly absent
    (so the error message names somewhere real)."""
    tried = []
    for root in _ROOTS:
        if not root:
            continue
        p = os.path.join(root, name, script)
        tried.append(p)
        if os.path.exists(p):
            return p
    return tried[0] if tried else os.path.join(name, script)


def run(cmd, cwd=None, timeout=120):
    """Run a guard. Returns (exit_code, combined_output)."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        out = ((p.stdout or "") + ("\n" + p.stderr if p.stderr else "")).strip()
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ds" % timeout
    except FileNotFoundError as e:
        return 127, "guard not found: %s" % e
    except Exception as e:  # noqa: BLE001 - report, never swallow
        return 1, str(e)


TOOLS = [
    {
        "name": "verify_claim",
        "description": (
            "Check whether an agent's report about its own work is true. Give it "
            "the agent's message; it finds the checkable claims (commits, pushes, "
            "clean trees, tests, files, PRs, URLs) and checks each against the "
            "repository, the remote and the test runner. Returns one of three "
            "verdicts per claim: verified, FALSE, or UNVERIFIABLE. The third "
            "matters: a checker that cannot see must not answer 'false', or it "
            "accuses honest agents whose work is safely on a remote it could not "
            "reach. Claims that are not mechanically checkable are skipped and "
            "counted, so a low check-count is itself the signal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "The agent's report or message to check."},
                "repo": {"type": "string",
                         "description": "Absolute path to the repository the claims are about."},
                "test_cmd": {"type": "string",
                             "description": "Optional command that runs the tests, if the report claims they pass."},
            },
            "required": ["text", "repo"],
        },
    },
    {
        "name": "check_routine_ran",
        "description": (
            "Ask whether a scheduled routine actually completed, rather than "
            "merely being scheduled. A job that is enabled, a job that ran and "
            "did nothing, and a job that started and hung all look identical "
            "from the outside. This reads completion stamps and reports the last "
            "time the named routine genuinely finished."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Absolute path to the repository."},
                "name": {"type": "string", "description": "Routine name, e.g. build, reflect, watch."},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "check_work_landed",
        "description": (
            "Ask whether an agent's work reached somewhere a human will actually "
            "find it, or is stranded on a branch nobody opens. Work that has not "
            "landed where the operator looks has not been delivered, however good "
            "it is. Checks the trunk you name — it does not assume 'main'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Absolute path to the repository."},
                "trunk": {"type": "string",
                          "description": "The trunk branch name. Required — this does not guess."},
            },
            "required": ["repo", "trunk"],
        },
    },
    {
        "name": "check_guards_armed",
        "description": (
            "Ask whether the guards are actually switched on in THIS checkout. A "
            "hook that is configured in a settings file but not present in the "
            "working tree fails open, silently, and is indistinguishable from "
            "protection. This is the check that catches a disarmed checker, and "
            "it is the one most verification tooling does not have — including, "
            "by definition, itself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Absolute path to the repository."},
            },
            "required": ["repo"],
        },
    },
]


def call_tool(name, args):
    repo = args.get("repo") or "."
    if not os.path.isdir(repo):
        return "no such repository path: %s" % repo

    if name == "verify_claim":
        script = guard_path("claimcheck", "claimcheck.py")
        if not os.path.exists(script):
            return "claimcheck is not installed next to this server (%s)" % script
        cmd = [sys.executable, script, args.get("text", ""), "--repo", repo, "--json"]
        if args.get("test_cmd"):
            cmd += ["--test-cmd", args["test_cmd"]]
        _code, out = run(cmd, cwd=repo)
        return out or "claimcheck produced no output"

    if name == "check_routine_ran":
        script = guard_path("sargas", "sargas.py")
        if not os.path.exists(script):
            return "sargas is not installed next to this server (%s)" % script
        cmd = [sys.executable, script, "--repo", repo]
        _code, out = run(cmd, cwd=repo)
        want = args.get("name")
        if want and out:
            lines = [l for l in out.splitlines() if want in l]
            return "\n".join(lines) if lines else out
        return out or "sargas produced no output"

    if name == "check_work_landed":
        script = guard_path("namo", "namo.py")
        if not os.path.exists(script):
            return "namo is not installed next to this server (%s)" % script
        cmd = [sys.executable, script, "check", "--repo", repo,
               "--trunk", args["trunk"]]
        code, out = run(cmd, cwd=repo)
        return "%s\n\n(exit %d — nonzero means the work has not landed)" % (out, code)

    if name == "check_guards_armed":
        script = guard_path("skydas", "skydas.py")
        if not os.path.exists(script):
            return "skydas is not installed next to this server (%s)" % script
        code, out = run([sys.executable, script, "check", "--repo", repo], cwd=repo)
        return "%s\n\n(exit %d — nonzero means a guard is configured but not armed)" % (out, code)

    return "unknown tool: %s" % name


def respond(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = req.get("method")
        rid = req.get("id")

        if method == "initialize":
            respond(rid, {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "guard-set", "version": VERSION},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            respond(rid, {"tools": TOOLS})
        elif method == "tools/call":
            params = req.get("params") or {}
            try:
                text = call_tool(params.get("name"), params.get("arguments") or {})
                respond(rid, {"content": [{"type": "text", "text": text}]})
            except Exception as e:  # noqa: BLE001 - surface, never hide
                respond(rid, {"content": [{"type": "text",
                                           "text": "guard failed: %s" % e}],
                              "isError": True})
        elif rid is not None:
            respond(rid, error={"code": -32601, "message": "unknown method: %s" % method})


if __name__ == "__main__":
    main()
