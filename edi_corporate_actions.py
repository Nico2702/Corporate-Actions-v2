"""
EDI Corporate Actions — Library
================================
Pure logic module for the EDI GetHistoricalCorporateActions API.

Public API:
  fetch_records(...)   -> dict   : Calls EDI API + normalizes dates (Step 1).
                                   Raises EDIAPIError on failure.
  normalize_dates(...) -> list   : Step 1 of the pipeline.
  deduplicate(...)     -> list   : Step 3 of the pipeline.
  merge_events(...)    -> list   : Step 4 of the pipeline.
  build_rows(...)      -> list   : Step 5 — classify + emit output rows.
  classify_event(...)  -> dict   : Single-record classification.

No Streamlit imports — this module is safe to use from any context
(tests, scripts, notebooks, other UIs).

Spec: NaroIX EDI CA Integration Specification v2.3
"""

import requests
import pandas as pd
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date


class EDIAPIError(Exception):
    """Raised when the EDI API returns a non-200 response or a connection fails."""
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


# ── Constants ─────────────────────────────────────────────────────────────────
US_MICS = {"XNAS", "XNYS"}
AU_MICS = {"XASX"}

RAW_COLUMNS = [
    "eventid", "optionid", "eventcd", "relatedeventcd", "eventsubtypecd",
    "marker", "paytypecd", "mandvoluflag",
    "exdt", "paydt", "recorddt", "declarationdt", "effectivedt",
    "expcompletiondt",
    "grossdividend", "netdividend", "divrate", "cashback",
    "declgrossamt", "declcurencd",
    "frank_div_raw",
    "ratioold", "rationew", "ratecurencd",
    "issueprice", "entissueprice", "depfees",
    "outsectycd", "operationalmic", "isin", "issuername",
    "offerorname", "outisin", "outbbgcompticker",
    "minimumprice", "maximumprice", "hostile", "mrgrstatus",
    "unconditionaldt", "compulsoryacqdt",
    "frequency", "periodenddt", "ntschangedt",
    "eventcreatedt", "feedgendate", "evtactioncd", "lstactioncd", "ntsactioncd",
    "voting", "defaultoptionflag", "optionelectiondt",
    "closedt",
    "issnewname", "issoldname", "namechangedt",
    "newlocalcode", "oldlocalcode", "newexchgcd", "oldexchgcd",
    "newcntrycd", "oldcntrycd", "newisin", "oldisin",
    "newcurencd", "oldcurencd", "newtradingcurencd", "oldtradingcurencd",
]


# ── Helpers ───────────────────────────────────────────────────────────────────
def safe_div(a, b):
    try:
        return float(a) / float(b)
    except (TypeError, ValueError, ZeroDivisionError):
        return None

def parse_feedgendate(val):
    if not val:
        return pd.Timestamp.min
    try:
        return pd.Timestamp(val)
    except Exception:
        return pd.Timestamp.min


# ── Classification ────────────────────────────────────────────────────────────
def fmt_stock_terms(rationew, ratioold) -> str:
    """Format rationew:ratioold exactly as delivered by EDI."""
    try:
        if not rationew or not ratioold:
            return ""
        rn = float(rationew); ro = float(ratioold)
        # Show as integers if both are whole numbers, otherwise as-is
        rn_str = str(int(rn)) if rn == int(rn) else str(rationew)
        ro_str = str(int(ro)) if ro == int(ro) else str(ratioold)
        return f"{rn_str} : {ro_str}"
    except Exception:
        return ""
