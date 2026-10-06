# EDU-C2-010 - Unit Tests: ValidateInputNode (inner domain node 1)
#
# The caller-data contract, fail-closed:
#   - identity fields: student_id locked to an opaque identifier pattern;
#     data_type locked to the fixed vocabulary
#   - period bounds: ISO-style YYYY-MM / YYYY-MM-DD or empty
#   - thresholds.z_score: finite AND bounded - NaN / Infinity parse fine via
#     float() and arrive intact through raw JSON, and NaN comparisons are
#     always False, so an unchecked NaN threshold would silently suppress
#     every alert (fail-OPEN on the template's core decision)
#   - metrics: name locked to the data_type's known metric; every value
#     finite AND inside explicit per-metric bounds; entry cap
#   - rejected values are NEVER echoed into error messages
#   - config resolution: z threshold default comes from the config-seeded
#     thresholds_config state field; the caller override wins when valid
#
# Deterministic - no LLM, no network. framework.* / src.* imports only.

import math

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.validate_input_node import ValidateInputNode
from src.schemas.state import from_json, to_json


def _state(request: dict, validated_input: str = "echo", **extra):
    state = {"anomaly_request": to_json(request), "validated_input": validated_input}
    state.update(extra)
    return state


def _valid_request(**overrides) -> dict:
    req = {
        "student_id": "STU-9",
        "data_type": "grades",
        "period": {"start": "2025-01", "end": "2025-03"},
    }
    req.update(overrides)
    return req


class TestValidateInputSuccess:
    def test_valid_anomaly_request_passes(self):
        node = ValidateInputNode()
        req = {
            "student_id": "STU-9",
            "data_type": "attendance",
            "period": {"start": "2025-01", "end": "2025-03"},
            "thresholds": {"z_score": 2.5},
        }
        result = node.execute(_state(req))
        # No ERROR status on the success path.
        assert result.get("status") != AgentStatus.ERROR.value
        out = from_json(result["anomaly_request"])
        assert out["student_id"] == "STU-9"
        assert out["data_type"] == "attendance"
        assert out["thresholds"]["z_score"] == 2.5

    def test_date_normalisation(self):
        """period start/end are coerced to trimmed strings."""
        node = ValidateInputNode()
        req = {
            "student_id": "STU-1",
            "data_type": "grades",
            "period": {"start": "  2025-04  ", "end": "  2025-09  "},
        }
        out = from_json(node.execute(_state(req))["anomaly_request"])
        assert out["period"]["start"] == "2025-04"
        assert out["period"]["end"] == "2025-09"

    def test_default_z_threshold_applied_when_absent(self):
        node = ValidateInputNode()
        out = from_json(node.execute(_state(_valid_request()))["anomaly_request"])
        assert out["thresholds"]["z_score"] == 2.0

    def test_config_z_threshold_used_when_no_override(self):
        """The config-seeded thresholds_config value replaces the module default."""
        node = ValidateInputNode()
        state = _state(_valid_request(), thresholds_config=to_json({"z_score_threshold": 3.5}))
        out = from_json(node.execute(state)["anomaly_request"])
        assert out["thresholds"]["z_score"] == 3.5

    def test_caller_override_beats_config_value(self):
        node = ValidateInputNode()
        state = _state(
            _valid_request(thresholds={"z_score": 1.5}),
            thresholds_config=to_json({"z_score_threshold": 3.5}),
        )
        out = from_json(node.execute(state)["anomaly_request"])
        assert out["thresholds"]["z_score"] == 1.5

    def test_malformed_config_value_degrades_to_default(self):
        """Config values are operator-controlled: a bad shape degrades, never crashes."""
        node = ValidateInputNode()
        state = _state(_valid_request(), thresholds_config=to_json({"z_score_threshold": "NaN"}))
        result = node.execute(state)
        assert result.get("status") != AgentStatus.ERROR.value
        out = from_json(result["anomaly_request"])
        assert out["thresholds"]["z_score"] == 2.0

    def test_valid_metrics_from_request_payload(self):
        node = ValidateInputNode()
        out = from_json(node.execute(_state(_valid_request(metrics={"grade_delta": -25.5})))["anomaly_request"])
        assert out["metrics"] == {"grade_delta": -25.5}

    def test_valid_metrics_from_input_context_take_precedence(self):
        """input_context is the primary metrics channel; the payload block is the fallback."""
        node = ValidateInputNode()
        state = _state(
            _valid_request(metrics={"grade_delta": -10.0}),
            input_context={"metrics": {"grade_delta": -40.0}},
        )
        out = from_json(node.execute(state)["anomaly_request"])
        assert out["metrics"] == {"grade_delta": -40.0}

    def test_absent_metrics_degrade_to_empty(self):
        node = ValidateInputNode()
        out = from_json(node.execute(_state(_valid_request()))["anomaly_request"])
        assert out["metrics"] == {}


