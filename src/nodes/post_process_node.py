"""AgentCore Platform v1.0"""

# EDU-C2-010 - PostProcessNode (outer post_process slot; output gate)
#
# Reads the serialised intervention alert from state["result"], which is
# populated by DomainWorkflowGraphNode.merge_output() (mapped from the inner
# graph's result output), and surfaces it as the finalized output AFTER
# enforcing the output contract. Four independent layers, in order:
#
#   (1) credential / student-PII scan - API keys, JWTs, Bearer tokens, raw
#       credential assignments, or a student e-mail address anywhere in the
#       assembled alert withhold the output entirely (sanitised stub,
#       status=ERROR);
#   (2) verbatim caller-text redaction - the alert is built from computed
#       cohort-relative statistics and a fixed action vocabulary only, so a
#       verbatim embedding of the caller's raw request text is a leak, not a
#       feature: any such embedding is replaced with [REDACTED];
#   (3) decimal precision grid - the documented external schema expresses
#       every statistic rounded to 2 decimal places; any decimal token with
#       more fraction digits is snapped onto that grid (off-grid values are
#       full-precision figures leaking to the external surface), with an
#       audit event;
#   (4) raw-magnitude / monetary-form grid - no comma-grouped number,
#       unformatted 5+-digit run, or currency-marked value belongs in an
#       alert whose statistics are small cohort-relative aggregates; any such
#       token is a raw record figure reaching the external surface and is
#       snapped onto a units-of-1,000 grid, with an audit event.
#
# The gate is a set of module-level helpers invoked inside execute() - NOT
# instance-method overrides (FunctionNode's _security_gate_output is @final -
# TypeError).
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

logger = logging.getLogger(__name__)

# Disallowed content patterns.
# Each tuple: (name, compiled regex) - order matters (most specific first).
_DISALLOWED_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    # API key patterns: sk-..., pk-..., ak-...
    ("api_key", re.compile(r"\b(?:sk|pk|ak)-[A-Za-z0-9]{16,}", re.IGNORECASE)),
    # JWT: three base64url segments separated by dots
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # Bearer token in Authorization-like context
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/]{20,}", re.IGNORECASE)),
    # Credential assignment patterns
    (
        "credential_assignment",
        re.compile(
            r"\b(?:password|passwd|secret|api_key|token|access_key|private_key)\s*[:=]\s*\S{8,}",
            re.IGNORECASE,
        ),
    ),
    # Student PII that must never surface in a returned alert: e-mail address.
    ("student_email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
]

_SANITISED_STUB = (
    "[OUTPUT BLOCKED by the output security gate - disallowed content "
    "detected. Review the generated intervention alert and retry without "
    "credential-like strings or raw student PII.]"
)

# State fields that must NEVER be embedded verbatim in the external response.
# The alert is assembled from computed statistics and a fixed action
# vocabulary only - caller-derived text (the raw request or its normalised
# echo) re-appearing verbatim means caller-controlled content reached the
# external surface.
_BLOCKED_FIELDS = frozenset(
    {
        "user_input",
        "validated_input",
    }
)

# Approved external precision for statistics: 2 decimal places (must match
# the schema note rendered by
# src/nodes/generate_intervention_alert_node.py - the alert RENDERS on this
# grid, this gate ENFORCES it). Any decimal token with 3+ fraction digits is
# off-grid.
_DECIMAL_RE = re.compile(r"(?P<sign>[+-]?)(?P<val>\d+\.\d{3,})")

# Approved external grid for raw-magnitude figures: units of 1,000. The
# alert's statistics are small cohort-relative aggregates - a comma-grouped
# number, an unformatted 5+-digit run, or a currency-marked value can only be
# a raw record figure leaking to the external surface.
_EXTERNAL_ROUND_UNIT = 1000
# Monetary-form tokens are identified by FORM and by CURRENCY CONTEXT, never
# by magnitude:
#   form:    comma-grouped numbers (9,999 / 1,234,567) and unformatted runs
#            of 5+ digits;
#   context: any bare 1-4 digit number associated with a currency marker -
#            SYMMETRICALLY: a 3-letter uppercase code or a currency symbol
#            (incl. fullwidth ￥ and 円/₩), before or after the value,
#            attached or separated by ANY whitespace run (spaces, tabs,
#            newlines - the delimiter grammar is `\s*`), signed or unsigned.
#            Word-boundary guards keep embedded acronyms structural. Any
#            standalone 3-letter uppercase word counts as a code on purpose:
#            a false snap fails SAFE while a missed leak does not.
# Structural tokens stay untouched: bare counts, years, 2-decimal statistics.
# ALL matched tokens, at ANY magnitude, must sit on the rounding grid;
# off-grid = a full-precision figure reaching the external surface,
# snapped + audited.
# Grammar (group-based; the marker/value delimiter uses no variable-width
# lookbehind, so it can be an ARBITRARY run of horizontal whitespace). Every
# value accepts an optional explicit +/- sign.
# Branch order matters: currency-context branches first, then form-based.
_CURRENCY_MARKER = r"(?:\b[A-Z]{3}|[¥￥$€£円₩])"
# Delimiter between a currency marker and its value: horizontal whitespace and at
# most ONE newline — never a paragraph break. A plain `\s*` spans blank lines, so a
# 3-letter uppercase word ending a line would bind to the number that opens the next
# block and rewrite it ("Currency: JPY\n\n3. Cash Position" -> "0. Cash Position").
# Every enumerated leak form (spaces, tabs, single newline, signed, symmetric,
# comma-grouped) still matches.
_GATE_DELIM = r"[ \t]*(?:\n[ \t]*)?"