def classify_event(row: dict) -> dict:
    result = {
        "event_type": "Other", "subtype": "",
        "dividend_amount": "", "tax_marker": "", "adjusted_wht": "", "dividend_currency": "",
        "depositary_fee": "", "tax_relief_fee": "",
        "stock_dividend_pct": "", "stock_dividend_ratio": "",
        "split_ratio": "", "split_terms": "",
        "subscription_price": "", "subscription_currency": "", "subscription_ratio": "",
        # MA / Deal fields (shared across TKOVR, DMRGR, MRGR, DIST)
        "ma_subtype": "", "ma_deal_type": "", "ma_offeror": "", "ma_hostile": "",
        "ma_cash_terms": "", "ma_cash_terms_currency": "",
        "eca_stock_ratio": "", "eca_stock_terms": "", "ma_offeror_isin": "", "ma_offeror_ticker": "",
        "ma_mandatory_voluntary": "",
        "ma_effective_date": "", "ma_exp_completion": "",
        "ma_merger_status": "", "ma_event_subtype": "",
        "new_name": "", "old_name": "", "id_change_dt": "",
        "new_local_code": "", "old_local_code": "",
        "new_exchg": "", "old_exchg": "",
        "new_country": "", "old_country": "",
        "new_isin": "", "old_isin": "",
        "new_currency": "", "old_currency": "",
        "new_trading_ccy": "", "old_trading_ccy": "",
        "ignore": False,
    }

    eventcd    = (row.get("eventcd")        or "").upper().strip()
    marker     = (row.get("marker")         or "").upper().strip()
    paytypecd  = (row.get("paytypecd")      or "").upper().strip()
    outsectycd = (row.get("outsectycd")     or "").upper().strip()
    op_mic     = (row.get("operationalmic") or "").upper().strip()

    # ── Global filter: Warrant (WAR) → always ignore ──────────────────────────
    if outsectycd == "WAR":
        result["ignore"] = True
        return result
    gross         = row.get("grossdividend")  or row.get("declgrossamt") or ""
    net           = row.get("netdividend")    or ""
    cashback      = row.get("cashback")       or ""
    ratecurencd   = row.get("ratecurencd")    or row.get("declcurencd") or ""
    rationew      = row.get("rationew")       or ""
    ratioold      = row.get("ratioold")       or ""
    issueprice    = row.get("issueprice")     or ""
    entissueprice = row.get("entissueprice")  or ""
    depositary_fee  = row.get("depfees")        or ""
    tax_relief_fee  = row.get("taxrelieffee")   or ""
    is_us = op_mic in US_MICS
    is_au = op_mic in AU_MICS
    is_br = op_mic == "BVMF"

    # ── TKOVR ─────────────────────────────────────────────────────────────────
    if eventcd == "TKOVR":
        result["event_type"]             = "Merger & Acquisition"
        result["ma_subtype"]             = ""
        result["ma_offeror"]             = row.get("offerorname") or ""
        result["ma_hostile"]             = row.get("hostile")     or ""
        result["ma_mandatory_voluntary"] = row.get("mandvoluflag") or ""
        result["ma_event_subtype"]       = row.get("eventsubtypecd") or ""
        if paytypecd == "C":
            result["ma_deal_type"]     = "Cash"
            result["ma_cash_terms"]    = row.get("minimumprice") or row.get("maximumprice") or ""
            result["ma_cash_terms_currency"] = row.get("ratecurencd") or row.get("tradingcurencd") or ""
        elif paytypecd == "S":
            result["ma_deal_type"]      = "Stock"
            result["ma_offeror_isin"]   = row.get("outisin")         or ""
            result["ma_offeror_ticker"] = row.get("outbbgcompticker") or ""
            ratio = safe_div(rationew, ratioold)
            result["eca_stock_ratio"]    = f"{ratio:.6f}" if ratio else ""
            result["eca_stock_terms"]    = fmt_stock_terms(rationew, ratioold) if ratio else ""
        elif paytypecd == "B":
            result["ma_deal_type"]           = "Cash & Stock"
            result["ma_cash_terms"]          = row.get("minimumprice") or row.get("maximumprice") or ""
            result["ma_cash_terms_currency"] = row.get("ratecurencd") or row.get("tradingcurencd") or ""
            result["ma_offeror_isin"]        = row.get("outisin")         or ""
            result["ma_offeror_ticker"]      = row.get("outbbgcompticker") or ""
            ratio = safe_div(rationew, ratioold)
            result["eca_stock_ratio"] = f"{ratio:.6f}" if ratio else ""
            result["eca_stock_terms"] = fmt_stock_terms(rationew, ratioold) if ratio else ""
        elif paytypecd == "D":
            result["ignore"] = True  # Debenture legs ignored
        else:
            result["ma_deal_type"] = paytypecd
        return result

    # ── DMRGR (Spin-Off / Demerger) ───────────────────────────────────────────
    if eventcd == "DMRGR":
        result["event_type"]             = "Spin-Off"
        result["ma_subtype"]             = "Demerger"
        result["ma_deal_type"]           = "Stock"
        result["ma_mandatory_voluntary"] = row.get("mandvoluflag") or ""
        result["ma_offeror_ticker"]      = row.get("outbbgcompticker") or ""
        result["ma_offeror_isin"]        = row.get("outisin") or ""
        result["ma_effective_date"]      = row.get("effectivedt") or ""
        result["ma_exp_completion"]      = row.get("expcompletiondt") or ""
        ratio = safe_div(rationew, ratioold)
        if ratio is not None:
            result["eca_stock_ratio"] = f"{ratio:.6f}"
            result["eca_stock_terms"] = fmt_stock_terms(rationew, ratioold)
        return result

    # ── MRGR (Merger — target distributes acquirer shares) ────────────────────
    if eventcd == "MRGR":
        result["event_type"]             = "Merger & Acquisition"
        result["ma_subtype"]             = ""
        result["ma_deal_type"]           = "Stock" if paytypecd == "S" else ("Cash" if paytypecd == "C" else (paytypecd or "Stock"))
        result["ma_mandatory_voluntary"] = row.get("mandvoluflag") or ""
        result["ma_offeror_ticker"]      = row.get("outbbgcompticker") or ""
        result["ma_offeror_isin"]        = row.get("outisin") or ""
        result["ma_merger_status"]       = row.get("mrgrstatus") or ""
        result["ma_effective_date"]      = row.get("effectivedt") or ""
        result["ma_exp_completion"]      = row.get("expcompletiondt") or ""
        ratio = safe_div(rationew, ratioold)
        if ratio is not None:
            result["eca_stock_ratio"] = f"{ratio:.6f}"
            result["eca_stock_terms"] = fmt_stock_terms(rationew, ratioold)
        if paytypecd in ("", None) or paytypecd == "C":
            result["ma_cash_terms"]    = row.get("minimumprice") or row.get("maximumprice") or ""
            result["ma_cash_terms_currency"] = row.get("ratecurencd") or row.get("tradingcurencd") or ""
        return result

    # ── DIST (Stock Distribution — e.g. Reverse Morris Trust distribution) ────
    if eventcd == "DIST":
        result["event_type"]             = "Stock Distribution"
        result["ma_subtype"]             = "Share Distribution"
        result["ma_deal_type"]           = "Stock"
        result["ma_mandatory_voluntary"] = row.get("mandvoluflag") or ""
        result["ma_offeror_ticker"]      = row.get("outbbgcompticker") or ""
        result["ma_offeror_isin"]        = row.get("outisin") or ""
        result["ma_effective_date"]      = row.get("effectivedt") or ""
        result["ma_exp_completion"]      = row.get("expcompletiondt") or ""
        ratio = safe_div(rationew, ratioold)
        if ratio is not None:
            result["eca_stock_ratio"] = f"{ratio:.6f}"
            result["eca_stock_terms"] = fmt_stock_terms(rationew, ratioold)
        return result

    # ── Rights Issue ──────────────────────────────────────────────────────────
    if eventcd in {"RTS", "ENT"}:
        result["event_type"] = "Rights Issue"
        sub_price = issueprice if issueprice else entissueprice
        result["subscription_price"]    = sub_price
        result["subscription_currency"] = ratecurencd
        ratio = safe_div(rationew, ratioold)
        result["subscription_ratio"] = f"{ratio:.6f}" if ratio else ""
        return result

    # ── Stock Split ───────────────────────────────────────────────────────────
    if eventcd in {"SD", "FSPLT"}:
        result["event_type"] = "Stock Split"
        result["subtype"]    = "Forward Stock Split"
        ratio = safe_div(rationew, ratioold)
        result["split_ratio"] = f"{ratio:.6f}" if ratio else ""
        if rationew and ratioold:
            result["split_terms"] = f"{rationew} : {ratioold}"
        return result

    if eventcd in {"CONSD", "RSPLT"}:
        result["event_type"] = "Stock Split"
        result["subtype"]    = "Reverse Stock Split"
        ratio = safe_div(rationew, ratioold)
        result["split_ratio"] = f"{ratio:.6f}" if ratio else ""
        if rationew and ratioold:
            result["split_terms"] = f"{rationew} : {ratioold}"
        return result

    # ── US: DIV/BON + S → Stock Split ────────────────────────────────────────
    if is_us and eventcd in {"DIV", "BON"} and paytypecd == "S":
        result["event_type"] = "Stock Split"
        result["subtype"]    = "Forward Stock Split"
        try:
            rn = float(rationew); ro = float(ratioold)
            result["split_ratio"] = f"{(rn + ro) / ro:.6f}"
            result["split_terms"] = f"{int(rn + ro)} : {int(ro)}"
        except (TypeError, ValueError):
            pass
        return result

    # ── non-US: DIV/BON + S → Stock Dividend ─────────────────────────────────
    if not is_us and eventcd in {"DIV", "BON"} and paytypecd == "S":
        result["event_type"] = "Stock Dividend"
        result["subtype"]    = "Bonus Issue" if eventcd == "BON" else ""
        ratio = safe_div(rationew, ratioold)
        if ratio is not None:
            result["stock_dividend_pct"]   = f"{ratio * 100:.4f}%"
            result["stock_dividend_ratio"] = f"{1 + ratio:.6f}"
        return result

    # ── DIV + B → Cash & Stock Dividend ──────────────────────────────────────
    if eventcd == "DIV" and paytypecd == "B" and marker != "SPL":
        result["event_type"] = "Cash + Stock Dividend"
        result["subtype"]    = "Both"
        if gross:
            result["dividend_amount"] = gross; result["tax_marker"] = "GROSS"
        elif net:
            result["dividend_amount"] = net;   result["tax_marker"] = "GROSS"
        result["dividend_currency"] = ratecurencd
        ratio = safe_div(rationew, ratioold)
        if ratio is not None:
            result["stock_dividend_pct"]   = f"{ratio * 100:.4f}%"
            result["stock_dividend_ratio"] = f"{1 + ratio:.6f}"
        result["depositary_fee"]  = depositary_fee
        result["tax_relief_fee"]  = tax_relief_fee
        return result

    # ── RCAP ──────────────────────────────────────────────────────────────────
    if eventcd == "RCAP":
        result["event_type"]        = "Special Dividend"
        result["subtype"]           = "Return of Capital"
        result["dividend_amount"]   = cashback
        result["tax_marker"]        = "NET"
        result["dividend_currency"] = ratecurencd
        result["depositary_fee"]  = depositary_fee
        result["tax_relief_fee"]  = tax_relief_fee
        return result

    # ── LIQ / MEM ─────────────────────────────────────────────────────────────
    if eventcd in {"LIQ", "MEM"}:
        result["event_type"] = "Special Dividend"
        result["subtype"]    = "Liquidation" if eventcd == "LIQ" else "Memorial"
        minprice = row.get("minimumprice") or ""
        maxprice = row.get("maximumprice") or ""
        liq_price = minprice if minprice == maxprice and minprice else (minprice or maxprice)
        if gross:
            result["dividend_amount"] = gross; result["tax_marker"] = "GROSS"
        elif net:
            result["dividend_amount"] = net;   result["tax_marker"] = "GROSS"
        elif liq_price:
            result["dividend_amount"] = liq_price; result["tax_marker"] = "GROSS"
        result["dividend_currency"] = ratecurencd
        result["depositary_fee"]  = depositary_fee
        result["tax_relief_fee"]  = tax_relief_fee
        return result

    # ── FRANK standalone (no DIV partner) → Cash Dividend with CFI or frankdiv as amount ──
    if eventcd == "FRANK" and row.get("_standalone_frank"):
        result["event_type"] = "Cash Dividend"
        amount = row.get("conduitfrgnincome") or row.get("frankdiv") or ""
        if amount:
            result["dividend_amount"] = amount
            result["tax_marker"]      = "GROSS"
        result["dividend_currency"] = ratecurencd
        if marker == "INT":
            result["subtype"] = "Interim"
        elif marker == "FNL":
            result["subtype"] = "Final"
        elif marker == "ANL":
            result["subtype"] = "Annual"
        return result

    # ── DIV / DIVIF / DRIP / PID ─────────────────────────────────────────────
    if eventcd in {"DIV", "DIVIF", "DRIP", "PID"}:
        if marker == "SPL":
            result["event_type"] = "Special Dividend"
        elif marker == "MEM":
            result["event_type"] = "Special Dividend"
            result["subtype"]    = "Memorial"
        elif marker == "ISC":
            result["event_type"] = "Cash Dividend"; result["subtype"] = "Interest on Capital"
        elif marker == "CGS":
            result["event_type"] = "Special Dividend"; result["subtype"] = "Short-Term Capital Gains"
        elif marker == "CGL":
            result["event_type"] = "Special Dividend"; result["subtype"] = "Long-Term Capital Gains"
        elif eventcd == "PID":
            result["event_type"] = "Cash Dividend"; result["subtype"] = "Property Income Distribution"
        elif marker == "INT":
            result["event_type"] = "Cash Dividend"; result["subtype"] = "Interim"
        elif marker == "FNL":
            result["event_type"] = "Cash Dividend"; result["subtype"] = "Final"
        elif marker == "ANL":
            result["event_type"] = "Cash Dividend"; result["subtype"] = "Annual"
        elif marker == "VAR":
            result["event_type"] = "Special Dividend"; result["subtype"] = "Variable"
        else:
            result["event_type"] = "Cash Dividend"

        if is_au:
            result["dividend_amount"] = net if net else gross
            result["tax_marker"]      = "GROSS"
        else:
            if gross:
                result["dividend_amount"] = gross; result["tax_marker"] = "GROSS"
            elif net:
                result["dividend_amount"] = net;   result["tax_marker"] = "GROSS"

        result["dividend_currency"] = ratecurencd
        if is_br and marker == "ISC":
            result["subtype"] = "Interest on Capital"; result["tax_marker"] = "GROSS"; result["adjusted_wht"] = "17.5%"
        # UK REIT PID override
        if row.get("_is_pid"):
            result["_base_subtype"] = result.get("subtype") or ""
            result["subtype"]       = "Property Income Distribution"
            result["adjusted_wht"]  = "20%"
        result["depositary_fee"]  = depositary_fee
        result["tax_relief_fee"]  = tax_relief_fee
        return result

    # ── DIVRC (REIT Dividend Reclassification) ────────────────────────────────
    if eventcd == "DIVRC":
        result["event_type"] = "Cash Dividend"
        result["subtype"]    = "REIT Reclassification"
        if gross:
            result["dividend_amount"] = gross; result["tax_marker"] = "GROSS"
        elif net:
            result["dividend_amount"] = net;   result["tax_marker"] = "GROSS"
        result["dividend_currency"] = ratecurencd
        result["depositary_fee"]    = depositary_fee
        result["tax_relief_fee"]    = tax_relief_fee
        return result

    # ── ANN (Announcement) → route by relatedeventcd ─────────────────────────
    if eventcd == "ANN":
        related = (row.get("relatedeventcd") or "").upper().strip()
        if related == "MRGR":
            result["event_type"] = "Merger & Acquisition"
            result["ma_subtype"] = "Announcement"
            result["ma_mandatory_voluntary"] = row.get("mandvoluflag") or ""
        elif related == "DMRGR":
            result["event_type"] = "Spin-Off"
            result["ma_subtype"] = "Announcement"
            result["ma_mandatory_voluntary"] = row.get("mandvoluflag") or ""
        elif related == "TKOVR":
            result["event_type"] = "Merger & Acquisition"
            result["ma_subtype"] = "Announcement"
            result["ma_mandatory_voluntary"] = row.get("mandvoluflag") or ""
        return result

    # ── LSTAT (Listing Status Change → Delisting or Trading Suspension) ─────────
    if eventcd == "LSTAT":
        related = (row.get("relatedeventcd") or "").upper().strip()
        if related == "LSTAT":
            result["event_type"] = "Trading Suspension"
        else:
            result["event_type"] = "Delisting"
            if related in ("TKOVR", "MRGR"):
                result["subtype"] = "Post-Merger"
            elif related == "DMRGR":
                result["subtype"] = "Post-Spin-Off"
            elif related in ("CLEAN", "CORR"):
                result["subtype"] = ""
            else:
                result["subtype"] = related or ""
        return result

    # ── ISCHG (Issuer Change — Name Change) ───────────────────────────────────
    if eventcd == "ISCHG":
        related = (row.get("relatedeventcd") or "").upper().strip()
        result["event_type"] = "ID Change"
        if related == "ISCHG":
            result["subtype"] = "Name Change"
        elif related == "CORR":
            result["subtype"] = "Correction"
        elif related == "CLEAN":
            result["subtype"] = "Data Clean"
        else:
            result["subtype"] = related or ""
        result["new_name"]       = row.get("issnewname")       or ""
        result["old_name"]       = row.get("issoldname")       or ""
        result["id_change_dt"]   = row.get("namechangedt") or row.get("effectivedt") or ""
        result["new_local_code"] = row.get("newlocalcode")     or ""
        result["old_local_code"] = row.get("oldlocalcode")     or ""
        result["new_exchg"]      = row.get("newexchgcd")       or ""
        result["old_exchg"]      = row.get("oldexchgcd")       or ""
        result["new_country"]    = row.get("newcntrycd")       or ""
        result["old_country"]    = row.get("oldcntrycd")       or ""
        result["new_isin"]       = row.get("newisin")          or ""
        result["old_isin"]       = row.get("oldisin")          or ""
        result["new_currency"]   = row.get("newcurencd")       or ""
        result["old_currency"]   = row.get("oldcurencd")       or ""
        result["new_trading_ccy"]= row.get("newtradingcurencd") or ""
        result["old_trading_ccy"]= row.get("oldtradingcurencd") or ""
        return result

    # ── SDCHG (Security Description Change — Currency Change) ────────────────
    if eventcd == "SDCHG":
        if row.get("newtradingcurencd") or row.get("oldtradingcurencd"):
            result["event_type"]     = "ID Change"
            result["subtype"]        = "Currency Change"
            result["id_change_dt"]   = row.get("effectivedt") or ""
            result["new_trading_ccy"]= row.get("newtradingcurencd") or ""
            result["old_trading_ccy"]= row.get("oldtradingcurencd") or ""
            result["new_currency"]   = row.get("newcurencd") or ""
            result["old_currency"]   = row.get("oldcurencd") or ""
            return result

    # ── ICC (ISIN Change) ─────────────────────────────────────────────────────
    if eventcd == "ICC":
        result["event_type"]   = "ID Change"
        result["subtype"]      = "ISIN Change"
        result["id_change_dt"] = row.get("effectivedt") or ""
        result["new_isin"]     = row.get("newisin")  or ""
        result["old_isin"]     = row.get("oldisin")  or ""
        return result

    # ── LCC (Listing Code Change — Ticker Change) ─────────────────────────────
    if eventcd == "LCC":
        result["event_type"]     = "ID Change"
        result["subtype"]        = "Ticker Change"
        result["id_change_dt"]   = row.get("effectivedt") or ""
        result["new_local_code"] = row.get("newlocalcode")      or ""
        result["old_local_code"] = row.get("oldlocalcode")      or ""
        result["new_exchg"]      = row.get("newexchgcd")        or ""
        result["old_exchg"]      = row.get("oldexchgcd")        or ""
        result["new_country"]    = row.get("newcntrycd")        or ""
        result["old_country"]    = row.get("oldcntrycd")        or ""
        result["new_isin"]       = row.get("newisin")           or ""
        result["old_isin"]       = row.get("oldisin")           or ""
        result["new_currency"]   = row.get("newcurencd")        or ""
        result["old_currency"]   = row.get("oldcurencd")        or ""
        result["new_trading_ccy"]= row.get("newtradingcurencd") or ""
        result["old_trading_ccy"]= row.get("oldtradingcurencd") or ""
        return result

    return result



