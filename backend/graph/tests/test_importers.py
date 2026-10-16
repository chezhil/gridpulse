"""MATPOWER parsing and conversion (no database)."""

import pytest

from graph import importers
from graph.client import InvalidRequestError
from graph.networks import sample_file, snapshot_from_spec, validate_spec
from graph.thermal import project_loading

TINY = """
function mpc = tiny
% a 3-bus test with comments, a phase shifter, an isolated bus and a negative load
mpc.version = '2';
mpc.baseMVA = 100;
mpc.bus = [
  1 3 0   0 0 0 1 1 0 230 1 1.1 0.9;   % slack
  2 1 90  0 0 0 1 1 0 230 1 1.1 0.9;
  3 1 -20 0 0 0 1 1 0 230 1 1.1 0.9;
  4 4 10  0 0 0 1 1 0 230 1 1.1 0.9;   % isolated
];
mpc.gen = [
  1 0 0 300 -300 1 100 1 1e9 0;
  2 30 0 50 -50 1 100 1 50 0;
  2 99 0 50 -50 1 100 0 50 0;          % out of service
];
mpc.branch = [
  1 2 0.01 0.10 0 150 0 0 0    0 1 -360 360;
  2 3 0.01 0.20 0 0   0 0 1.05 5 1 -360 360;
  1 3 0.01 0.15 0 0   0 0 0    0 0 -360 360;  % out of service
];
mpc.gencost = [2 0 0 3 0.1 20 0];
"""


def test_parse_and_convert_tiny_case():
    mpc = importers.parse_matpower(TINY)
    assert mpc["baseMVA"] == 100 and len(mpc["bus"]) == 4 and len(mpc["branch"]) == 3
    spec, notes, flows = importers.matpower_to_spec(mpc, "tiny", battery_mw=10)
    out, warnings = validate_spec(spec)
    assert warnings == []
    gens = {g["id"]: g for g in out["generators"]}
    assert gens["GEN_000_B001"]["capacity_mw"] is None            # 1e9 placeholder = unlimited
    assert gens["GEN_B002"]["output_mw"] == pytest.approx(30.0)    # out-of-service unit ignored
    assert gens["GEN_B003"]["output_mw"] == pytest.approx(20.0)    # negative load -> generation
    assert [ln["id"] for ln in out["lines"]] == ["BR001", "BR002"]  # out-of-service branch dropped
    assert out["lines"][0]["limit_mw"] == 150.0                    # rateA used when given
    assert out["lines"][1]["impedance_pu"] == pytest.approx(0.21)  # x * tap ratio
    assert "B004" not in {b["id"] for b in out["buses"]} | {s["id"] for s in out["substations"]}
    joined = " ".join(notes)
    assert "isolated" in joined and "phase-shifting" in joined and "negative load" in joined
    # The imported spec solves: 90 MW load at bus 2, 30 from GEN_B002, 20 from bus 3, slack supplies 40.
    p = project_loading(snapshot_from_spec(out))
    slack = next(g for g in p["generators"] if g["role"] == "SLACK")
    assert slack["output_mw"] == pytest.approx(40.0)


def test_bundled_samples_parse():
    for name, buses in (("ieee14", 14), ("ieee30", 30), ("ieee118", 118)):
        mpc = importers.parse_matpower(sample_file(name))
        assert len(mpc["bus"]) == buses


@pytest.mark.parametrize("content,fragment", [
    ("", "empty"),
    ("mpc.bus = [1 3 0];", "missing"),
    ("mpc.bus = [1 3 x 0];\nmpc.gen=[1 0 0 0 0 1 100 1 10 0];\nmpc.branch=[1 2 0 .1 0 0 0 0 0 0 1];", "non-numeric"),
    ("mpc.bus = [1 3];\nmpc.gen=[1 0 0 0 0 1 100 1 10 0];\nmpc.branch=[1 2 0 .1 0 0 0 0 0 0 1];", "at least 3 columns"),
])
def test_bad_files_are_rejected(content, fragment):
    with pytest.raises(InvalidRequestError) as exc:
        importers.parse_matpower(content)
    assert fragment in exc.value.message


def test_convert_gridpulse_json_and_bad_format():
    out = importers.convert("gridpulse", '{"spec": {"name": "x", "generators": []}}', name="renamed")
    assert out["spec"]["name"] == "renamed"
    with pytest.raises(InvalidRequestError):
        importers.convert("cim", "...")
    with pytest.raises(InvalidRequestError):
        importers.convert("matpower", TINY, options={"tier1_buses": ["a"]})
