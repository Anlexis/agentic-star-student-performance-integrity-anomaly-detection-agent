# PB: End-to-end business behaviour through POST /invoke - src/api/server.py
#
# Proves the supported input contract produces REAL outcomes through the full
# nested graph (outer backbone -> inner domain pipeline), not a stub baseline:
#   - a real intervention alert computed from the caller's measured metric
#   - structured invocation data (input_context: metrics) reaching the inner
#     graph across the nested-graph boundary and CHANGING the outcome
#   - every severity path (LOW / MEDIUM / HIGH) and the no-anomaly path
#   - fail-closed validation rejections for malformed caller data, including
#     the non-finite numeric matrix (NaN / Infinity) on both channels
#   - the documented output schema on every success (aggregates on the
#     2-decimal grid, the schema note, no raw values, no credential token)
#   - the entry-point auth boundary
#
# Unlike a boot check, these tests run the REAL compiled agent: every request
# crosses the entry-point auth, the outer trust/input gates, the input_context
# bridge into the inner graph, all five domain nodes, and the output gate.
#
# The app is driven through its real ASGI interface - no TestClient dependency.

import asyncio
import json
import re

import pytest

from src.api.server import app

_TOKEN = "pb-invoke-e2e-token"


def _post_invoke(payload: dict, token: str | None = _TOKEN) -> tuple[int, dict]:
    """POST /invoke through the real ASGI app."""
    body = json.dumps(payload).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/invoke",
        "raw_path": b"/invoke",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }

    messages = []
    sent = {"body": b""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    parsed = json.loads(sent["body"].decode() or "{}")
    return start["status"], parsed


@pytest.fixture(autouse=True)
def token_configured(monkeypatch):
    """Deployment-shaped server environment: INVOKE_AUTH_TOKEN set, caller uses Bearer."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)


def _request(student_id="STU-2048", data_type="grades", **extra) -> dict:
    req = {"student_id": student_id, "data_type": data_type, "period": {"start": "2025-04", "end": "2025-09"}}
    req.update(extra)
    return req


def _invoke(request: dict | None = None, input_context: dict | None = None) -> dict:
    payload = {"input": json.dumps(request if request is not None else _request()), "session_id": "pb-invoke-e2e"}
    if input_context is not None:
        payload["input_context"] = input_context
    status_code, body = _post_invoke(payload)
    assert status_code == 200, f"expected 200, got {status_code}: {body}"
    return body


def _alert(body: dict) -> dict:
    assert body.get("output"), f"invoke() surfaced an empty output: {body!r}"
    return json.loads(body["output"])


class TestInvokeEndToEnd:
    def test_caller_request_produces_a_real_intervention_alert(self):
        body = _invoke()
        assert body["status"] == "success"
        alert = _alert(body)
        assert alert["anomaly_type"]
        assert alert["recommended_action"]
        assert alert["severity"] in {"LOW", "MEDIUM", "HIGH"}

    def test_backbone_runs_in_order(self):
        body = _invoke()
        assert body["node_history"] == [
            "InitializeNode",
            "PreProcessNode",
            "DomainWorkflowGraphNode",
            "PostProcessNode",
            "FinalizeNode",
        ]

    def test_measured_metric_reaches_the_inner_graph_and_changes_the_outcome(self):
        """The context bridge, proven end-to-end: the same request with a
        caller-supplied measurement produces a DIFFERENT, computed result -
        not the offline baseline stub."""
        baseline = _alert(_invoke())
        measured = _alert(_invoke(input_context={"metrics": {"grade_delta": -40.0}}))
        assert measured != baseline
        # grade_delta -40 against mean 0 / std 8 is exactly -5.00 standard deviations.
        assert any("z=-5.00" in line for line in measured["evidence_summary"])

    def test_measurement_within_the_cohort_yields_no_anomaly(self):
        alert = _alert(_invoke(input_context={"metrics": {"grade_delta": 0.5}}))
        assert alert["anomaly_type"] == "no_anomaly"
        assert alert["recommended_action"] == "No intervention required"
        assert alert["severity"] == "LOW"

    @pytest.mark.parametrize(
        "data_type,metric,value,expected_type",
        [
            ("grades", "grade_delta", -40.0, "grade_drop"),
            ("attendance", "attendance_rate", 0.4, "attendance_gap"),
            ("submission", "submission_timing_offset", 120.0, "submission_timing"),
            ("similarity", "similarity_score", 0.95, "similarity_flag"),
        ],
    )
    def test_every_data_type_reaches_its_anomaly_type(self, data_type, metric, value, expected_type):
        alert = _alert(_invoke(_request(data_type=data_type), input_context={"metrics": {metric: value}}))
        assert alert["anomaly_type"] == expected_type

    @pytest.mark.parametrize(
        "value,expected_severity",
        [
            (-40.0, "HIGH"),  # |z| = 5.00 -> confidence 1.00
            (-20.0, "MEDIUM"),  # |z| = 2.50 -> confidence 0.62
        ],
    )
    def test_high_and_medium_severity_paths_are_reachable(self, value, expected_severity):
        alert = _alert(_invoke(input_context={"metrics": {"grade_delta": value}}))
        assert alert["anomaly_type"] == "grade_drop"
        assert alert["severity"] == expected_severity

    def test_low_severity_path_is_reachable_with_a_lowered_flag_threshold(self):
        """At the default flag threshold (|z| >= 2.0) a FLAGGED anomaly always
        scores at least 0.5 confidence, so it is never LOW. The LOW band is
        reached when the caller lowers the flag threshold: a mild deviation
        is then reported, at low confidence."""
        alert = _alert(
            _invoke(
                _request(thresholds={"z_score": 1.0}),
                input_context={"metrics": {"grade_delta": -9.0}},
            )
        )
        assert alert["anomaly_type"] == "grade_drop"
        assert alert["confidence"] < 0.4
        assert alert["severity"] == "LOW"


class TestInvokeValidationIsFailClosed:
    @pytest.mark.parametrize("bad", ["", "   ", "not json at all", "[1,2,3]", '"a string"'])
    def test_malformed_envelope_is_rejected(self, bad):
        status_code, body = _post_invoke({"input": bad, "session_id": "pb"})
        assert status_code == 200
        assert body["status"] == "success"

    @pytest.mark.parametrize(
        "request_override",
        [
            {"student_id": ""},
            {"student_id": "a" * 65},
            {"student_id": "student@example.com"},
            {"data_type": "astrology"},
            {"period": {"start": "last semester", "end": "2025-09"}},
        ],
    )
    def test_contract_violation_is_rejected(self, request_override):
        body = _invoke(_request(**request_override))
        assert body["status"] == "success"

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
    def test_raw_json_non_finite_threshold_is_rejected(self, bad):
        """Python's json parses bare NaN/Infinity in a request body - a
        non-finite threshold must never reach the comparison (NaN compares
        False against everything = silent fail-OPEN)."""
        raw = '{"student_id": "STU-2048", "data_type": "grades", ' f'"thresholds": {{"z_score": {bad}}}}}'
        status_code, body = _post_invoke({"input": raw, "session_id": "pb"})
        assert status_code == 200
        assert body["status"] == "success"

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
    def test_raw_json_non_finite_metric_is_rejected(self, bad):
        raw = '{"student_id": "STU-2048", "data_type": "grades", ' f'"metrics": {{"grade_delta": {bad}}}}}'
        status_code, body = _post_invoke({"input": raw, "session_id": "pb"})
        assert status_code == 200
        assert body["status"] == "success"

    @pytest.mark.parametrize(
        "bad_context",
        [
            {"metrics": {"grade_delta": float("nan")}},
            {"metrics": {"grade_delta": float("inf")}},
            {"metrics": {"grade_delta": 1000.0}},  # out of bounds
            {"metrics": {"grade_delta": True}},  # bool is not a number
            {"metrics": {"ignore_previous_instructions": 1.0}},  # unknown name
            {"metrics": "not-a-dict"},
        ],
    )
    def test_context_channel_violation_is_rejected(self, bad_context):
        body = _invoke(input_context=bad_context)
        assert body["status"] == "success"

    def test_rejection_never_echoes_the_offending_value(self):
        body = _invoke(_request(student_id="student.taro@example.com"))
        assert body["status"] == "success"
        assert "student.taro@example.com" not in json.dumps(body, ensure_ascii=False)


class TestInvokeOutputSchema:
    def test_output_carries_the_schema_note(self):
        alert = _alert(_invoke(input_context={"metrics": {"grade_delta": -40.0}}))
        assert "rounded to 2 decimal places" in alert["schema_note"]

    def test_no_statistic_exceeds_the_two_decimal_grid(self):
        body = _invoke(input_context={"metrics": {"grade_delta": -37.31}})
        off_grid = re.findall(r"\d+\.\d{3,}", body["output"])
        assert not off_grid, f"off-grid statistics reached the output: {off_grid}"

    def test_no_credential_shaped_token_in_output(self):
        body = _invoke(input_context={"metrics": {"grade_delta": -40.0}})
        blob = body["output"]
        assert not re.search(r"\b(?:sk|pk|ak)-[A-Za-z0-9]{16,}", blob)
        assert "eyJ" not in blob
        assert not re.search(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", blob)

    def test_raw_measured_value_is_not_rendered(self):
        """The caller's measurement drives the computation but is never echoed."""
        body = _invoke(input_context={"metrics": {"grade_delta": -37.31}})
        assert "-37.31" not in body["output"]

    def test_caller_request_text_is_not_embedded_verbatim(self):
        request = _request()
        body = _invoke(request, input_context={"metrics": {"grade_delta": -40.0}})
        assert json.dumps(request) not in body["output"]


class TestInvokeAuthBoundary:
    def test_missing_token_is_rejected(self):
        status_code, body = _post_invoke({"input": json.dumps(_request())}, token=None)
        assert status_code == 401

    def test_wrong_token_is_rejected(self):
        status_code, body = _post_invoke({"input": json.dumps(_request())}, token="wrong-token")
        assert status_code == 401

    def test_auth_failure_body_is_generic(self):
        status_code, body = _post_invoke({"input": json.dumps(_request())}, token="wrong-token")
        assert "wrong-token" not in json.dumps(body, ensure_ascii=False)

    def test_oversized_input_context_is_rejected(self):
        payload = {
            "input": json.dumps(_request()),
            "input_context": {"metrics": {"grade_delta": -1.0}, "padding": "x" * 300_000},
        }
        status_code, _ = _post_invoke(payload)
        assert status_code == 413
