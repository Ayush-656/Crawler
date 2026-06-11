#!/usr/bin/env python3
"""
Ads.txt / App-Ads.txt Crawler v3
  Sidebar  – choose between ads.txt or app-ads.txt before crawling
  Tab 1    – Line Checker   : pivot table  (domains = rows, lines = columns, Yes / No / Error)
  Tab 2    – Network Finder : find every seller ID for a given network / relation per domain
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
import requests
import streamlit as st

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Ads.txt / App-Ads.txt Crawler",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ───────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
[data-testid="stSidebar"] {
    background: linear-gradient(180deg,#0f0c29,#302b63,#24243e);
}
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] span,
[data-testid="stSidebar"] div { color:#dde1ff !important; }
[data-testid="stSidebar"] .stButton > button {
    background:linear-gradient(135deg,#667eea,#764ba2)!important;
    color:white!important; border:none!important;
    border-radius:10px!important; font-weight:700!important;
}
[data-testid="stSidebar"] hr { border-color:#444!important; }

/* file-type badge in header */
.badge-appads {
    display:inline-block;
    background:#667eea; color:white;
    padding:3px 12px; border-radius:99px;
    font-size:1rem; font-weight:700;
    vertical-align:middle; margin-left:10px;
}
.badge-ads {
    display:inline-block;
    background:#f59e0b; color:white;
    padding:3px 12px; border-radius:99px;
    font-size:1rem; font-weight:700;
    vertical-align:middle; margin-left:10px;
}

/* hero banner */
.hero {
    background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
    padding:1.5rem 2rem; border-radius:14px; color:white;
    margin-bottom:1.4rem;
    box-shadow:0 4px 20px rgba(102,126,234,.35);
}
.hero h1 { margin:0; font-size:2rem; }
.hero p  { margin:.4rem 0 0; opacity:.9; font-size:1rem; }

/* file-type selector card */
.ft-card {
    background: white;
    border: 2px solid #e5e7eb;
    border-radius: 14px;
    padding: 1.2rem 1.4rem;
    margin-bottom: 0.5rem;
    box-shadow: 0 2px 8px rgba(0,0,0,.06);
}
.ft-card.selected-appads { border-color: #667eea; background: #f0f2ff; }
.ft-card.selected-ads    { border-color: #f59e0b; background: #fffbeb; }

/* metric tiles */
.metric-row { display:flex; gap:1rem; margin:1.2rem 0; flex-wrap:wrap; }
.tile {
    flex:1; min-width:120px; background:white;
    border-radius:12px; padding:1rem 1.2rem;
    box-shadow:0 2px 10px rgba(0,0,0,.07);
    border-top:4px solid #667eea;
}
.tile.green  { border-top-color:#22c55e; }
.tile.red    { border-top-color:#ef4444; }
.tile.amber  { border-top-color:#f59e0b; }
.tile-label  { font-size:.78rem; color:#6b7280; font-weight:600; letter-spacing:.04em; }
.tile-value  { font-size:2rem; font-weight:800; color:#111; }
</style>
""", unsafe_allow_html=True)


# ── Core logic ────────────────────────────────────────────────────────────────

