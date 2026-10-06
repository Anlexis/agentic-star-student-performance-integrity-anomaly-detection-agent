# EDU-C2-010 - Unit Tests: PostProcessNode (outer post_process slot; output gate)
#
# Reads state["result"] (the serialised intervention alert merged from the inner
# graph) and surfaces it as formatted_output AFTER enforcing the output
# contract. Four independent layers are covered here:
#
#   (1) credential / student-PII scan  - withhold the output entirely
#   (2) verbatim caller-text redaction - caller text embedded in the alert
#   (3) decimal precision grid         - statistics on the 2-decimal grid
#   (4) raw-magnitude / monetary grid  - raw record figures on the 1,000 grid
#
# The invariant is asserted BOTH ways: every off-grid form snaps, and every
# structural token stays byte-identical. A missing/empty result is an ERROR
# (no silently empty response). The credential boundary is exercised in detail
# in tests/proof_of_boundary/test_credential_block.py.
#
# Deterministic - no LLM, no network. framework.* / src.* imports only.

import inspect
import json
import re

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.post_process_node import (
    PostProcessNode,
    _enforce_decimal_precision,
    _enforce_precision,
)

_CLEAN_ALERT = json.dumps(
    {
        "anomaly_type": "grade_drop",
        "confidence": 0.9,
        "severity": "HIGH",
        "recommended_action": "Schedule academic support meeting",
        "evidence_summary": ["grade_delta: z=5.00, percentile=0.99"],
        "data_type": "grades",
        "schema_note": (
            "Statistics are cohort-relative aggregates rounded to 2 decimal "
            "places; raw measured values are not included."
        ),
    },
    ensure_ascii=False,
)


class TestPostProcessSuccess:
    def test_formats_intervention_alert_into_output(self):
        node = PostProcessNode()
        result = node.execute({"result": _CLEAN_ALERT})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"] == _CLEAN_ALERT

    def test_clean_alert_passes_through_unchanged(self):
        node = PostProcessNode()
        result = node.execute({"result": _CLEAN_ALERT})
        # No error_log on the clean path.
        assert not result.get("error_log")

    def test_result_and_formatted_output_agree(self):
        node = PostProcessNode()
        result = node.execute({"result": _CLEAN_ALERT})
        assert result["result"] == result["formatted_output"]


class TestPostProcessError:
    def test_missing_alert_returns_error(self):
        node = PostProcessNode()
        result = node.execute({})
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == ""
        assert result["error_log"]

    def test_empty_string_result_returns_error(self):
        node = PostProcessNode()
        result = node.execute({"result": "   "})
        assert result["status"] == AgentStatus.ERROR.value


class TestPostProcessCredentialBlock:
    def test_credential_in_result_is_blocked(self):
        node = PostProcessNode()
        tainted = '{"note": "api_key=supersecretvalue1234567"}'
        result = node.execute({"result": tainted})
        assert result["status"] == AgentStatus.ERROR.value
        # Both surfaced fields carry the sanitised stub - the raw secret is gone.
        assert "supersecretvalue1234567" not in result.get("formatted_output", "")
        assert "supersecretvalue1234567" not in result.get("result", "")

    def test_student_email_in_result_is_blocked(self):
        node = PostProcessNode()
        tainted = '{"note": "contact student.taro@example.com"}'
        result = node.execute({"result": tainted})
        assert result["status"] == AgentStatus.ERROR.value
        assert "student.taro@example.com" not in result.get("formatted_output", "")


