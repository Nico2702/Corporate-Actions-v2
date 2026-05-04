"""
FactSet Corporate Actions — Library
====================================
Pure logic module for the FactSet `corporate-actions` endpoint:
  https://api.factset.com/content/factset-global-prices/v1/corporate-actions

Mirrors the public surface of `edi_corporate_actions` so that `app.py` can
treat both providers symmetrically. The output schema (rows + meta) is
identical to the EDI module — both produce the same standardized columns
(Event_Type, Subtype, Dividend_Amount, ECA_Status, MA_*, etc.).

Status:
  - fetch_records()  : implemented (auth + URL + meta)
  - normalize_dates(), deduplicate(), merge_events(), classify_event(),
    build_rows(): NOT YET IMPLEMENTED — pipeline awaits a sample response
    so we know FactSet's record schema and event vocabulary.
"""

import requests
from datetime import date


class FactSetAPIError(Exception):
    """Raised on FactSet API failure."""
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


# ── Auth helpers ──────────────────────────────────────────────────────────────
def _build_auth_header(token: str) -> str:
    """Accepts either:
       - 'Basic <base64>'   → used as-is
       - '<base64>'         → prefixed with 'Basic '
    Both are common ways to paste FactSet credentials.
    """
    token = token.strip()
    if token.lower().startswith("basic "):
        return token
    return f"Basic {token}"


# ── API ───────────────────────────────────────────────────────────────────────
def fetch_records(
    ticker: str,
    token: str,
    operational_mic: str | None = None,   # accepted for symmetry; not used (exchange is in ticker)
    from_date: date | None = None,
    timeout: int = 30,
) -> dict:
    """
    Calls the FactSet corporate-actions endpoint.

    Args:
      ticker:    FactSet ticker-exchange ID (e.g. "AAPL-NAS").
      token:     Basic auth credential — either "Basic <base64>" or just "<base64>".
      from_date: Optional lower bound, sent as ?startDate=YYYY-MM-DD.
      timeout:   HTTP timeout in seconds.

    Returns:
      {
        "records": [...],
        "meta": {
            "isin":           str,   # echo of ticker (key kept for UI symmetry)
            "record_count":   str,
            "total_records":  str,
            "rate_limit":     str,
            "rate_remaining": str,
            "calls":          [call_info],
        },
      }

    Raises:
      FactSetAPIError: on HTTP error or connection failure.
    """
    base_url = "https://api.factset.com/content/factset-global-prices/v1/corporate-actions"
    params = [
        f"ids={ticker}",
        "eventCategory=ALL",
        "cancelledDividend=include",
        "batch=N",
    ]
    if from_date:
        params.append(f"startDate={from_date.strftime('%Y-%m-%d')}")
    url = f"{base_url}?{'&'.join(params)}"

    headers = {
        "accept":        "application/json",
        "Authorization": _build_auth_header(token),
    }

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.exceptions.ConnectionError as e:
        raise FactSetAPIError("Could not connect to FactSet API.") from e
    except requests.exceptions.Timeout as e:
        raise FactSetAPIError(f"FactSet API request timed out after {timeout}s.") from e
    except requests.exceptions.RequestException as e:
        raise FactSetAPIError(f"Unexpected error: {e}") from e

    # Handle status codes.
    if response.status_code == 204:
        raw_records = []
    elif response.status_code == 200:
        # We don't yet know FactSet's exact response envelope. Try the most
        # common shapes — once we see a sample we'll lock this down.
        try:
            data = response.json()
        except ValueError:
            raw_records = []
        else:
            if isinstance(data, list):
                raw_records = data
            elif isinstance(data, dict):
                raw_records = (
                    data.get("data")
                    or data.get("results")
                    or data.get("jsondata")
                    or []
                )
            else:
                raw_records = []
    else:
        raise FactSetAPIError(
            f"API Error {response.status_code}: {response.text[:500]}",
            status_code=response.status_code,
        )

    call_info = {
        "label":          "factset",
        "url":            url,
        "status_code":    response.status_code,
        "body_preview":   response.text[:500] if response.text else "",
        "headers":        dict(response.headers),
        "record_count":   str(len(raw_records)),
        "total_records":  response.headers.get("X-Total-Records",      "–"),
        "rate_limit":     response.headers.get("X-Ratelimit-Limit",    "–"),
        "rate_remaining": response.headers.get("X-Ratelimit-Remaining","–"),
    }

    return {
        "records": raw_records,    # not yet normalized — pipeline TBD
        "meta": {
            "isin":           ticker,
            "record_count":   str(len(raw_records)),
            "total_records":  call_info["total_records"],
            "rate_limit":     call_info["rate_limit"],
            "rate_remaining": call_info["rate_remaining"],
            "calls":          [call_info],
        },
    }


# ── Pipeline (stubs — to be implemented once we have a sample response) ──────
def normalize_dates(records):
    raise NotImplementedError("FactSet pipeline not yet implemented.")

def deduplicate(records):
    raise NotImplementedError("FactSet pipeline not yet implemented.")

def merge_events(records):
    raise NotImplementedError("FactSet pipeline not yet implemented.")

def classify_event(row: dict) -> dict:
    raise NotImplementedError("FactSet pipeline not yet implemented.")

def build_rows(processed_records):
    raise NotImplementedError("FactSet pipeline not yet implemented.")