def normalize_domain(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
    return raw.split("/")[0].split("?")[0].split("#")[0].lower().strip()


def parse_ads_line(raw: str) -> Optional[Dict]:
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
    raw = raw.split("#")[0].strip()
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < 3:
        return None
    return {
        "domain":    parts[0].lower(),
        "seller_id": parts[1],
        "relation":  parts[2].upper(),
        "tag":       parts[3].lower() if len(parts) > 3 else "",
    }


def lines_match(file_entry: Dict, query: Dict) -> bool:
    if file_entry["domain"]            != query["domain"]:            return False
    if file_entry["seller_id"].lower() != query["seller_id"].lower(): return False
    if file_entry["relation"]          != query["relation"]:          return False
    if query["tag"] and file_entry["tag"].lower() != query["tag"]:    return False
    return True


def fetch_file(domain: str, file_type: str, timeout: int = 12) -> Dict:
    """
    Fetch ads.txt or app-ads.txt for *domain*.
    file_type must be either 'ads.txt' or 'app-ads.txt'.
    Tries HTTPS then HTTP.
    """
    headers = {"User-Agent": "AdsTxtCrawler/3.0"}
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}/{file_type}"
        try:
            r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            if r.status_code == 200:
                return {"ok": True,  "url": url, "text": r.text, "error": ""}
            if r.status_code == 404:
                return {"ok": False, "url": url, "text": "", "error": "404 Not Found"}
        except Exception:
            pass
    return {
        "ok":    False,
        "url":   f"https://{domain}/{file_type}",
        "text":  "",
        "error": "Connection failed / timeout",
    }


def parse_domain_list(text: str) -> List[str]:
    seen, result = set(), []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        d = normalize_domain(line)
        if d and d not in seen:
            seen.add(d)
            result.append(d)
    return result


def parse_query_list(text: str) -> List[Dict]:
    result, seen = [], set()
    for line in text.splitlines():
        q = parse_ads_line(line)
        if q:
            key = make_query_key(q)
            if key not in seen:
                seen.add(key)
                result.append(q)
    return result


def make_query_key(q: Dict) -> str:
    parts = [q["domain"], q["seller_id"], q["relation"]]
    if q["tag"]:
        parts.append(q["tag"])
    return ", ".join(parts)


# ── Crawl workers ─────────────────────────────────────────────────────────────

def crawl_lines(domain: str, queries: List[Dict], timeout: int, file_type: str) -> List[Dict]:
    """Tab 1: one result row per (domain, query)."""
    fetch = fetch_file(domain, file_type, timeout)
    rows  = []
    if fetch["ok"]:
        entries = [e for ln in fetch["text"].splitlines()
                   if (e := parse_ads_line(ln)) is not None]
        for q in queries:
            match = next((e for e in entries if lines_match(e, q)), None)
            rows.append({"Domain": domain, "Line": make_query_key(q),
                         "Found": "Yes" if match else "No",
                         "_ok": True, "_err": ""})
    else:
        for q in queries:
            rows.append({"Domain": domain, "Line": make_query_key(q),
                         "Found": "Error", "_ok": False, "_err": fetch["error"]})
    return rows


def crawl_network(domain: str, network: str, relation: str,
                  timeout: int, file_type: str) -> Dict:
    """Tab 2: find all seller IDs for a network+relation in one domain."""
    fetch = fetch_file(domain, file_type, timeout)
    if not fetch["ok"]:
        return {"Domain": domain, "Seller IDs": "", "Count": 0,
                "Found": "Error", "_ok": False, "_err": fetch["error"]}
    entries = [e for ln in fetch["text"].splitlines()
               if (e := parse_ads_line(ln)) is not None]
    matching = [e for e in entries
                if e["domain"] == network.lower()
                and (not relation or e["relation"] == relation.upper())]
    ids = [e["seller_id"] for e in matching]
    return {"Domain": domain, "Seller IDs": ", ".join(ids),
            "Count": len(ids), "Found": "Yes" if ids else "No",
            "_ok": True, "_err": ""}


def run_parallel(domain_list: List[str], task_fn, workers: int):
    """Parallel crawl with live progress bar. task_fn(domain) → list|dict."""
    all_results, completed, total = [], 0, len(domain_list)
    prog = st.progress(0.0, text=f"Starting — 0 / {total} domains")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fmap = {pool.submit(task_fn, d): d for d in domain_list}
        for fut in as_completed(fmap):
            dom = fmap[fut]
            try:
                res = fut.result()
                (all_results.extend if isinstance(res, list) else all_results.append)(res)
            except Exception:
                pass
            completed += 1
            prog.progress(
                completed / total,
                text=f"🔄 {completed} / {total} — {dom}  "
                     f"({round(time.time() - t0, 1)}s elapsed)"
            )
    prog.progress(1.0, text=f"✅ Done in {round(time.time() - t0, 1)}s")
    return all_results


