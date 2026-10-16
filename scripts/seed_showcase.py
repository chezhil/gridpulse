"""Fill a running GridPulse with showcase data, over its public HTTP API.

For every demo login it saves the four showcase grids (backend/graph/showcase.py)
plus an imported IEEE 30-bus case, then rehearses each one so its graph memory
holds incidents and playbooks. On the live grid it runs every scenario a few
times (building the audit trail and the learning curve), records one refused
hospital-breaker trip for the audit page, and leaves the grid nominal.

Re-running is safe: showcase networks with the same name are replaced.

    python scripts/seed_showcase.py                       # http://localhost:8000
    python scripts/seed_showcase.py --base https://<app>  # a deployed site

No AI calls are made, so it uses none of the Groq quota.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# The ids behind the console's one-click demo logins, plus the Lab's default user.
OWNERS = {"OP-NAMAN-01": "operator", "ENG-CHEZ-02": "engineer", "AUD-JYOTHI-03": "auditor", "LAB_OPERATOR": "operator"}
TEMPLATES = ["harbor_city", "cascade_valley", "sunridge_microgrid", "riverside_industrial"]
IEEE_NAME = "IEEE 30-bus test system (imported)"
SCENARIO_ROUNDS = 2


class Api:
    def __init__(self, base, token=None):
        self.base, self.token = base.rstrip("/"), token

    def call(self, method, path, body=None, timeout=180, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                text = res.read().decode()
                return text if raw else json.loads(text or "null")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:400]
            raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from None

    def login(self, username, role):
        r = self.call("POST", "/api/auth/login", {"username": username, "role": role, "demo": True})
        return Api(self.base, r["access_token"])


def say(msg):
    print(msg, flush=True)


def seed_networks(api, owner):
    names = {api.call("GET", f"/api/networks/templates/{t}")["spec"]["name"] for t in TEMPLATES} | {IEEE_NAME}
    for n in api.call("GET", "/api/networks")["networks"]:
        if n["name"] in names:
            api.call("DELETE", f"/api/networks/{n['id']}")
    created = []
    for t in TEMPLATES:
        created.append(api.call("POST", "/api/networks", {"template": t}))
    sample = api.call("GET", "/api/networks/samples/ieee30", raw=True)
    conv = api.call("POST", "/api/networks/convert",
                    {"format": "matpower", "content": sample, "name": IEEE_NAME, "battery_mw": 30})
    spec = {**conv["spec"], "description": "The standard IEEE 30-bus test case imported from MATPOWER, with Tier-1 loads "
                                           "and a 30 MW battery at each Tier-1 bus."}
    created.append(api.call("POST", "/api/networks", {"spec": spec}))
    for net in created:
        r = api.call("POST", f"/api/networks/{net['id']}/rehearse", {"max_cases": 40}, timeout=600)
        say(f"  {owner}: {net['name']:<38} {r['cases']:>2} cases rehearsed, {r['saved']} saved, "
            f"{r['not_saveable']} not saveable, {r['memory']['playbooks']} playbooks")


def wait_for(api, statuses, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        status = api.call("GET", "/api/sim/state")["scenario"]["status"]
        if status in statuses:
            return status
        time.sleep(0.5)
    raise RuntimeError(f"scenario did not finish (last status {status})")


def seed_live(api):
    api.call("POST", "/api/sim/reset", {})
    for rnd in range(SCENARIO_ROUNDS):
        for sid in (1, 2, 3):
            api.call("POST", f"/api/sim/trigger-scenario/{sid}?mode=deterministic")
            status = wait_for(api, {"MITIGATED", "FAILED"})
            say(f"  live grid: scenario {sid} round {rnd + 1}: {status}")
            api.call("POST", "/api/sim/reset", {})
    # The guard must refuse the hospital's breaker; the refusal is part of the audit story.
    try:
        api.call("POST", "/api/mcp/tools/scada_trip_breaker", {"breaker_id": "BRK_HOSP_01", "reason_code": "SHOWCASE_GUARD_CHECK"})
        say("  WARNING: hospital breaker trip was not refused")
    except RuntimeError as exc:
        say(f"  live grid: hospital breaker trip refused as expected ({str(exc).split(':', 1)[0]})")
    api.call("POST", "/api/sim/reset", {})


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--skip-live", action="store_true", help="only seed saved networks")
    args = ap.parse_args()
    root = Api(args.base)
    health = root.call("GET", "/health")
    if health.get("status") != "HEALTHY":
        sys.exit(f"backend is not healthy: {health}")
    say(f"Seeding {args.base}")
    for owner, role in OWNERS.items():
        seed_networks(root.login(owner, role), owner)
    if not args.skip_live:
        seed_live(root.login("OP-NAMAN-01", "operator"))
    verify = root.call("GET", "/api/audit/verify")
    say(f"Done. Audit chain valid={verify['valid']} with {verify['checked']} records.")


if __name__ == "__main__":
    main()
