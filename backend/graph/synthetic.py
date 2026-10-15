"""Deterministic synthetic meshed grids for scale benchmarks.

A ``rows x cols`` lattice of ``:Bus`` nodes with directed ``:LINE``
relationships pointing right and down (power flows away from the source at
B0_0), plus seeded diagonal ties. Every bus has a reactance and rating on its
lines and a seeded load, so the same grid can be traversed in FalkorDB,
traversed in SQL, and solved with :mod:`graph.powerflow`.

These graphs live under their own graph names and never touch the canonical
demo topology.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass
class SyntheticGrid:
    size: int
    rows: int
    cols: int
    seed: int
    buses: List[Dict[str, object]]
    lines: List[Dict[str, object]]

    @property
    def graph_name(self) -> str:
        return f"gridpulse_bench_{self.size}"

    @property
    def source(self) -> str:
        return "B0_0"


def bus_id(r: int, c: int) -> str:
    return f"B{r}_{c}"


def generate(size: int, seed: int = 7, diagonal_prob: float = 0.2) -> SyntheticGrid:
    if size < 4:
        raise ValueError("size must be at least 4")
    rows = max(2, int(math.sqrt(size)))
    cols = max(2, size // rows)
    rng = random.Random(seed)
    # About 350 MW of total load whatever the size, so the base case is within line ratings.
    scale = 350.0 / (rows * cols * 0.6)
    buses = [
        {"id": bus_id(r, c), "row": r, "col": c, "load_mw": round(rng.uniform(0.2, 1.0) * scale, 4)}
        for r in range(rows)
        for c in range(cols)
    ]
    buses[0]["load_mw"] = 0.0
    lines: List[Dict[str, object]] = []

    def add(a: Tuple[int, int], b: Tuple[int, int]) -> None:
        lines.append(
            {
                "id": f"L{len(lines)}",
                "src": bus_id(*a),
                "dst": bus_id(*b),
                "impedance_pu": round(rng.uniform(0.005, 0.03), 5),
                "limit_mw": float(rng.choice((250, 400, 600))),
            }
        )

    for r in range(rows):
        for c in range(cols):
            if c + 1 < cols:
                add((r, c), (r, c + 1))
            if r + 1 < rows:
                add((r, c), (r + 1, c))
            if r + 1 < rows and c + 1 < cols and rng.random() < diagonal_prob:
                add((r, c), (r + 1, c + 1))
    return SyntheticGrid(size=rows * cols, rows=rows, cols=cols, seed=seed, buses=buses, lines=lines)


def benchmark_origins(grid: SyntheticGrid, count: int = 10, seed: int = 11) -> List[str]:
    """Origins away from the far edges, so a 6-hop trace has a full neighbourhood."""
    rng = random.Random(seed)
    r_hi, c_hi = max(1, grid.rows - 7), max(1, grid.cols - 7)
    return [bus_id(rng.randrange(r_hi), rng.randrange(c_hi)) for _ in range(count)]
