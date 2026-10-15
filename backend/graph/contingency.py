"""N-1 security scan: lose each energised circuit in turn and re-solve.

Every contingency is an exact re-solve of the ``dc-pf/1`` model, so results
match what the trace endpoint would report if that circuit tripped.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .thermal import MODEL_ID, project_loading


def scan_n1(snapshot: Dict[str, Any], threshold_pct: float = 95.0) -> Dict[str, Any]:
    base = project_loading(snapshot, threshold_pct=threshold_pct)
    results: List[Dict[str, Any]] = []
    for c in base["circuits"]:
        if c["status"] != "ENERGIZED":
            continue
        outage = (c["span_id"], c["circuit"])
        p = project_loading(snapshot, [outage], threshold_pct)
        others = [x for x in p["circuits"] if (x["span_id"], x["circuit"]) != outage]
        worst = max(others, key=lambda x: x["loading_pct"], default=None)
        worst_sub = max(p["substations"], key=lambda s: s["loading_pct"], default=None)
        results.append(
            {
                "outage": f"{outage[0]}:{outage[1]}",
                "span_id": outage[0],
                "circuit": outage[1],
                "outcome": p["outcome"],
                "secure": p["outcome"] == "WITHIN_LIMITS",
                "worst_element": f"{worst['span_id']}:{worst['circuit']}" if worst else None,
                "worst_loading_pct": worst["loading_pct"] if worst else None,
                "worst_substation_loading_pct": worst_sub["loading_pct"] if worst_sub else None,
                "overloads": [o["element"] for o in p["overloads"]],
                "islanded_substations": p["islanded_substations"],
                "required_relief_mw": p["required_relief_mw"],
            }
        )
    results.sort(key=lambda r: (r["secure"], -(r["worst_loading_pct"] or 0), r["outage"]))
    critical = [r["outage"] for r in results if not r["secure"]]
    return {
        "model": MODEL_ID,
        "threshold_pct": threshold_pct,
        "base_outcome": base["outcome"],
        "contingency_count": len(results),
        "critical_outages": critical,
        "n1_secure": base["outcome"] == "WITHIN_LIMITS" and not critical,
        "contingencies": results,
    }
