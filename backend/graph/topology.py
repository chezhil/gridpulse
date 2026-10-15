"""Canonical GridPulse topology and nominal state.

This module is the single source of truth for graph IDs and nominal values.
Representation decisions (see backend/graph/README.md, "Graph representation"):

* Spans are relationships, not nodes. Each span ID is one corridor segment
  carrying two parallel circuits, so it appears as two relationships keyed by
  ``(id, circuit)``: ``UPPER_PRIMARY`` and ``LOWER_BACKUP``.
* ``TX_880``..``TX_887`` join adjacent towers (``TX_880`` = T1->T2, so
  ``TX_882`` = T3->T4). The ninth ID, ``TX_888``, is the terminal segment
  T9->SUB_METRO and uses the schema's ``TERMINATES_AT`` relationship type.
* Load is reached through ``Substation-[:DISTRIBUTES_TO]->Consumer``; the
  example ``Span-[:FEEDS]->Consumer`` path from the original spec does not exist.

IDs marked "proposed" are not in the shared spec and need team sign-off.
"""

from __future__ import annotations

from typing import Any, Dict, List

SCHEMA_VERSION = "gridpulse-graph/1"

CIRCUITS = ("UPPER_PRIMARY", "LOWER_BACKUP")
SPAN_STATUSES = ("ENERGIZED", "TRIPPED")
TOWER_STATUSES = ("NOMINAL", "FAULT", "REROUTED")
SUBSTATION_STATUSES = ("ONLINE", "OVERLOAD_RISK", "ISLANDED")
BESS_STATUSES = ("STANDBY", "DISCHARGING", "OFFLINE")
CONSUMER_CATEGORIES = ("HOSPITAL", "RESIDENTIAL", "COMMERCIAL", "INDUSTRIAL")

NODE_LABELS = ("HydroPlant", "Tower", "Substation", "BESS", "Consumer")
EDGE_TYPES = ("FEEDS", "SPAN", "TERMINATES_AT", "CONNECTED", "DISTRIBUTES_TO")
TRANSMISSION_EDGE_TYPES = ("FEEDS", "SPAN", "TERMINATES_AT")

META_ID = "GRID_META"

HYDRO: List[Dict[str, Any]] = [
    {
        "id": "HYDRO_ALPINE_01",
        "name": "Headwaters Pumped Hydro",
        "output_mw": 840.0,
        "voltage_kv": 500,
        "world_x": 275,
    }
]

# world_x is spread evenly over the corridor x=520..5190 and elevation falls from
# the alpine headwaters to the valley floor. Replace with frontend/index.html
# coordinates if the landing page uses different ones.
_TOWER_X = [520, 1104, 1688, 2271, 2855, 3439, 4023, 4606, 5190]
_TOWER_ELEVATION = [1850.0, 1720.0, 1580.0, 1410.0, 1240.0, 1050.0, 880.0, 720.0, 560.0]
TOWERS: List[Dict[str, Any]] = [
    {
        "id": f"T{i + 1}",
        "world_x": _TOWER_X[i],
        "elevation_m": _TOWER_ELEVATION[i],
        "status": "NOMINAL",
        "insulator_temp_c": 38.0,
    }
    for i in range(9)
]

SUBSTATIONS: List[Dict[str, Any]] = [
    {
        "id": "SUB_METRO",
        "city": "MetroGrid",
        "capacity_mw": 1000.0,
        "current_load_mw": 840.0,
        "transformer_temp_c": 65.0,
        "status": "ONLINE",
        "world_x": 5545,
    }
]

BESS_UNITS: List[Dict[str, Any]] = [
    {
        "id": "BESS_METRO_01",
        "soc_pct": 78.0,
        "max_discharge_mw": 60.0,
        "active_injection_mw": 0.0,
        "status": "STANDBY",
        "world_x": 5610,
    }
]

# Nominal demand sums to the 840 MW hydro output.
CONSUMERS: List[Dict[str, Any]] = [
    {
        "id": "HOSP_ST_JUDE",
        "name": "St. Jude Regional Trauma Center",
        "category": "HOSPITAL",
        "priority_tier": 1,
        "mw_demand": 30.0,
        "breaker_id": "BRK_HOSP_01",  # proposed
        "energized": True,
        "world_x": 6304,
    },
    {
        "id": "RES_VALLEY_SUBURBS",  # proposed
        "name": "Valley Suburbs",
        "category": "RESIDENTIAL",
        "priority_tier": 2,
        "mw_demand": 210.0,
        "breaker_id": "BRK_RES_04",  # proposed
        "energized": True,
        "world_x": 4100,
    },
    {
        "id": "COM_METRO_CORE",  # proposed
        "name": "Metro Core High-Rises",
        "category": "COMMERCIAL",
        "priority_tier": 2,
        "mw_demand": 538.0,
        "breaker_id": "BRK_COM_07",  # proposed
        "energized": True,
        "world_x": 6600,
    },
    {
        "id": "IND_METRO_12",  # proposed
        "name": "Riverside Industrial Works",
        "category": "INDUSTRIAL",
        "priority_tier": 3,
        "mw_demand": 62.0,
        "breaker_id": "BRK_IND_12",
        "energized": True,
        "world_x": 5900,
    },
]

