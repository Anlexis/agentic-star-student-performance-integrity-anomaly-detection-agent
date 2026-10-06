# EDU-C2-010 - Unit Tests: GenerateInterventionAlertNode (inner node 5; terminal)
#
# Maps the classified anomaly to a recommended intervention, computes severity
# from confidence (LOW < medium_at <= MEDIUM < high_at <= HIGH, bands resolved
# from the config-seeded thresholds_config field), builds an evidence summary
# from the flagged metrics, runs an output pre-screen, and emits the
# intervention_alert + serialised result. On a pre-screen violation it returns
# ERROR and emits NO alert/result.
#
# Output schema: the alert renders cohort-relative AGGREGATES only, each
# rounded to 2 decimal places, and carries the schema note stating that
# contract. Raw measured values never appear.
#
# Deterministic - no LLM, no network. framework.* / src.* imports only.

import inspect
import json
import re

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.generate_intervention_alert_node import GenerateInterventionAlertNode
from src.schemas.state import from_json, to_json


def _score(z=3.0, anomalous=True, percentile=0.99, source="measured"):
    return {
        "z_score": z,
        "percentile": percentile,
        "is_anomalous": anomalous,
        "source": source,
    }


def _state(anomaly_type, confidence, *, deviation_scores=None, data_type="grades", thresholds_config=None):
    state = {
        "anomaly_type": anomaly_type,
        "anomaly_confidence": confidence,
        "deviation_scores": to_json(deviation_scores or {}),
        "anomaly_request": to_json({"student_id": "STU-1", "data_type": data_type}),
    }
    if thresholds_config is not None:
        state["thresholds_config"] = to_json(thresholds_config)
    return state


class TestInterventionActionMapping:
    def test_grade_drop_maps_to_academic_support_action(self):
        node = GenerateInterventionAlertNode()
        result = node.execute(_state("grade_drop", 0.9, deviation_scores={"grade_delta": _score()}))
        assert result["status"] == AgentStatus.SUCCESS.value
        alert = from_json(result["intervention_alert"])
        assert alert["recommended_action"] == "Schedule academic support meeting"
        assert alert["anomaly_type"] == "grade_drop"

    def test_similarity_flag_maps_to_integrity_board(self):
        node = GenerateInterventionAlertNode()
        result = node.execute(_state("similarity_flag", 0.85, deviation_scores={"similarity_score": _score()}))
        alert = from_json(result["intervention_alert"])
        assert alert["recommended_action"] == "Refer to academic integrity board"

    def test_no_anomaly_maps_to_no_intervention(self):
        node = GenerateInterventionAlertNode()
        result = node.execute(_state("no_anomaly", 0.0))
        alert = from_json(result["intervention_alert"])
        assert alert["recommended_action"] == "No intervention required"
        # no_anomaly always renders LOW severity.
        assert alert["severity"] == "LOW"


class TestInterventionSeverity:
    def test_severity_high_at_confidence_0_8(self):
        node = GenerateInterventionAlertNode()
        result = node.execute(_state("grade_drop", 0.8, deviation_scores={"grade_delta": _score()}))
        alert = from_json(result["intervention_alert"])
        assert alert["severity"] == "HIGH"

    def test_severity_medium_band(self):
        node = GenerateInterventionAlertNode()
        result = node.execute(_state("grade_drop", 0.5, deviation_scores={"grade_delta": _score()}))
        alert = from_json(result["intervention_alert"])
        assert alert["severity"] == "MEDIUM"

    def test_severity_low_band(self):
        node = GenerateInterventionAlertNode()
        result = node.execute(_state("grade_drop", 0.2, deviation_scores={"grade_delta": _score()}))
        alert = from_json(result["intervention_alert"])
        assert alert["severity"] == "LOW"

    def test_config_bands_shift_the_severity(self):
        """The live bands come from config, not from hardcoded constants."""
        node = GenerateInterventionAlertNode()
        result = node.execute(
            _state(
                "grade_drop",
                0.5,
                deviation_scores={"grade_delta": _score()},
                thresholds_config={"confidence_medium": 0.2, "confidence_high": 0.45},
            )
        )
        alert = from_json(result["intervention_alert"])
        # 0.5 is MEDIUM under the defaults but HIGH under these bands.
        assert alert["severity"] == "HIGH"

    @pytest.mark.parametrize(
        "bad_config",
        [
            {"confidence_medium": "NaN", "confidence_high": "Infinity"},
            {"confidence_medium": 0.9, "confidence_high": 0.4},  # inverted
            {"confidence_medium": -1.0, "confidence_high": 5.0},  # out of range
        ],
    )
    def test_malformed_config_bands_degrade_to_defaults(self, bad_config):
        node = GenerateInterventionAlertNode()
        result = node.execute(
            _state(
                "grade_drop",
                0.5,
                deviation_scores={"grade_delta": _score()},
                thresholds_config=bad_config,
            )
        )
        alert = from_json(result["intervention_alert"])
        assert alert["severity"] == "MEDIUM"  # the documented default bands


