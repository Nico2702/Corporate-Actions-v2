"""
Validation — EDI vs FactSet Cross-Source Comparison
=====================================================
Compares classified rows from `edi_corporate_actions` and
`factset_corporate_actions` to highlight discrepancies.

Match key:  ISIN-MIC + Ex_Date + Event_Type
Scope:      Cash Dividend and Special Dividend only (others ignored).

Numerical comparisons are rounded to 6 decimal places to avoid false
positives from float artifacts. Empty/None values are treated as equal.
"""

# Event-types currently included in the validation scope.
SCOPED_TYPES = ("Cash Dividend", "Special Dividend")

# Required fields (mismatches are flagged).
REQUIRED_FIELDS = (
    "Event_Type",
    "Dividend_Amount",
    "Dividend_Currency",
    "Tax_Marker",
    "Adjusted_WHT",
    "exdt",                # = Ex_Date
)

# Display-only fields (shown in detail view, never trigger a fail).
# Note: Subtype is NOT here — it's part of the match key (so different
# subtypes match as separate events, e.g. Interest on Capital vs Ordinary).
DISPLAY_FIELDS = (
    "Frankdiv",
    "CFI",
    "Depositary_Fee",
    "Tax_Relief_Fee",
)


def _norm(v):
    """Normalize a value for comparison.
    - None / "" / "  " → None (treated as equal regardless of variant)
    - Numerics → float rounded to 6 decimals (for monetary fields)
    - Strings → trimmed and lowercased so 'GROSS' == ' gross '
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    s = str(v).strip()
    if s == "":
        return None
    # Try numeric coercion first — handles "0.44" vs "0.4400"
    try:
        return round(float(s), 6)
    except ValueError:
        return s.lower()


# Subtypes that describe dividend frequency only (not semantic classification).
# These are normalized to empty for match-key purposes so EDI's "Interim" etc.
# don't fail to match a FactSet record without a frequency subtype. The
# original value is preserved in the display row.
FREQUENCY_SUBTYPES = {
    "interim",
    "final",
    "annual",
    "variable",
}


def _norm_subtype_for_key(subtype: str) -> str:
    """Treat frequency-only subtypes as empty when building the match key."""
    s = (subtype or "").strip()
    if s.lower() in FREQUENCY_SUBTYPES:
        return ""
    return s


def _key(row: dict) -> tuple:
    """Returns the match key (isin_mic, exdt, event_type, subtype) for a row.

    Subtype is part of the key only when non-empty AND not a frequency
    indicator — this allows multiple Cash Dividends on the same ex-date
    (e.g. Brazilian Interest on Capital + Ordinary Dividend) to be matched
    as distinct events, while keeping frequency-only subtypes (Interim,
    Final, ...) from breaking matches with sources that don't carry them.
    """
    isin = (row.get("isin") or "").strip()
    mic  = (row.get("operationalmic") or "").strip()
    isin_mic = f"{isin}-{mic}" if mic else isin
    subtype  = _norm_subtype_for_key(row.get("Subtype") or "")
    return (isin_mic, row.get("exdt") or "", row.get("Event_Type") or "", subtype)


def _filter_in_scope(rows):
    """Keep only Cash/Special Dividend events with a non-empty match key."""
    out = []
    for r in rows:
        if r.get("Event_Type") not in SCOPED_TYPES:
            continue
        # Need at least an ISIN and Ex_Date to participate in matching.
        if not r.get("isin") or not r.get("exdt"):
            continue
        out.append(r)
    return out


def _compare_fields(edi_row: dict, fs_row: dict) -> list[dict]:
    """For a matched pair, return a list of field comparisons."""
    diffs = []
    for f in REQUIRED_FIELDS + DISPLAY_FIELDS:
        edi_val = edi_row.get(f)
        fs_val  = fs_row.get(f)
        is_match = _norm(edi_val) == _norm(fs_val)
        diffs.append({
            "field":      f,
            "edi":        edi_val if edi_val not in (None, "") else "",
            "factset":    fs_val  if fs_val  not in (None, "") else "",
            "match":      is_match,
            "required":   f in REQUIRED_FIELDS,
        })
    return diffs


def validate(edi_rows, factset_rows) -> list[dict]:
    """
    Compare EDI and FactSet rows.

    Returns a list of dicts (one per event), each with:
      {
        "status":   "match" | "mismatch" | "only_edi" | "only_factset",
        "key":      (isin_mic, exdt, event_type),
        "isin_mic": str,
        "exdt":     str,
        "event_type": str,
        "edi_row":  dict | None,
        "factset_row": dict | None,
        "fields":   list[{field, edi, factset, match, required}],
                    # empty for only_* entries
        "diff_summary": str,    # short textual summary of mismatches
      }
    """
    edi_scoped = _filter_in_scope(edi_rows)
    fs_scoped  = _filter_in_scope(factset_rows)

    edi_by_key = {_key(r): r for r in edi_scoped}
    fs_by_key  = {_key(r): r for r in fs_scoped}

    all_keys = set(edi_by_key) | set(fs_by_key)
    results = []

    for k in sorted(all_keys, key=lambda x: (x[1] or "", x[0], x[2], x[3]), reverse=True):
        isin_mic, exdt, etype, _normalized_subtype = k
        edi_row = edi_by_key.get(k)
        fs_row  = fs_by_key.get(k)

        # For display, prefer the original (non-normalized) subtype from
        # whichever source has the row. EDI takes precedence so the user sees
        # the frequency indicator like "Interim" if EDI provides one.
        display_subtype = ""
        if edi_row:
            display_subtype = (edi_row.get("Subtype") or "").strip()
        if not display_subtype and fs_row:
            display_subtype = (fs_row.get("Subtype") or "").strip()

        if edi_row and fs_row:
            fields = _compare_fields(edi_row, fs_row)
            mismatched = [f for f in fields if f["required"] and not f["match"]]
            status = "match" if not mismatched else "mismatch"
            summary = "; ".join(
                f"{f['field']}: {f['edi']!r} ≠ {f['factset']!r}" for f in mismatched
            ) if mismatched else "—"
        elif edi_row:
            fields = []
            status = "only_edi"
            summary = "Event missing in FactSet"
        else:
            fields = []
            status = "only_factset"
            summary = "Event missing in EDI"

        results.append({
            "status":       status,
            "key":          k,
            "isin_mic":     isin_mic,
            "exdt":         exdt,
            "event_type":   etype,
            "subtype":      display_subtype,
            "edi_row":      edi_row,
            "factset_row":  fs_row,
            "fields":       fields,
            "diff_summary": summary,
        })

    return results


def status_icon(status: str) -> str:
    """Maps a status string to an emoji for compact display."""
    return {
        "match":        "✅",
        "mismatch":     "⚠️",
        "only_edi":     "⬅️",
        "only_factset": "➡️",
    }.get(status, "?")


def status_label(status: str) -> str:
    return {
        "match":        "Match",
        "mismatch":     "Mismatch",
        "only_edi":     "Only EDI",
        "only_factset": "Only FactSet",
    }.get(status, status)