_FEEDERS = {
    "HOSP_ST_JUDE": "FDR_MED_01",
    "RES_VALLEY_SUBURBS": "FDR_RES_04",
    "COM_METRO_CORE": "FDR_COM_07",
    "IND_METRO_12": "FDR_IND_12",
}

# Circuit ratings. The backup circuit has a lower rating than the primary, so
# losing the primary on one segment puts the full corridor flow (840 MW) on an
# 800 MW circuit; that is what makes the lightning scenario an overload.
CIRCUIT_RATINGS = {
    "UPPER_PRIMARY": {"impedance_pu": 0.010, "limit_mw": 900.0},
    "LOWER_BACKUP": {"impedance_pu": 0.015, "limit_mw": 800.0},
}
NOMINAL_SAG_M = 14.0

SPAN_SEGMENTS: List[Dict[str, str]] = [
    {"id": f"TX_{880 + i}", "from": f"T{i + 1}", "to": f"T{i + 2}"} for i in range(8)
] + [{"id": "TX_888", "from": "T9", "to": "SUB_METRO"}]


def _nominal_split(total_mw: float) -> Dict[str, float]:
    admittance = {c: 1.0 / CIRCUIT_RATINGS[c]["impedance_pu"] for c in CIRCUITS}
    total_y = sum(admittance.values())
    return {c: round(total_mw * admittance[c] / total_y, 3) for c in CIRCUITS}


def nominal_city_demand_mw() -> float:
    return sum(c["mw_demand"] for c in CONSUMERS)


def span_rows() -> List[Dict[str, Any]]:
    """One row per circuit relationship, with its nominal properties."""
    split = _nominal_split(nominal_city_demand_mw())
    rows = []
    for seg in SPAN_SEGMENTS:
        for circuit in CIRCUITS:
            rows.append(
                {
                    "id": seg["id"],
                    "circuit": circuit,
                    "from": seg["from"],
                    "to": seg["to"],
                    "rel_type": "TERMINATES_AT" if seg["to"] == "SUB_METRO" else "SPAN",
                    "props": {
                        "impedance_pu": CIRCUIT_RATINGS[circuit]["impedance_pu"],
                        "limit_mw": CIRCUIT_RATINGS[circuit]["limit_mw"],
                        "flow_mw": split[circuit],
                        "sag_m": NOMINAL_SAG_M,
                        "status": "ENERGIZED",
                        "kv": 500,
                    },
                }
            )
    return rows


def feeds_rows() -> List[Dict[str, Any]]:
    return [{"from": "HYDRO_ALPINE_01", "to": "T1", "props": {"mw": 840.0, "kv": 500}}]


def connected_rows() -> List[Dict[str, Any]]:
    return [
        {
            "from": "BESS_METRO_01",
            "to": "SUB_METRO",
            "props": {"inverter_id": "INV_METRO_01", "max_mw": 60.0},  # inverter_id proposed
        }
    ]


def distributes_rows() -> List[Dict[str, Any]]:
    return [
        {
            "from": "SUB_METRO",
            "to": c["id"],
            "props": {"feeder_id": _FEEDERS[c["id"]], "priority_tier": c["priority_tier"]},
        }
        for c in CONSUMERS
    ]


def nodes_by_label() -> Dict[str, List[Dict[str, Any]]]:
    return {
        "HydroPlant": HYDRO,
        "Tower": TOWERS,
        "Substation": SUBSTATIONS,
        "BESS": BESS_UNITS,
        "Consumer": CONSUMERS,
    }


def all_node_ids() -> List[str]:
    return [n["id"] for nodes in nodes_by_label().values() for n in nodes]


def edge_key(rel_type: str, src: str, dst: str, span_id: str = "", circuit: str = "") -> str:
    return f"{rel_type}|{src}|{dst}|{span_id}|{circuit}"


def all_edge_keys() -> List[str]:
    keys = [edge_key(r["rel_type"], r["from"], r["to"], r["id"], r["circuit"]) for r in span_rows()]
    keys += [edge_key("FEEDS", r["from"], r["to"]) for r in feeds_rows()]
    keys += [edge_key("CONNECTED", r["from"], r["to"]) for r in connected_rows()]
    keys += [edge_key("DISTRIBUTES_TO", r["from"], r["to"]) for r in distributes_rows()]
    return keys


EXPECTED_NODE_COUNT = len(all_node_ids())
EXPECTED_EDGE_COUNT = len(all_edge_keys())
