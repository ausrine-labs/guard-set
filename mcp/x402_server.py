#!/usr/bin/env python3
"""x402_server.py — the Guard Set, sold per call, to agents that pay their own way.

An agent with a wallet does not read a README, sign up, or wait for a human.
It calls a URL, gets HTTP 402 with machine-readable payment terms, pays in
USDC, and calls again. That is the whole transaction. This server is that.

    python3 x402_server.py --pay-to 0xYOURADDRESS --port 8402

Endpoints:

    GET  /.well-known/x402       free  — the price list, for discovery
    GET  /health                 free  — liveness
    POST /verify_claim           paid  — is what this agent told you true?
    POST /check_routine_ran      paid  — did the scheduled job come back?
    POST /check_work_landed      paid  — did the work reach a human?
    POST /check_guards_armed     paid  — are the checks even switched on?

Settlement is deliberately NOT implemented here. This server produces correct
402 challenges and verifies that an X-PAYMENT header is present and
well-formed, then serves. Putting it in front of a real facilitator
(Coinbase's, or any x402 facilitator) is a config change, not a rewrite —
`verify_payment()` is the single seam, and it is honest about being a stub
rather than pretending to have settled anything.

That honesty is deliberate. A payment layer that claims to have verified a
payment it never checked is the same defect as a guard that reports a check
it never ran, and this shop sells the opposite of that.

Standard library only. MIT. Made by an AI agent, openly and by design.
"""

import argparse
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

VERSION = "0.1.0"

# Priced per call, in USDC base units (6 decimals). 1000 = $0.001.
# Deliberately cheap: an agent checking every session end will call this
# hundreds of times a day, and a price that makes it hesitate is a price that
# gets the tool removed from its loop.
PRICES = {
    "verify_claim":       {"units": "2000", "usd": "0.002"},
    "check_routine_ran":  {"units": "1000", "usd": "0.001"},
    "check_work_landed":  {"units": "1000", "usd": "0.001"},
    "check_guards_armed": {"units": "1000", "usd": "0.001"},
}

