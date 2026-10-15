"""
GridPulse SCADA MCP Schemas
Canonical schemas for telemetry, scenarios, tools, and audit events.
Strictly adheres to GITHUB_RULES.md and SPEC.md canonical IDs.
"""

from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime, timezone


# Canonical IDs (Single Source of Truth)
HYDRO_PLANT_ID = "HYDRO_ALPINE_01"
TOWER_IDS = ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"]
SPAN_IDS = [f"TX_88{i}" for i in range(9)]  # TX_880 through TX_888
SUBSTATION_ID = "SUB_METRO"
BESS_ID = "BESS_METRO_01"
TIER1_HOSPITAL_ID = "HOSP_ST_JUDE"
TIER3_BREAKER_ID = "BRK_IND_12"

# Span endpoint mapping (Canonical 8 consecutive pairs + 1 auxiliary tie span)
SPAN_MAPPING = {
    "TX_880": ("T1", "T2"),
    "TX_881": ("T2", "T3"),
    "TX_882": ("T3", "T4"),  # Lightning strike target span
    "TX_883": ("T4", "T5"),
    "TX_884": ("T5", "T6"),  # Wildfire corridor span
    "TX_885": ("T6", "T7"),
    "TX_886": ("T7", "T8"),
    "TX_887": ("T8", "T9"),
    "TX_888": ("T9", "SUB_METRO"),  # Terminal span connecting T9 to Substation
}


class ScenarioInfo(BaseModel):
    id: int = Field(default=0, description="0=Nominal, 1=Lightning, 2=Wildfire, 3=Heatwave")
    name: str = Field(default="NOMINAL_GRID_FLOW", description="Identifier of the grid scenario")
    status: Literal["NOMINAL", "TRIGGERED", "ANALYZING", "MITIGATING", "MITIGATED", "FAILED"] = "NOMINAL"


class MetricsInfo(BaseModel):
    graphblas_latency_ms: Optional[float] = Field(default=None, description="FalkorDB GraphBLAS matrix compute latency")
    city_load_mw: float = Field(default=840.0, description="Current metropolitan power demand in MW")
    city_load_pct: float = Field(default=100.0, description="Substation capacity percentage")
    bess_injection_mw: float = Field(default=0.0, description="Real-time BESS inverter active injection in MW")
    tier1_hospital_status: Literal["PROTECTED", "ONLINE", "AT_RISK", "ISLANDED"] = "PROTECTED"


class SpanStatus(BaseModel):
    id: str
    from_tower: str = Field(alias="from")
    to_tower: str = Field(alias="to")
    upper_status: Literal["NOMINAL", "ENERGIZED", "FAULT_TRIPPED", "DE_ENERGIZED", "OVERLOAD_WARNING"] = "NOMINAL"
    lower_status: Literal["NOMINAL", "ENERGIZED", "STANDBY", "REROUTED_BESS", "TRIPPED"] = "NOMINAL"

    model_config = ConfigDict(populate_by_name=True)



class TelemetryPayload(BaseModel):
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    scenario: ScenarioInfo
    metrics: MetricsInfo
    spans: List[SpanStatus]
    active_cypher: str = ""
    agent_log: str = ""


class BlastRadiusRequest(BaseModel):
    origin_node_id: str = "T3"
    max_hops: int = Field(6, ge=1, le=12)


class DownstreamOverload(BaseModel):
    substation_id: str
    city: str
    capacity_mw: float
    projected_load_mw: float
    load_pct: float
    hops_from_fault: int
    thermal_collapse_seconds: int = 90
    risk_level: Literal["CRITICAL", "HIGH", "MODERATE", "LOW"]


class BlastRadiusResult(BaseModel):
    origin_node_id: str
    max_hops: int
    overloaded_substations: List[DownstreamOverload]
    executed_cypher: str
    cypher_audit_hash: str
    latency_ms: float
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class BessCandidate(BaseModel):
    id: str
    soc_pct: float
    max_discharge_mw: float
    available_mw: float
    status: str


class Tier3LoadCandidate(BaseModel):
    consumer_id: str
    name: str
    breaker_id: str
    mw_demand: float
    priority_tier: int = 3
    energized: bool


class MitigationAssetsResult(BaseModel):
    substation_id: str
    required_mw: float
    available_bess: List[BessCandidate]
    interruptible_loads: List[Tier3LoadCandidate]
    shielded_tier1_assets: List[str] = [TIER1_HOSPITAL_ID]
    executed_cypher: str
    cypher_audit_hash: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class TripBreakerRequest(BaseModel):
    breaker_id: str
    reason_code: str
    cypher_audit_hash: str


class TripBreakerResult(BaseModel):
    breaker_id: str
    status: Literal["TRIPPED", "OPEN", "REJECTED_PROTECTED_ASSET", "FAILED"]
    shed_mw: float
    cypher_audit_hash: str
    reason_code: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    detail: str


class DispatchBessRequest(BaseModel):
    bess_id: str
    inject_mw: float = Field(gt=0, le=60, allow_inf_nan=False)
    target_circuit: Literal["UPPER_PRIMARY", "LOWER_BACKUP"] = "LOWER_BACKUP"


class DispatchBessResult(BaseModel):
    bess_id: str
    status: Literal["DISCHARGING", "STANDBY", "REJECTED_LOW_SOC", "REJECTED_OVER_CAPACITY", "FAILED"]
    injected_mw: float
    target_circuit: str
    remaining_soc_pct: float
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    detail: str


class AuditRecord(BaseModel):
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    run_id: str
    scenario_id: int
    action: str
    target: str
    evidence_query: str
    evidence_hash: str
    reason: str
    outcome: Literal["SUCCESS", "REJECTED", "FAILED"]
    detail: str
