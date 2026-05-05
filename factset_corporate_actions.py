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


# ── Pipeline ─────────────────────────────────────────────────────────────────

# Spec-defined event-code groups (used for both classify & build_rows).
DIVIDEND_CODES   = ("DVC", "DVCD", "DRP")
STOCK_DIV_PCT    = ("DVS", "DVSS")        # use distPct directly
STOCK_DIV_RATIO  = ("BNS", "BNSS")        # ratio = distNewTerm / distOldTerm
SPLIT_FWD_CODES  = ("SPL", "FSP")
SPLIT_REV_CODES  = ("RSP",)
SPLIT_CODES      = SPLIT_FWD_CODES + SPLIT_REV_CODES
RIGHTS_CODES     = ("DSR",)
SPINOFF_CODES    = ("SPO",)

# US listing reclassification: when the stock is listed on a US MIC (NYSE/NASDAQ),
# any of these stock-dividend codes are reclassified as a Forward Stock Split —
# matches EDI's behaviour. Adjust this set later if specific codes should stay
# as stock dividends.
US_MICS                  = {"XNAS", "XNYS"}
US_RECLASSIFY_TO_SPLIT   = ("DVS", "DVSS", "BNS", "BNSS")

# Date fields we touch in normalize_dates (defensive — FactSet already gives ISO).
_DATE_FIELDS = ("effectiveDate", "payDate", "recordDate", "announcementDate")


def normalize_dates(records):
    """Step 1 — normalize date strings (FactSet already uses ISO YYYY-MM-DD,
    but we replace stray '/' just in case)."""
    for r in records:
        for f in _DATE_FIELDS:
            v = r.get(f)
            if v and isinstance(v, str):
                r[f] = v.replace("/", "-")
    return records


def deduplicate(records):
    """Step 3 — keep one record per eventId. FactSet has no concept of
    optionid (no multi-leg events in our spec)."""
    seen = set()
    out = []
    for r in records:
        key = r.get("eventId")
        if key and key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# Codes that trigger an adjustment reverse on dividends sharing the same ex-date.
# When FactSet returns a stock-div or split on day X, it has already adjusted
# any cash-dividend on day X. We undo this so users see the unadjusted amount.
ADJ_REVERSE_TRIGGERS = STOCK_DIV_PCT + STOCK_DIV_RATIO + SPLIT_CODES


def merge_events(records):
    """Step 4 — undo FactSet's same-day adjustment on cash dividends.

    When a cash dividend (DVC/DVCD/DRP) shares its `effectiveDate` with a
    stock-dividend or split event (DVS/DVSS/BNS/BNSS/SPL/FSP/RSP), FactSet
    delivers the dividend amount already divided by the split's `adjFactor`.
    We reverse this: store the original (adjusted) value in a new field and
    overwrite `amtGrossDecUnadj` with the un-adjusted value.

    Falls back to leaving the dividend untouched if `adjFactor` is missing.
    """
    records = list(records)

    # Group adjustment triggers by ex-date for O(1) lookup.
    triggers_by_date = {}
    for r in records:
        eventcd = (r.get("eventTypeCode") or "").upper()
        if eventcd in ADJ_REVERSE_TRIGGERS:
            d = r.get("effectiveDate")
            if d:
                triggers_by_date.setdefault(d, []).append(r)

    if not triggers_by_date:
        return records

    # For each cash dividend, check whether the same ex-date carries a trigger.
    for r in records:
        eventcd = (r.get("eventTypeCode") or "").upper()
        if eventcd not in DIVIDEND_CODES:
            continue
        # Skip Acquisition records (divType=16) — these aren't dividends, so
        # no FactSet split-adjustment was applied to them.
        if r.get("divTypeCode") == 16:
            continue
        # Idempotency guard: skip if we've already processed this record.
        if "_amtGrossDecAdjusted" in r:
            continue
        d = r.get("effectiveDate")
        if d not in triggers_by_date:
            continue

        # Pick the first trigger with a usable adjFactor (could be more than
        # one — rare but possible).
        adj_factor = None
        for t in triggers_by_date[d]:
            af = t.get("adjFactor")
            try:
                af_f = float(af) if af is not None else None
            except (TypeError, ValueError):
                af_f = None
            if af_f and af_f != 0:
                adj_factor = af_f
                break

        if adj_factor is None:
            # No usable adjFactor — leave the dividend as-is.
            continue

        # Preserve the adjusted value, then overwrite with the unadjusted.
        original = r.get("amtGrossDecUnadj")
        try:
            original_f = float(original) if original is not None else None
        except (TypeError, ValueError):
            original_f = None
        if original_f is None:
            continue

        r["_amtGrossDecAdjusted"] = original_f                 # what FactSet sent
        r["amtGrossDecUnadj"]     = original_f / adj_factor    # the true unadjusted amount

    return records