# A raw-magnitude figure may carry a decimal part, and every value alternative
# absorbs it as part of the SAME token. Without that, the fraction of a decimal
# is a standalone 5+-digit run in its own right: "8.512345" would have its
# fraction read as an amount and rewritten to "8.512,000", and an off-grid
# amount would snap its integer part while the fraction dangled behind
# ("JPY 1234.56" -> "JPY 1,000.56") - a value that is neither the true figure
# nor on the grid. Decimals are NOT exempt from the grid: an off-grid amount in
# currency context still snaps, but as ONE number ("JPY 1234.56" -> "JPY 1,000").
# And the absorption has to be irrevocable. A bare optional - `(?:\.\d+)?` -
# lets the engine backtrack out of the fraction whenever the character behind
# it fails the trailing guard, re-match the integer part alone, and dangle the
# fraction after all: "JPY 1234.56m" -> "JPY 1,000.56m". The second arm closes
# that exit - take the fraction whole, or assert no fraction begins here.
_VAL_FRACTION = r"(?:\.\d+|(?!\.\d))"

_NUM_TOKEN_RE = re.compile(
    # LEADING guard: a match may never BEGIN inside a number or inside a
    # fraction, so a digit and the decimal point both bar the position before
    # it. The decimal point is what stops "8.512345" being re-entered at its
    # fraction after the whole-token match fails.
    # There is deliberately NO trailing guard: the grammar's own `\b` and
    # greedy digit runs end a token, and a trailing `.` must stay legal or an
    # amount that ends a sentence ("The book totals JPY 9999.") would escape
    # the grid entirely.
    r"(?<![\d.])"
    # marker THEN value: "JPY 9999", "JPY  -9999", "JPY\t9999", "¥9999", "USD\n+9999".
    # The value alternatives accept a comma-grouped form FIRST: the regex is
    # leftmost-first, so without it "JPY 1,234" would match as marker + "1"
    # (mangling the number on the snap) instead of as the whole grouped value
    # - an on-grid "JPY 1,000" must stay byte-identical, and an off-grid
    # "JPY 1,234" must snap as 1234, not as 1.
    rf"(?:(?P<pre>{_CURRENCY_MARKER}{_GATE_DELIM})"
    rf"(?P<val_after>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{1,4}}{_VAL_FRACTION})\b"
    # value THEN marker: "9999 JPY", "-9999\tJPY", "9999円", "+9999  $"
    rf"|(?P<val_before>[+-]?\d{{1,4}}{_VAL_FRACTION})"
    rf"(?P<post>{_GATE_DELIM}(?:[A-Z]{{3}}\b|[¥￥$€£円₩]))"
    # form-based, standalone at any magnitude: comma-grouped or 5+-digit runs
    rf"|(?P<val_form>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{5,}}{_VAL_FRACTION}))"
)


def _enforce_decimal_precision(result: str) -> Tuple[str, int]:
    """Snap every off-grid decimal token onto the 2-decimal external grid.

    Returns (sanitised_result, redaction_count). A redaction means a
    full-precision statistic reached the external surface - the gate rounds
    it onto the documented 2-decimal grid. The explicit sign of the original
    token is preserved on the snapped replacement.
    """
    redactions = 0

    def _snap(match: "re.Match[str]") -> str:
        nonlocal redactions
        redactions += 1
        snapped = f"{float(match.group('val')):.2f}"
        return f"{match.group('sign')}{snapped}"

    return _DECIMAL_RE.sub(_snap, result), redactions


def _enforce_precision(result: str) -> Tuple[str, int]:
    """Snap every monetary-form token onto the approved external grid.

    Returns (sanitised_result, redaction_count). A redaction means a
    full-precision raw figure reached the external surface - the gate rounds
    it onto the approved grid. The currency marker, the original delimiter
    whitespace, and the explicit sign of the original token are all preserved
    on the snapped replacement. A decimal part belongs to the token and snaps
    with it - the whole figure moves onto the grid, never just its integer
    part with the fraction left dangling behind.
    """
    redactions = 0

    def _snap(match: "re.Match[str]") -> str:
        nonlocal redactions
        pre = match.group("pre") or ""
        post = match.group("post") or ""
        token = match.group("val_after") or match.group("val_before") or match.group("val_form")
        # float(), not int(): the token may carry a decimal part, and int()
        # would raise on it.
        value = float(token.replace(",", ""))  # float() understands leading +/-
        if value % _EXTERNAL_ROUND_UNIT == 0:
            return match.group(0)
        redactions += 1
        snapped = round(value / _EXTERNAL_ROUND_UNIT) * _EXTERNAL_ROUND_UNIT
        plus = "+" if token.startswith("+") and snapped >= 0 else ""
        return f"{pre}{plus}{snapped:,d}{post}"

    return _NUM_TOKEN_RE.sub(_snap, result), redactions


