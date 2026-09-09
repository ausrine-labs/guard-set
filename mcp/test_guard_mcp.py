#!/usr/bin/env python3
"""test_guard_mcp.py — prove the MCP server behaves, by speaking real JSON-RPC
to it over stdio, exactly as an agent client would.

Nothing here is mocked. Each test starts the actual server as a subprocess,
completes the handshake, calls a tool, and asserts on what comes back. A test
that exercises a stub proves the stub.

The disposable-repo tests build a throwaway git repository, commit into it,
and check the server's verdict against a fact we constructed and therefore
know. That is the only honest way to test a verifier: you must already know
the answer.

    python3 test_guard_mcp.py

Exit 0 = all passed. Exit 1 = at least one did not.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "guard_mcp.py")

PASSED = 0
FAILED = []


def check(name, ok, detail=""):
    global PASSED
    if ok:
        PASSED += 1
        print("PASS  %s" % name)
    else:
        FAILED.append(name)
        print("FAIL  %s" % name)
        if detail:
            print("        %s" % str(detail)[:200])


def talk(messages, timeout=180):
    """Send JSON-RPC messages to a fresh server, return parsed responses."""
    payload = "".join(json.dumps(m) + "\n" for m in messages)
    p = subprocess.run([sys.executable, SERVER], input=payload,
                       capture_output=True, text=True, timeout=timeout)
    out = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def call(tool, args):
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": tool, "arguments": args}},
    ]
    res = talk(msgs)
    for r in res:
        if r.get("id") == 2:
            content = (r.get("result") or {}).get("content") or []
            return content[0]["text"] if content else ""
    return ""


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args),
                          capture_output=True, text=True)


def make_repo(tmp):
    """A real git repo with one real commit, so we KNOW the ground truth."""
    d = tempfile.mkdtemp(dir=tmp)
    git(d, "init", "-q", "-b", "trunk")
    open(os.path.join(d, "a.txt"), "w").write("one\n")
    git(d, "add", "a.txt")
    git(d, "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-q", "-m", "first")
    sha = git(d, "log", "--format=%h", "-1").stdout.strip()
    return d, sha


def main():
    print("guard-mcp server tests\n")

    # ── protocol ─────────────────────────────────────────────────────────
    res = talk([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
    init = next((r for r in res if r.get("id") == 1), None)
    check("initialize returns a valid JSON-RPC result",
          init is not None and "result" in init, init)
    check("handshake advertises the server by name",
          init and init["result"]["serverInfo"]["name"] == "guard-set",
          init["result"]["serverInfo"] if init else "")
    check("handshake declares tool capability",
          init and "tools" in init["result"]["capabilities"])

    res = talk([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    listing = next((r for r in res if r.get("id") == 2), None)
    tools = (listing.get("result") or {}).get("tools", []) if listing else []
    names = sorted(t["name"] for t in tools)
    check("exposes exactly the four guards",
          names == ["check_guards_armed", "check_routine_ran",
                    "check_work_landed", "verify_claim"], names)
    check("every tool carries a description an agent can select on",
          all(len(t.get("description", "")) > 80 for t in tools),
          [(t["name"], len(t.get("description", ""))) for t in tools])
    check("every tool declares a JSON schema with required fields",
          all(t["inputSchema"]["type"] == "object" and t["inputSchema"].get("required")
              for t in tools))
    check("check_work_landed requires an explicit trunk and does not guess",
          "trunk" in next(t for t in tools if t["name"] == "check_work_landed")["inputSchema"]["required"])

    res = talk([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 9, "method": "nonsense/method"}])
    err = next((r for r in res if r.get("id") == 9), None)
    check("unknown method returns a JSON-RPC error, not a crash",
          err is not None and "error" in err, err)

    # ── verdicts against constructed ground truth ────────────────────────
    tmp = tempfile.mkdtemp()
    try:
        repo, sha = make_repo(tmp)

        out = call("verify_claim", {"text": "committed as %s" % sha, "repo": repo})
        parsed = json.loads(out) if out.startswith("{") else {}
        verdicts = [c["verdict"] for c in parsed.get("claims", [])]
        check("a TRUE commit claim is verified",
              "verified" in verdicts, out[:200])

        out = call("verify_claim", {"text": "committed as deadbeef1234", "repo": repo})
        parsed = json.loads(out) if out.startswith("{") else {}
        verdicts = [c["verdict"] for c in parsed.get("claims", [])]
        check("a FALSE commit claim is caught",
              "FALSE" in verdicts, out[:200])
        check("a report containing a false claim is marked untrustworthy",
              parsed.get("trustworthy") is False, parsed.get("trustworthy"))

        # A dirty tree makes "the tree is clean" false — a fact we create.
        open(os.path.join(repo, "dirty.txt"), "w").write("uncommitted\n")
        out = call("verify_claim", {"text": "the tree is clean", "repo": repo})
        parsed = json.loads(out) if out.startswith("{") else {}
        check("'tree is clean' is caught as false when the tree is dirty",
              any(c["verdict"] == "FALSE" for c in parsed.get("claims", [])),
              out[:200])

        # ── refusal behaviour: the part that matters most ────────────────
        out = call("verify_claim", {"text": "committed as abc1234",
                                    "repo": os.path.join(tmp, "does-not-exist")})
        check("a nonexistent repo path is refused, not silently passed",
              "no such repository" in out.lower(), out[:200])

        out = call("check_work_landed", {"repo": repo, "trunk": "trunk"})
        check("check_work_landed runs and reports an exit code",
              "exit" in out.lower(), out[:200])

        out = call("check_guards_armed", {"repo": repo})
        check("check_guards_armed runs against a repo with no guards at all",
              len(out) > 0, out[:200])

        out = call("nonexistent_tool", {"repo": repo})
        check("an unknown tool name is reported, not silently ignored",
              "unknown tool" in out.lower(), out[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILED:
        print("RESULT: %d passed, FAILURES: %s" % (PASSED, "; ".join(FAILED)))
        return 1
    print("RESULT: %d checks — ALL PASS" % PASSED)
    return 0


if __name__ == "__main__":
    sys.exit(main())