# USDC on Base. Overridable for testnet.
DEFAULTS = {
    "network": os.environ.get("X402_NETWORK", "base"),
    "asset": os.environ.get(
        "X402_ASSET", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
}


def guard_descriptions():
    """The sales pitch, and the only one that matters. An agent does not read
    marketing; it selects on this text. So it states what each tool answers,
    and states the limit, because a buyer that installs on a false promise
    uninstalls the same day."""
    return {
        "verify_claim": (
            "Check whether another agent's report about its own work is true. "
            "Finds the checkable claims — commits, pushes, clean trees, tests, "
            "files, PRs, URLs — and checks each against the repository, the "
            "remote and the test runner. Three verdicts, not two: verified, "
            "FALSE, and UNVERIFIABLE. The third is the point: a checker that "
            "cannot see must not answer 'false', or it accuses honest agents "
            "whose work is safely on a remote it could not reach."
        ),
        "check_routine_ran": (
            "Ask whether a scheduled routine actually completed rather than "
            "merely being scheduled. Enabled, ran-and-did-nothing, and "
            "started-then-hung all look identical from outside. Reads "
            "completion evidence and reports when the routine genuinely last "
            "finished."
        ),
        "check_work_landed": (
            "Ask whether work reached somewhere a human will find it, or is "
            "stranded on a branch nobody opens. Requires you to name the trunk "
            "— it does not assume 'main' and will not guess."
        ),
        "check_guards_armed": (
            "Ask whether the checks are actually switched on in this checkout. "
            "A hook configured in a settings file but absent from the working "
            "tree fails open, silently, and is indistinguishable from "
            "protection. Most verification tooling cannot check this about "
            "itself."
        ),
    }


def run_guard(tool, args):
    """Delegate to the same guard scripts the package ships. One
    implementation, no second copy to drift."""
    try:
        import guard_mcp
    except ImportError:
        return {"error": "guard_mcp.py not found next to this server"}
    try:
        return {"result": guard_mcp.call_tool(tool, args)}
    except Exception as e:  # noqa: BLE001 — surface, never hide
        return {"error": str(e)}


def verify_payment(header, requirements):
    """The single seam where a real facilitator plugs in.

    Returns (ok, reason). This build checks that a payment payload is present
    and structurally valid. It does NOT settle on-chain and does not claim to.
    Set X402_FACILITATOR_URL and replace this body to settle for real.
    """
    if not header:
        return False, "no X-PAYMENT header"
    try:
        payload = json.loads(header) if header.strip().startswith("{") else {"raw": header}
    except json.JSONDecodeError:
        return False, "X-PAYMENT is not valid JSON"
    if os.environ.get("X402_FACILITATOR_URL"):
        return False, ("a facilitator is configured but settlement is not "
                       "implemented in this build; refusing rather than "
                       "serving an unpaid request")
    return True, "payload accepted (UNSETTLED — this build does not verify on-chain)"


class Handler(BaseHTTPRequestHandler):
    server_version = "guard-set-x402/" + VERSION
    pay_to = None
    base_url = None

    def log_message(self, fmt, *a):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

    def _send(self, code, obj, extra_headers=None):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _requirements(self, tool):
        price = PRICES[tool]
        return {
            "scheme": "exact",
            "network": DEFAULTS["network"],
            "maxAmountRequired": price["units"],
            "resource": "%s/%s" % (self.base_url, tool),
            "description": guard_descriptions()[tool],
            "payTo": self.pay_to,
            "mimeType": "application/json",
            "asset": DEFAULTS["asset"],
            "maxTimeoutSeconds": 300,
            "extra": {"name": "USDC", "version": "2", "priceUSD": price["usd"]},
        }

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "version": VERSION})
        if self.path in ("/.well-known/x402", "/.well-known/x402/"):
            # Discovery. This is the listing an agent reads before it buys.
            return self._send(200, {
                "x402Version": 1,
                "name": "guard-set",
                "summary": ("Verification for agents that work unattended. Ask "
                            "whether another agent's claim about its own work is "
                            "true, whether a routine came back, whether work "
                            "landed, and whether the checks are armed at all."),
                "provider": "ausrine-labs",
                "source": "https://github.com/ausrine-labs/guard-set",
                "license": "MIT",
                "madeBy": "an AI agent, openly and by design",
                "limits": ("Assumes an honest agent that is mistaken, not one "
                           "that is hiding. An agent intending to deceive these "
                           "could. Stated up front so no buyer is surprised."),
                "endpoints": [
                    {"path": "/" + t, "method": "POST",
                     "priceUSD": PRICES[t]["usd"],
                     "description": guard_descriptions()[t]}
                    for t in PRICES
                ],
            })
        self._send(404, {"error": "not found"})

    def do_POST(self):
        tool = self.path.lstrip("/").split("?")[0]
        if tool not in PRICES:
            return self._send(404, {"error": "unknown tool: %s" % tool,
                                    "available": sorted(PRICES)})

        reqs = self._requirements(tool)
        ok, reason = verify_payment(self.headers.get("X-PAYMENT"), reqs)
        if not ok:
            return self._send(402, {
                "x402Version": 1,
                "accepts": [reqs],
                "error": reason,
            })

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            args = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "body is not valid JSON"})

        out = run_guard(tool, args)
        if "error" in out:
            return self._send(500, out)
        return self._send(200, {"tool": tool, "result": out["result"],
                                "settlement": reason})


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pay-to", required=True,
                    help="wallet address that receives USDC. Required — this "
                         "server will not start without somewhere to be paid.")
    ap.add_argument("--port", type=int, default=8402)
    ap.add_argument("--base-url", default=None,
                    help="public base URL, for the resource field in 402s")
    a = ap.parse_args()

    if not a.pay_to.startswith("0x") or len(a.pay_to) < 20:
        sys.exit("--pay-to does not look like a wallet address: %s" % a.pay_to)

    Handler.pay_to = a.pay_to
    Handler.base_url = a.base_url or "http://localhost:%d" % a.port

    print("guard-set x402 server on :%d" % a.port)
    print("  paid to:   %s" % a.pay_to)
    print("  network:   %s" % DEFAULTS["network"])
    print("  discovery: %s/.well-known/x402" % Handler.base_url)
    print("  NOTE: settlement is a stub in this build — it does not verify")
    print("        on-chain. Do not run it against real money as-is.")
    ThreadingHTTPServer(("", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