def normalize_dates(records):
    """Normalize date fields: replace slashes with dashes (e.g. 2026/04/01 → 2026-04-01)."""
    date_fields = ["exdt", "paydt", "recorddt", "declarationdt", "effectivedt",
                   "expcompletiondt", "closedt", "unconditionaldt", "compulsoryacqdt",
                   "optionelectiondt", "ntschangedt", "periodenddt", "eventcreatedt",
                   "feedgendate", "namechangedt"]
    for r in records:
        for f in date_fields:
            v = r.get(f)
            if v and isinstance(v, str):
                r[f] = v.replace("/", "-")
    return records


# ── Step 1: Deduplicate ───────────────────────────────────────────────────────
def deduplicate(records):
    raw_df = pd.DataFrame(records)
    raw_df["_ts"] = raw_df["feedgendate"].apply(parse_feedgendate)
    # Give FRANK priority over DRIP when both have same eventid/optionid/mic/feedgendate
    raw_df["_eventcd_priority"] = raw_df["eventcd"].apply(
        lambda x: 0 if str(x).upper() == "FRANK" else (1 if str(x).upper() == "DRIP" else 2)
    )
    raw_df = (
        raw_df
        .sort_values(["_ts", "_eventcd_priority"], ascending=[False, True])
        .drop_duplicates(subset=["eventid", "optionid", "operationalmic"], keep="first")
        .drop(columns=["_ts", "_eventcd_priority"])
    )
    return raw_df.to_dict(orient="records")



