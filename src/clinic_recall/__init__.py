"""Clinic Recall data plane (Phase 1).

Deterministic, per-clinic data layer for Clinic Recall: the relational schema
(PRD section 9), a CSV-first sync worker (Cliniko behind an adapter), and the
deterministic candidate detection (FR-05) and eligibility (FR-06) logic that
produce a per-clinic candidate queue.

This package is intentionally free of any AI / prompt / voice code. All business
logic here is deterministic and test-driven. It is kept distinct from the
vendored voice accelerator under ``apps/artagent``.
"""

from __future__ import annotations

__all__ = [
    "__version__",
]

__version__ = "0.1.0"
