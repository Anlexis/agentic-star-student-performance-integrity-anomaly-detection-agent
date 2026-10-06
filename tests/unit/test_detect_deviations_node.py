# EDU-C2-010 - Unit Tests: DetectDeviationsNode (inner domain node 3)
#
# Computes per-metric deviation scores (z-score / percentile / is_anomalous)
# comparing the student against the cohort baseline. When the request carries
# explicit per-metric measurements (request["metrics"]), those are used directly
# - which makes the z-score deterministic and assertable. Pure computation, no
# external service.
#
# Deterministic - no LLM, no network. framework.* / src.* imports only.

import pytest

from src.nodes.detect_deviations_node import DetectDeviationsNode
from src.schemas.state import from_json, to_json


def _state(baseline: dict, *, metrics=None, z_score=2.0, student_id="STU-1"):
    req = {
        "student_id": student_id,
        "data_type": "grades",
        "thresholds": {"z_score": z_score},
    }
    if metrics is not None:
        req["metrics"] = metrics
    return {
        "anomaly_request": to_json(req),
        "cohort_baseline": to_json(baseline),
    }


# A single-metric baseline: mean=0, std=8 (mirrors the grades grade_delta stats).
_GRADE_BASELINE = {"grade_delta": {"mean": 0.0, "std": 8.0, "p25": -5.0, "p75": 5.0, "n_students": 240}}


class TestDetectDeviationsScoring:
    def test_above_threshold_z_score_flagged_anomalous(self):
        node = DetectDeviationsNode()
        # value 40, mean 0, std 8 -> z = 5.0 >= 2.0 -> anomalous.
        result = node.execute(_state(_GRADE_BASELINE, metrics={"grade_delta": 40.0}))
        scores = from_json(result["deviation_scores"])
        assert "grade_delta" in scores
        assert scores["grade_delta"]["z_score"] == pytest.approx(5.0)
        assert scores["grade_delta"]["is_anomalous"] is True

    def test_within_baseline_not_flagged(self):
        node = DetectDeviationsNode()
        # value 4, mean 0, std 8 -> z = 0.5 < 2.0 -> not anomalous.
        result = node.execute(_state(_GRADE_BASELINE, metrics={"grade_delta": 4.0}))
        scores = from_json(result["deviation_scores"])
        assert scores["grade_delta"]["z_score"] == pytest.approx(0.5)
        assert scores["grade_delta"]["is_anomalous"] is False

    def test_negative_deviation_also_flagged_by_abs_z(self):
        node = DetectDeviationsNode()
        # value -24, mean 0, std 8 -> z = -3.0; abs(z)=3.0 >= 2.0 -> anomalous.
        result = node.execute(_state(_GRADE_BASELINE, metrics={"grade_delta": -24.0}))
        scores = from_json(result["deviation_scores"])
        assert scores["grade_delta"]["z_score"] == pytest.approx(-3.0)
        assert scores["grade_delta"]["is_anomalous"] is True

    def test_percentile_in_unit_interval(self):
        node = DetectDeviationsNode()
        result = node.execute(_state(_GRADE_BASELINE, metrics={"grade_delta": 8.0}))
        pct = from_json(result["deviation_scores"])["grade_delta"]["percentile"]
        assert 0.0 <= pct <= 1.0

    def test_custom_threshold_changes_flagging(self):
        node = DetectDeviationsNode()
        # z = 2.5; with z_score threshold 3.0 it must NOT be flagged.
        result = node.execute(_state(_GRADE_BASELINE, metrics={"grade_delta": 20.0}, z_score=3.0))
        scores = from_json(result["deviation_scores"])
        assert scores["grade_delta"]["z_score"] == pytest.approx(2.5)
        assert scores["grade_delta"]["is_anomalous"] is False


class TestDetectDeviationsRobustness:
    def test_missing_baseline_metric_handled_gracefully(self):
        """A malformed (non-dict) baseline metric is skipped, not crashed on."""
        node = DetectDeviationsNode()
        baseline = {"grade_delta": "not-a-dict"}
        result = node.execute(_state(baseline, metrics={"grade_delta": 40.0}))
        scores = from_json(result["deviation_scores"])
        # The malformed metric is skipped -> empty score map, no exception.
        assert scores == {}

    def test_empty_baseline_yields_empty_scores(self):
        node = DetectDeviationsNode()
        result = node.execute(_state({}, metrics={}))
        assert from_json(result["deviation_scores"]) == {}

    def test_deviation_scores_is_json_string(self):
        node = DetectDeviationsNode()
        result = node.execute(_state(_GRADE_BASELINE, metrics={"grade_delta": 1.0}))
        assert isinstance(result["deviation_scores"], str)


class TestDetectDeviationsContract:
    def test_execute_method_signature(self):
        """Node contract: execute(self, state); no legacy invoke entry point."""
        import inspect

        assert hasattr(DetectDeviationsNode, "execute")
        params = list(inspect.signature(DetectDeviationsNode.execute).parameters.keys())
        assert len(params) >= 2
        assert params[1] == "state"
        assert "_invoke_impl" not in DetectDeviationsNode.__dict__
