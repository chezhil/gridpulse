"""``/api/networks/*``: build your own grid and run test cases on it.

Every route needs a login token. Networks are owned by the session's
``operator_id`` and stored in their own FalkorDB graph; other operators get
404 for networks they do not own. Errors use the same ``{"detail": {...}}``
envelope as ``/api/graph`` (see ``router.py``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from backend.auth.router import OperatorSession, verify_operator_token

from . import algorithms, importers, networks, queries
from .router import CircuitRef, _run

networks_router = APIRouter(prefix="/api/networks", tags=["networks"])


class NetworkCreate(BaseModel):
    template: Optional[str] = Field(None, description="Start from a template id (see GET /api/networks/templates).")
    name: Optional[str] = Field(None, max_length=80)
    spec: Optional[Dict[str, Any]] = Field(None, description="Full network spec; overrides the template if both are given.")


class NetworkSpecBody(BaseModel):
    spec: Dict[str, Any]


class TestCase(BaseModel):
    name: Optional[str] = Field(None, max_length=80)
    type: str = Field("scenario", description="'scenario' or 'n1'")
    trip: List[Dict[str, Any]] = Field(default_factory=list, examples=[[{"span_id": "TX_882", "circuit": "UPPER_PRIMARY"}]])
    demand_scale: Optional[float] = Field(None, gt=0, le=5)
    bess_injection_mw: Dict[str, float] = Field(default_factory=dict)
    deenergize: List[str] = Field(default_factory=list)


class SimulateBody(BaseModel):
    cases: List[TestCase] = Field(..., min_length=1, max_length=networks.MAX_CASES)
    threshold_pct: float = Field(95.0, ge=1, le=150)


class NetworkTrace(BaseModel):
    origin_id: str = Field(pattern=r"^[A-Z0-9_]{1,40}$")
    max_hops: int = Field(6, ge=1, le=queries.MAX_HOPS_LIMIT)
    assume_tripped: List[CircuitRef] = Field(default_factory=list, max_length=50)


@networks_router.get("/templates")
def templates() -> Dict[str, Any]:
    """Starter grids: the demo corridor, a ring, and a meshed city."""
    return networks.list_templates()


@networks_router.get("/templates/{template_id}")
def template(template_id: str) -> Dict[str, Any]:
    return _run(lambda: {"id": template_id, "spec": networks.get_template(template_id)})


class ConvertBody(BaseModel):
    format: str = Field("matpower", description="'matpower' (.m case file) or 'gridpulse' (spec JSON)")
    content: str = Field(..., max_length=importers.MAX_CONTENT_BYTES)
    name: Optional[str] = Field(None, max_length=80)
    tier1_buses: Optional[List[int]] = Field(None, description="MATPOWER bus numbers to mark Tier-1; default picks ~5% of load buses")
    battery_mw: float = Field(0.0, ge=0, le=10000, description="Add a battery of this size at each Tier-1 bus")


@networks_router.get("/samples/{name}", response_class=PlainTextResponse)
def sample(name: str) -> str:
    """Bundled public MATPOWER files (ieee14, ieee30, ieee118) to try the importer."""
    return _run(lambda: networks.sample_file(name))


@networks_router.post("/convert")
def convert(body: ConvertBody, session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    """Convert an uploaded file into a spec plus validation, without saving it."""
    def run():
        out = importers.convert(body.format, body.content, body.name,
                                {"tier1_buses": body.tier1_buses, "battery_mw": body.battery_mw})
        spec, warnings = networks.validate_spec(out["spec"])
        return {**out, "spec": spec, "warnings": warnings}
    return _run(run)


@networks_router.post("/validate")
def validate(body: NetworkSpecBody, session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    """Check a spec without saving it (422 lists every problem)."""
    def run():
        spec, warnings = networks.validate_spec(body.spec)
        return {"valid": True, "warnings": warnings, "spec": spec}
    return _run(run)


@networks_router.get("")
def list_networks(session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    return _run(lambda: networks.list_networks(session.operator_id))


@networks_router.post("", status_code=201)
def create_network(body: NetworkCreate, session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    def run():
        spec = body.spec or (networks.get_template(body.template) if body.template else None)
        if spec is None:
            spec = {"name": body.name or "Untitled network"}
        if body.name:
            spec = {**spec, "name": body.name}
        return networks.create_network(session.operator_id, spec)
    return _run(run)


@networks_router.get("/{network_id}")
def get_network(network_id: str, session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    return _run(lambda: networks.get_network(session.operator_id, network_id))


@networks_router.put("/{network_id}")
def replace_network(network_id: str, body: NetworkSpecBody, session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    return _run(lambda: networks.replace_network(session.operator_id, network_id, body.spec))


@networks_router.delete("/{network_id}")
def delete_network(network_id: str, session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    return _run(lambda: networks.delete_network(session.operator_id, network_id))


@networks_router.post("/{network_id}/simulate")
def simulate(network_id: str, body: SimulateBody, session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    """Run test cases (trips, demand scaling, battery/shedding what-ifs, N-1) and suggest the smallest safe fix."""
    cases = [{k: v for k, v in c.model_dump().items() if v not in (None, [], {})} for c in body.cases]
    return _run(lambda: networks.simulate(session.operator_id, network_id, cases, body.threshold_pct))


@networks_router.get("/{network_id}/topology")
def topology(network_id: str, session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    return _run(lambda: queries.get_topology(networks.owned_client(session.operator_id, network_id)))


@networks_router.post("/{network_id}/trace")
def trace(network_id: str, body: NetworkTrace, session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    return _run(lambda: queries.trace_cascade(
        body.origin_id, max_hops=body.max_hops, assume_tripped=[t.model_dump() for t in body.assume_tripped],
        client=networks.owned_client(session.operator_id, network_id)))


@networks_router.post("/{network_id}/contingency")
def contingency(network_id: str, session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    return _run(lambda: queries.run_contingency_scan(client=networks.owned_client(session.operator_id, network_id)))


@networks_router.get("/{network_id}/criticality")
def criticality(network_id: str, limit: int = 10, session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    return _run(lambda: algorithms.criticality_ranking(limit, networks.owned_client(session.operator_id, network_id)))


@networks_router.get("/{network_id}/reroute")
def reroute(network_id: str, source_id: str, target_id: str, k: int = 3,
            session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    return _run(lambda: algorithms.reroute_paths(source_id, target_id, k,
                                                 client=networks.owned_client(session.operator_id, network_id)))


@networks_router.get("/{network_id}/islands")
def islands(network_id: str, session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    return _run(lambda: algorithms.connected_components(networks.owned_client(session.operator_id, network_id)))


@networks_router.get("/{network_id}/zones")
def zones(network_id: str, session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    return _run(lambda: algorithms.zones(networks.owned_client(session.operator_id, network_id)))



class RehearseBody(BaseModel):
    max_cases: int = Field(40, ge=1, le=200)


@networks_router.post("/{network_id}/rehearse")
def rehearse(network_id: str, body: RehearseBody, session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    """Train the network's memory by rehearsing its critical outages and demand surges."""
    return _run(lambda: networks.rehearse(session.operator_id, network_id, body.max_cases))


@networks_router.get("/{network_id}/memory")
def network_memory(network_id: str, session: OperatorSession = Depends(verify_operator_token)) -> Dict[str, Any]:
    """Learning curve and playbooks of a saved network."""
    from . import memory

    def run():
        client = networks.owned_client(session.operator_id, network_id)
        return {**memory.learning_curve(client), "playbooks": memory.list_playbooks(client)["playbooks"]}
    return _run(run)