# ── Step 2: Merge ─────────────────────────────────────────────────────────────
def merge_events(records_list):
    # Pre-pass: transfer frankdiv/unfrankdiv from FRANK records onto DIV records (same eventid+mic)
    frank_map = {}
    for r in records_list:
        if (r.get("eventcd") or "").upper() == "FRANK":
            key = (r.get("eventid"), r.get("operationalmic"))
            if r.get("frankdiv") or r.get("conduitfrgnincome"):
                frank_map[key] = r

    for r in records_list:
        if (r.get("eventcd") or "").upper() == "FRANK":
            r["frank_div_raw"]    = r.get("frankdiv") or ""
            r["cfi_raw"]          = r.get("conduitfrgnincome") or ""
        if (r.get("eventcd") or "").upper() == "DIV":
            key = (r.get("eventid"), r.get("operationalmic"))
            if key in frank_map:
                r["_frankdiv"] = frank_map[key].get("frankdiv") or ""
                r["_cfi"]      = frank_map[key].get("conduitfrgnincome") or ""

    # Mark FRANK records that have no corresponding DIV record as standalone
    div_keys = {(r.get("eventid"), r.get("operationalmic"))
                for r in records_list if (r.get("eventcd") or "").upper() == "DIV"}
    for r in records_list:
        if (r.get("eventcd") or "").upper() == "FRANK":
            key = (r.get("eventid"), r.get("operationalmic"))
            if key not in div_keys and (r.get("frankdiv") or r.get("conduitfrgnincome")):
                r["_standalone_frank"] = True

    # Pre-pass: mark DIV records as PID if:
    #   1. structcd=REIT AND operationalmic=XLON, OR
    #   2. there is a PID record with the same eventid
    # Also transfer propertyincomediviendportion / nonpropertyincomediviendportion from PID record
    pid_map = {}
    for r in records_list:
        if (r.get("eventcd") or "").upper() == "PID":
            key = (r.get("eventid"), r.get("operationalmic"))
            pid_map[key] = r

    for r in records_list:
        if (r.get("eventcd") or "").upper() in ("DIV", "DIVIF"):
            is_uk_reit = ((r.get("structcd") or "").upper() == "REIT"
                          and (r.get("operationalmic") or "").upper() == "XLON")
            key = (r.get("eventid"), r.get("operationalmic"))
            has_pid_partner = key in pid_map
            if is_uk_reit or has_pid_partner:
                r["_is_pid"] = True
                if key in pid_map:
                    pid_rec = pid_map[key]
                    pid_amt = pid_rec.get("propertyincomediviendportion") or ""
                    r["_pid_amount"] = pid_amt
                    # Calculate non-PID as gross - PID if not explicitly provided
                    non_pid = pid_rec.get("nonpropertyincomediviendportion") or ""
                    if not non_pid and pid_amt:
                        gross = r.get("grossdividend") or r.get("declgrossamt") or ""
                        try:
                            non_pid = str(round(float(gross) - float(pid_amt), 10))
                        except (ValueError, TypeError):
                            non_pid = ""
                    r["_non_pid_amount"] = non_pid

    groups = defaultdict(list)
    for r in records_list:
        key = (r.get("eventid", ""), r.get("operationalmic", ""))
        groups[key].append(r)

    merged = []
    for (eid, mic), group in groups.items():
        if len(group) == 1:
            merged.append(group[0])
            continue

        # ── DRIP handling ─────────────────────────────────────────────────────
        # If group has both DIV and DRIP with same eventid:
        #   → Keep DIV as primary, discard DRIP
        #   → If DIV has no amount, try DRIP as fallback for grossdividend/declgrossamt
        # If group has only DRIP (standalone):
        #   → Keep DRIP — will be classified as Cash/Special Dividend in classifier
        div_recs  = [r for r in group if (r.get("eventcd") or "").upper() == "DIV"]
        drip_recs = [r for r in group if (r.get("eventcd") or "").upper() == "DRIP"]
        if div_recs and drip_recs:
            # DIV takes priority — enrich with DRIP amount if DIV has none
            div = div_recs[0]
            if not div.get("grossdividend") and not div.get("declgrossamt"):
                drip = drip_recs[0]
                if drip.get("grossdividend"):
                    div["grossdividend"] = drip.get("grossdividend")
                elif drip.get("declgrossamt"):
                    div["declgrossamt"]  = drip.get("declgrossamt")
                    div["declcurencd"]   = drip.get("declcurencd") or div.get("declcurencd") or ""
            group = [r for r in group if (r.get("eventcd") or "").upper() != "DRIP"]
        # standalone DRIP (no DIV in group) — keep as-is, falls through to classifier

        if not group:
            continue
        if len(group) == 1:
            merged.append(group[0])
            continue

        eventcd    = (group[0].get("eventcd") or "").upper().strip()
        option_ids = [r.get("optionid", "") for r in group]

        # If multiple records but only because of empty optionid alongside real ones →
        # take DIV as primary, keep FRANK separate (carries frankdiv data)
        real_ids = [oid for oid in option_ids if str(oid).strip()]
        if len(set(real_ids)) <= 1:
            frank_recs = [r for r in group if (r.get("eventcd") or "").upper() == "FRANK"]
            other_recs = [r for r in group if (r.get("eventcd") or "").upper() != "FRANK"]
            chosen = next((r for r in other_recs if str(r.get("optionid","")).strip()), other_recs[0] if other_recs else group[0])
            merged.append(chosen)
            merged.extend(frank_recs)
            continue

        # ── TKOVR: multiple optionids → merge all options ─────────────────────
        if eventcd == "TKOVR" and len(set(option_ids)) > 1:
            base = dict(sorted(group, key=lambda r: str(r.get("optionid", "")))[0])
            base["_is_tkovr_election"] = True
            # Filter out Debenture legs — ignored
            group = [r for r in group if r.get("paytypecd", "") != "D"]
            paytypes = sorted(set(r.get("paytypecd", "") for r in group if r.get("paytypecd", "") != "D"))
            base["_tkovr_paytypes"] = paytypes

            cash_opt  = next((r for r in group if r.get("paytypecd") == "C"), None)
            stock_opt = next((r for r in group if r.get("paytypecd") == "S"), None)
            mixed_opt = next((r for r in group if r.get("paytypecd") == "B"), None)

            if cash_opt:
                base["_ma_cash_terms"]    = cash_opt.get("minimumprice") or cash_opt.get("maximumprice") or ""
                base["_ma_cash_terms_currency"] = cash_opt.get("ratecurencd") or cash_opt.get("tradingcurencd") or ""
            if stock_opt:
                base["_ma_offeror_isin"]   = stock_opt.get("outisin")          or ""
                base["_ma_offeror_ticker"] = stock_opt.get("outbbgcompticker")  or ""
                ratio = safe_div(stock_opt.get("rationew"), stock_opt.get("ratioold"))
                base["_eca_stock_ratio"]    = f"{ratio:.6f}" if ratio else ""
                base["_eca_stock_terms"]    = fmt_stock_terms(stock_opt.get("rationew"), stock_opt.get("ratioold"))
            if mixed_opt:
                base["_ma_cash_terms"]        = mixed_opt.get("minimumprice") or mixed_opt.get("maximumprice") or ""
                base["_ma_cash_terms_currency"]    = mixed_opt.get("ratecurencd") or mixed_opt.get("tradingcurencd") or ""
                base["_ma_offeror_isin"]      = base.get("_ma_offeror_isin")   or mixed_opt.get("outisin")          or ""
                base["_ma_offeror_ticker"]    = base.get("_ma_offeror_ticker") or mixed_opt.get("outbbgcompticker")  or ""
                ratio = safe_div(mixed_opt.get("rationew"), mixed_opt.get("ratioold"))
                if ratio and not base.get("_eca_stock_ratio"):
                    base["_eca_stock_ratio"] = f"{ratio:.6f}"
                    base["_eca_stock_terms"] = fmt_stock_terms(mixed_opt.get("rationew"), mixed_opt.get("ratioold"))
                elif ratio:
                    base["_ma_cash_terms_ratio"] = f"{ratio:.6f}"  # preserve for Mixed Cash component

            merged.append(base)
            continue

        # ── Dividend Election: voting=V, multiple optionids ───────────────────
        votings = [r.get("voting", "") for r in group]
        markers = [r.get("marker", "") for r in group]

        # DIV+SPL with multiple optionids: always Special Dividend
        # Priority: C leg → B leg → group[0]; never take S-only leg
        if "SPL" in markers and len(set(option_ids)) > 1:
            default_row = (
                next((r for r in group if r.get("paytypecd") == "C"), None) or
                next((r for r in group if r.get("paytypecd") == "B"), None) or
                group[0]
            )
            merged.append(dict(default_row))
            continue

        if any(v == "V" for v in votings) and len(set(option_ids)) > 1 and "SPL" not in markers:
            # Drop scrip legs (paytypecd=S) — always ignored
            cash_group = [r for r in group if r.get("paytypecd") != "S"]

            # Check if there is a real stock election leg (paytypecd=B)
            b_row = next((r for r in cash_group if r.get("paytypecd") == "B"), None)

            if b_row:
                # Real Cash or Stock election — existing logic
                cash_row  = next((r for r in cash_group if str(r.get("optionid", "")) == "1"), None)
                stock_row = next((r for r in group     if str(r.get("optionid", "")) == "2"), None)
                if not cash_row or not stock_row:
                    merged.extend(cash_group)
                    continue
                combined = dict(cash_row)
                combined["_is_election"]        = True
                combined["_opt1_grossdividend"] = cash_row.get("grossdividend", "")
                combined["_opt1_netdividend"]   = cash_row.get("netdividend", "")
                combined["_opt2_rationew"]      = stock_row.get("rationew", "")
                combined["_opt2_ratioold"]      = stock_row.get("ratioold", "")
                combined["optionelectiondt"]    = (stock_row.get("optionelectiondt") or
                                                   cash_row.get("optionelectiondt") or "")
                combined["rationew"]            = stock_row.get("rationew", "")
                combined["ratioold"]            = stock_row.get("ratioold", "")
                combined["paytypecd"]           = "B"
                merged.append(combined)
                continue

            # Currency election — multiple paytypecd=C with different currencies
            # Priority: defaultoptionflag=T with amount → optionid=1 with amount → any default
            def _has_amount(r):
                return bool(r.get("grossdividend") or r.get("netdividend"))

            chosen = next(
                (r for r in cash_group if r.get("defaultoptionflag") == "T" and _has_amount(r)),
                None
            )
            if not chosen:
                chosen = next(
                    (r for r in cash_group if str(r.get("optionid", "")) == "1" and _has_amount(r)),
                    None
                )
            if not chosen:
                # Any optionid with amount
                chosen = next(
                    (r for r in sorted(cash_group, key=lambda x: str(x.get("optionid", "")))
                     if _has_amount(r)),
                    None
                )
            if not chosen:
                chosen = next(
                    (r for r in cash_group if r.get("defaultoptionflag") == "T"),
                    cash_group[0] if cash_group else group[0]
                )
            merged.append(dict(chosen))
            continue

        merged.extend(group)

    return merged



