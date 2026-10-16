"""The showcase grids must stay valid and tell the stories their descriptions promise."""
import pytest

from graph import networks as nw
from graph.contingency import scan_n1
from graph.showcase import SHOWCASE
from graph.thermal import project_loading


@pytest.mark.parametrize("key", sorted(SHOWCASE))
def test_showcase_grid_is_valid_and_healthy_at_normal_demand(key):
    spec, warnings = nw.validate_spec(SHOWCASE[key]())
    assert warnings == []
    snapshot = nw.snapshot_from_spec(spec)
    assert project_loading(snapshot)["outcome"] == "WITHIN_LIMITS"
    # Every element is drawn somewhere, so the editor opens the grid as designed.
    ids = {x["id"] for part in ("generators", "buses", "substations", "loads", "batteries") for x in spec[part]}
    assert ids <= set(spec["layout"])
    # Each grid has at least one single-line loss worth demonstrating.
    assert any(not c["secure"] for c in scan_n1(snapshot, 95.0)["contingencies"])


def test_cascade_valley_spur_islands_the_clinic():
    snapshot = nw.snapshot_from_spec(nw.validate_spec(SHOWCASE["cascade_valley"]())[0])
    assert project_loading(snapshot, [("SPUR_SUMMIT", "UPPER_PRIMARY")])["outcome"] == "ISLANDING_PREDICTED"


def test_showcase_grids_are_templates():
    assert set(SHOWCASE) <= {t["id"] for t in nw.list_templates()["templates"]}