def classify_event(row: dict, mic: str = "") -> dict:
    """Classify a FactSet record into our standard event_type / subtype.
    Anything outside the spec lands as 'Other'.

    Args:
      row: a FactSet record.
      mic: operational MIC of the listing (e.g. 'XNAS') — used for market-
           specific reclassification (US: stock dividends → forward splits).
    """
    eventcd  = (row.get("eventTypeCode") or "").upper()
    spec_flg = row.get("dividendsSpecFlag")
    is_us    = mic.upper() in US_MICS
    result   = {"event_type": "Other", "subtype": "", "ignore": False}

    # ── Dividends ─────────────────────────────────────────────────────────────
    if eventcd in DIVIDEND_CODES:
        div_type = row.get("divTypeCode")

        # divTypeCode=16 → not actually a dividend, but the cash terms of
        # an Acquisition wrapped in a dividend-shaped record. Reclassify
        # as Merger & Acquisition / Cash deal.
        if div_type == 16:
            result["event_type"] = "Merger & Acquisition"
            result["subtype"]    = ""
            return result

        if spec_flg == 1:
            result["event_type"] = "Special Dividend"
        else:
            # spec_flg = 0 OR null → Cash Dividend per spec
            result["event_type"] = "Cash Dividend"
        # Subtype: certain divTypeCodes carry semantic meaning (mirrors EDI conventions)
        if div_type == 1:
            result["event_type"] = "Cash Dividend"
        elif div_type == 2:
            result["event_type"] = "Special Dividend"
        elif div_type == 4:
            result["event_type"] = "Cash Dividend"
            result["subtype"]    = "Interest on Capital"
        elif div_type == 5:
            result["event_type"] = "Special Dividend"
            result["subtype"]    = "Liquidation"
        elif div_type == 10:
            result["event_type"] = "Special Dividend"
            result["subtype"]    = "Short-Term Capital Gains"
        elif div_type == 11:
            result["event_type"] = "Special Dividend"
            result["subtype"]    = "Long-Term Capital Gains"
        elif div_type == 12:
            result["event_type"] = "Special Dividend"
            result["subtype"]    = "Medium-Term Capital Gains"
        elif div_type == 19:
            result["subtype"] = "Property Income Distribution"
        elif div_type == 21:
            # Brazil-specific: BVMF code 21 is Interest on Capital, NOT Return of Capital
            if mic.upper() == "BVMF":
                result["event_type"] = "Cash Dividend"
                result["subtype"]    = "Interest on Capital"
            else:
                result["subtype"]    = "Return of Capital"
        # divTypeCode=0 → dividend is cancelled (overrides any dividendStatus from feed)
        if div_type == 0:
            result["status_override"] = "Cancelled"
        return result

    # ── US listing override: stock dividends → Forward Stock Split ────────────
    # Mirrors EDI's behaviour for US-listed shares (XNAS/XNYS).
    if is_us and eventcd in US_RECLASSIFY_TO_SPLIT:
        result["event_type"] = "Stock Split"
        result["subtype"]    = "Forward Stock Split"
        return result

    # ── Stock Dividend ────────────────────────────────────────────────────────
    if eventcd in STOCK_DIV_PCT or eventcd in STOCK_DIV_RATIO:
        result["event_type"] = "Stock Dividend"
        return result

    # ── Stock Split ───────────────────────────────────────────────────────────
    if eventcd in SPLIT_CODES:
        result["event_type"] = "Stock Split"
        result["subtype"]    = "Reverse Stock Split" if eventcd in SPLIT_REV_CODES else "Forward Stock Split"
        return result

    # ── Rights Issue ──────────────────────────────────────────────────────────
    if eventcd in RIGHTS_CODES:
        result["event_type"] = "Rights Issue"
        return result

    # ── Spin-Off ──────────────────────────────────────────────────────────────
    if eventcd in SPINOFF_CODES:
        result["event_type"] = "Spin-Off"
        result["subtype"]    = "Demerger"
        return result

    return result