# ── Style helpers ─────────────────────────────────────────────────────────────

def _cell_style(val):
    if val == "Yes":   return "background-color:#dcfce7;color:#166534;font-weight:700"
    if val == "No":    return "background-color:#fee2e2;color:#991b1b;font-weight:700"
    if val == "Error": return "background-color:#fef3c7;color:#92400e;font-weight:700"
    return ""


def apply_style(df: pd.DataFrame, subset=None):
    try:
        return df.style.map(_cell_style, subset=subset)
    except AttributeError:
        return df.style.applymap(_cell_style, subset=subset)


def metric_html(tiles: list) -> str:
    parts = [
        f'<div class="tile {cls}">'
        f'<div class="tile-label">{lbl}</div>'
        f'<div class="tile-value">{val}</div></div>'
        for lbl, val, cls in tiles
    ]
    return f'<div class="metric-row">{"".join(parts)}</div>'


# ── Session state init ────────────────────────────────────────────────────────

for _k in ("std_raw", "std_keys", "finder_df", "finder_meta", "last_file_type"):
    if _k not in st.session_state:
        st.session_state[_k] = None


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("## ⚙️ Configuration")

    # ── 1. File type selector ─────────────────────────────────────────────────
    st.markdown("### 📄 File type to crawl")

    file_type = st.radio(
        "File type",
        options=["app-ads.txt", "ads.txt"],
        index=0,
        key="file_type_sel",
        horizontal=True,
        label_visibility="collapsed",
        help=(
            "app-ads.txt — used by mobile apps (IAB standard)\n"
            "ads.txt     — used by websites"
        ),
    )

    # Visual confirmation card
    if file_type == "app-ads.txt":
        st.markdown(
            '<div class="ft-card selected-appads">'
            '🟣 <b>app-ads.txt</b> selected<br>'
            '<span style="font-size:.85rem;color:#555;">'
            'Crawling <code>https://domain/<b>app-ads.txt</b></code></span>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="ft-card selected-ads">'
            '🟡 <b>ads.txt</b> selected<br>'
            '<span style="font-size:.85rem;color:#555;">'
            'Crawling <code>https://domain/<b>ads.txt</b></code></span>'
            '</div>',
            unsafe_allow_html=True,
        )

    # Warn if file type changed since last run
    if (st.session_state.last_file_type is not None
            and st.session_state.last_file_type != file_type
            and (st.session_state.std_raw or st.session_state.finder_df is not None)):
        st.warning(
            f"⚠️ File type changed to **{file_type}**.  "
            "Previous results are from **"
            f"{st.session_state.last_file_type}** — re-run to refresh.",
            icon="⚠️",
        )

    st.divider()

    # ── 2. Domains ────────────────────────────────────────────────────────────
    st.markdown("### 🌐 Domains to crawl")

    dm = st.radio("Domain input", ["📁 Upload .txt", "✏️ Paste text"],
                  key="dm", horizontal=True, label_visibility="collapsed")
    domains_raw = ""
    if dm == "📁 Upload .txt":
        df_upload = st.file_uploader(
            "domains.txt  —  one domain per line", type="txt", key="d_up"
        )
        if df_upload:
            domains_raw = df_upload.read().decode("utf-8", errors="replace")
    else:
        domains_raw = st.text_area(
            "Domains", key="d_txt", height=160, label_visibility="collapsed",
            placeholder="apps.mxplayer.in\nbattleprime.com\nbeautyplus.com\n…",
        )

    domain_list = parse_domain_list(domains_raw) if domains_raw.strip() else []
    if domain_list:
        st.success(f"✔ {len(domain_list)} domain(s) ready")

    st.divider()

    # ── 3. Settings ───────────────────────────────────────────────────────────
    st.markdown("### ⚡ Settings")
    workers = st.slider("Parallel workers", 1, 30, 8,
                        help="Domains crawled simultaneously")
    timeout = st.slider("Timeout per domain (s)", 5, 30, 10)