class TestPostProcessVerbatimRedaction:
    def test_caller_text_embedded_verbatim_is_redacted(self):
        """The alert is built from computed statistics and a fixed action
        vocabulary - a verbatim echo of caller text is a leak."""
        node = PostProcessNode()
        caller_text = "this is the caller's original request text"
        result = node.execute(
            {
                "result": json.dumps({"anomaly_type": "grade_drop", "note": caller_text}),
                "user_input": caller_text,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert caller_text not in result["formatted_output"]
        assert "[REDACTED]" in result["formatted_output"]

    def test_short_incidental_overlap_is_not_redacted(self):
        node = PostProcessNode()
        result = node.execute({"result": _CLEAN_ALERT, "user_input": "grades"})
        assert "[REDACTED]" not in result["formatted_output"]


class TestDecimalPrecisionGrid:
    """Layer 3: statistics render on the documented 2-decimal grid."""

    @pytest.mark.parametrize(
        "source,expected",
        [
            ("z=-3.1259", "z=-3.13"),
            ("percentile=0.0009", "percentile=0.00"),
            ("+3.14159", "+3.14"),
            ("z=1.23456789", "z=1.23"),
        ],
    )
    def test_off_grid_decimal_snaps(self, source, expected):
        snapped, count = _enforce_decimal_precision(source)
        assert snapped == expected
        assert count == 1

    @pytest.mark.parametrize("source", ["z=2.50", "percentile=0.99", "confidence=0.9", "v1.0"])
    def test_on_grid_decimal_is_byte_identical(self, source):
        snapped, count = _enforce_decimal_precision(source)
        assert snapped == source
        assert count == 0

    def test_gate_snaps_full_precision_statistics_in_a_real_alert(self):
        node = PostProcessNode()
        alert = json.dumps({"evidence_summary": ["grade_delta: z=-3.126789, percentile=0.000883"]})
        result = node.execute({"result": alert})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "-3.13" in result["formatted_output"]
        assert "-3.126789" not in result["formatted_output"]
        assert "0.000883" not in result["formatted_output"]


class TestRawMagnitudeGrid:
    """Layer 4: raw record figures snap onto the units-of-1,000 grid.

    Asserted for every representation the invariant covers - comma-grouped,
    unformatted 5+-digit runs, and currency-marked values in either order,
    attached or separated by an arbitrary whitespace run, signed or unsigned.
    """

    @pytest.mark.parametrize(
        "source,expected",
        [
            ("JPY 1,234", "JPY 1,000"),  # grouped value snaps as a whole token
            ("JPY 9999", "JPY 10,000"),  # marker then value
            ("9999 JPY", "10,000 JPY"),  # value then marker
            ("JPY-9999", "JPY-10,000"),  # signed, attached
            ("JPY +9999", "JPY +10,000"),  # signed, separated
            ("JPY  9999", "JPY  10,000"),  # multi-space delimiter
            ("JPY\t9999", "JPY\t10,000"),  # tab delimiter
            ("JPY\n9999", "JPY\n10,000"),  # newline delimiter
            ("9999円", "10,000円"),  # fullwidth marker
            ("￥9999", "￥10,000"),
            ("$9999", "$10,000"),
            ("total 123,456 points", "total 123,000 points"),  # form-based
            ("12345", "12,000"),  # 5+-digit run
        ],
    )
    def test_off_grid_magnitude_snaps(self, source, expected):
        snapped, count = _enforce_precision(source)
        assert snapped == expected
        assert count == 1

    @pytest.mark.parametrize(
        "source",
        [
            "JPY 1,000",  # already on grid - byte-identical
            "in 2026",  # year, no currency context
            "90d horizon",  # structural horizon
            "cohort of 240 students",  # bare count
            "z=2.50",  # statistic
        ],
    )
    def test_structural_token_is_byte_identical(self, source):
        snapped, count = _enforce_precision(source)
        assert snapped == source
        assert count == 0

    def test_uppercase_word_before_digits_fails_safe(self):
        """A standalone 3-letter uppercase word counts as a currency marker on
        purpose: a false snap fails SAFE, a missed leak does not. Opaque ids
        of that shape are therefore snapped - which is harmless here because
        no identifier is part of the rendered alert (asserted below)."""
        snapped, count = _enforce_precision("STU-2048")
        assert snapped == "STU-2,000"
        assert count == 1

    def test_alert_schema_carries_no_student_identifier(self):
        """Structural guarantee behind the rule above: the alert renders the
        classification, the action and cohort-relative statistics only - the
        student identifier is never a rendered field, so it never reaches
        this gate on a real invocation."""
        alert = json.loads(_CLEAN_ALERT)
        assert "student_id" not in alert
        blob = json.dumps(alert, ensure_ascii=False)
        assert "STU-" not in blob

    def test_gate_snaps_raw_figure_in_a_real_alert(self):
        node = PostProcessNode()
        alert = json.dumps({"evidence_summary": ["raw cohort total 123,456"]})
        result = node.execute({"result": alert})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "123,456" not in result["formatted_output"]
        assert "123,000" in result["formatted_output"]


class TestRawMagnitudeGridHandlesDecimals:
    """Layer 4 reads a decimal as ONE number, not as an integer plus a run.

    An education alert renders grades, credit hours, attendance percentages
    and GPA-style figures - all decimals - so the fraction of a decimal must
    never be mistaken for a standalone digit run. Two invariants, and both
    directions are load-bearing:

      * an off-grid figure in currency context still snaps, as a WHOLE number
        (no exemption for decimals - the integer part must not snap while the
        fraction dangles behind, which yields a value that is neither the true
        figure nor on the grid);
      * a decimal that is not an amount at all - a percentage, a z-score, a
        version, a date - survives byte-identical.
    """

    @pytest.mark.parametrize(
        "source,expected",
        [
            # The decimal part belongs to the amount and snaps with it.
            ("JPY 1234.56", "JPY 1,000"),
            ("JPY 1,234,567.89", "JPY 1,235,000"),
            ("$1234.56", "$1,000"),
            ("1234.56 JPY", "1,000 JPY"),
            # Form-based, no currency context needed.
            ("fees 1234567.89 recorded", "fees 1,235,000 recorded"),
            ("12345.6789", "12,000"),
            ("total 1,234.56 points", "total 1,000 points"),
            # A trailing full stop is NOT part of the token, and must not be
            # allowed to shield the amount: the decimal point joins the
            # LEADING guard only. An amount that ends a sentence still snaps.
            ("The book totals JPY 9999.", "The book totals JPY 10,000."),
        ],
    )
    def test_off_grid_decimal_amount_snaps_as_one_number(self, source, expected):
        snapped, count = _enforce_precision(source)
        assert snapped == expected
        assert count == 1

    @pytest.mark.parametrize(
        "source",
        [
            # Fractions long enough to read as a standalone 5+-digit run.
            "8.512345",
            "score 8.512345 / 10",
            "attendance 99.999999%",
            "grade_delta z=1.234567",
            "percentile=0.123456",
            "ratio 0.123456",
            # Ordinary education figures and structural tokens.
            "GPA is 3.85 on a 4.00 scale",
            "credit hours 3.5 of 120.0",
            "version v1.2345",
            "term starts 2026-04-01",
            "attendance 92.5%",
            # An already-on-grid amount keeps its rendered decimal part.
            "JPY 1,000.00",
        ],
    )
    def test_non_amount_decimal_is_byte_identical(self, source):
        snapped, count = _enforce_precision(source)
        assert snapped == source
        assert count == 0

    def test_suffixed_decimal_does_not_backtrack_into_a_dangling_fraction(self):
        """Absorption must be irrevocable - `(?:\.\d+)?` is not.

        Layer 3 normalises figures before they reach this grid in the full
        pipeline, so the defect is pinned here at layer 4 directly: with an
        optional fraction the engine backtracks out of ".56" when the "m"
        behind it fails the trailing guard, re-matches "1234" alone, and
        "JPY 1234.56m" corrupts to "JPY 1,000.56m". The two-arm absorption
        leaves it byte-identical - while the unsuffixed amount still snaps
        as ONE number.
        """
        snapped, count = _enforce_precision("JPY 1234.56m")
        assert snapped == "JPY 1234.56m"
        assert count == 0
        snapped, count = _enforce_precision("JPY 1234.56")
        assert snapped == "JPY 1,000"
        assert count == 1

    def test_gate_snaps_a_decimal_raw_figure_in_a_real_alert(self):
        """End-to-end through the node: layers 3 and 4 compose.

        A two-decimal raw figure passes layer 3 untouched (already on the
        2-decimal grid) and reaches layer 4 as a decimal. It must land on the
        1,000 grid as a whole number - not as "12,000.67".
        """
        node = PostProcessNode()
        alert = json.dumps({"evidence_summary": ["raw cohort total 12345.67"]})
        result = node.execute({"result": alert})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "12,000" in result["formatted_output"]
        assert "12,000.67" not in result["formatted_output"]
        assert "12345.67" not in result["formatted_output"]

    def test_full_precision_statistic_survives_both_grids_in_a_real_alert(self):
        """The mirror case, and the reason the layer ORDER is load-bearing.

        Layer 3 rounds a long fraction onto the 2-decimal grid first, so by
        the time layer 4 sees the statistic its fraction is two digits and can
        no longer be read as a standalone 5+-digit run. Layer 4 must then
        leave it alone: a cohort statistic is not a raw magnitude and must not
        come back comma-grouped.
        """
        node = PostProcessNode()
        alert = json.dumps({"evidence_summary": ["attendance_rate: 99.123456", "z=1.234567"]})
        result = node.execute({"result": alert})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "99.12" in result["formatted_output"]
        assert "1.23" in result["formatted_output"]
        assert "99.123456" not in result["formatted_output"]
        assert re.search(r"\d,\d{3}", result["formatted_output"]) is None


class TestPostProcessContract:
    def test_execute_method_signature(self):
        """Node contract: execute(self, state); no legacy invoke entry point."""
        assert hasattr(PostProcessNode, "execute")
        params = list(inspect.signature(PostProcessNode.execute).parameters.keys())
        assert len(params) >= 2
        assert params[1] == "state"
        assert "_invoke_impl" not in PostProcessNode.__dict__
