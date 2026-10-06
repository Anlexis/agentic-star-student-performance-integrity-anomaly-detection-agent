# Student Performance & Integrity Anomaly Detection Agent

AI agent for detecting anomalies in student performance and academic integrity, built with Agentic Star.

> **Category**: Cat 2 (domain-specific multi-step workflow — anomaly-detection pipeline)
> **Industry**: Education
> **Template ID**: EDU-C2-010

## Overview

Flags individual students whose academic signals deviate significantly from their cohort
baseline, and recommends a concrete intervention. A caller (LMS administrator,
student-affairs coordinator, or academic advisor) submits an anomaly-detection request —
an opaque student id, one of four data types (`grades`, `attendance`, `submission`,
`similarity`), an optional reporting period, and optionally the student's measured
metric via `input_context` — and receives a structured intervention alert: the anomaly
type, a confidence score, a LOW/MEDIUM/HIGH severity band, a recommended action, and a
cohort-relative evidence summary. The pipeline is deterministic and network-free: it
scores the measured value against a cohort baseline (z-score and percentile), classifies
the dominant anomaly signal, and renders the alert behind a mandatory output gate. Every
caller-supplied field is validated fail-closed (finite, bounded, inert identifiers), and
the alert renders cohort-relative aggregates only — raw measured values, free-text
caller content, and student PII never reach the output. The bundled cohort-baseline
service is an offline stub; production deployments replace it with a real cohort
warehouse or LMS analytics query. Alerts are advisory — a human advisor or integrity
board makes the final decision.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent
fails at graph compile / start-up preflight rather than starting in a partially
working state. This is intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/` for the design specification and test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