# ══════════════════════════════════════════════════════════════════════════════
# HEADER (dynamic — reflects chosen file type)
# ══════════════════════════════════════════════════════════════════════════════

badge_cls  = "badge-appads" if file_type == "app-ads.txt" else "badge-ads"
badge_html = f'<span class="{badge_cls}">{file_type}</span>'

st.markdown(
    f"""
<div class="hero">
  <h1>🔍 Ads.txt Crawler {badge_html}</h1>
  <p>
    Crawling <b>{file_type}</b> across your domains.<br>
    <b>Line Checker</b>: pivot table — domains as rows, checked entries as columns, Yes / No per cell.<br>
    <b>Network Finder</b>: find every Seller ID for an ad network and relation across all domains.
  </p>
</div>
""",
    unsafe_allow_html=True,
)


# ══════════════════════════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════════════════════════

tab1, tab2 = st.tabs([f"📋  Line Checker", "🔎  Network Finder"])


# ─────────────────────────────────────────────────────────────────────────────
# TAB 1 — Line Checker
# ─────────────────────────────────────────────────────────────────────────────

with tab1:

    st.markdown(f"#### Lines to check  *(against `{file_type}`)*")
    st.caption("Format per line:  `domain, seller_id, relation`  or  "
               "`domain, seller_id, relation, tag`")

    lm = st.radio("Lines input", ["📁 Upload .txt", "✏️ Paste text"],
                  key="lm", horizontal=True)
    lines_raw = ""
    if lm == "📁 Upload .txt":
        lf_upload = st.file_uploader(
            "lines.txt", type="txt", key="l_up",
            help="One entry per line: domain, seller_id, relation[, tag]",
        )
        if lf_upload:
            lines_raw = lf_upload.read().decode("utf-8", errors="replace")
    else:
        lines_raw = st.text_area(
            "Lines", key="l_txt", height=160,
            placeholder=(
                "google.com, pub-6968738577620513, RESELLER, f08c47fec0942fa0\n"
                "smartadserver.com, 5427, RESELLER, 060d053dcf45cbf3\n"
                "video.unrulymedia.com, 906189653, RESELLER"
            ),
        )

    query_list = parse_query_list(lines_raw) if lines_raw.strip() else []
    if query_list:
        st.caption(f"✔ {len(query_list)} line(s) loaded  (duplicates removed)")

    if st.button(f"🚀 Run Line Check  [{file_type}]", type="primary", key="run_std"):
        if not domain_list:
            st.error("⚠️ Add domains in the sidebar first.")
        elif not query_list:
            st.error("⚠️ Provide at least one line to check.")
        else:
            _ql = query_list[:]
            _to = timeout
            _ft = file_type
            raw = run_parallel(
                domain_list,
                lambda d: crawl_lines(d, _ql, _to, _ft),
                workers,
            )
            st.session_state.std_raw       = raw
            st.session_state.std_keys      = [make_query_key(q) for q in _ql]
            st.session_state.last_file_type = file_type

    # ── Results ───────────────────────────────────────────────────────────────
    if st.session_state.std_raw:

        # Show stale-data warning if file type changed
        lft = st.session_state.last_file_type
        if lft and lft != file_type:
            st.warning(
                f"These results are from **{lft}**. "
                f"You have now selected **{file_type}** — click Run to refresh.",
                icon="⚠️",
            )
        else:
            st.info(f"Results for **{lft}**", icon="📄")

        raw_rows     = st.session_state.std_raw
        ordered_keys = st.session_state.std_keys or []

        df_long = pd.DataFrame(raw_rows)
        yes_n   = (df_long["Found"] == "Yes").sum()
        no_n    = (df_long["Found"] == "No").sum()
        err_n   = (df_long["Found"] == "Error").sum()
        total_n = len(df_long)
        pct     = round(yes_n / total_n * 100, 1) if total_n else 0

        st.markdown(metric_html([
            ("TOTAL CHECKS", f"{total_n:,}",  ""),
            ("YES",          f"{yes_n:,}",     "green"),
            ("NO",           f"{no_n:,}",      "red"),
            ("FETCH ERRORS", f"{err_n:,}",     "amber"),
            ("FOUND RATE",   f"{pct}%",         ""),
        ]), unsafe_allow_html=True)

        # Build pivot
        try:
            pivot = df_long.pivot_table(
                index="Domain", columns="Line", values="Found", aggfunc="first"
            )
            ordered_cols = [k for k in ordered_keys if k in pivot.columns]
            pivot = pivot[ordered_cols]
            pivot.index.name   = None
            pivot.columns.name = None

            disp = pivot.copy()
            disp.insert(0, "Crawled Domain", pivot.index)
            disp = disp.reset_index(drop=True)

            val_cols = [c for c in disp.columns if c != "Crawled Domain"]
            styled   = apply_style(disp, subset=val_cols)

            st.divider()
            st.subheader(f"📊 Results  —  Domains × Lines  [{lft}]")
            st.dataframe(styled, use_container_width=True, hide_index=True)
            st.caption(
                "Rows = crawled domains  ·  Columns = lines checked  ·  "
                "Values = **Yes** / **No** / **Error**"
            )

        except Exception as exc:
            st.warning(f"Pivot failed ({exc}); showing flat view.")
            disp = df_long[["Domain", "Line", "Found"]].rename(
                columns={"Domain": "Crawled Domain"}
            )
            st.dataframe(disp, use_container_width=True, hide_index=True)

        # Fetch-error detail
        err_rows = df_long[df_long["Found"] == "Error"][["Domain", "_err"]].drop_duplicates()
        if not err_rows.empty:
            with st.expander(f"⚠️ {len(err_rows)} fetch error(s)"):
                st.dataframe(
                    err_rows.rename(columns={"_err": "Error"}),
                    use_container_width=True, hide_index=True,
                )

        # Export
        st.divider()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = (lft or "unknown").replace(".", "_")
        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                "⬇️ Export pivot table (CSV)",
                data=disp.to_csv(index=False).encode(),
                file_name=f"line_checker_{slug}_pivot_{ts}.csv",
                mime="text/csv", use_container_width=True,
            )
        with c2:
            flat = df_long[["Domain", "Line", "Found"]].rename(
                columns={"Domain": "Crawled Domain"}
            )
            st.download_button(
                "⬇️ Export flat list (CSV)",
                data=flat.to_csv(index=False).encode(),
                file_name=f"line_checker_{slug}_flat_{ts}.csv",
                mime="text/csv", use_container_width=True,
            )


