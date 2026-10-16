"""Pure tests of the radial-dc/1 loading model (no FalkorDB needed)."""

import pytest

from graph.client import GraphDataError
from graph.thermal import project_loading

LIGHTNING = [("TX_882", "UPPER_PRIMARY")]


def circuit(result, span_id, name):
    return next(c for c in result["circuits"] if c["span_id"] == span_id and c["circuit"] == name)


def sub(result, sid="SUB_METRO"):
    return next(s for s in result["substations"] if s["id"] == sid)


def set_consumer(snapshot, cid, **props):
    next(c for c in snapshot["consumers"] if c["id"] == cid).update(props)


def test_nominal_is_within_limits(snapshot):
    r = project_loading(snapshot)
    assert r["outcome"] == "WITHIN_LIMITS"
    assert r["required_relief_mw"] == 0
    assert circuit(r, "TX_882", "UPPER_PRIMARY")["flow_mw"] == pytest.approx(504.0)
    assert circuit(r, "TX_882", "LOWER_BACKUP")["flow_mw"] == pytest.approx(336.0)
    assert sub(r)["loading_pct"] == pytest.approx(84.0)


def test_lightning_overloads_backup_on_faulted_segment_only(snapshot):
    r = project_loading(snapshot, LIGHTNING)
    assert r["outcome"] == "OVERLOAD_PREDICTED"
    backup = circuit(r, "TX_882", "LOWER_BACKUP")
    assert backup["flow_mw"] == pytest.approx(840.0)
    assert backup["loading_pct"] == pytest.approx(105.0)
    assert circuit(r, "TX_882", "UPPER_PRIMARY")["flow_mw"] == 0
    assert [o["element"] for o in r["overloads"]] == ["TX_882:LOWER_BACKUP"]
    assert r["required_relief_mw"] == pytest.approx(80.0)
    assert sub(r)["projected_status"] == "OVERLOAD_RISK"
    assert sub(r)["overloaded_supply_circuits"] == ["TX_882:LOWER_BACKUP"]


def test_stored_trip_matches_assumed_trip(snapshot):
    circ = next(c for c in snapshot["circuits"] if c["span_id"] == "TX_882" and c["circuit"] == "UPPER_PRIMARY")
    circ["status"] = "TRIPPED"
    assert project_loading(snapshot)["required_relief_mw"] == pytest.approx(80.0)


def test_bess_alone_is_not_enough_but_bess_plus_shed_is(snapshot):
    snapshot["bess"][0]["active_injection_mw"] = 45.0
    r = project_loading(snapshot, LIGHTNING)
    assert r["outcome"] == "OVERLOAD_PREDICTED"
    assert r["required_relief_mw"] == pytest.approx(35.0)

    set_consumer(snapshot, "IND_METRO_12", energized=False)
    r = project_loading(snapshot, LIGHTNING)
    assert r["outcome"] == "WITHIN_LIMITS"
    assert circuit(r, "TX_882", "LOWER_BACKUP")["flow_mw"] == pytest.approx(840 - 45 - 62)


def test_heatwave_overloads_substation_not_lines(snapshot):
    for c in snapshot["consumers"]:
        c["mw_demand"] *= 1.2
    r = project_loading(snapshot)
    assert [o["element"] for o in r["overloads"]] == ["SUB_METRO"]
    assert sub(r)["loading_pct"] == pytest.approx(100.8)
    assert r["required_relief_mw"] == pytest.approx(1008 - 950)


def test_both_circuits_tripped_islands_substation(snapshot):
    r = project_loading(snapshot, [("TX_884", "UPPER_PRIMARY"), ("TX_884", "LOWER_BACKUP")])
    assert r["outcome"] == "ISLANDING_PREDICTED"
    assert r["islanded_substations"] == ["SUB_METRO"]
    assert sub(r)["projected_status"] == "ISLANDED"
    assert circuit(r, "TX_880", "UPPER_PRIMARY")["flow_mw"] == 0
    assert "SUB_METRO" not in r["energized_nodes"]


def test_threshold_is_respected(snapshot):
    assert project_loading(snapshot, LIGHTNING, threshold_pct=110)["outcome"] == "WITHIN_LIMITS"


def test_missing_property_is_explicit(snapshot):
    del snapshot["consumers"][0]["mw_demand"]
    with pytest.raises(GraphDataError) as exc:
        project_loading(snapshot)
    assert exc.value.details["problems"][0]["missing"] == ["mw_demand"]


def test_unknown_assumed_circuit_is_rejected(snapshot):
    from graph.client import InvalidRequestError
    with pytest.raises(InvalidRequestError):
        project_loading(snapshot, [("TX_999", "UPPER_PRIMARY")])


def test_projection_does_not_mutate_snapshot(snapshot):
    before = repr(snapshot)
    project_loading(snapshot, LIGHTNING)
    assert repr(snapshot) == before