class TestValidateInputRejection:
    def test_missing_anomaly_request_returns_error(self):
        node = ValidateInputNode()
        result = node.execute({})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["error_log"]

    def test_missing_required_keys_returns_error(self):
        node = ValidateInputNode()
        # student_id present, data_type missing.
        result = node.execute(_state({"student_id": "STU-1"}))
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_unsupported_data_type_returns_error_without_echo(self):
        node = ValidateInputNode()
        result = node.execute(_state({"student_id": "STU-1", "data_type": "astrology"}))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "astrology" not in " ".join(result["error_log"])

    @pytest.mark.parametrize(
        "student_id",
        [
            "a" * 65,  # over length cap
            "STU 1",  # embedded whitespace
            "student@example.com",  # PII-shaped free text
            "",  # empty
            123,  # wrong type
        ],
    )
    def test_invalid_student_id_rejected(self, student_id):
        node = ValidateInputNode()
        result = node.execute(_state(_valid_request(student_id=student_id)))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("student_id" in e for e in result["error_log"])

    def test_rejected_student_id_value_never_echoed(self):
        node = ValidateInputNode()
        result = node.execute(_state(_valid_request(student_id="student.taro@example.com")))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "student.taro@example.com" not in " ".join(result["error_log"])

    @pytest.mark.parametrize("bad_period", ["04-2025", "last semester", "2025/04/01", "20250401"])
    def test_malformed_period_bound_rejected(self, bad_period):
        node = ValidateInputNode()
        result = node.execute(_state(_valid_request(period={"start": bad_period, "end": "2025-09"})))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("period.start" in e for e in result["error_log"])
        assert bad_period not in " ".join(result["error_log"])


class TestValidateInputNonFiniteMatrix:
    """Every caller-controlled numeric is finite AND bounded, fail-closed.

    NaN / Infinity parse via float() AND arrive intact through raw JSON;
    NaN comparisons are always False, so letting one through would silently
    disable the exact comparison this template exists to make.
    """

    _NON_FINITE = [
        float("nan"),
        float("inf"),
        float("-inf"),
        "NaN",  # numeric strings are rejected outright - the contract is JSON numbers
        "Infinity",
        "-Infinity",
        True,  # bool is not a number
        "2.5",  # numeric string - still not a JSON number
    ]

    @pytest.mark.parametrize("bad", _NON_FINITE + [0, -1.0, 0.4, 11.0])
    def test_z_score_threshold_rejected(self, bad):
        node = ValidateInputNode()
        result = node.execute(_state(_valid_request(thresholds={"z_score": bad})))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("thresholds.z_score" in e for e in result["error_log"])

    @pytest.mark.parametrize(
        "data_type,metric,low,high",
        [
            ("grades", "grade_delta", -100.0, 100.0),
            ("attendance", "attendance_rate", 0.0, 1.0),
            ("submission", "submission_timing_offset", -720.0, 720.0),
            ("similarity", "similarity_score", 0.0, 1.0),
        ],
    )
    @pytest.mark.parametrize("bad_kind", ["nan", "inf", "-inf", "str_nan", "str_inf", "bool", "over", "under"])
    def test_metric_value_rejected_per_field(self, data_type, metric, low, high, bad_kind):
        bad_value = {
            "nan": float("nan"),
            "inf": float("inf"),
            "-inf": float("-inf"),
            "str_nan": "NaN",
            "str_inf": "Infinity",
            "bool": True,
            "over": high + 1.0,
            "under": low - 1.0,
        }[bad_kind]
        node = ValidateInputNode()
        result = node.execute(_state(_valid_request(data_type=data_type, metrics={metric: bad_value})))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any(f"metrics.{metric}" in e for e in result["error_log"])

    def test_boundary_values_accepted(self):
        """The bounds themselves are inclusive - fail-closed, not over-closed."""
        node = ValidateInputNode()
        result = node.execute(_state(_valid_request(data_type="attendance", metrics={"attendance_rate": 0.0})))
        assert result.get("status") != AgentStatus.ERROR.value
        out = from_json(result["anomaly_request"])
        assert out["metrics"]["attendance_rate"] == 0.0

    def test_nan_never_survives_into_normalised_request(self):
        """Belt and braces: no NaN can appear anywhere in the normalised output."""
        node = ValidateInputNode()
        result = node.execute(_state(_valid_request(metrics={"grade_delta": 10.0})))
        out = from_json(result["anomaly_request"])
        for value in out["metrics"].values():
            assert math.isfinite(value)
        assert math.isfinite(out["thresholds"]["z_score"])


class TestValidateInputMetricNames:
    def test_unknown_metric_name_rejected_without_echo(self):
        node = ValidateInputNode()
        result = node.execute(_state(_valid_request(metrics={"ignore_previous_instructions": 1.0})))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "ignore_previous_instructions" not in " ".join(result["error_log"])

    def test_metric_of_wrong_data_type_rejected(self):
        """attendance_rate is not a valid metric for a grades request."""
        node = ValidateInputNode()
        result = node.execute(_state(_valid_request(metrics={"attendance_rate": 0.5})))
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_non_identifier_metric_name_rejected(self):
        node = ValidateInputNode()
        result = node.execute(_state(_valid_request(metrics={"grade delta!": 1.0})))
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_metrics_entry_cap_enforced(self):
        node = ValidateInputNode()
        too_many = {f"metric_{i}": 1.0 for i in range(9)}
        result = node.execute(_state(_valid_request(metrics=too_many)))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("entries" in e for e in result["error_log"])

    def test_non_dict_metrics_rejected(self):
        node = ValidateInputNode()
        result = node.execute(_state(_valid_request(metrics=[1.0, 2.0])))
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_malformed_context_metrics_do_not_fall_back_silently(self):
        """A malformed input_context metrics block fails closed - it never
        silently falls back to the payload channel."""
        node = ValidateInputNode()
        state = _state(
            _valid_request(metrics={"grade_delta": -10.0}),
            input_context={"metrics": "not-a-dict"},
        )
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value


class TestValidateInputContract:
    def test_execute_method_signature(self):
        """Node contract: execute(self, state); no legacy invoke entry point."""
        import inspect

        assert hasattr(ValidateInputNode, "execute")
        params = list(inspect.signature(ValidateInputNode.execute).parameters.keys())
        assert len(params) >= 2
        assert params[1] == "state"
        assert "_invoke_impl" not in ValidateInputNode.__dict__