# ─────────────────────────────────────────────────────────────────────────────
# TAB 2 — Network Finder
# ─────────────────────────────────────────────────────────────────────────────

with tab2:

    st.markdown(f"#### Find all entries for a specific ad network  *(in `{file_type}`)*")
    st.caption(
        "The crawler fetches each domain's file and lists **every Seller ID** "
        "that matches your ad-network and relation. "
        "Multiple IDs in one cell, comma-separated."
    )

    nc1, nc2 = st.columns([2, 1])
    with nc1:
        network_input = st.text_input(
            "Ad Network domain", value="xapads.com",
            placeholder="xapads.com", key="net_in",
        )
    with nc2:
        relation_sel = st.selectbox(
            "Relation", ["DIRECT", "RESELLER", "Any"], key="rel_sel"
        )

    if st.button(f"🔎 Find Entries  [{file_type}]", type="primary", key="run_finder"):
        if not domain_list:
            st.error("⚠️ Add domains in the sidebar first.")
        elif not network_input.strip():
            st.error("⚠️ Enter an ad-network domain.")
        else:
            _net = normalize_domain(network_input)
            _rel = "" if relation_sel == "Any" else relation_sel
            _to  = timeout
            _ft  = file_type
            rows = run_parallel(
                domain_list,
                lambda d: crawl_network(d, _net, _rel, _to, _ft),
                workers,
            )
            st.session_state.finder_df   = pd.DataFrame(rows)
            st.session_state.finder_meta = (_net, relation_sel, file_type)
            st.session_state.last_file_type = file_type

    # ── Results ───────────────────────────────────────────────────────────────
    if st.session_state.finder_df is not None:
        df_f = st.session_state.finder_df
        net_lbl, rel_lbl, ft_lbl = (st.session_state.finder_meta or ("", "", ""))

        # Stale warning
        if ft_lbl and ft_lbl != file_type:
            st.warning(
                f"These results are from **{ft_lbl}**. "
                f"You have now selected **{file_type}** — click Find to refresh.",
                icon="⚠️",
            )
        else:
            st.info(f"Results for **{ft_lbl}**", icon="📄")

        yes_f   = (df_f["Found"] == "Yes").sum()
        no_f    = (df_f["Found"] == "No").sum()
        err_f   = (df_f["Found"] == "Error").sum()
        total_f = len(df_f)

        st.markdown(metric_html([
            ("DOMAINS CHECKED",   f"{total_f:,}", ""),
            ("YES — entry found",  f"{yes_f:,}",  "green"),
            ("NO  — not found",    f"{no_f:,}",   "red"),
            ("FETCH ERRORS",       f"{err_f:,}",  "amber"),
        ]), unsafe_allow_html=True)

        st.divider()
        st.subheader(f"📊 {net_lbl}  ·  {rel_lbl}  [{ft_lbl}]")

        fc1, fc2 = st.columns(2)
        with fc1:
            sf = st.multiselect("Filter by Found", ["Yes", "No", "Error"],
                                default=["Yes", "No", "Error"], key="ff_s")
        with fc2:
            dom_f = st.multiselect(
                "Filter by domain",
                sorted(df_f["Domain"].unique().tolist()),
                default=[], key="ff_d",
            )

        view_f = df_f[df_f["Found"].isin(sf)].copy()
        if dom_f:
            view_f = view_f[view_f["Domain"].isin(dom_f)]

        COLS   = ["Domain", "Seller IDs", "Count", "Found"]
        disp_f = view_f[COLS].copy()
        styled_f = apply_style(disp_f, subset=["Found"])

        st.dataframe(
            styled_f, use_container_width=True, hide_index=True,
            column_config={
                "Domain":     st.column_config.TextColumn("Domain",    width="medium"),
                "Seller IDs": st.column_config.TextColumn(
                    f"Seller ID(s) [{rel_lbl}] — comma-separated if multiple",
                    width="large",
                ),
                "Count":      st.column_config.NumberColumn("Count",   width="small"),
                "Found":      st.column_config.TextColumn("Found",     width="small"),
            },
        )
        st.caption(f"Showing {len(view_f):,} of {total_f:,} domains")

        err_rows_f = df_f[df_f["Found"] == "Error"][["Domain", "_err"]].drop_duplicates()
        if not err_rows_f.empty:
            with st.expander(f"⚠️ {len(err_rows_f)} fetch error(s)"):
                st.dataframe(
                    err_rows_f.rename(columns={"_err": "Error"}),
                    use_container_width=True, hide_index=True,
                )

        # Export
        st.divider()
        ts2  = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = (ft_lbl or "unknown").replace(".", "_")
        st.download_button(
            "⬇️ Export Network Finder results (CSV)",
            data=disp_f.to_csv(index=False).encode(),
            file_name=f"network_finder_{net_lbl}_{slug}_{ts2}.csv",
            mime="text/csv", use_container_width=True,
        )
