"""Linear (DC) power flow, PTDF and LODF for arbitrary meshed networks.

Pure numerical code with no FalkorDB dependency. Bus injections are in MW (generation
positive, load negative); branch reactances are in per-unit. With a lossless
DC approximation, flows are linear in the injections, so sensitivities (PTDF)
and single-outage redistribution factors (LODF) follow from one solve of the
reduced susceptance matrix.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Hashable, Iterable, List, Optional, Sequence, Set

import numpy as np

EPS = 1e-9


@dataclass(frozen=True)
class Branch:
    key: Hashable
    src: str
    dst: str
    x: float  # per-unit reactance, > 0


@dataclass
class FlowResult:
    slack: str
    island: Set[str]                      # buses connected to the slack
    flows: Dict[Hashable, float]          # signed MW, positive src->dst; 0 for out-of-island
    ptdf: Dict[Hashable, Dict[str, float]] = field(default_factory=dict)  # branch -> bus -> MW/MW


def connected(buses: Iterable[str], branches: Sequence[Branch], start: str) -> Set[str]:
    adj: Dict[str, List[str]] = {b: [] for b in buses}
    for br in branches:
        adj.setdefault(br.src, []).append(br.dst)
        adj.setdefault(br.dst, []).append(br.src)
    seen: Set[str] = set()
    queue = deque([start])
    while queue:
        n = queue.popleft()
        if n in seen:
            continue
        seen.add(n)
        queue.extend(adj.get(n, ()))
    return seen


class Network:
    """Factorised DC model of the island that contains ``slack``."""

    def __init__(self, buses: Iterable[str], branches: Sequence[Branch], slack: str) -> None:
        self.slack = slack
        self.all_branches = list(branches)
        self.island = connected(buses, self.all_branches, slack)
        self.branches = [b for b in self.all_branches if b.src in self.island and b.dst in self.island]
        others = sorted(b for b in self.island if b != slack)
        self.index = {b: i for i, b in enumerate(others)}
        n, m = len(others), len(self.branches)
        # Reduced incidence (slack column removed) and branch susceptances.
        self.A = np.zeros((m, n))
        self.y = np.zeros(m)
        for k, br in enumerate(self.branches):
            if br.x <= 0:
                raise ValueError(f"branch {br.key} has non-positive reactance")
            self.y[k] = 1.0 / br.x
            if br.src in self.index:
                self.A[k, self.index[br.src]] = 1.0
            if br.dst in self.index:
                self.A[k, self.index[br.dst]] = -1.0
        B = self.A.T @ (self.y[:, None] * self.A)
        # PTDF[k, i]: MW on branch k per MW injected at bus i and withdrawn at the slack.
        self.ptdf = (self.y[:, None] * self.A) @ np.linalg.inv(B) if n else np.zeros((m, 0))
        self.row = {br.key: k for k, br in enumerate(self.branches)}

    def flows(self, injections: Dict[str, float]) -> Dict[Hashable, float]:
        p = np.zeros(len(self.index))
        for bus, mw in injections.items():
            if bus in self.index:
                p[self.index[bus]] += mw
        f = self.ptdf @ p if len(p) else np.zeros(len(self.branches))
        out = {br.key: 0.0 for br in self.all_branches}
        for k, br in enumerate(self.branches):
            out[br.key] = float(f[k])
        return out

    def ptdf_for(self, key: Hashable, bus: str) -> float:
        if key not in self.row or bus not in self.index:
            return 0.0
        return float(self.ptdf[self.row[key], self.index[bus]])

    def transfer_ptdf(self, key: Hashable, src: str, dst: str) -> float:
        """MW on ``key`` per MW transferred from ``src`` to ``dst``."""
        return self.ptdf_for(key, src) - self.ptdf_for(key, dst)

    def n1_flows(self, base: Dict[Hashable, float]):
        """Post-outage flows for every single-branch outage at once.

        Returns ``(keys, post, bridges)``: ``post[l, k]`` is the flow on branch
        ``keys[l]`` after losing ``keys[k]``; columns for bridge outages (which
        split the island and have no LODF) are NaN and listed in ``bridges``.
        """
        keys = [br.key for br in self.branches]
        f = np.array([base[k] for k in keys])
        # M[l, k]: MW on l per MW transferred across branch k's endpoints.
        M = self.ptdf @ self.A.T
        denom = 1.0 - np.diag(M)
        bridge = np.abs(denom) < 1e-7
        safe = np.where(bridge, 1.0, denom)
        lodf = M / safe[None, :]
        np.fill_diagonal(lodf, -1.0)
        post = f[:, None] + lodf * f[None, :]
        post[:, bridge] = np.nan
        return keys, post, [keys[i] for i in np.flatnonzero(bridge)]

    def lodf(self, outage: Hashable) -> Optional[Dict[Hashable, float]]:
        """Line outage distribution factors for losing ``outage``.

        Returns ``None`` when the outage splits the island (a bridge), in which
        case LODF is undefined and the caller must re-solve.
        """
        k = self.row[outage]
        br = self.branches[k]
        denom = 1.0 - self.transfer_ptdf(outage, br.src, br.dst)
        if abs(denom) < 1e-7:
            return None
        out = {}
        for j, other in enumerate(self.branches):
            if j == k:
                out[other.key] = -1.0
            else:
                out[other.key] = self.transfer_ptdf(other.key, br.src, br.dst) / denom
        return out


def solve(buses: Iterable[str], branches: Sequence[Branch], slack: str, injections: Dict[str, float]) -> FlowResult:
    """One DC power flow. Buses outside the slack's island get no supply."""
    net = Network(buses, branches, slack)
    island_inj = {b: mw for b, mw in injections.items() if b in net.island and b != slack}
    flows = net.flows(island_inj)
    ptdf = {br.key: {b: net.ptdf_for(br.key, b) for b in net.index} for br in net.branches}
    return FlowResult(slack=slack, island=net.island, flows=flows, ptdf=ptdf)