# ── Step 3: Build rows ────────────────────────────────────────────────────────
MA_FIELDS = [
    "MA_Offeror", "MA_Hostile", "MA_Mand_Vol", "MA_Event_Subtype",
    "Deal_Type",
    "MA_Cash_Terms", "MA_Cash_Terms_Currency",
    "ECA_Stock_Ratio", "ECA_Stock_Terms", "MA_Offeror_ISIN", "MA_Offeror_Ticker",
    "MA_Effective_Date", "MA_Exp_Completion",
    "MA_Merger_Status",
    "MA_Close_Date",
    "ECA_Status",
    "New_Name", "Old_Name", "ID_Change_Date",
    "New_Local_Code", "Old_Local_Code",
    "New_Exchg", "Old_Exchg",
    "New_Country", "Old_Country",
    "New_ISIN", "Old_ISIN",
    "New_Currency", "Old_Currency",
    "New_Trading_CCY", "Old_Trading_CCY",
]
DIV_FIELDS = ["Dividend_Amount","Frankdiv","CFI","Tax_Marker","Adjusted_WHT","Depositary_Fee","Tax_Relief_Fee","Dividend_Currency",
              "Stock_Div_Pct","Stock_Div_Ratio","Split_Ratio","Split_Terms",
              "Sub_Price","Sub_Currency","Sub_Ratio","Default_Option",
              "REIT_Flag","Creation_Date"]