class TestInterventionAlertShape:
    def test_intervention_alert_is_json_serialisable(self):
        node = GenerateInterventionAlertNode()
        result = node.execute(_state("grade_drop", 0.9, deviation_scores={"grade_delta": _score()}))
        # intervention_alert + result are JSON strings; round-trip must succeed.
        alert = json.loads(result["intervention_alert"])
        assert isinstance(alert, dict)
        assert json.loads(result["result"]) == alert

    def test_evidence_summary_lists_flagged_metrics(self):
        node = GenerateInterventionAlertNode()
        result = node.execute(_state("grade_drop", 0.9, deviation_scores={"grade_delta": _score()}))
        alert = from_json(result["intervention_alert"])
        assert isinstance(alert["evidence_summary"], list)
        assert any("grade_delta" in line for line in alert["evidence_summary"])

    def test_result_equals_intervention_alert(self):
        node = GenerateInterventionAlertNode()
        result = node.execute(_state("grade_drop", 0.9, deviation_scores={"grade_delta": _score()}))
        assert result["result"] == result["intervention_alert"]

    def test_alert_carries_the_schema_note(self):
        node = GenerateInterventionAlertNode()
        result = node.execute(_state("grade_drop", 0.9, deviation_scores={"grade_delta": _score()}))
        alert = from_json(result["intervention_alert"])
        assert "rounded to 2 decimal places" in alert["schema_note"]

    def test_evidence_summary_renders_on_the_two_decimal_grid(self):
        node = GenerateInterventionAlertNode()
        result = node.execute(
            _state(
                "grade_drop",
                0.9,
                deviation_scores={"grade_delta": _score(z=-3.126789, percentile=0.000883)},
            )
        )
        alert = from_json(result["intervention_alert"])
        line = alert["evidence_summary"][0]
        assert "z=-3.13" in line
        assert "percentile=0.00" in line
        # No token in the rendered line carries more than 2 fraction digits.
        assert not re.search(r"\d+\.\d{3,}", line)

    def test_confidence_rendered_on_the_two_decimal_grid(self):
        node = GenerateInterventionAlertNode()
        result = node.execute(_state("grade_drop", 0.87654321, deviation_scores={"grade_delta": _score()}))
        alert = from_json(result["intervention_alert"])
        assert alert["confidence"] == 0.88

    def test_raw_measured_value_never_rendered(self):
        """The alert renders cohort-relative aggregates only - a raw measured
        value carried on the score dict must not reach the alert."""
        node = GenerateInterventionAlertNode()
        scores = dict(_score())
        scores["value"] = 123456.789  # a raw figure a future change might add
        result = node.execute(_state("grade_drop", 0.9, deviation_scores={"grade_delta": scores}))
        blob = result["intervention_alert"]
        assert "123456" not in blob
        assert "123,456" not in blob


class TestInterventionPreScreen:
    def test_student_email_in_alert_is_blocked(self):
        node = GenerateInterventionAlertNode()
        result = node.execute(_state("grade_drop", 0.9, deviation_scores={"grade_delta": _score()}, data_type=""))
        # Baseline: a clean alert passes.
        assert result["status"] == AgentStatus.SUCCESS.value

        tainted = _state("grade_drop", 0.9, deviation_scores={"grade_delta": _score()})
        tainted["anomaly_request"] = to_json({"student_id": "STU-1", "data_type": "student.taro@example.com"})
        blocked = node.execute(tainted)
        assert blocked["status"] == AgentStatus.ERROR.value
        assert "intervention_alert" not in blocked
        assert "result" not in blocked


class TestInterventionContract:
    def test_execute_method_signature(self):
        """Node contract: execute(self, state); no legacy invoke entry point."""
        assert hasattr(GenerateInterventionAlertNode, "execute")
        params = list(inspect.signature(GenerateInterventionAlertNode.execute).parameters.keys())
        assert len(params) >= 2
        assert params[1] == "state"
        assert "_invoke_impl" not in GenerateInterventionAlertNode.__dict__
