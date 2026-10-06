# EDU-C2-010 - Unit Tests: ClassifyAnomalyNode (inner domain node 4)
#
# Determines the primary anomaly_type + overall anomaly_confidence from the
# per-metric deviation_scores. Single flagged metric -> its mapped anomaly_type;
# >=2 flagged -> multi_signal; none -> no_anomaly. Confidence is a [0,1]
# normalisation of the triggering |z| (ceiling 4.0). Pure classification logic.
#
# Deterministic - no LLM, no network. framework.* / src.* imports only.

import pytest

from src.nodes.classify_anomaly_node import ClassifyAnomalyNode
from src.schemas.state import to_json


def _score(z, anomalous):
    return {"value": 0.0, "z_score": z, "percentile": 0.5, "is_anomalous": anomalous}


def _state(deviation_scores: dict):
    return {"deviation_scores": to_json(deviation_scores)}


class TestClassifyAnomalyType:
    def test_single_metric_flagged_maps_to_its_anomaly_type(self):
        node = ClassifyAnomalyNode()
        # grade_delta -> grade_drop.
        result = node.execute(_state({"grade_delta": _score(5.0, True)}))
        assert result["anomaly_type"] == "grade_drop"

    def test_attendance_metric_maps_to_attendance_gap(self):
        node = ClassifyAnomalyNode()
        result = node.execute(_state({"attendance_rate": _score(-3.0, True)}))
        assert result["anomaly_type"] == "attendance_gap"

    def test_similarity_metric_maps_to_similarity_flag(self):
        node = ClassifyAnomalyNode()
        result = node.execute(_state({"similarity_score": _score(4.0, True)}))
        assert result["anomaly_type"] == "similarity_flag"

    def test_multi_signal_when_two_or_more_flagged(self):
        node = ClassifyAnomalyNode()
        scores = {
            "grade_delta": _score(3.0, True),
            "attendance_rate": _score(-2.5, True),
        }
        result = node.execute(_state(scores))
        assert result["anomaly_type"] == "multi_signal"

    def test_no_flags_yields_no_anomaly(self):
        node = ClassifyAnomalyNode()
        scores = {
            "grade_delta": _score(0.5, False),
            "attendance_rate": _score(1.0, False),
        }
        result = node.execute(_state(scores))
        assert result["anomaly_type"] == "no_anomaly"
        assert result["anomaly_confidence"] == 0.0


class TestClassifyAnomalyConfidence:
    def test_confidence_score_in_range(self):
        node = ClassifyAnomalyNode()
        for z in (2.0, 3.5, 5.0, 8.0):
            result = node.execute(_state({"grade_delta": _score(z, True)}))
            conf = result["anomaly_confidence"]
            assert 0.0 <= conf <= 1.0, f"confidence {conf} out of [0,1] for z={z}"

    def test_high_z_saturates_confidence_to_one(self):
        node = ClassifyAnomalyNode()
        # |z| 8.0 / ceiling 4.0 -> clamps to 1.0.
        result = node.execute(_state({"grade_delta": _score(8.0, True)}))
        assert result["anomaly_confidence"] == pytest.approx(1.0)

    def test_confidence_proportional_below_ceiling(self):
        node = ClassifyAnomalyNode()
        # |z| 2.0 / ceiling 4.0 -> 0.5.
        result = node.execute(_state({"grade_delta": _score(2.0, True)}))
        assert result["anomaly_confidence"] == pytest.approx(0.5)


class TestClassifyAnomalyRobustness:
    def test_empty_scores_is_no_anomaly(self):
        node = ClassifyAnomalyNode()
        result = node.execute(_state({}))
        assert result["anomaly_type"] == "no_anomaly"

    def test_malformed_score_entry_is_skipped(self):
        node = ClassifyAnomalyNode()
        scores = {"grade_delta": "not-a-dict", "attendance_rate": _score(3.0, True)}
        result = node.execute(_state(scores))
        # Only the well-formed flagged metric counts -> single -> attendance_gap.
        assert result["anomaly_type"] == "attendance_gap"

    def test_missing_deviation_scores_is_non_fatal(self):
        node = ClassifyAnomalyNode()
        result = node.execute({})
        assert result["anomaly_type"] == "no_anomaly"


class TestClassifyAnomalyContract:
    def test_execute_method_signature(self):
        """Node contract: execute(self, state); no legacy invoke entry point."""
        import inspect

        assert hasattr(ClassifyAnomalyNode, "execute")
        params = list(inspect.signature(ClassifyAnomalyNode.execute).parameters.keys())
        assert len(params) >= 2
        assert params[1] == "state"
        assert "_invoke_impl" not in ClassifyAnomalyNode.__dict__