def derive_eca_status(r, eventcd):
    """Derive ECA_Status for M&A and Spin-Off events."""
    from datetime import datetime
    today = datetime.today().date()

    def is_past(d):
        try: return datetime.strptime(str(d)[:10], "%Y-%m-%d").date() < today
        except: return False

    subtypecd  = (r.get("eventsubtypecd") or "").upper()
    closedt    = r.get("closedt") or r.get("_ma_close_date") or ""
    effectivedt= r.get("effectivedt") or ""
    exdt       = r.get("exdt") or ""

    if subtypecd in ("MRGR", "TENDMRGR"):
        return "Completed"
    if closedt and is_past(closedt):
        return "Completed"
    if effectivedt and is_past(effectivedt):
        return "Completed"
    if eventcd == "DMRGR" and exdt and is_past(exdt):
        return "Completed"
    return "Pending"


def build_rows(processed_records):
    rows = []
    for r in processed_records:
        is_election       = r.get("_is_election", False) and r.get("marker", "") != "SPL"
        is_tkovr_election = r.get("_is_tkovr_election", False)
        cl                = classify_event(r)

        # Always skip ignored events (WAR / Warrants, Debenture legs in TKOVR).
        if cl["ignore"]:
            continue

        row = {col: r.get(col, "") for col in RAW_COLUMNS}
        # Ex-Date fallback:
        # TKOVR/MRGR → effectivedt only (= offer commencement date).
        #   closedt (offer expiry / merger close) is already shown in MA_Close_Date
        #   and must not be used as exdt — deal may still be pending.
        # LIQ with no exdt but with an amount → recorddt as fallback.
        # All other events → effectivedt only.
        if not row.get("exdt"):
            if (r.get("eventcd", "").upper() == "LIQ"
                    and (r.get("grossdividend") or r.get("netdividend")
                         or r.get("minimumprice") or r.get("maximumprice"))
                    and r.get("recorddt")):
                row["exdt"] = r.get("recorddt", "")
            else:
                row["exdt"] = r.get("effectivedt", "")
        # initialise derived fields
        for f in DIV_FIELDS + MA_FIELDS:
            row[f] = ""

        if is_tkovr_election:
            paytypes = r.get("_tkovr_paytypes", [])
            label_map = {"C": "Cash", "S": "Stock", "B": "Cash & Stock"}
            deal_type_label = " + ".join(label_map.get(p, p) for p in paytypes)
            row["Event_Type"]        = "Merger & Acquisition"
            row["Subtype"]           = "Election" if len(paytypes) >= 2 else deal_type_label
            row["Deal_Type"]         = deal_type_label
            row["MA_Offeror"]        = r.get("offerorname", "")
            row["MA_Hostile"]        = r.get("hostile", "")
            row["MA_Mand_Vol"]       = r.get("mandvoluflag", "")
            row["MA_Event_Subtype"]  = r.get("eventsubtypecd", "")
            row["MA_Cash_Terms"]     = r.get("_ma_cash_terms", "")
            row["MA_Cash_Terms_Currency"]  = r.get("_ma_cash_terms_currency", "")
            row["ECA_Stock_Ratio"]    = r.get("_eca_stock_ratio", "")
            row["ECA_Stock_Terms"]    = r.get("_eca_stock_terms", "")
            row["MA_Offeror_ISIN"]   = r.get("_ma_offeror_isin", "")
            row["MA_Offeror_Ticker"] = r.get("_ma_offeror_ticker", "")
            row["MA_Close_Date"]     = r.get("closedt", "")
            row["ECA_Status"]        = derive_eca_status(r, r.get("eventcd","").upper())

        elif cl["event_type"] == "Merger & Acquisition":
            row["Event_Type"]        = "Merger & Acquisition"
            row["Subtype"]           = cl["ma_subtype"] if cl["ma_subtype"] else cl["ma_deal_type"]
            row["Deal_Type"]         = cl["ma_deal_type"]
            row["MA_Offeror"]        = cl["ma_offeror"]
            row["MA_Hostile"]        = cl["ma_hostile"]
            row["MA_Mand_Vol"]       = cl["ma_mandatory_voluntary"]
            row["MA_Event_Subtype"]  = cl["ma_event_subtype"]
            row["MA_Cash_Terms"]          = cl["ma_cash_terms"]
            row["MA_Cash_Terms_Currency"] = cl["ma_cash_terms_currency"]
            row["ECA_Stock_Ratio"]        = cl["eca_stock_ratio"]
            row["ECA_Stock_Terms"]        = cl["eca_stock_terms"]
            row["MA_Offeror_ISIN"]        = cl["ma_offeror_isin"]
            row["MA_Offeror_Ticker"] = cl["ma_offeror_ticker"]
            row["MA_Effective_Date"] = cl["ma_effective_date"]
            row["MA_Exp_Completion"] = cl["ma_exp_completion"]
            row["MA_Merger_Status"]  = cl["ma_merger_status"]
            row["MA_Close_Date"]     = r.get("closedt", "")
            row["ECA_Status"]        = derive_eca_status(r, r.get("eventcd","").upper())

        elif cl["event_type"] in ("Spin-Off", "Stock Distribution"):
            row["Event_Type"]        = cl["event_type"]
            row["Subtype"]           = cl["ma_subtype"]
            row["Deal_Type"]         = cl["ma_deal_type"]
            row["MA_Mand_Vol"]       = cl["ma_mandatory_voluntary"]
            row["ECA_Stock_Ratio"]    = cl["eca_stock_ratio"]
            row["ECA_Stock_Terms"]    = cl["eca_stock_terms"]
            row["MA_Offeror_ISIN"]   = cl["ma_offeror_isin"]
            row["MA_Offeror_Ticker"] = cl["ma_offeror_ticker"]
            row["MA_Effective_Date"] = cl["ma_effective_date"]
            row["MA_Exp_Completion"] = cl["ma_exp_completion"]
            row["MA_Merger_Status"]  = cl["ma_merger_status"]
            row["MA_Cash_Terms"]     = cl["ma_cash_terms"]
            row["MA_Cash_Terms_Currency"]  = cl["ma_cash_terms_currency"]
            row["ECA_Status"]        = derive_eca_status(r, r.get("eventcd","").upper())

        elif is_election:
            row["Event_Type"]        = "Cash or Stock Dividend"
            row["Subtype"]           = "Election"
            row["Dividend_Amount"]   = r.get("_opt1_grossdividend") or r.get("_opt1_netdividend") or ""
            row["Tax_Marker"]        = "GROSS"
            row["Dividend_Currency"] = r.get("ratecurencd", "")
            row["Depositary_Fee"]    = r.get("depfees", "")
            row["Tax_Relief_Fee"]    = r.get("taxrelieffee", "")
            ratio = safe_div(r.get("_opt2_rationew"), r.get("_opt2_ratioold"))
            row["Stock_Div_Pct"]     = f"{ratio*100:.4f}%" if ratio else ""
            row["Stock_Div_Ratio"]   = f"{1+ratio:.6f}"    if ratio else ""
            row["Default_Option"]    = "Cash"

        elif cl["event_type"] == "ID Change":
            row["Event_Type"]     = "ID Change"
            row["Subtype"]        = cl["subtype"]
            row["New_Name"]        = cl["new_name"]
            row["Old_Name"]        = cl["old_name"]
            row["ID_Change_Date"]  = cl["id_change_dt"]
            row["New_Local_Code"]  = cl["new_local_code"]
            row["Old_Local_Code"]  = cl["old_local_code"]
            row["New_Exchg"]       = cl["new_exchg"]
            row["Old_Exchg"]       = cl["old_exchg"]
            row["New_Country"]     = cl["new_country"]
            row["Old_Country"]     = cl["old_country"]
            row["New_ISIN"]        = cl["new_isin"]
            row["Old_ISIN"]        = cl["old_isin"]
            row["New_Currency"]    = cl["new_currency"]
            row["Old_Currency"]    = cl["old_currency"]
            row["New_Trading_CCY"] = cl["new_trading_ccy"]
            row["Old_Trading_CCY"] = cl["old_trading_ccy"]

        else:
            row["Event_Type"]        = cl["event_type"]
            row["Subtype"]           = cl["subtype"]
            row["Dividend_Amount"]   = cl["dividend_amount"]
            row["Tax_Marker"]        = cl["tax_marker"]
            row["Adjusted_WHT"]      = cl["adjusted_wht"]
            row["Frankdiv"]          = r.get("_frankdiv", "") or r.get("frankdiv", "")
            row["CFI"]               = r.get("_cfi", "") or r.get("conduitfrgnincome", "")
            # Adjusted WHT for Australian dividends
            _frankdiv_val = r.get("_frankdiv") or r.get("frankdiv") or ""
            _cfi_val      = r.get("_cfi") or r.get("conduitfrgnincome") or ""
            if (_frankdiv_val or _cfi_val) and cl.get("dividend_amount"):
                try:
                    wht_au   = 0.30
                    frankdiv = float(_frankdiv_val or 0)
                    cfi      = float(_cfi_val or 0)
                    div_amt  = float(cl["dividend_amount"])
                    if div_amt > 0:
                        adj_wht = wht_au * (1 - (frankdiv + cfi) / div_amt)
                        adj_pct = adj_wht * 100
                        decimals = 2 if adj_pct == round(adj_pct, 2) else 6
                        row["Adjusted_WHT"] = f"{adj_pct:.{decimals}f}%"
                except (ValueError, TypeError):
                    pass
            row["Dividend_Currency"] = cl["dividend_currency"]
            row["Depositary_Fee"]    = cl["depositary_fee"]
            row["Tax_Relief_Fee"]    = cl["tax_relief_fee"]
            row["Stock_Div_Pct"]     = cl["stock_dividend_pct"]
            row["Stock_Div_Ratio"]   = cl["stock_dividend_ratio"]
            row["Split_Ratio"]       = cl["split_ratio"]
            row["Split_Terms"]       = cl["split_terms"]
            row["Sub_Price"]         = cl["subscription_price"]
            row["Sub_Currency"]      = cl["subscription_currency"]
            row["Sub_Ratio"]         = cl["subscription_ratio"]

        row["_ignored"] = cl["ignore"]
        # Creation_Date — universal across all event types
        row["Creation_Date"] = r.get("eventcreatedt", "")
        # REIT_Flag — True if structcd=REIT
        row["REIT_Flag"] = (r.get("structcd") or "").upper() == "REIT"
        # Evt_Status — human-readable action code
        _act = (r.get("evtactioncd") or "").upper()
        row["Evt_Status"] = {"I": "New", "U": "Updated", "D": "Deleted", "C": "Cancelled"}.get(_act, _act)

        # ── PID split: if PID_Amount and Non_PID_Amount both known → two rows ──
        pid_amt     = r.get("_pid_amount") or ""
        non_pid_amt = r.get("_non_pid_amount") or ""
        if pid_amt and non_pid_amt and row.get("Subtype") == "Property Income Distribution":
            # Row 1: PID portion
            row_pid = dict(row)
            row_pid["Dividend_Amount"] = pid_amt
            row_pid["Adjusted_WHT"]    = "20%"
            row_pid["Subtype"]         = "Property Income Distribution"
            rows.append(row_pid)
            # Row 2: Non-PID portion
            row_non = dict(row)
            row_non["Dividend_Amount"] = non_pid_amt
            row_non["Adjusted_WHT"]    = ""
            row_non["Subtype"]         = cl.get("_base_subtype") or ""
            rows.append(row_non)
        else:
            rows.append(row)
    return rows


