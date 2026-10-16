"""Showcase networks: four fictional but physically plausible grids for demos.

Each one is built to show a different behaviour of the engine:

* ``harbor_city`` - a meshed metro grid with offshore wind. Most single-line
  losses reroute safely; a few overload a feeder and are saved by battery
  support plus Tier-3 shedding.
* ``cascade_valley`` - a hydro valley with a radial mountain spur. Losing the
  spur islands a clinic: no battery or shedding can fix that, so it shows a
  NOT_SAVEABLE case and why microgrids matter.
* ``sunridge_microgrid`` - a solar ring microgrid. Losing a ring segment next
  to the grid tie is saved by battery plus shedding the bus depot; a heatwave
  surge needs battery support.
* ``riverside_industrial`` - a double-circuit industrial corridor where one
  huge Tier-3 smelter is the obvious load to shed.

All values (MW, impedances, ratings) are invented for illustration.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List


def _line(lid: str, a: str, b: str, x: float, limit: float, backup: float = 0.0, backup_x: float = 0.0) -> Dict[str, Any]:
    line: Dict[str, Any] = {"id": lid, "from": a, "to": b, "impedance_pu": x, "limit_mw": limit}
    if backup:
        line["backup"] = {"impedance_pu": backup_x or x * 1.4, "limit_mw": backup}
    return line


def _load(cid: str, name: str, sub: str, mw: float, tier: int, category: str) -> Dict[str, Any]:
    return {"id": cid, "name": name, "substation": sub, "mw_demand": mw, "priority_tier": tier, "category": category}


def harbor_city() -> Dict[str, Any]:
    rows, cols = 3, 3
    bus = lambda r, c: f"H{r}{c}"
    lines: List[Dict[str, Any]] = []
    n = 0
    for r in range(1, rows + 1):
        for c in range(1, cols + 1):
            # Lines leaving the gas plant's bus carry most of the city's power, so they are rated higher.
            if c < cols:
                n += 1
                lines.append(_line(f"HL{n:02d}", bus(r, c), bus(r, c + 1), 0.012, 760.0 if (r, c) == (1, 1) else 560.0))
            if r < rows:
                n += 1
                lines.append(_line(f"HL{n:02d}", bus(r, c), bus(r + 1, c), 0.012, 760.0 if (r, c) == (1, 1) else 560.0))
    lines += [
        _line("FEED_DOWNTOWN_A", "H23", "SUB_DOWNTOWN", 0.006, 400.0),
        _line("FEED_DOWNTOWN_B", "H22", "SUB_DOWNTOWN", 0.006, 400.0),
        _line("FEED_PORT", "H33", "SUB_PORT", 0.006, 260.0, backup=200.0),
        _line("FEED_UPTOWN", "H13", "SUB_UPTOWN", 0.006, 330.0, backup=270.0),
        _line("FEED_AIRPORT", "H32", "SUB_AIRPORT", 0.006, 200.0, backup=170.0),
    ]
    layout = {bus(r, c): {"x": 120 + (c - 1) * 190, "y": 90 + (r - 1) * 150} for r in range(1, rows + 1) for c in range(1, cols + 1)}
    layout.update({"GEN_HARBOR_GAS": {"x": -20, "y": 90}, "GEN_OFFSHORE_WIND": {"x": -20, "y": 390},
                   "SUB_UPTOWN": {"x": 700, "y": -80}, "SUB_DOWNTOWN": {"x": 720, "y": 240},
                   "SUB_AIRPORT": {"x": 310, "y": 540}, "SUB_PORT": {"x": 700, "y": 560}})
    return {
        "name": "Harbor City metro",
        "description": "Meshed 9-bus metro grid with a gas plant and offshore wind feeding downtown, the port, uptown and the airport. "
                       "Two hospitals are Tier 1; the container terminal, cold storage and cargo hub can be shed.",
        "generators": [
            {"id": "GEN_HARBOR_GAS", "name": "Harbor combined-cycle gas plant", "output_mw": 0.0, "capacity_mw": 1100.0, "connects_to": "H11"},
            {"id": "GEN_OFFSHORE_WIND", "name": "Offshore wind farm", "output_mw": 260.0, "capacity_mw": 300.0, "connects_to": "H31"},
        ],
        "buses": [{"id": bus(r, c), "name": f"Harbor bus {r}-{c}"} for r in range(1, rows + 1) for c in range(1, cols + 1)],
        "substations": [
            {"id": "SUB_DOWNTOWN", "name": "Downtown substation", "capacity_mw": 520.0},
            {"id": "SUB_PORT", "name": "Port substation", "capacity_mw": 300.0},
            {"id": "SUB_UPTOWN", "name": "Uptown substation", "capacity_mw": 360.0},
            {"id": "SUB_AIRPORT", "name": "Airport substation", "capacity_mw": 220.0},
        ],
        "lines": lines,
        "loads": [
            _load("HARBOR_GENERAL", "Harbor General Hospital", "SUB_DOWNTOWN", 45.0, 1, "HOSPITAL"),
            _load("FINANCIAL_DISTRICT", "Financial district towers", "SUB_DOWNTOWN", 260.0, 2, "COMMERCIAL"),
            _load("METRO_SUBWAY", "Metro subway traction", "SUB_DOWNTOWN", 70.0, 2, "COMMERCIAL"),
            _load("CONVENTION_CENTER", "Convention center", "SUB_DOWNTOWN", 60.0, 3, "COMMERCIAL"),
            _load("CONTAINER_TERMINAL", "Container terminal cranes", "SUB_PORT", 110.0, 3, "INDUSTRIAL"),
            _load("DESALINATION_PLANT", "Desalination plant", "SUB_PORT", 80.0, 2, "INDUSTRIAL"),
            _load("COLD_STORAGE", "Port cold storage", "SUB_PORT", 50.0, 3, "INDUSTRIAL"),
            _load("CHILDRENS_HOSPITAL", "Children's hospital", "SUB_UPTOWN", 30.0, 1, "HOSPITAL"),
            _load("UPTOWN_HOMES", "Uptown homes", "SUB_UPTOWN", 210.0, 2, "RESIDENTIAL"),
            _load("HARBOR_UNIVERSITY", "Harbor university", "SUB_UPTOWN", 60.0, 2, "COMMERCIAL"),
            _load("AIRPORT_TERMINALS", "Airport terminals", "SUB_AIRPORT", 120.0, 2, "COMMERCIAL"),
            _load("CARGO_HUB", "Air cargo hub", "SUB_AIRPORT", 60.0, 3, "INDUSTRIAL"),
        ],
        "batteries": [
            {"id": "BESS_DOWNTOWN", "substation": "SUB_DOWNTOWN", "max_discharge_mw": 60.0, "soc_pct": 82.0},
            {"id": "BESS_PORT", "substation": "SUB_PORT", "max_discharge_mw": 40.0, "soc_pct": 70.0},
        ],
        "layout": layout,
    }


def cascade_valley() -> Dict[str, Any]:
    corridor = [f"V{i}" for i in range(1, 7)]
    lines = [_line(f"VL{i}", corridor[i - 1], corridor[i], 0.018, 640.0, backup=420.0) for i in range(1, 6)]
    lines += [
        _line("TIE_RIVERTOWN", "V6", "SUB_RIVERTOWN", 0.006, 420.0, backup=330.0),
        _line("TIE_LAKESIDE", "V5", "SUB_LAKESIDE", 0.006, 260.0, backup=200.0),
        _line("SPUR_SUMMIT", "V3", "SUB_SUMMIT", 0.03, 110.0),  # single radial line: losing it islands the summit
    ]
    layout = {v: {"x": 60 + i * 150, "y": 220} for i, v in enumerate(corridor)}
    layout.update({"HYDRO_FALLS": {"x": -60, "y": 120}, "HYDRO_MILL_DAM": {"x": 510, "y": 360},
                   "SUB_SUMMIT": {"x": 360, "y": 40}, "SUB_LAKESIDE": {"x": 660, "y": 380}, "SUB_RIVERTOWN": {"x": 950, "y": 220}})
    return {
        "name": "Cascade Valley hydro",
        "description": "Two hydro plants on a double-circuit valley corridor feeding Rivertown and Lakeside, plus a single radial spur "
                       "to the Summit ski village. Losing the spur islands the mountain clinic: no battery or shedding can save it.",
        "generators": [
            {"id": "HYDRO_MILL_DAM", "name": "Mill dam run-of-river hydro", "output_mw": 180.0, "capacity_mw": 200.0, "connects_to": "V4"},
            {"id": "HYDRO_FALLS", "name": "Upper Falls reservoir hydro", "output_mw": 0.0, "capacity_mw": 700.0, "connects_to": "V1"},
        ],
        "buses": [{"id": v, "name": f"Valley tower {v[1:]}"} for v in corridor],
        "substations": [
            {"id": "SUB_RIVERTOWN", "name": "Rivertown substation", "capacity_mw": 400.0},
            {"id": "SUB_LAKESIDE", "name": "Lakeside substation", "capacity_mw": 230.0},
            {"id": "SUB_SUMMIT", "name": "Summit substation", "capacity_mw": 95.0},
        ],
        "lines": lines,
        "loads": [
            _load("REGIONAL_HOSPITAL", "Valley regional hospital", "SUB_RIVERTOWN", 40.0, 1, "HOSPITAL"),
            _load("RIVERTOWN_HOMES", "Rivertown homes", "SUB_RIVERTOWN", 190.0, 2, "RESIDENTIAL"),
            _load("PAPER_MILL", "Paper mill", "SUB_RIVERTOWN", 85.0, 3, "INDUSTRIAL"),
            _load("LUMBER_YARD", "Lumber yard", "SUB_RIVERTOWN", 35.0, 3, "INDUSTRIAL"),
            _load("WATER_TREATMENT", "Lakeside water treatment", "SUB_LAKESIDE", 25.0, 1, "INDUSTRIAL"),
            _load("LAKESIDE_HOMES", "Lakeside homes", "SUB_LAKESIDE", 140.0, 2, "RESIDENTIAL"),
            _load("MARINA", "Marina and boatyard", "SUB_LAKESIDE", 20.0, 3, "COMMERCIAL"),
            _load("MOUNTAIN_CLINIC", "Summit mountain clinic", "SUB_SUMMIT", 12.0, 1, "HOSPITAL"),
            _load("ALPINE_VILLAGE", "Alpine village", "SUB_SUMMIT", 25.0, 2, "RESIDENTIAL"),
            _load("SKI_RESORT", "Ski lifts and snowmaking", "SUB_SUMMIT", 40.0, 3, "COMMERCIAL"),
        ],
        "batteries": [
            {"id": "BESS_RIVERTOWN", "substation": "SUB_RIVERTOWN", "max_discharge_mw": 50.0, "soc_pct": 76.0},
            {"id": "BESS_SUMMIT", "substation": "SUB_SUMMIT", "max_discharge_mw": 20.0, "soc_pct": 64.0},
        ],
        "layout": layout,
    }


def sunridge_microgrid() -> Dict[str, Any]:
    ring = [f"S{i}" for i in range(1, 7)]
    lines = [_line(f"SR{i + 1}", ring[i], ring[(i + 1) % 6], 0.02, 330.0) for i in range(6)]
    lines += [
        _line("TAP_CAMPUS", "S2", "SUB_CAMPUS", 0.008, 230.0, backup=180.0),
        _line("TAP_MEDICAL", "S3", "SUB_MEDICAL", 0.008, 130.0, backup=110.0),
        _line("TAP_TOWN", "S5", "SUB_TOWN", 0.008, 210.0, backup=170.0),
        _line("TAP_DEPOT", "S6", "SUB_DEPOT", 0.008, 130.0),  # single tap: the depot islands if it trips
    ]
    import math
    layout = {s: {"x": round(360 + 190 * math.cos(math.pi / 3 * i), 1), "y": round(260 + 190 * math.sin(math.pi / 3 * i), 1)}
              for i, s in enumerate(ring)}
    layout.update({"GEN_GRID_TIE": {"x": 640, "y": 260}, "SOLAR_SUNRIDGE": {"x": 80, "y": 260},
                   "SUB_CAMPUS": {"x": 560, "y": 520}, "SUB_MEDICAL": {"x": 160, "y": 520},
                   "SUB_TOWN": {"x": 160, "y": 0}, "SUB_DEPOT": {"x": 560, "y": 0}})
    return {
        "name": "Sunridge solar microgrid",
        "description": "A six-bus ring microgrid with a grid tie and a community solar farm, serving a university, a medical "
                       "campus, the town centre and an electric-bus depot. Losing a ring segment next to the grid tie overloads the other "
                       "side; the depot sits on a single tap and islands if it trips.",
        "generators": [
            {"id": "GEN_GRID_TIE", "name": "Regional grid tie", "output_mw": 0.0, "capacity_mw": 540.0, "connects_to": "S1"},
            {"id": "SOLAR_SUNRIDGE", "name": "Sunridge community solar", "output_mw": 140.0, "capacity_mw": 160.0, "connects_to": "S4"},
        ],
        "buses": [{"id": s, "name": f"Ring bus {s[1:]}"} for s in ring],
        "substations": [
            {"id": "SUB_CAMPUS", "name": "University campus", "capacity_mw": 220.0},
            {"id": "SUB_MEDICAL", "name": "Medical campus", "capacity_mw": 110.0},
            {"id": "SUB_TOWN", "name": "Town centre", "capacity_mw": 200.0},
            {"id": "SUB_DEPOT", "name": "Bus depot", "capacity_mw": 120.0},
        ],
        "lines": lines,
        "loads": [
            _load("UNIVERSITY_LABS", "University research labs", "SUB_CAMPUS", 90.0, 2, "COMMERCIAL"),
            _load("STUDENT_DORMS", "Student residences", "SUB_CAMPUS", 60.0, 2, "RESIDENTIAL"),
            _load("STADIUM", "Stadium and arena", "SUB_CAMPUS", 40.0, 3, "COMMERCIAL"),
            _load("SUNRIDGE_MEDICAL", "Sunridge medical center", "SUB_MEDICAL", 55.0, 1, "HOSPITAL"),
            _load("NURSING_HOME", "Hillcrest nursing home", "SUB_MEDICAL", 20.0, 1, "HOSPITAL"),
            _load("MEDICAL_OFFICES", "Medical office park", "SUB_MEDICAL", 15.0, 2, "COMMERCIAL"),
            _load("TOWN_HOMES", "Sunridge homes", "SUB_TOWN", 120.0, 2, "RESIDENTIAL"),
            _load("MAIN_STREET", "Main street shops", "SUB_TOWN", 45.0, 2, "COMMERCIAL"),
            _load("EV_BUS_DEPOT", "Electric bus chargers", "SUB_DEPOT", 60.0, 3, "INDUSTRIAL"),
            _load("LOGISTICS_HUB", "Logistics warehouse", "SUB_DEPOT", 35.0, 3, "INDUSTRIAL"),
        ],
        "batteries": [
            {"id": "BESS_CAMPUS", "substation": "SUB_CAMPUS", "max_discharge_mw": 30.0, "soc_pct": 74.0},
            {"id": "BESS_MEDICAL", "substation": "SUB_MEDICAL", "max_discharge_mw": 25.0, "soc_pct": 91.0},
        ],
        "layout": layout,
    }


def riverside_industrial() -> Dict[str, Any]:
    corridor = [f"I{i}" for i in range(1, 6)]
    lines = [_line(f"IL{i}", corridor[i - 1], corridor[i], 0.015, 700.0, backup=520.0) for i in range(1, 5)]
    lines += [
        _line("TAP_SMELTER", "I3", "SUB_SMELTER", 0.005, 420.0, backup=330.0),
        _line("TAP_CHEMICAL", "I3", "SUB_CHEMICAL", 0.006, 190.0, backup=160.0),
        _line("TAP_TOWNSHIP", "I5", "SUB_TOWNSHIP", 0.006, 230.0, backup=180.0),
    ]
    layout = {v: {"x": 60 + i * 180, "y": 220} for i, v in enumerate(corridor)}
    layout.update({"GEN_CCGT_RIVERSIDE": {"x": -60, "y": 120}, "GEN_WOOD_BIOMASS": {"x": 780, "y": 100},
                   "SUB_SMELTER": {"x": 360, "y": 60}, "SUB_CHEMICAL": {"x": 360, "y": 400}, "SUB_TOWNSHIP": {"x": 900, "y": 300}})
    return {
        "name": "Riverside industrial park",
        "description": "A double-circuit industrial corridor: a gas plant and a biomass plant supply an aluminium smelter, a "
                       "chemical works and a township. The 280 MW smelter is the natural load to shed.",
        "generators": [
            {"id": "GEN_WOOD_BIOMASS", "name": "Riverside biomass plant", "output_mw": 120.0, "capacity_mw": 140.0, "connects_to": "I5"},
            {"id": "GEN_CCGT_RIVERSIDE", "name": "Riverside combined-cycle plant", "output_mw": 0.0, "capacity_mw": 900.0, "connects_to": "I1"},
        ],
        "buses": [{"id": v, "name": f"Corridor bus {v[1:]}"} for v in corridor],
        "substations": [
            {"id": "SUB_SMELTER", "name": "Smelter substation", "capacity_mw": 400.0},
            {"id": "SUB_CHEMICAL", "name": "Chemical works substation", "capacity_mw": 180.0},
            {"id": "SUB_TOWNSHIP", "name": "Township substation", "capacity_mw": 210.0},
        ],
        "lines": lines,
        "loads": [
            _load("ALUMINIUM_SMELTER", "Aluminium smelter potlines", "SUB_SMELTER", 280.0, 3, "INDUSTRIAL"),
            _load("ROLLING_MILL", "Rolling mill", "SUB_SMELTER", 90.0, 3, "INDUSTRIAL"),
            _load("CHEMICAL_WORKS", "Chemical works (safety systems)", "SUB_CHEMICAL", 120.0, 2, "INDUSTRIAL"),
            _load("WATER_WORKS", "Regional water works", "SUB_CHEMICAL", 30.0, 1, "INDUSTRIAL"),
            _load("TOWNSHIP_HOMES", "Township homes", "SUB_TOWNSHIP", 150.0, 2, "RESIDENTIAL"),
            _load("COMMUNITY_HOSPITAL", "Community hospital", "SUB_TOWNSHIP", 25.0, 1, "HOSPITAL"),
            _load("SCHOOL_DISTRICT", "School district", "SUB_TOWNSHIP", 20.0, 2, "COMMERCIAL"),
        ],
        "batteries": [
            {"id": "BESS_SMELTER", "substation": "SUB_SMELTER", "max_discharge_mw": 80.0, "soc_pct": 68.0},
            {"id": "BESS_TOWNSHIP", "substation": "SUB_TOWNSHIP", "max_discharge_mw": 30.0, "soc_pct": 88.0},
        ],
        "layout": layout,
    }


def _fan_out(build: Callable[[], Dict[str, Any]]) -> Callable[[], Dict[str, Any]]:
    """Lay each substation's loads and batteries out in a row beside it, facing away from the grid."""
    import math

    def wrapped() -> Dict[str, Any]:
        spec = build()
        layout = spec["layout"]
        grid = [layout[b["id"]] for b in spec["buses"] if b["id"] in layout]
        cx, cy = sum(p["x"] for p in grid) / len(grid), sum(p["y"] for p in grid) / len(grid)
        for sub in spec["substations"]:
            at = layout[sub["id"]]
            items = [x["id"] for x in spec["loads"] + spec["batteries"] if x["substation"] == sub["id"]]
            dx, dy = at["x"] - cx, at["y"] - cy
            norm = math.hypot(dx, dy) or 1.0
            ux, uy = dx / norm, dy / norm          # away from the grid
            px, py = -uy, ux                       # along the row
            for i, item in enumerate(items):
                k = i - (len(items) - 1) / 2
                layout.setdefault(item, {"x": round(at["x"] + ux * 120 + px * k * 66, 1),
                                         "y": round(at["y"] + uy * 120 + py * k * 78, 1)})
        return spec
    wrapped.__name__ = build.__name__
    return wrapped


SHOWCASE: Dict[str, Callable[[], Dict[str, Any]]] = {
    "harbor_city": _fan_out(harbor_city),
    "cascade_valley": _fan_out(cascade_valley),
    "sunridge_microgrid": _fan_out(sunridge_microgrid),
    "riverside_industrial": _fan_out(riverside_industrial),
}
