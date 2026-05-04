"""
FactSet Corporate Actions — Library (Skeleton)
================================================
Pure logic module for the FactSet Corporate Actions API.

Mirrors the public surface of `edi_corporate_actions` so that `app.py` can
treat both providers symmetrically. The output schema (rows + meta) is
identical to the EDI module — both produce the same standardized columns
(Event_Type, Subtype, Dividend_Amount, ECA_Status, MA_*, etc.).

Status: SKELETON — to be implemented once API docs / sample response are
available. All public functions raise NotImplementedError for now.
"""

from datetime import date


class FactSetAPIError(Exception):
    """Raised on FactSet API failure."""
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


# ── Public API (matches edi_corporate_actions) ────────────────────────────────
def fetch_records(
    isin: str,
    token: str,
    operational_mic: str | None = None,
    from_date: date | None = None,
    timeout: int = 30,
) -> dict:
    """
    Fetches Corporate Actions from FactSet and returns the same shape as
    `edi_corporate_actions.fetch_records`:
      {
        "records": [...],
        "meta": {
            "isin":           str,
            "record_count":   str,
            "total_records":  str,
            "rate_limit":     str,
            "rate_remaining": str,
            "calls":          [call_info, ...],
        },
      }
    """
    raise NotImplementedError("FactSet integration not yet implemented.")


def normalize_dates(records):
    """Date normalization (FactSet-specific). Step 1 of the pipeline."""
    raise NotImplementedError("FactSet integration not yet implemented.")


def deduplicate(records):
    """Deduplication (FactSet-specific keys)."""
    raise NotImplementedError("FactSet integration not yet implemented.")


def merge_events(records):
    """Merge multi-leg events (FactSet-specific)."""
    raise NotImplementedError("FactSet integration not yet implemented.")


def classify_event(row: dict) -> dict:
    """Classify a single FactSet record into the standardized event_type."""
    raise NotImplementedError("FactSet integration not yet implemented.")


def build_rows(processed_records):
    """Build standardized output rows from processed FactSet records."""
    raise NotImplementedError("FactSet integration not yet implemented.")