# ── API ───────────────────────────────────────────────────────────────────────
def _fetch_one(
    isin: str,
    token: str,
    operational_mic: str | None,
    from_date: date | None,
    date_param_name: str | None,
    timeout: int,
) -> tuple[list, dict]:
    """
    Performs a single EDI API call.

    Args:
      date_param_name: 'fromexdate', 'fromdate', or None to omit the date filter.
                       Only takes effect when from_date is also set.

    Returns:
      (raw_records, call_info) where call_info contains URL, status, headers,
      body preview, and the relevant rate-limit / record-count headers.

    Raises:
      EDIAPIError: on non-200/204 response or connection failure.
    """
    url = (
        f"https://api3.exchange-data.com/GetHistoricalCorporateActions"
        f"?format=JSON&ISIN={isin}"
        f"{'&operationalMic=' + operational_mic if operational_mic else ''}"
    )
    if from_date and date_param_name:
        url += f"&{date_param_name}={from_date.strftime('%Y-%m-%d')}"

    try:
        response = requests.get(url, headers={"authorization": token}, timeout=timeout)
    except requests.exceptions.ConnectionError as e:
        raise EDIAPIError(f"Could not connect to EDI API ({date_param_name or 'no-date'}).") from e
    except requests.exceptions.Timeout as e:
        raise EDIAPIError(f"EDI API request timed out after {timeout}s ({date_param_name or 'no-date'}).") from e
    except requests.exceptions.RequestException as e:
        raise EDIAPIError(f"Unexpected error ({date_param_name or 'no-date'}): {e}") from e

    # 204 No Content = success, but no records for this query (not an error).
    if response.status_code == 204:
        raw_records = []
    elif response.status_code == 200:
        raw_records = response.json().get("jsondata", [])
    else:
        raise EDIAPIError(
            f"API Error {response.status_code} ({date_param_name or 'no-date'}): {response.text[:500]}",
            status_code=response.status_code,
        )

    call_info = {
        "label":          date_param_name or "no-date",
        "url":            url,
        "status_code":    response.status_code,
        "body_preview":   response.text[:500] if response.text else "",
        "headers":        dict(response.headers),
        "record_count":   response.headers.get("X-Record-Count",       "–"),
        "total_records":  response.headers.get("X-Total-Records",      "–"),
        "rate_limit":     response.headers.get("X-Ratelimit-Limit",    "–"),
        "rate_remaining": response.headers.get("X-Ratelimit-Remaining","–"),
    }
    return raw_records, call_info


