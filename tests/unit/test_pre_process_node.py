# EDU-C2-010 - Unit Tests: PreProcessNode (outer pre_process slot; input gate)
#
# PreProcessNode parses + validates the JSON anomaly-detection request, rejects
# empty / malformed / unsupported input, surface-strips student PII (e-mail /
# phone) into validated_input, and emits anomaly_request as a JSON string.
#
# Tests drive node.execute({state}) directly (the trust gate lives in
# BaseNode.__call__, which we do not exercise here). Deterministic - no LLM, no
# network. framework.* / src.* imports only.

import json


from framework.schemas.agent_status import AgentStatus

from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import from_json


def _request(**overrides):
    base = {
        "student_id": "STU-12345",
        "data_type": "grades",
        "period": {"start": "2025-04", "end": "2025-09"},
        "thresholds": {"z_score": 2.0},
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


class TestPreProcessSuccess:
    def test_valid_json_input_parsed_correctly(self):
        node = PreProcessNode()
        result = node.execute({"user_input": _request()})
        assert result["status"] == AgentStatus.SUCCESS.value
        # anomaly_request is a JSON STRING (checkpoint msgpack safety).
        assert isinstance(result["anomaly_request"], str)
        req = from_json(result["anomaly_request"])
        assert req["student_id"] == "STU-12345"
        assert req["data_type"] == "grades"
        assert req["period"] == {"start": "2025-04", "end": "2025-09"}

    def test_validated_input_is_populated(self):
        node = PreProcessNode()
        result = node.execute({"user_input": _request()})
        assert isinstance(result["validated_input"], str)
        assert result["validated_input"].strip()

    def test_enriched_context_carries_channel(self):
        node = PreProcessNode()
        result = node.execute({"user_input": _request(), "input_context": {"channel": "lms"}})
        assert result["enriched_context"]["channel"] == "lms"
        assert result["enriched_context"]["source"] == "StudentPerformanceAnomalyDetectionAgent"

    def test_surface_pii_stripped_from_validated_input(self):
        """Free-text e-mail / phone are redacted; opaque student_id is preserved."""
        node = PreProcessNode()
        raw = json.dumps(
            {
                "student_id": "STU-77",
                "data_type": "grades",
                "note": "contact taro@example.com / tel 03-1234-5678",
            },
            ensure_ascii=False,
        )
        result = node.execute({"user_input": raw})
        vi = result["validated_input"]
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "taro@example.com" not in vi
        assert "03-1234-5678" not in vi
        assert "[REDACTED]" in vi
        # The opaque student_id is NOT PII and must survive.
        assert "STU-77" in vi


class TestPreProcessRejection:
    def test_empty_user_input_returns_error(self):
        node = PreProcessNode()
        result = node.execute({"user_input": ""})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["error_log"]

    def test_whitespace_only_user_input_returns_error(self):
        node = PreProcessNode()
        result = node.execute({"user_input": "   \n\t "})
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_missing_user_input_returns_error(self):
        node = PreProcessNode()
        result = node.execute({})
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_non_json_input_returns_error(self):
        node = PreProcessNode()
        result = node.execute({"user_input": "this is not json"})
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_non_object_json_returns_error(self):
        """A JSON array (not an object) must be rejected."""
        node = PreProcessNode()
        result = node.execute({"user_input": json.dumps([1, 2, 3])})
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_missing_student_id_returns_error(self):
        node = PreProcessNode()
        payload = json.dumps({"data_type": "grades"}, ensure_ascii=False)
        result = node.execute({"user_input": payload})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("student_id" in m for m in result["error_log"])

    def test_missing_data_type_returns_error(self):
        node = PreProcessNode()
        payload = json.dumps({"student_id": "STU-1"}, ensure_ascii=False)
        result = node.execute({"user_input": payload})
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_unsupported_data_type_returns_error(self):
        node = PreProcessNode()
        result = node.execute({"user_input": _request(data_type="telepathy")})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("telepathy" in m for m in result["error_log"])


class TestPreProcessContract:
    def test_execute_method_signature(self):
        """Node contract: execute(self, state); no legacy invoke entry point."""
        import inspect

        assert hasattr(PreProcessNode, "execute")
        sig = inspect.signature(PreProcessNode.execute)
        params = list(sig.parameters.keys())
        assert len(params) >= 2, f"execute() must accept (self, state), got {params}"
        assert params[1] == "state", f"second parameter must be 'state', got {params[1]!r}"
        assert "_invoke_impl" not in PreProcessNode.__dict__, "_invoke_impl() must not be defined - use execute()"
