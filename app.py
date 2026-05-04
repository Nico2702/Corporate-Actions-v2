"""
EDI + FactSet Corporate Actions — Streamlit UI
================================================
Streamlit frontend with two data sources (EDI, FactSet) and a Validation
tab that compares them. Each provider lives in its own library module;
this file is purely UI + orchestration.
"""

import streamlit as st
import pandas as pd
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor

import edi_corporate_actions as edi
import factset_corporate_actions as factset

# ── Page Config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="EDI Corporate Actions", page_icon="📊", layout="wide")

# ── Styling ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .main { background-color: #0f1117; }
    .stApp { background-color: #0f1117; color: #e0e0e0; }
    div[data-testid="stMetricValue"] { font-size: 1.4rem; font-weight: 700; color: #ffffff; }
    div[data-testid="stMetricLabel"] { font-size: 0.75rem; color: #888; }
    .event-badge {
        display: inline-block; padding: 2px 10px; border-radius: 12px;
        font-size: 0.75rem; font-weight: 600;
    }
    .badge-cash     { background: #1a3a2a; color: #4caf50; border: 1px solid #4caf50; }
    .badge-special  { background: #3a2a1a; color: #ff9800; border: 1px solid #ff9800; }
    .badge-stock    { background: #1a2a3a; color: #2196f3; border: 1px solid #2196f3; }
    .badge-split    { background: #2a1a3a; color: #9c27b0; border: 1px solid #9c27b0; }
    .badge-rights   { background: #3a1a1a; color: #f44336; border: 1px solid #f44336; }
    .badge-takeover { background: #1a2a2a; color: #00bcd4; border: 1px solid #00bcd4; }
    .badge-demerger { background: #2a1a2a; color: #e040fb; border: 1px solid #e040fb; }
    .badge-merger   { background: #1a1a2a; color: #7986cb; border: 1px solid #7986cb; }
    .badge-delisting    { background: #2a1a1a; color: #ff5252; border: 1px solid #ff5252; }
    .badge-suspension   { background: #2a2a1a; color: #ffeb3b; border: 1px solid #ffeb3b; }
    .badge-cancelled    { background: #3a1a1a; color: #ff1744; border: 2px solid #ff1744; font-weight: bold; }
    .badge-other    { background: #2a2a2a; color: #aaa;    border: 1px solid #aaa; }
    section[data-testid="stSidebar"] { background-color: #161b22; }
    h1, h2, h3 { color: #ffffff; }
    .stAlert { border-radius: 8px; }
</style>
""", unsafe_allow_html=True)

# ── UI Constants ──────────────────────────────────────────────────────────────
EVENT_TYPE_COLORS = {
    "Cash Dividend":          "badge-cash",
    "Special Dividend":       "badge-special",
    "Stock Dividend":         "badge-stock",
    "Cash or Stock Dividend": "badge-stock",
    "Cash + Stock Dividend":  "badge-stock",
    "Stock Split":            "badge-split",
    "Rights Issue":           "badge-rights",
    "Merger & Acquisition":   "badge-takeover",
    "Spin-Off":               "badge-demerger",
    "Stock Distribution":     "badge-demerger",
    "Delisting":              "badge-delisting",
    "Trading Suspension":     "badge-suspension",
    "ID Change":              "badge-other",
    "Other":                  "badge-other",
}



# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ API Settings")
    edi_key     = st.text_input("EDI API Key",     type="password", placeholder="Bearer token...")
    factset_key = st.text_input("FactSet API Key", type="password", placeholder="(optional, FactSet integration in progress)")
    st.divider()
    st.markdown("### 🔍 Query Parameters")
    isin   = st.text_input("ISIN", placeholder="e.g. CH1256740924", help="Used by EDI")
    ticker = st.text_input("Ticker (FactSet)", placeholder="e.g. AAPL-NAS", help="FactSet ticker-exchange format")
    op_mic = st.text_input("Operational MIC", placeholder="e.g. XSWX")
    use_dates = st.checkbox("Filter From Date", value=True)
    if use_dates:
        from_date = st.date_input("From Date", value=date.today() - timedelta(days=365))
    else:
        from_date = None
    st.divider()
    st.markdown("### 🎛️ Display Filters")
    event_type_filter = st.multiselect(
        "Filter by Event Type",
        options=["Cash Dividend", "Special Dividend", "Stock Dividend",
                 "Cash or Stock Dividend", "Cash + Stock Dividend",
                 "Stock Split", "Rights Issue",
                 "Merger & Acquisition", "Spin-Off", "Stock Distribution",
                 "Other"],
        default=[]
    )
    st.divider()
    debug_mode = st.checkbox("🔧 Debug-Modus (URL, Status, Headers anzeigen)", value=False)
    fetch_btn = st.button("🔄 Fetch Corporate Actions", use_container_width=True, type="primary")


# ── Main ──────────────────────────────────────────────────────────────────────
st.title("📊 Corporate Actions Viewer")
st.caption("EDI + FactSet — with cross-source validation")

if not fetch_btn and "edi_records" not in st.session_state and "factset_records" not in st.session_state:
    with st.expander("📖 Classification Logic Reference", expanded=False):
        st.markdown("""
        **Dividends**
        | Event Type | Subtype | EDI Condition |
        |---|---|---|
        | Cash Dividend | — | `eventcd` ∈ {DIV,DIVIF,DRIP,FRANK,PID}, `marker` ≠ SPL |
        | Cash Dividend | Interest on Capital | `marker` = ISC |
        | Special Dividend | — | `marker` = SPL |
        | Special Dividend | Return of Capital | `eventcd` = RCAP |
        | Special Dividend | Liquidation/Memorial | `eventcd` ∈ {LIQ,MEM} |
        | Stock Dividend | — | non-US, `eventcd` ∈ {DIV,BON}, `paytypecd` = S |
        | Cash or Stock Dividend | Shareholder Election | `voting`=V, multiple optionids |

        **Corporate Events**
        | Event Type | Subtype | EDI Condition |
        |---|---|---|
        | Stock Split | Forward | US: `eventcd` ∈ {DIV,BON,SD,FSPLT}, `paytypecd`=S |
        | Stock Split | Reverse | `eventcd` ∈ {CONSD,RSPLT} |
        | Rights Issue | — | `eventcd` ∈ {RTS,ENT} |

        **Takeovers / M&A / Spin-Offs**
        | Event Type | Subtype | Deal Type | EDI Condition |
        |---|---|---|---|
        | Merger & Acquisition | — | Cash | `eventcd`=TKOVR, `paytypecd`=C |
        | Merger & Acquisition | — | Stock | `eventcd`=TKOVR, `paytypecd`=S |
        | Merger & Acquisition | — | Cash & Stock | `eventcd`=TKOVR, `paytypecd`=B |
        | Merger & Acquisition | Election | Cash + Stock + … | `eventcd`=TKOVR, multiple optionids |
        | Merger & Acquisition | — | Stock | `eventcd`=MRGR, `paytypecd`=S |
        | Merger & Acquisition | Announcement | — | `eventcd`=ANN, `relatedeventcd`=MRGR/TKOVR |
        | Spin-Off | Demerger | Stock | `eventcd`=DMRGR |
        | Spin-Off | Announcement | — | `eventcd`=ANN, `relatedeventcd`=DMRGR |
        | Stock Distribution | Share Distribution | Stock | `eventcd`=DIST |
        """)
    st.info("👈 Configure query parameters in the sidebar and click **Fetch Corporate Actions**.")
    st.stop()

if fetch_btn and not edi_key and not factset_key:
    st.error("⚠️ Please enter at least one API Key (EDI or FactSet).")
    st.stop()
if fetch_btn and edi_key and not isin:
    st.error("⚠️ EDI is enabled — please enter an ISIN.")
    st.stop()
if fetch_btn and factset_key and not ticker:
    st.error("⚠️ FactSet is enabled — please enter a Ticker (e.g. AAPL-NAS).")
    st.stop()

# ── API Call: parallel fetch of both providers ────────────────────────────────
def _fetch_edi():
    if not edi_key:
        return None, "No EDI API key provided."
    try:
        result = edi.fetch_records(
            isin=isin, token=edi_key,
            operational_mic=op_mic or None,
            from_date=from_date,
        )
        return result, None
    except edi.EDIAPIError as e:
        return None, str(e)
    except Exception as e:
        return None, f"Unexpected error: {e}"

def _fetch_factset():
    if not factset_key:
        return None, "No FactSet API key provided."
    if not ticker:
        return None, "No FactSet Ticker provided (e.g. AAPL-NAS)."
    try:
        result = factset.fetch_records(
            ticker=ticker, token=factset_key,
            from_date=from_date,
        )
        return result, None
    except factset.FactSetAPIError as e:
        return None, str(e)
    except NotImplementedError:
        return None, "FactSet integration not yet implemented."
    except Exception as e:
        return None, f"Unexpected error: {e}"

if fetch_btn:
    with st.spinner("Fetching from EDI + FactSet..."):
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_edi = ex.submit(_fetch_edi)
            f_fs  = ex.submit(_fetch_factset)
            edi_result, edi_error = f_edi.result()
            fs_result,  fs_error  = f_fs.result()

        # Remember the user-supplied ISIN so the FactSet tab can stamp it on rows
        st.session_state["query_isin"] = isin

        # Store EDI
        if edi_result is not None:
            st.session_state["edi_records"]     = edi_result["records"]
            st.session_state["edi_rec_count"]   = edi_result["meta"]["record_count"]
            st.session_state["edi_total_recs"]  = edi_result["meta"]["total_records"]
            st.session_state["edi_rate_remain"] = edi_result["meta"]["rate_remaining"]
            st.session_state["edi_rate_limit"]  = edi_result["meta"]["rate_limit"]
            st.session_state["edi_isin"]        = edi_result["meta"]["isin"]
            st.session_state["edi_debug"]       = {"calls": edi_result["meta"]["calls"]}
            st.session_state.pop("edi_error", None)
        else:
            st.session_state["edi_error"] = edi_error
            for k in ("edi_records", "edi_rec_count", "edi_total_recs", "edi_rate_remain", "edi_rate_limit", "edi_isin", "edi_debug"):
                st.session_state.pop(k, None)

        # Store FactSet
        if fs_result is not None:
            st.session_state["factset_records"] = fs_result["records"]
            st.session_state["factset_meta"]    = fs_result["meta"]
            st.session_state.pop("factset_error", None)
        else:
            st.session_state["factset_error"] = fs_error
            for k in ("factset_records", "factset_meta"):
                st.session_state.pop(k, None)


# ── EDI Tab Renderer ──────────────────────────────────────────────────────────
def render_edi_tab():
    """Renders the full EDI workflow: meta header, debug panel, classified
    tables, raw fields, event detail, export. Returns early on missing data
    instead of calling st.stop() so other top-level tabs keep working."""
    if "edi_error" in st.session_state:
        st.error(f"❌ EDI: {st.session_state['edi_error']}")
        return
    if "edi_records" not in st.session_state:
        st.info("EDI noch nicht geladen — Fetch in der Sidebar starten.")
        return

    records     = st.session_state["edi_records"]
    rec_count   = st.session_state["edi_rec_count"]
    total_recs  = st.session_state["edi_total_recs"]
    rate_remain = st.session_state["edi_rate_remain"]
    rate_limit  = st.session_state["edi_rate_limit"]

    # ── Meta ──────────────────────────────────────────────────────────────────────
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("ISIN", st.session_state.get("edi_isin", isin))
    m2.metric("Records Returned", rec_count)
    m3.metric("Total Records", total_recs)
    m4.metric("Rate Limit", rate_limit)
    m5.metric("Rate Remaining", rate_remain)
    st.divider()

    # ── Debug Panel ───────────────────────────────────────────────────────────────
    if debug_mode and "edi_debug" in st.session_state:
        calls = st.session_state["edi_debug"].get("calls", [])
        title = f"🔧 Debug Info — {len(calls)} API-Call{'s' if len(calls) != 1 else ''}"
        with st.expander(title, expanded=True):
            st.markdown(f"**Records nach Merge & Dedup:** {len(records)}")
            for i, c in enumerate(calls, 1):
                st.markdown(f"---")
                st.markdown(f"### Call {i}: `{c['label']}`  →  Status `{c['status_code']}`")
                st.markdown("**URL:**")
                st.code(c["url"], language="text")
                cols = st.columns(4)
                cols[0].metric("X-Record-Count",       c["record_count"])
                cols[1].metric("X-Total-Records",      c["total_records"])
                cols[2].metric("X-Ratelimit-Limit",    c["rate_limit"])
                cols[3].metric("X-Ratelimit-Remaining", c["rate_remaining"])
                st.markdown("**Response Headers:**")
                st.json(c["headers"])
                if c["body_preview"]:
                    st.markdown("**Response Body (erste 500 Zeichen):**")
                    st.code(c["body_preview"], language="json")
                else:
                    st.info("Response Body ist leer (typisch bei Status 204).")
        st.divider()

    if not records:
        st.warning("No corporate action records found for the given parameters.")
        return

    # ── Process ───────────────────────────────────────────────────────────────────
    deduped   = edi.deduplicate(records)
    processed = edi.merge_events(deduped)
    rows      = edi.build_rows(processed)
    df        = pd.DataFrame(rows)

    if event_type_filter:
        df = df[df["Event_Type"].isin(event_type_filter)]

    if df.empty:
        st.warning("No events match the current filters.")
        return

    # ── Summary ───────────────────────────────────────────────────────────────────
    issuer = df["issuername"].iloc[0] if "issuername" in df.columns else isin
    st.subheader(f"📋 {len(df)} Events — {issuer}")

    type_counts = df["Event_Type"].value_counts()
    cols = st.columns(min(len(type_counts), 7))
    for i, (etype, cnt) in enumerate(type_counts.items()):
        badge_cls = EVENT_TYPE_COLORS.get(etype, "badge-other")
        cols[i % len(cols)].markdown(
            f'<div style="text-align:center">'
            f'<span class="event-badge {badge_cls}">{etype}</span>'
            f'<br><b style="font-size:1.5rem">{cnt}</b></div>',
            unsafe_allow_html=True
        )
    st.divider()


    # ── Tabs ──────────────────────────────────────────────────────────────────────
    tab1, tab2, tab3 = st.tabs(["🏷️ Classified Events", "📄 Raw API Fields", "🔎 Event Detail"])

    with tab1:
        # ── Deleted / Cancelled Warning ───────────────────────────────────────────
        dc_events = df[df["Evt_Status"].isin(["Deleted", "Cancelled"])] if "Evt_Status" in df.columns else pd.DataFrame()
        # Only actionable if ex-date was known AND at least one value field was populated
        if not dc_events.empty:
            has_exdt = dc_events["exdt"].astype(str).str.strip().ne("")
            has_value = (
                dc_events["Dividend_Amount"].astype(str).str.strip().ne("") |
                dc_events["Split_Ratio"].astype(str).str.strip().ne("") |
                dc_events["Sub_Price"].astype(str).str.strip().ne("") |
                dc_events["Sub_Ratio"].astype(str).str.strip().ne("") |
                dc_events["Stock_Div_Ratio"].astype(str).str.strip().ne("") |
                dc_events["ECA_Stock_Ratio"].astype(str).str.strip().ne("") |
                dc_events["ECA_Stock_Terms"].astype(str).str.strip().ne("") |
                dc_events["MA_Cash_Terms"].astype(str).str.strip().ne("")
            )
            dc_events = dc_events[has_exdt & has_value]
        if not dc_events.empty:
            lines = []
            for _, r in dc_events.iterrows():
                lines.append(f"**{r.get('Evt_Status')}** — eventid `{r.get('eventid')}` | "
                             f"{r.get('Event_Type', 'Other')} | ex-date: {r.get('exdt') or '—'}")
            st.error(
                f"⚠️ **{len(dc_events)} event(s) marked as Deleted/Cancelled — "
                f"remove from system if already loaded.**\n\n" + "\n\n".join(lines)
            )

        hide_other = st.toggle("Hide 'Other' events", value=True)
        df_display = df[df["Event_Type"] != "Other"] if hide_other else df
        div_display = [
            "Event_Type", "Subtype", "Evt_Status", "eventcd", "marker", "paytypecd",
            "exdt", "paydt", "recorddt",
            "Dividend_Amount", "Frankdiv", "CFI", "Tax_Marker", "Adjusted_WHT", "Depositary_Fee", "Tax_Relief_Fee", "Dividend_Currency",
            "Stock_Div_Pct", "Stock_Div_Ratio", "Split_Ratio", "Split_Terms",
            "Sub_Price", "Sub_Currency", "Sub_Ratio",
            "Default_Option", "optionelectiondt",
        ]
        ma_display = [
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
        meta_display = [
            "REIT_Flag", "Creation_Date",
            "feedgendate", "evtactioncd", "lstactioncd", "ntsactioncd",
            "eventid", "optionid", "isin", "issuername", "operationalmic",
        ]
        display_cols = [c for c in div_display + ma_display + meta_display if c in df.columns]
        st.dataframe(
            df_display[display_cols],
            use_container_width=True,
            height=500,
            column_config={
                "Event_Type":           st.column_config.TextColumn("Event Type",          width=160),
                "Subtype":              st.column_config.TextColumn("Subtype",              width=210),
                "Evt_Status":           st.column_config.TextColumn("Status",               width=90),
                "exdt":                 st.column_config.DateColumn("Ex-Date"),
                "paydt":                st.column_config.DateColumn("Pay Date"),
                "recorddt":             st.column_config.DateColumn("Record Date"),
                "Dividend_Amount":      st.column_config.NumberColumn("Div Amount",        format="%.4f"),
                "Depositary_Fee":       st.column_config.NumberColumn("Dep. Fee",           format="%.4f"),
                "Adjusted_WHT":         st.column_config.TextColumn("Adjusted WHT",         width=100),
                "Frankdiv":             st.column_config.TextColumn("Frankdiv",             width=90),
                "CFI":                  st.column_config.TextColumn("CFI",                  width=90),
                "Tax_Relief_Fee":       st.column_config.NumberColumn("Tax Relief Fee",     format="%.4f"),
                "Sub_Price":            st.column_config.NumberColumn("Sub Price",          format="%.4f"),
                "Split_Terms":          st.column_config.TextColumn("Split Terms",           width=100),
                "MA_Cash_Terms":          st.column_config.NumberColumn("Cash Terms",          format="%.4f"),
                "MA_Cash_Terms_Currency": st.column_config.TextColumn("Cash Terms Currency",  width=120),
                "Default_Option":       st.column_config.TextColumn("Default Option",       width=110),
                "optionelectiondt":     st.column_config.TextColumn("Election DL",          width=120),
                "MA_Offeror":           st.column_config.TextColumn("Offeror",              width=190),
                "MA_Hostile":           st.column_config.TextColumn("Hostile",              width=70),
                "MA_Mand_Vol":          st.column_config.TextColumn("M/V",                 width=50),
                "MA_Event_Subtype":     st.column_config.TextColumn("Deal Subtype",         width=120),
                "Deal_Type":            st.column_config.TextColumn("Deal Type",             width=120),
                "ECA_Stock_Ratio":       st.column_config.TextColumn("Stock Terms",            width=120),
                "ECA_Stock_Terms":       st.column_config.TextColumn("Stock Ratio",            width=110),
                "MA_Offeror_ISIN":      st.column_config.TextColumn("Counterparty ISIN",    width=140),
                "MA_Offeror_Ticker":    st.column_config.TextColumn("Counterparty Ticker",  width=130),
                "MA_Effective_Date":    st.column_config.TextColumn("Effective Date",        width=120),
                "MA_Exp_Completion":    st.column_config.TextColumn("Exp. Completion",      width=125),
                "MA_Merger_Status":     st.column_config.TextColumn("Merger Status",        width=100),
                "MA_Close_Date":        st.column_config.TextColumn("Offer Expiry / Close Date", width=150),
                "ECA_Status":           st.column_config.TextColumn("ECA Status",           width=100),
                "New_Name":             st.column_config.TextColumn("New Name",             width=200),
                "Old_Name":             st.column_config.TextColumn("Old Name",             width=200),
                "ID_Change_Date":       st.column_config.TextColumn("Change Date",          width=120),
                "New_Local_Code":       st.column_config.TextColumn("New Ticker",           width=100),
                "Old_Local_Code":       st.column_config.TextColumn("Old Ticker",           width=100),
                "New_Exchg":            st.column_config.TextColumn("New Exchange",         width=100),
                "Old_Exchg":            st.column_config.TextColumn("Old Exchange",         width=100),
                "New_Country":          st.column_config.TextColumn("New Country",          width=90),
                "Old_Country":          st.column_config.TextColumn("Old Country",          width=90),
                "New_ISIN":             st.column_config.TextColumn("New ISIN",             width=130),
                "Old_ISIN":             st.column_config.TextColumn("Old ISIN",             width=130),
                "New_Currency":         st.column_config.TextColumn("New Currency",         width=90),
                "Old_Currency":         st.column_config.TextColumn("Old Currency",         width=90),
                "New_Trading_CCY":      st.column_config.TextColumn("New Trading CCY",      width=110),
                "Old_Trading_CCY":      st.column_config.TextColumn("Old Trading CCY",      width=110),
                "REIT_Flag":            st.column_config.CheckboxColumn("REIT",                 width=70),
                "Creation_Date":        st.column_config.TextColumn("Creation Date",        width=130),
                "feedgendate":          st.column_config.TextColumn("Feed Gen Date",         width=130),
                "evtactioncd":          st.column_config.TextColumn("Evt Action",            width=80),
                "lstactioncd":          st.column_config.TextColumn("LST Action",            width=80),
                "ntsactioncd":          st.column_config.TextColumn("NTS Action",            width=80),
                "optionid":             st.column_config.TextColumn("Option ID",             width=75),
            }
        )

        # ── Deleted / Cancelled Expander ─────────────────────────────────────────
        if not dc_events.empty:
            with st.expander(f"⚠️ Deleted / Cancelled Events ({len(dc_events)})", expanded=False):
                dc_cols = [c for c in ["Evt_Status", "Event_Type", "Subtype", "eventcd",
                                        "exdt", "paydt", "eventid", "Creation_Date",
                                        "feedgendate", "evtactioncd"] if c in dc_events.columns]
                st.dataframe(dc_events[dc_cols], use_container_width=True,
                             column_config={
                                 "Evt_Status": st.column_config.TextColumn("Status",     width=90),
                                 "Event_Type": st.column_config.TextColumn("Event Type", width=160),
                                 "exdt":       st.column_config.DateColumn("Ex-Date"),
                                 "paydt":      st.column_config.DateColumn("Pay Date"),
                                 "evtactioncd":st.column_config.TextColumn("Raw Code",   width=80),
                             })

    with tab2:
        raw_cols = [c for c in edi.RAW_COLUMNS if c in df.columns]
        st.dataframe(df[raw_cols], use_container_width=True, height=500)

    with tab3:
        if len(df) > 0:
            def _event_label(row):
                date_hint = (row["exdt"] or
                             str(row.get("Creation_Date", ""))[:10] or
                             str(row.get("feedgendate", ""))[:10])
                subtype   = row.get("Subtype", "")
                deal_type = row.get("Deal_Type", "")
                type_hint = " · ".join(filter(None, [subtype, deal_type])) or "—"
                return (f"{row['eventid']} | {row['Event_Type']} — "
                        f"{type_hint} | {row.get('issuername','')} | {date_hint}")

            event_options = [_event_label(row) for _, row in df.iterrows()]
            selected = st.selectbox("Select Event", event_options, key="tab3_select")
            idx = event_options.index(selected)
            sel = df.iloc[idx].to_dict()

            c1, c2 = st.columns(2)
            with c1:
                # ── Classification ─────────────────────────────────────────────
                st.markdown("**🏷️ Classification**")
                evt = str(sel.get("Event_Type", ""))
                is_deal = evt in ("Merger & Acquisition", "Spin-Off", "Stock Distribution")
                is_id_change = evt == "ID Change"

                if is_deal:
                    detail = {
                        "Event_Type":          sel.get("Event_Type"),
                        "Subtype":             sel.get("Subtype"),
                        "Deal_Type":           sel.get("Deal_Type"),
                        "REIT_Flag":           sel.get("REIT_Flag"),
                        "Mandatory_Voluntary": sel.get("MA_Mand_Vol"),
                    }
                    if evt == "Merger & Acquisition":
                        detail.update({
                            "Offeror":           sel.get("MA_Offeror"),
                            "Hostile":           sel.get("MA_Hostile"),
                            "Deal_Subtype_Code": sel.get("MA_Event_Subtype"),
                        })
                    detail.update({
                        "Counterparty_Ticker": sel.get("MA_Offeror_Ticker"),
                        "Counterparty_ISIN":   sel.get("MA_Offeror_ISIN"),
                        "Stock_Terms":         sel.get("ECA_Stock_Ratio"),
                        "Stock_Ratio":         sel.get("ECA_Stock_Terms"),
                        "Cash_Terms":          sel.get("MA_Cash_Terms"),
                        "Cash_Terms_Currency": sel.get("MA_Cash_Terms_Currency"),
                        "Effective_Date":      sel.get("MA_Effective_Date"),
                        "Exp_Completion":      sel.get("MA_Exp_Completion"),
                        "Merger_Status":       sel.get("MA_Merger_Status"),
                        "Election_Deadline":   sel.get("optionelectiondt"),
                        "Unconditional_Date":  sel.get("unconditionaldt"),
                        "Compulsory_Acq_Date": sel.get("compulsoryacqdt"),
                        "Offer_Expiry_Close_Date": sel.get("MA_Close_Date"),
                        "ECA_Status":          sel.get("ECA_Status"),
                        "New_Name":            sel.get("New_Name"),
                        "Old_Name":            sel.get("Old_Name"),
                        "ID_Change_Date":      sel.get("ID_Change_Date"),
                        "New_Local_Code":      sel.get("New_Local_Code"),
                        "Old_Local_Code":      sel.get("Old_Local_Code"),
                        "New_Exchg":           sel.get("New_Exchg"),
                        "Old_Exchg":           sel.get("Old_Exchg"),
                        "New_Country":         sel.get("New_Country"),
                        "Old_Country":         sel.get("Old_Country"),
                        "New_ISIN":            sel.get("New_ISIN"),
                        "Old_ISIN":            sel.get("Old_ISIN"),
                        "New_Currency":        sel.get("New_Currency"),
                        "Old_Currency":        sel.get("Old_Currency"),
                        "New_Trading_CCY":     sel.get("New_Trading_CCY"),
                        "Old_Trading_CCY":     sel.get("Old_Trading_CCY"),
                    })
                    st.json({k: v for k, v in detail.items() if v not in (None, "")})
                elif is_id_change:
                    st.json({k: v for k, v in {
                        "Event_Type":      sel.get("Event_Type"),
                        "Subtype":         sel.get("Subtype"),
                        "REIT_Flag":       sel.get("REIT_Flag"),
                        "ID_Change_Date":  sel.get("ID_Change_Date"),
                        "New_Name":        sel.get("New_Name"),
                        "Old_Name":        sel.get("Old_Name"),
                        "New_ISIN":        sel.get("New_ISIN"),
                        "Old_ISIN":        sel.get("Old_ISIN"),
                        "New_Local_Code":  sel.get("New_Local_Code"),
                        "Old_Local_Code":  sel.get("Old_Local_Code"),
                        "New_Exchg":       sel.get("New_Exchg"),
                        "Old_Exchg":       sel.get("Old_Exchg"),
                        "New_Country":     sel.get("New_Country"),
                        "Old_Country":     sel.get("Old_Country"),
                        "New_Currency":    sel.get("New_Currency"),
                        "Old_Currency":    sel.get("Old_Currency"),
                        "New_Trading_CCY": sel.get("New_Trading_CCY"),
                        "Old_Trading_CCY": sel.get("Old_Trading_CCY"),
                    }.items() if v not in (None, "")})
                else:
                    st.json({k: v for k, v in {
                        "Event_Type":        sel.get("Event_Type"),
                        "Subtype":           sel.get("Subtype"),
                        "REIT_Flag":         sel.get("REIT_Flag"),
                        "Dividend_Amount":   sel.get("Dividend_Amount"),
                        "Tax_Marker":        sel.get("Tax_Marker"),
                        "Adjusted_WHT":      sel.get("Adjusted_WHT"),
                        "Frankdiv":          sel.get("Frankdiv"),
                        "CFI":               sel.get("CFI"),
                        "Depositary_Fee":    sel.get("Depositary_Fee"),
                        "Tax_Relief_Fee":    sel.get("Tax_Relief_Fee"),
                        "Dividend_Currency": sel.get("Dividend_Currency"),
                        "Stock_Div_Pct":     sel.get("Stock_Div_Pct"),
                        "Stock_Div_Ratio":   sel.get("Stock_Div_Ratio"),
                        "Split_Ratio":       sel.get("Split_Ratio"),
                        "Split_Terms":       sel.get("Split_Terms"),
                        "Sub_Price":         sel.get("Sub_Price"),
                        "Sub_Currency":      sel.get("Sub_Currency"),
                        "Sub_Ratio":         sel.get("Sub_Ratio"),
                        "Default_Option":    sel.get("Default_Option"),
                        "Election_Deadline": sel.get("optionelectiondt"),
                    }.items() if v not in (None, "")})

                # ── Lifecycle ──────────────────────────────────────────────────
                st.markdown("**⏱️ Lifecycle**")
                st.json({k: v for k, v in {
                    "Ex_Date":       sel.get("exdt"),
                    "Pay_Date":      sel.get("paydt"),
                    "Record_Date":   sel.get("recorddt"),
                    "Creation_Date": sel.get("Creation_Date"),
                    "Feed_Gen_Date": sel.get("feedgendate"),
                    "Evt_Action":    sel.get("evtactioncd"),
                    "LST_Action":    sel.get("lstactioncd"),
                    "NTS_Action":    sel.get("ntsactioncd"),
                }.items() if v not in (None, "")})

            with c2:
                st.markdown("**📄 Raw Fields**")
                st.json({col: sel.get(col, "") for col in edi.RAW_COLUMNS})
                st.markdown("**🔧 Derived Fields**")
                derived_cols = ["Event_Type", "Subtype", "Deal_Type",
                                "Dividend_Amount", "Frankdiv", "CFI", "Tax_Marker", "Adjusted_WHT", "Depositary_Fee", "Tax_Relief_Fee", "Dividend_Currency",
                                "Stock_Div_Pct", "Stock_Div_Ratio", "Split_Ratio", "Split_Terms",
                                "Sub_Price", "Sub_Currency", "Sub_Ratio", "Default_Option",
                                "MA_Offeror", "MA_Hostile", "MA_Mand_Vol", "MA_Event_Subtype",
                                "MA_Cash_Terms", "MA_Cash_Terms_Currency",
                                "ECA_Stock_Ratio", "ECA_Stock_Terms",
                                "MA_Offeror_ISIN", "MA_Offeror_Ticker",
                                "MA_Effective_Date", "MA_Exp_Completion",
                                "MA_Merger_Status", "MA_Close_Date",
                                "New_Name", "Old_Name", "ID_Change_Date",
                                "New_Local_Code", "Old_Local_Code",
                                "New_Exchg", "Old_Exchg",
                                "New_Country", "Old_Country",
                                "New_ISIN", "Old_ISIN",
                                "New_Currency", "Old_Currency",
                                "New_Trading_CCY", "Old_Trading_CCY",
                                "REIT_Flag", "Creation_Date"]
                st.json({col: sel.get(col, "") for col in derived_cols})

    # ── Export ────────────────────────────────────────────────────────────────────
    st.divider()
    col_dl1, col_dl2, _ = st.columns([1, 1, 4])
    with col_dl1:
        csv = df.drop(columns=["_ignored"], errors="ignore").to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Download CSV", csv, f"edi_ca_{isin}.csv", "text/csv")
    with col_dl2:
        json_out = df.drop(columns=["_ignored"], errors="ignore").to_json(orient="records", indent=2)
        st.download_button("⬇️ Download JSON", json_out, f"edi_ca_{isin}.json", "application/json")


# ── FactSet Tab Renderer ──────────────────────────────────────────────────────
def render_factset_tab():
    if "factset_error" in st.session_state:
        st.error(f"❌ FactSet: {st.session_state['factset_error']}")
        return
    if "factset_records" not in st.session_state:
        st.info("FactSet noch nicht geladen — Fetch in der Sidebar starten.")
        return

    raw_records = st.session_state["factset_records"]
    meta        = st.session_state.get("factset_meta", {})
    query_isin  = st.session_state.get("query_isin", "")

    # ── Header metrics (mirrors EDI tab) ──────────────────────────────────────
    cols = st.columns(4)
    cols[0].metric("Ticker",          meta.get("isin", "–"))
    cols[1].metric("Records Returned", meta.get("record_count", "–"))
    cols[2].metric("Rate Limit",       meta.get("rate_limit", "–"))
    cols[3].metric("Rate Remaining",   meta.get("rate_remaining", "–"))
    st.divider()

    if not raw_records:
        st.warning("No FactSet corporate action records found.")
        return

    # ── Pipeline ──────────────────────────────────────────────────────────────
    normalized = factset.normalize_dates(raw_records)
    deduped    = factset.deduplicate(normalized)
    processed  = factset.merge_events(deduped)
    rows       = factset.build_rows(processed, isin=query_isin)
    df         = pd.DataFrame(rows)

    # Apply event-type filter (shared with EDI tab via the sidebar)
    if event_type_filter:
        df = df[df["Event_Type"].isin(event_type_filter)]

    if df.empty:
        st.warning("No FactSet events match the current filters.")
        return

    # ── Summary badges (event-type counts) ────────────────────────────────────
    st.subheader(f"📋 {len(df)} FactSet Events")
    type_counts = df["Event_Type"].value_counts()
    badge_cols = st.columns(min(len(type_counts), 7))
    for i, (etype, cnt) in enumerate(type_counts.items()):
        badge_cls = EVENT_TYPE_COLORS.get(etype, "badge-other")
        badge_cols[i % len(badge_cols)].markdown(
            f'<div style="text-align:center">'
            f'<span class="event-badge {badge_cls}">{etype}</span>'
            f'<br><b style="font-size:1.5rem">{cnt}</b></div>',
            unsafe_allow_html=True
        )
    st.divider()

    # ── Classified events table ───────────────────────────────────────────────
    hide_other = st.toggle("Hide 'Other' events", value=True, key="fs_hide_other")
    df_display = df[df["Event_Type"] != "Other"] if hide_other else df

    display_cols = [
        "Event_Type", "Subtype", "Evt_Status", "eventcd",
        "exdt", "paydt", "recorddt",
        "Dividend_Amount", "Tax_Marker", "Dividend_Currency",
        "Stock_Div_Pct", "Stock_Div_Ratio",
        "Split_Ratio", "Split_Terms",
        "Sub_Price", "Sub_Currency", "Sub_Ratio",
        "eventid", "isin", "fsymId",
    ]
    display_cols = [c for c in display_cols if c in df_display.columns]

    st.dataframe(
        df_display[display_cols],
        use_container_width=True,
        height=400,
        column_config={
            "Event_Type":        st.column_config.TextColumn("Event Type", width=160),
            "Subtype":           st.column_config.TextColumn("Subtype", width=120),
            "Evt_Status":        st.column_config.TextColumn("Status", width=90),
            "exdt":              st.column_config.DateColumn("Ex-Date"),
            "paydt":              st.column_config.DateColumn("Pay Date"),
            "recorddt":          st.column_config.DateColumn("Record Date"),
            "Dividend_Amount":   st.column_config.NumberColumn("Div Amount", format="%.4f"),
            "Tax_Marker":        st.column_config.TextColumn("Tax", width=70),
            "Dividend_Currency": st.column_config.TextColumn("Ccy", width=60),
            "Sub_Price":         st.column_config.NumberColumn("Sub Price", format="%.4f"),
            "fsymId":            st.column_config.TextColumn("FactSet fsymId", width=120),
        },
    )

    # ── Raw JSON expander (for inspection) ────────────────────────────────────
    with st.expander("📄 Raw FactSet Records (JSON)", expanded=False):
        st.json(raw_records, expanded=False)


# ── Validation Tab Renderer ──────────────────────────────────────────────────
def render_validation_tab():
    st.info("✅ Validation Tab — vergleicht CAs vom selben Typ am selben Ex-Tag "
            "zwischen EDI und FactSet. Wird gebaut sobald FactSet Daten liefert.")


# ── Top-Level Tabs ────────────────────────────────────────────────────────────
tab_edi, tab_fs, tab_val = st.tabs(["📊 EDI", "📊 FactSet", "✅ Validation"])

with tab_edi:
    render_edi_tab()

with tab_fs:
    render_factset_tab()

with tab_val:
    render_validation_tab()