def _min_str_int(*values: str) -> str:
    """Return the smallest of several stringly-typed integers (e.g. rate-limit
    headers). Falls back to '–' if none parse as ints."""
    nums = []
    for v in values:
        try:
            nums.append(int(v))
        except (TypeError, ValueError):
            continue
    return str(min(nums)) if nums else "–"


def _max_str_int(*values: str) -> str:
    """Same as _min_str_int but returns the largest value."""
    nums = []
    for v in values:
        try:
            nums.append(int(v))
        except (TypeError, ValueError):
            continue
    return str(max(nums)) if nums else "–"


def fetch_records(
    isin: str,
    token: str,
    operational_mic: str | None = None,
    from_date: date | None = None,
    timeout: int = 30,
) -> dict:
    """
    Fetches Corporate Actions from the EDI API and normalizes dates (Step 1).

    When `from_date` is set, performs TWO calls in parallel — one with
    `fromexdate=` (filters by ex-date) and one with `fromdate=` (broader
    filter that catches M&A / Spin-Off events without an ex-date) — and
    deduplicates the merged result by (eventid, optionid, operationalmic).

    When `from_date` is None, only a single call is made (no date filter).

    Args:
      isin:            ISIN to query (required).
      token:           Bearer token for the `authorization` header (required).
      operational_mic: Optional MIC filter (e.g. "XSWX").
      from_date:       Optional ex-date lower bound. Triggers dual-fetch if set.
      timeout:         HTTP timeout in seconds (per call).

    Returns:
      {
        "records": [...],     # merged & deduplicated, normalized records
        "meta": {
            "isin":           str,
            "record_count":   str,   # number of unique records returned
            "total_records":  str,   # max of both calls' X-Total-Records
            "rate_limit":     str,
            "rate_remaining": str,   # MIN across calls (worst-case headroom)
            "calls":          [call_info, ...],   # one per HTTP call made
        },
      }

    Raises:
      EDIAPIError: if any of the underlying HTTP calls fails.
    """
    # No date filter -> single call, fromexdate omitted entirely.
    if not from_date:
        records, info = _fetch_one(isin, token, operational_mic, None, None, timeout)
        return {
            "records": normalize_dates(records),
            "meta": {
                "isin":           isin,
                "record_count":   info["record_count"],
                "total_records":  info["total_records"],
                "rate_limit":     info["rate_limit"],
                "rate_remaining": info["rate_remaining"],
                "calls":          [info],
            },
        }

    # Date filter set -> two parallel calls.
    with ThreadPoolExecutor(max_workers=2) as executor:
        f_ex   = executor.submit(_fetch_one, isin, token, operational_mic, from_date, "fromexdate", timeout)
        f_full = executor.submit(_fetch_one, isin, token, operational_mic, from_date, "fromdate",   timeout)
        # .result() re-raises any EDIAPIError from the worker threads.
        records_ex,   info_ex   = f_ex.result()
        records_full, info_full = f_full.result()

    # Merge with dedup by (eventid, optionid, operationalmic).
    # Order: fromexdate results first (ex-date events), then fromdate-only extras.
    seen, merged = set(), []
    for r in records_ex + records_full:
        key = (r.get("eventid"), r.get("optionid"), r.get("operationalmic"))
        if key not in seen:
            seen.add(key)
            merged.append(r)

    return {
        "records": normalize_dates(merged),
        "meta": {
            "isin":           isin,
            "record_count":   str(len(merged)),
            "total_records":  _max_str_int(info_ex["total_records"], info_full["total_records"]),
            "rate_limit":     info_full["rate_limit"],
            "rate_remaining": _min_str_int(info_ex["rate_remaining"], info_full["rate_remaining"]),
            "calls":          [info_ex, info_full],
        },
    }