def _security_gate_output(content: str) -> Optional[str]:
    """Run the output content gate (credential / student-PII scan).

    Returns the name of the first matched violation, or None if clean.
    Module-level function (not a node instance method) - the framework
    auto-wraps node instance methods on the real invoke path, so the gate
    must live at module level.
    """
    for name, pattern in _DISALLOWED_PATTERNS:
        if pattern.search(content):
            return name
    return None


def _redact_blocked_fields(result: str, state: AgentState) -> Tuple[str, List[str]]:
    """Replace verbatim embeddings of caller-derived state text with [REDACTED].

    Returns (sanitised_result, redacted_field_names). Only substantial values
    (len > 10) are matched so short incidental overlaps are not redacted.
    """
    redacted: List[str] = []
    sanitised = result
    for field in sorted(_BLOCKED_FIELDS):
        value = state.get(field)
        if isinstance(value, str) and len(value) > 10 and value in sanitised:
            sanitised = sanitised.replace(value, "[REDACTED]")
            redacted.append(field)
    return sanitised, redacted


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Output gate: enforce the alert's output contract before it is returned.

    Outer backbone post_process slot. Reads state["result"] (the merged
    serialised intervention alert from DomainWorkflowGraphNode.merge_output())
    and applies the four gate layers before the response is returned to the
    caller. This node runs the gate itself - the layers are module-level
    helpers called from execute().

    Input state keys:
        result: serialised intervention alert (from merge_output)

    Output state keys (partial dict):
        formatted_output: gated output (schema-enforced alert if clean;
                          blocked stub on a credential/PII violation)
        result:           gated alongside formatted_output
        status:           AgentStatus.SUCCESS or AgentStatus.ERROR
        error_log:        (on error) list of error messages
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "output": message,
                "formatted_output": message,
            }
        result = state.get("result") or ""

        if not result or not str(result).strip():
            # No alert was generated - surface ERROR so the caller is not handed
            # a silently empty response.
            return {
                "formatted_output": "",
                "status": AgentStatus.ERROR.value,
                "error_log": ["PostProcessNode: missing intervention_alert / result in state"],
            }

        # -- Layer 1: credential / student-PII scan (withhold entirely) --
        violation = _security_gate_output(str(result))
        if violation:
            logger.error(
                "PostProcessNode: OUTPUT BLOCKED - violation type: %s",
                violation,
            )
            emit_trace_event(
                "post_process_credential_violation",
                {"violation": violation},
                state,
            )
            return {
                "formatted_output": _SANITISED_STUB,
                "result": _SANITISED_STUB,
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PostProcessNode: output blocked - disallowed content detected ({violation})"],
            }

        # -- Layer 2: verbatim caller-text redaction --
        sanitised_output, redacted_fields = _redact_blocked_fields(str(result), state)
        if redacted_fields:
            logger.error(
                "PostProcessNode: caller-derived text embedded verbatim in output - %s",
                ", ".join(redacted_fields),
            )
            emit_trace_event(
                "post_process_blocked_field_redaction",
                {"fields": redacted_fields},
                state,
            )

        # -- Layer 3: decimal precision grid (2 decimal places) --
        sanitised_output, decimal_redactions = _enforce_decimal_precision(sanitised_output)
        if decimal_redactions:
            logger.warning(
                "PostProcessNode: %d off-grid decimal token(s) snapped to the 2-decimal grid",
                decimal_redactions,
            )
            emit_trace_event(
                "post_process_decimal_precision_redaction",
                {"redaction_count": decimal_redactions},
                state,
            )

        # -- Layer 4: raw-magnitude / monetary-form grid --
        sanitised_output, precision_redactions = _enforce_precision(sanitised_output)
        if precision_redactions:
            logger.warning(
                "PostProcessNode: %d off-grid raw-magnitude token(s) snapped to the external grid",
                precision_redactions,
            )
            emit_trace_event(
                "post_process_precision_redaction",
                {"redaction_count": precision_redactions},
                state,
            )

        # Clean - domain audit: a finalized intervention alert was emitted.
        emit_trace_event(
            "post_process_formatted",
            {"output_chars": len(sanitised_output)},
            state,
        )

        return {
            "formatted_output": sanitised_output,
            "result": sanitised_output,
            "status": AgentStatus.SUCCESS.value,
        }
