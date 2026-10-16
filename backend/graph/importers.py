"""Import standard grid files into GridPulse network specs.

Supported:

* ``matpower`` - MATPOWER case files (``.m``), the de-facto exchange format for
  public test grids (IEEE 14/30/57/118/300, PEGASE, ...). Parsed here without
  MATLAB or extra dependencies: ``mpc.baseMVA``, ``mpc.bus``, ``mpc.gen`` and
  ``mpc.branch`` are read; cost data and AC-only columns are ignored.
* ``gridpulse`` - a GridPulse network spec as JSON (what ``GET /api/networks/{id}``
  returns under ``spec``).

MATPOWER files carry physics but not GridPulse semantics, so the converter
adds clearly reported overlays: ``priority_tier`` per load, synthetic line
ratings where ``rateA`` is 0 (1.6 x base-case DC flow, min 100 MW), substation
capacities (1.5 x load) and optional batteries at Tier-1 buses.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from .client import InvalidRequestError
from .powerflow import Branch, Network

MAX_CONTENT_BYTES = 5_000_000

# MATPOWER column indices (0-based).
BUS_I, BUS_TYPE, PD = 0, 1, 2
GEN_BUS, PG, GEN_STATUS, PMAX = 0, 1, 7, 8
F_BUS, T_BUS, BR_X, RATE_A, TAP, SHIFT, BR_STATUS = 0, 1, 3, 5, 8, 9, 10
ISOLATED = 4
REF = 3

_MATRIX = re.compile(r"mpc\.(\w+)\s*=\s*\[(.*?)\]\s*;", re.S)
_SCALAR = re.compile(r"mpc\.baseMVA\s*=\s*([0-9.eE+-]+)\s*;")


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("%", 1)[0] for line in text.splitlines())


def parse_matpower(text: str) -> Dict[str, Any]:
    """Parse the numeric matrices of a MATPOWER case file."""
    if not isinstance(text, str) or not text.strip():
        raise InvalidRequestError("MATPOWER content is empty")
    if len(text.encode("utf-8", "ignore")) > MAX_CONTENT_BYTES:
        raise InvalidRequestError(f"file too large (max {MAX_CONTENT_BYTES // 1_000_000} MB)")
    clean = _strip_comments(text)
    out: Dict[str, Any] = {"baseMVA": 100.0}
    m = _SCALAR.search(clean)
    if m:
        out["baseMVA"] = float(m.group(1))
    for name, body in _MATRIX.findall(clean):
        rows = []
        for raw in re.split(r"[;\n]", body):
            raw = raw.strip().strip(",")
            if not raw:
                continue
            try:
                row = [float(x) for x in re.split(r"[\s,]+", raw) if x]
            except ValueError as exc:
                raise InvalidRequestError(f"mpc.{name}: non-numeric value in row '{raw[:60]}'") from exc
            rows.append(row)
        out[name] = rows
    missing = [k for k in ("bus", "gen", "branch") if not out.get(k)]
    if missing:
        raise InvalidRequestError(f"not a MATPOWER case: missing mpc.{', mpc.'.join(missing)}")
    for name, width in (("bus", 3), ("gen", 9), ("branch", 11)):
        short = [i for i, r in enumerate(out[name]) if len(r) < width]
        if short:
            raise InvalidRequestError(f"mpc.{name} rows need at least {width} columns (row {short[0] + 1})")
    return out


def _bus_id(n: int, width: int) -> str:
    return f"B{n:0{width}d}"


def matpower_to_spec(
    mpc: Dict[str, Any],
    name: str = "Imported MATPOWER case",
    tier1_buses: Optional[List[int]] = None,
    tier3_every: int = 4,
    battery_mw: float = 0.0,
    rating_factor: float = 1.6,
) -> Tuple[Dict[str, Any], List[str], Dict[str, float]]:
    """Convert parsed MATPOWER matrices to a GridPulse spec.

    Returns ``(spec, notes, base_flows)`` where ``base_flows`` maps branch id
    to the signed DC flow (MW) used to derive synthetic ratings.
    """
    notes: List[str] = []
    buses = [r for r in mpc["bus"] if int(r[BUS_TYPE]) != ISOLATED]
    if len(mpc["bus"]) != len(buses):
        notes.append(f"skipped {len(mpc['bus']) - len(buses)} isolated bus(es)")
    if not buses:
        raise InvalidRequestError("MATPOWER case has no connected buses")
    # Unused columns may hold nan (real case files do); the ones read here must be numbers.
    # Inf is allowed only where MATPOWER means "no limit".
    for matrix, finite, not_nan in (("bus", (BUS_I, BUS_TYPE, PD), ()), ("gen", (GEN_BUS, PG, GEN_STATUS), (PMAX,)),
                                  ("branch", (F_BUS, T_BUS, BR_X, TAP, SHIFT, BR_STATUS), (RATE_A,))):
        if any(not math.isfinite(r[c]) for r in mpc[matrix] for c in finite) or \
                any(math.isnan(r[c]) for r in mpc[matrix] for c in not_nan):
            raise InvalidRequestError(f"mpc.{matrix}: bus numbers, status, power and rating columns must be numbers")
    numbers = [int(r[BUS_I]) for r in buses]
    width = max(3, len(str(max(numbers))))
    known = set(numbers)
    refs = [int(r[BUS_I]) for r in buses if int(r[BUS_TYPE]) == REF]
    load_mw: Dict[int, float] = {}
    for r in buses:
        if r[PD]:
            load_mw[int(r[BUS_I])] = load_mw.get(int(r[BUS_I]), 0.0) + float(r[PD])

    gens: Dict[int, Dict[str, float]] = {}
    for r in mpc["gen"]:
        if int(r[GEN_STATUS]) <= 0 or int(r[GEN_BUS]) not in known:
            continue
        g = gens.setdefault(int(r[GEN_BUS]), {"pg": 0.0, "pmax": 0.0})
        g["pg"] += float(r[PG])
        pmax = float(r[PMAX])
        # Tools write 9999/1e9-style placeholders for "unlimited"; treat those as no capacity limit.
        g["pmax"] = math.inf if pmax >= 1e5 or g["pmax"] == math.inf else g["pmax"] + max(pmax, 0.0)
    # Negative loads are injections; model them as fixed generators.
    for b, mw in list(load_mw.items()):
        if mw < 0:
            g = gens.setdefault(b, {"pg": 0.0, "pmax": 0.0})
            g["pg"] += -mw
            g["pmax"] += -mw
            del load_mw[b]
            notes.append(f"bus {b}: negative load modelled as generation")
    # And negative generation (pumped storage, synchronous condensers) is a load.
    for b, g in gens.items():
        if g["pg"] < 0:
            load_mw[b] = load_mw.get(b, 0.0) - g["pg"]
            g["pg"] = 0.0
            notes.append(f"bus {b}: negative generation modelled as load")
    if not gens:
        raise InvalidRequestError("case has no in-service generators")
    slack = refs[0] if refs and refs[0] in gens else max(gens, key=lambda b: gens[b]["pmax"])
    if not refs:
        notes.append(f"no reference bus; using bus {slack} (largest generator) as slack")

    branches = []
    shifted = 0
    for r in mpc["branch"]:
        if int(r[BR_STATUS]) <= 0:
            continue
        fb, tb = int(r[F_BUS]), int(r[T_BUS])
        if fb not in known or tb not in known or fb == tb:
            continue
        tap = float(r[TAP]) or 1.0
        x = abs(float(r[BR_X]) * tap)
        if x <= 0:
            x = 1e-4
            notes.append(f"branch {fb}-{tb}: zero reactance replaced by 1e-4 pu")
        if float(r[SHIFT]):
            shifted += 1
        branches.append({"from": fb, "to": tb, "x": x, "rate": float(r[RATE_A])})
    if shifted:
        notes.append(f"{shifted} phase-shifting transformer(s): shift angle ignored in DC model")

    # Base-case DC solve on the imported physics, to rate unrated branches.
    load_buses = sorted(load_mw)
    ids = {b: _bus_id(b, width) for b in numbers}
    net_branches = [Branch(f"BR{i + 1:03d}", ids[br["from"]], ids[br["to"]], br["x"]) for i, br in enumerate(branches)]
    net = Network(list(ids.values()), net_branches, ids[slack])
    inj = {ids[b]: -mw for b, mw in load_mw.items()}
    for b, g in gens.items():
        if b != slack:
            inj[ids[b]] = inj.get(ids[b], 0.0) + g["pg"]
    flows = net.flows(inj)
    if len(net.island) < len(ids):
        notes.append(f"{len(ids) - len(net.island)} bus(es) are not connected to the slack bus")

    if tier1_buses is None:
        step = max(1, round(len(load_buses) / max(1, round(0.05 * len(load_buses)))))
        tier1 = set(load_buses[step // 2::step]) or set(load_buses[:1])
    else:
        tier1 = {b for b in tier1_buses if b in load_mw}
    tier3 = {b for i, b in enumerate(load_buses) if tier3_every and i % tier3_every == tier3_every - 1 and b not in tier1}

    spec_buses, substations, loads, batteries = [], [], [], []
    for b in numbers:
        mw = load_mw.get(b, 0.0)
        if mw > 0:
            substations.append({"id": ids[b], "capacity_mw": float(max(10, math.ceil(1.5 * mw / 10) * 10))})
            tier = 1 if b in tier1 else 3 if b in tier3 else 2
            loads.append({"id": f"LOAD_{ids[b]}", "substation": ids[b], "mw_demand": round(mw, 3), "priority_tier": tier,
                          "category": {1: "HOSPITAL", 2: "RESIDENTIAL", 3: "INDUSTRIAL"}[tier]})
            if tier == 1 and battery_mw > 0:
                batteries.append({"id": f"BESS_{ids[b]}", "substation": ids[b], "max_discharge_mw": float(battery_mw), "soc_pct": 80.0})
        else:
            spec_buses.append({"id": ids[b]})

    generators = [{"id": f"GEN_000_{ids[slack]}", "name": "Slack", "output_mw": 0.0,
                   "capacity_mw": round(gens[slack]["pmax"], 3) if 0 < gens[slack]["pmax"] < math.inf else None,
                   "connects_to": ids[slack]}]
    for b, g in sorted(gens.items()):
        if b == slack:
            continue
        cap = max(g["pmax"], g["pg"])
        generators.append({"id": f"GEN_{ids[b]}", "output_mw": round(g["pg"], 3),
                           "capacity_mw": round(cap, 3) if 0 < cap < math.inf else None,
                           "connects_to": ids[b]})

    lines, rated = [], 0
    for i, br in enumerate(branches):
        lid = f"BR{i + 1:03d}"
        if br["rate"] > 0:
            limit = br["rate"]
            rated += 1
        else:
            limit = float(max(100, math.ceil(rating_factor * abs(flows.get(lid, 0.0)) / 10) * 10))
        lines.append({"id": lid, "from": ids[br["from"]], "to": ids[br["to"]], "impedance_pu": round(br["x"], 6), "limit_mw": limit})
    if rated < len(lines):
        notes.append(f"{len(lines) - rated} of {len(lines)} branches had no rating (rateA=0): "
                     f"rated at {rating_factor} x base-case flow, min 100 MW")
    notes.append(f"priority tiers are synthetic: {len(tier1)} Tier-1 (hospital) bus(es), every {tier3_every}th load Tier-3")
    if battery_mw > 0:
        notes.append(f"added a {battery_mw:g} MW battery at each Tier-1 bus")

    spec = {"name": name[:80], "description": "Imported from a MATPOWER case.", "generators": generators,
            "buses": spec_buses, "substations": substations, "lines": lines, "loads": loads, "batteries": batteries}
    return spec, notes, {k: round(v, 4) for k, v in flows.items() if k.startswith("BR")}


def convert(fmt: str, content: str, name: Optional[str] = None, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Convert uploaded file content to a spec (not saved). Used by the import UI."""
    options = options or {}
    if fmt == "gridpulse":
        try:
            spec = json.loads(content)
        except ValueError as exc:
            raise InvalidRequestError(f"invalid JSON: {exc}") from exc
        spec = spec.get("spec", spec) if isinstance(spec, dict) else spec
        if name and isinstance(spec, dict):
            spec["name"] = name
        return {"format": fmt, "spec": spec, "notes": []}
    if fmt == "matpower":
        tier1 = options.get("tier1_buses")
        if tier1 is not None and (not isinstance(tier1, list) or not all(isinstance(b, int) for b in tier1)):
            raise InvalidRequestError("tier1_buses must be a list of bus numbers")
        battery = options.get("battery_mw", 0.0)
        if isinstance(battery, bool) or not isinstance(battery, (int, float)) or not 0 <= battery <= 10_000:
            raise InvalidRequestError("battery_mw must be between 0 and 10000")
        mpc = parse_matpower(content)
        spec, notes, _ = matpower_to_spec(mpc, name or "Imported MATPOWER case", tier1_buses=tier1, battery_mw=float(battery))
        return {"format": fmt, "spec": spec, "notes": notes,
                "summary": {"buses": len(spec["buses"]) + len(spec["substations"]), "branches": len(spec["lines"]),
                            "generators": len(spec["generators"]), "loads": len(spec["loads"])}}
    raise InvalidRequestError("format must be 'matpower' or 'gridpulse'")