def _safe_div(a, b):
    """Float division that swallows None/zero/string errors."""
    try:
        a, b = float(a), float(b)
        return a / b if b else None
    except (TypeError, ValueError):
        return None


def _fmt_terms(new, old) -> str:
    """Format 'new : old' — integers when whole numbers, raw values otherwise.
    Mirrors edi.fmt_stock_terms so both providers produce identical strings."""
    try:
        if new is None or old is None or not old:
            return ""
        rn, ro = float(new), float(old)
        rn_str = str(int(rn)) if rn == int(rn) else str(new)
        ro_str = str(int(ro)) if ro == int(ro) else str(old)
        return f"{rn_str} : {ro_str}"
    except (TypeError, ValueError):
        return ""


def build_rows(processed_records, isin: str = "", mic: str = ""):
    """Step 5 — build standardized output rows matching EDI's column schema.

    Args:
      processed_records: deduped + merged FactSet records.
      isin:              user-supplied ISIN from the sidebar — copied into
                         each row so the Validation tab can match against EDI.
      mic:               user-supplied operational MIC from the sidebar —
                         needed for market-specific tax rules (e.g. BVMF for
                         Brazilian Interest on Capital WHT).
    """
    mic = (mic or "").upper()
    rows = []
    is_us = mic in US_MICS
    for r in processed_records:
        cl       = classify_event(r, mic=mic)
        eventcd  = (r.get("eventTypeCode") or "").upper()
        div_type = r.get("divTypeCode")

        # ── Initialise the row with all EDI-aligned columns (mostly empty) ────
        row = {
            # Core
            "Event_Type":   cl["event_type"],
            "Subtype":      cl["subtype"],
            "Evt_Status":   cl.get("status_override") or r.get("dividendStatus") or "",
            "eventid":      r.get("eventId", ""),
            "optionid":     "1",     # FactSet has no optionid concept
            "eventcd":      eventcd,
            "marker":       "",
            "paytypecd":    "",

            # Identifiers
            "isin":           isin,                    # from user input
            "issuername":     "",                       # not in FactSet response
            "operationalmic": mic,                      # from user input
            "fsymId":         r.get("fsymId", ""),     # FactSet-specific extra

            # Dates
            "exdt":           r.get("effectiveDate", ""),
            "paydt":          r.get("payDate", ""),
            "recorddt":       r.get("recordDate", ""),
            "declarationdt":  r.get("announcementDate", ""),
            "effectivedt":    r.get("effectiveDate", ""),

            # Dividend fields
            "Dividend_Amount":          "",
            "Dividend_Amount_Adjusted": "",
            "Tax_Marker":               "",
            "Adjusted_WHT":      "",
            "Frankdiv":          "",
            "CFI":               "",
            "Depositary_Fee":    "",
            "Tax_Relief_Fee":    "",
            "Dividend_Currency": "",

            # Stock / Split / Rights
            "Stock_Div_Pct":   "",
            "Stock_Div_Ratio": "",
            "Split_Ratio":     "",
            "Split_Terms":     "",
            "Sub_Price":       "",
            "Sub_Currency":    "",
            "Sub_Ratio":       "",
            "Default_Option":  "",

            # M&A / Spin-Off / ID Change — not classified by FactSet spec yet
            "Deal_Type":              "",
            "MA_Offeror":             "",
            "MA_Hostile":              "",
            "MA_Mand_Vol":             "",
            "MA_Event_Subtype":       "",
            "MA_Cash_Terms":          "",
            "MA_Cash_Terms_Currency": "",
            "ECA_Stock_Ratio":        "",
            "ECA_Stock_Terms":        "",
            "MA_Offeror_ISIN":        "",
            "MA_Offeror_Ticker":      "",
            "MA_Effective_Date":      "",
            "MA_Exp_Completion":      "",
            "MA_Merger_Status":       "",
            "MA_Close_Date":          "",
            "ECA_Status":             "",

            # Meta
            "REIT_Flag":     False,
            "Creation_Date": r.get("announcementDate", ""),
            "feedgendate":   "",
        }

        # ── divTypeCode=16: Acquisition (cash terms wrapped in dividend record) ─
        if eventcd in DIVIDEND_CODES and div_type == 16:
            row["Deal_Type"]              = "Cash"
            row["MA_Cash_Terms"]          = r.get("amtGrossDecUnadj") or ""
            row["MA_Cash_Terms_Currency"] = r.get("declaredCurrency") or ""
            # Dividend_Amount + Dividend_Currency stay empty — this is not a dividend.

        # ── Cash / Special Dividend ───────────────────────────────────────────
        elif eventcd in DIVIDEND_CODES:
            row["Dividend_Amount"]          = r.get("amtGrossDecUnadj") or ""
            row["Dividend_Amount_Adjusted"] = r.get("_amtGrossDecAdjusted") or ""
            row["Dividend_Currency"]        = r.get("declaredCurrency") or ""
            # Tax_Marker: NET only when divTypeCode=21 AND it's a real Return of
            # Capital — i.e. not on BVMF, where 21 means Interest on Capital (GROSS).
            if div_type == 21 and mic != "BVMF":
                row["Tax_Marker"] = "NET"
            else:
                row["Tax_Marker"] = "GROSS"
            # divTypeCode=19 → Property Income Distribution: 20% UK REIT WHT (mirrors EDI)
            if div_type == 19:
                row["Adjusted_WHT"] = "20%"
            # BVMF + Interest on Capital (code 4 OR code 21) → 17.5% BR WHT (mirrors EDI)
            elif mic == "BVMF" and div_type in (4, 21):
                row["Adjusted_WHT"] = "17.5%"

        # ── Stock Dividend / US Forward Split branch ─────────────────────────
        # On US listings (XNAS/XNYS), DVS/DVSS/BNS/BNSS are reclassified as
        # Forward Stock Split — values go into Split_* fields with EDI's
        # formula: ratio = (new + old) / old, terms = "(new+old) : old".
        if is_us and eventcd in US_RECLASSIFY_TO_SPLIT:
            new = r.get("distNewTerm")
            old = r.get("distOldTerm")
            try:
                rn = float(new); ro = float(old)
                if ro:
                    row["Split_Ratio"] = f"{(rn + ro) / ro:.6f}"
                    row["Split_Terms"] = f"{int(rn + ro)} : {int(ro)}"
            except (TypeError, ValueError):
                pass

        # ── Stock Dividend (DVS/DVSS use distPct directly) — non-US ──────────
        elif eventcd in STOCK_DIV_PCT:
            pct = r.get("distPct")
            if pct is not None:
                try:
                    p = float(pct)
                    row["Stock_Div_Pct"]   = f"{p:.4f}%"
                    row["Stock_Div_Ratio"] = f"{1 + p/100:.6f}"
                except (TypeError, ValueError):
                    pass

        # ── Stock Dividend (BNS/BNSS use new/old ratio) — non-US ─────────────
        elif eventcd in STOCK_DIV_RATIO:
            ratio = _safe_div(r.get("distNewTerm"), r.get("distOldTerm"))
            if ratio is not None:
                row["Stock_Div_Pct"]   = f"{ratio*100:.4f}%"
                row["Stock_Div_Ratio"] = f"{1 + ratio:.6f}"

        # ── Stock Split (native split codes — apply on all markets) ──────────
        if eventcd in SPLIT_CODES:
            ratio = _safe_div(r.get("distNewTerm"), r.get("distOldTerm"))
            if ratio is not None:
                row["Split_Ratio"] = f"{ratio:.6f}"
            new, old = r.get("distNewTerm"), r.get("distOldTerm")
            if new and old:
                row["Split_Terms"] = f"{new} : {old}"

        # ── Rights Issue ──────────────────────────────────────────────────────
        if eventcd in RIGHTS_CODES:
            row["Sub_Price"]    = r.get("rightsIssuePrice") or ""
            row["Sub_Currency"] = r.get("rightsIssueCurrency") or ""
            ratio = _safe_div(r.get("distNewTerm"), r.get("distOldTerm"))
            if ratio is not None:
                row["Sub_Ratio"] = f"{ratio:.6f}"

        # ── Spin-Off ──────────────────────────────────────────────────────────
        if eventcd in SPINOFF_CODES:
            new, old = r.get("distNewTerm"), r.get("distOldTerm")
            ratio = _safe_div(new, old)
            if ratio is not None:
                row["ECA_Stock_Ratio"] = f"{ratio:.6f}"
            row["ECA_Stock_Terms"] = _fmt_terms(new, old)

        rows.append(row)
    return rows
