#!/usr/bin/env python3
"""
Ads.txt / App-Ads.txt Crawler  v4
  - match_fields selector  (2 = domain+ID only, default; 3 = +relation; 4 = +tag)
  - Tab 2 seller-ID upload → builds "network, id, RELATION" lines and runs a pivot
  - Smarter fetch: retry, HTML-page detection, detailed error labels
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
    page_title="Ads.txt Crawler",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ───────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
[data-testid="stSidebar"] {
    background:linear-gradient(180deg,#0f0c29,#302b63,#24243e);
}
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] span,
[data-testid="stSidebar"] div { color:#dde1ff !important; }
[data-testid="stSidebar"] .stButton>button {
    background:linear-gradient(135deg,#667eea,#764ba2)!important;
    color:white!important;border:none!important;
    border-radius:10px!important;font-weight:700!important;
}
[data-testid="stSidebar"] hr{border-color:#444!important;}

.hero{
    background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
    padding:1.3rem 1.8rem;border-radius:12px;color:white;
    margin-bottom:1.2rem;box-shadow:0 4px 20px rgba(102,126,234,.3);
}
.hero h1{margin:0;font-size:1.8rem;}
.hero p {margin:.3rem 0 0;opacity:.9;font-size:.95rem;}

.ft-pill{
    display:inline-block;padding:2px 12px;border-radius:99px;
    font-size:.9rem;font-weight:700;vertical-align:middle;margin-left:8px;
}
.ft-appads{background:#667eea;color:#fff;}
.ft-ads   {background:#f59e0b;color:#fff;}

.metric-row{display:flex;gap:.8rem;margin:1rem 0;flex-wrap:wrap;}
.tile{
    flex:1;min-width:110px;background:white;
    border-radius:10px;padding:.9rem 1rem;
    box-shadow:0 2px 8px rgba(0,0,0,.07);
    border-top:4px solid #667eea;
}
.tile.green{border-top-color:#22c55e;}
.tile.red  {border-top-color:#ef4444;}
.tile.amber{border-top-color:#f59e0b;}
.tile-lbl{font-size:.72rem;color:#6b7280;font-weight:600;letter-spacing:.04em;}
.tile-val{font-size:1.8rem;font-weight:800;color:#111;}

.mode-tag{
    display:inline-block;background:#f0f4ff;color:#3730a3;
    border:1px solid #c7d2fe;border-radius:6px;
    padding:2px 10px;font-size:.8rem;font-weight:600;margin-bottom:.5rem;
}
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# CORE  LOGIC
# ══════════════════════════════════════════════════════════════════════════════

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


def lines_match(entry: Dict, query: Dict, match_fields: int = 2) -> bool:
    """
    match_fields controls how many fields are compared:
      2 → domain + seller_id   (default — most lenient)
      3 → + relation
      4 → + tag (only when query includes a tag)
    Domain is ALWAYS compared (it defines the ad network).
    """
    if entry["domain"] != query["domain"]:
        return False
    if match_fields >= 2 and entry["seller_id"].lower() != query["seller_id"].lower():
        return False
    if match_fields >= 3 and entry["relation"] != query["relation"]:
        return False
    if match_fields >= 4 and query["tag"] and entry["tag"].lower() != query["tag"]:
        return False
    return True


def fetch_file(domain: str, file_type: str, timeout: int) -> Dict:
    """
    Fetch ads.txt / app-ads.txt with:
      • HTTPS → HTTP fallback
      • 1 automatic retry on transient connection errors
      • HTML-page detection (catches servers that return 200 + error HTML)
      • Specific error labels (timeout / DNS / SSL / HTTP code / empty / HTML)
    """
    headers = {
        "User-Agent": "AdsTxtCrawler/4.0",
        "Accept": "text/plain, text/*, */*",
    }
    last_error = "Connection failed"
    max_attempts = 2          # 1 initial + 1 retry

    for attempt in range(max_attempts):
        if attempt > 0:
            time.sleep(0.8)   # brief back-off before retry

        for scheme in ("https", "http"):
            url = f"{scheme}://{domain}/{file_type}"
            try:
                r = requests.get(
                    url, headers=headers, timeout=timeout, allow_redirects=True
                )

                if r.status_code == 200:
                    # ── Content validation ────────────────────────────────────
                    ctype   = r.headers.get("Content-Type", "")
                    content = r.text.strip()

                    # Empty body
                    if not content:
                        last_error = "Empty file (200 OK but no content)"
                        continue

                    # Server returned an HTML error page with status 200
                    if "html" in ctype.lower() or re.match(
                        r"<\s*(!doctype|html)", content[:40], re.I
                    ):
                        last_error = "HTML page returned (file probably missing)"
                        continue

                    return {"ok": True, "url": url, "text": r.text, "error": ""}

                elif r.status_code == 404:
                    # Definitive — no point retrying
                    return {
                        "ok": False, "url": url, "text": "",
                        "error": "File not found (404)",
                    }
                elif r.status_code in (401, 403):
                    return {
                        "ok": False, "url": url, "text": "",
                        "error": f"Access denied (HTTP {r.status_code})",
                    }
                elif r.status_code >= 500:
                    last_error = f"Server error (HTTP {r.status_code})"
                    # Retry may help for 5xx
                else:
                    last_error = f"Unexpected HTTP {r.status_code}"

            except requests.exceptions.Timeout:
                # No point retrying a timeout — server is just slow
                return {
                    "ok": False, "url": url, "text": "",
                    "error": f"Request timed out ({timeout}s)",
                }
            except requests.exceptions.SSLError:
                last_error = "SSL / certificate error"
                # Try http on next scheme iteration, don't retry https
            except requests.exceptions.ConnectionError as exc:
                msg = str(exc).lower()
                if any(k in msg for k in ("getaddrinfo", "name or service", "nodename", "nxdomain")):
                    # DNS failure — no retry will fix this
                    return {
                        "ok": False, "url": url, "text": "",
                        "error": "DNS lookup failed (domain not resolved)",
                    }
                elif "connection refused" in msg:
                    return {
                        "ok": False, "url": url, "text": "",
                        "error": "Connection refused by server",
                    }
                else:
                    last_error = "Network connection error"
            except Exception as exc:
                last_error = f"Unexpected error: {str(exc)[:60]}"

    return {
        "ok": False,
        "url": f"https://{domain}/{file_type}",
        "text": "",
        "error": last_error,
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
            key = _qkey(q)
            if key not in seen:
                seen.add(key)
                result.append(q)
    return result


def parse_seller_ids(text: str) -> List[str]:
    """One seller ID per line (or comma-separated). Strips duplicates."""
    ids, seen = [], set()
    for line in text.splitlines():
        for part in line.split(","):
            sid = part.strip()
            if sid and not sid.startswith("#") and sid not in seen:
                seen.add(sid)
                ids.append(sid)
    return ids


def _qkey(q: Dict) -> str:
    parts = [q["domain"], q["seller_id"], q["relation"]]
    if q["tag"]:
        parts.append(q["tag"])
    return ", ".join(parts)


# ── Crawl workers ─────────────────────────────────────────────────────────────

def crawl_lines(
    domain: str,
    queries: List[Dict],
    timeout: int,
    file_type: str,
    match_fields: int,
) -> List[Dict]:
    """Used by both Tab 1 and Tab 2 (specific-ID mode)."""
    fetch = fetch_file(domain, file_type, timeout)
    rows  = []
    if fetch["ok"]:
        entries = [
            e for ln in fetch["text"].splitlines()
            if (e := parse_ads_line(ln)) is not None
        ]
        for q in queries:
            match = next(
                (e for e in entries if lines_match(e, q, match_fields)), None
            )
            rows.append({
                "Domain": domain,
                "Line":   _qkey(q),
                "Found":  "Yes" if match else "No",
                "_ok":    True,
                "_err":   "",
            })
    else:
        for q in queries:
            rows.append({
                "Domain": domain,
                "Line":   _qkey(q),
                "Found":  "Error",
                "_ok":    False,
                "_err":   fetch["error"],
            })
    return rows


def crawl_network_all(
    domain: str,
    network: str,
    relation: str,
    timeout: int,
    file_type: str,
) -> Dict:
    """Tab 2 Find-All mode: list every seller ID for network+relation."""
    fetch = fetch_file(domain, file_type, timeout)
    if not fetch["ok"]:
        return {
            "Domain":     domain,
            "Seller IDs": "",
            "Count":      0,
            "Found":      "Error",
            "_ok":        False,
            "_err":       fetch["error"],
        }
    entries = [
        e for ln in fetch["text"].splitlines()
        if (e := parse_ads_line(ln)) is not None
    ]
    matching = [
        e for e in entries
        if e["domain"] == network.lower()
        and (not relation or e["relation"] == relation.upper())
    ]
    ids = [e["seller_id"] for e in matching]
    return {
        "Domain":     domain,
        "Seller IDs": ", ".join(ids),
        "Count":      len(ids),
        "Found":      "Yes" if ids else "No",
        "_ok":        True,
        "_err":       "",
    }


def run_parallel(domain_list: List[str], task_fn, workers: int) -> List:
    all_results, done, total = [], 0, len(domain_list)
    prog = st.progress(0.0, text=f"0 / {total} domains")
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
            done += 1
            prog.progress(
                done / total,
                text=f"🔄 {done} / {total} — {dom} ({round(time.time()-t0,1)}s)",
            )
    prog.progress(1.0, text=f"✅ Finished in {round(time.time()-t0,1)}s")
    return all_results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _style(val):
    if val == "Yes":   return "background-color:#dcfce7;color:#166534;font-weight:700"
    if val == "No":    return "background-color:#fee2e2;color:#991b1b;font-weight:700"
    if val == "Error": return "background-color:#fef3c7;color:#92400e;font-weight:700"
    return ""


def styled_df(df: pd.DataFrame, cols):
    try:
        return df.style.map(_style, subset=cols)
    except AttributeError:
        return df.style.applymap(_style, subset=cols)


def tiles_html(items):       # items: [(label, value, css_class), ...]
    parts = [
        f'<div class="tile {c}"><div class="tile-lbl">{l}</div>'
        f'<div class="tile-val">{v}</div></div>'
        for l, v, c in items
    ]
    return f'<div class="metric-row">{"".join(parts)}</div>'


def build_pivot(raw_rows, ordered_keys):
    """Convert flat rows to domain × line pivot; return display DataFrame."""
    df = pd.DataFrame(raw_rows)
    pivot = df.pivot_table(
        index="Domain", columns="Line", values="Found", aggfunc="first"
    )
    cols = [k for k in ordered_keys if k in pivot.columns]
    pivot = pivot[cols]
    pivot.index.name = pivot.columns.name = None
    out = pivot.copy()
    out.insert(0, "Crawled Domain", pivot.index)
    return out.reset_index(drop=True)


def stale_warn(last_ft, current_ft):
    if last_ft and last_ft != current_ft:
        st.warning(
            f"Showing results for **{last_ft}**. "
            f"File type is now **{current_ft}** — re-run to refresh.",
            icon="⚠️",
        )


# ── Session state ─────────────────────────────────────────────────────────────

for _k in ("std_raw", "std_keys", "std_ft",
           "finder_df", "finder_meta", "finder_mode"):
    if _k not in st.session_state:
        st.session_state[_k] = None


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("## ⚙️ Settings")

    # 1 ── File type ───────────────────────────────────────────────────────────
    st.markdown("**File type**")
    file_type = st.radio(
        "ft", ["app-ads.txt", "ads.txt"],
        index=0, horizontal=True,
        label_visibility="collapsed",
    )
    ft_cls = "ft-appads" if file_type == "app-ads.txt" else "ft-ads"
    st.markdown(
        f'Crawling: <span class="ft-pill {ft_cls}">{file_type}</span>',
        unsafe_allow_html=True,
    )
    st.divider()

    # 2 ── Domains ────────────────────────────────────────────────────────────
    st.markdown("**Domains**")
    dm = st.radio("dm", ["📁 Upload", "✏️ Paste"], horizontal=True,
                  label_visibility="collapsed", key="dm")
    domains_raw = ""
    if dm == "📁 Upload":
        up = st.file_uploader("domains.txt", type="txt", key="dup",
                              label_visibility="collapsed")
        if up:
            domains_raw = up.read().decode("utf-8", errors="replace")
    else:
        domains_raw = st.text_area(
            "Domains", key="dtxt", height=130, label_visibility="collapsed",
            placeholder="apps.mxplayer.in\nbattleprime.com\n…",
        )
    domain_list = parse_domain_list(domains_raw) if domains_raw.strip() else []
    if domain_list:
        st.caption(f"✔ {len(domain_list)} domain(s)")
    st.divider()

    # 3 ── Match fields ────────────────────────────────────────────────────────
    st.markdown("**Fields to match** *(Line Checker)*")
    match_fields = st.radio(
        "mf",
        options=[2, 3, 4],
        index=0,
        horizontal=True,
        label_visibility="collapsed",
        format_func=lambda x: {
            2: "2 — Domain + ID",
            3: "3 — + Relation",
            4: "4 — + Tag",
        }[x],
    )
    st.caption({
        2: "Match on **Ad Network domain** and **Seller ID** only.",
        3: "Also require **Relation** (RESELLER / DIRECT) to match.",
        4: "Also require **Tag** (certification ID) to match.",
    }[match_fields])
    st.divider()

    # 4 ── Crawl settings ──────────────────────────────────────────────────────
    st.markdown("**Crawl settings**")
    workers = st.slider("Parallel workers", 1, 30, 8)
    timeout = st.slider("Timeout per domain (s)", 5, 60, 12)


# ══════════════════════════════════════════════════════════════════════════════
# HEADER
# ══════════════════════════════════════════════════════════════════════════════

pill = f'<span class="ft-pill {ft_cls}">{file_type}</span>'
st.markdown(
    f'<div class="hero">'
    f'<h1>🔍 Ads.txt Crawler {pill}</h1>'
    f'<p>'
    f'<b>Line Checker</b> — verify specific entries; pivot table (domains × lines).<br>'
    f'<b>Network Finder</b> — find all or check specific seller IDs for one ad network.'
    f'</p></div>',
    unsafe_allow_html=True,
)


# ══════════════════════════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════════════════════════

tab1, tab2 = st.tabs(["📋  Line Checker", "🔎  Network Finder"])


# ─────────────────────────────────────────────────────────────────────────────
# TAB 1 — Line Checker
# ─────────────────────────────────────────────────────────────────────────────

with tab1:

    st.markdown(
        f"Checking against **`{file_type}`** · "
        f"matching **{match_fields} field(s)**: "
        + {2: "domain + seller ID", 3: "domain + seller ID + relation",
           4: "domain + seller ID + relation + tag"}[match_fields]
    )

    # Lines input
    lm = st.radio("lm", ["📁 Upload lines.txt", "✏️ Paste lines"],
                  horizontal=True, label_visibility="collapsed", key="lm")
    lines_raw = ""
    if lm == "📁 Upload lines.txt":
        lup = st.file_uploader(
            "lines.txt", type="txt", key="lup",
            label_visibility="collapsed",
            help="One entry per line: domain, seller_id, relation[, tag]",
        )
        if lup:
            lines_raw = lup.read().decode("utf-8", errors="replace")
    else:
        lines_raw = st.text_area(
            "Lines to check", key="ltxt", height=150,
            label_visibility="collapsed",
            placeholder=(
                "google.com, pub-6968738577620513, RESELLER, f08c47fec0942fa0\n"
                "smartadserver.com, 5427, RESELLER, 060d053dcf45cbf3\n"
                "video.unrulymedia.com, 906189653, RESELLER"
            ),
        )

    query_list = parse_query_list(lines_raw) if lines_raw.strip() else []
    if query_list:
        st.caption(f"✔ {len(query_list)} line(s) loaded")

    if st.button(
        f"🚀 Run Line Check  [{file_type}]",
        type="primary", key="run_std", use_container_width=True,
    ):
        if not domain_list:
            st.error("Add domains in the sidebar first.")
        elif not query_list:
            st.error("Provide at least one line to check.")
        else:
            _ql, _to, _ft, _mf = query_list[:], timeout, file_type, match_fields
            raw = run_parallel(
                domain_list,
                lambda d: crawl_lines(d, _ql, _to, _ft, _mf),
                workers,
            )
            st.session_state.std_raw  = raw
            st.session_state.std_keys = [_qkey(q) for q in _ql]
            st.session_state.std_ft   = _ft

    # ── Results ───────────────────────────────────────────────────────────────
    if st.session_state.std_raw:
        stale_warn(st.session_state.std_ft, file_type)

        raw_rows     = st.session_state.std_raw
        ordered_keys = st.session_state.std_keys or []
        df_long      = pd.DataFrame(raw_rows)

        yes_n  = (df_long["Found"] == "Yes").sum()
        no_n   = (df_long["Found"] == "No").sum()
        err_n  = (df_long["Found"] == "Error").sum()
        total_n = len(df_long)

        st.markdown(tiles_html([
            ("TOTAL",        f"{total_n:,}",                  ""),
            ("YES",          f"{yes_n:,}",                    "green"),
            ("NO",           f"{no_n:,}",                     "red"),
            ("ERRORS",       f"{err_n:,}",                    "amber"),
            ("FOUND RATE",   f"{round(yes_n/total_n*100,1) if total_n else 0}%", ""),
        ]), unsafe_allow_html=True)

        try:
            disp = build_pivot(raw_rows, ordered_keys)
            val_cols = [c for c in disp.columns if c != "Crawled Domain"]
            st.divider()
            st.subheader("Results")
            st.dataframe(
                styled_df(disp, val_cols),
                use_container_width=True, hide_index=True,
            )
            st.caption(
                "Rows = crawled domains  ·  Columns = lines checked  ·  "
                "Values = **Yes** / **No** / **Error**"
            )
        except Exception as exc:
            st.warning(f"Pivot build failed ({exc}) — showing flat view.")
            disp = df_long[["Domain","Line","Found"]].rename(columns={"Domain":"Crawled Domain"})
            st.dataframe(styled_df(disp, ["Found"]), use_container_width=True, hide_index=True)

        # Fetch-error breakdown
        errs = (df_long[df_long["Found"]=="Error"][["Domain","_err"]]
                .drop_duplicates().rename(columns={"Domain":"Domain","_err":"Error reason"}))
        if not errs.empty:
            with st.expander(f"⚠️ {len(errs)} fetch error(s) — click to expand"):
                st.dataframe(errs, use_container_width=True, hide_index=True)

        st.divider()
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = file_type.replace(".", "_")
        c1, c2 = st.columns(2)
        with c1:
            st.download_button("⬇️ Pivot CSV",
                data=disp.to_csv(index=False).encode(),
                file_name=f"pivot_{slug}_{ts}.csv", mime="text/csv",
                use_container_width=True)
        with c2:
            flat = df_long[["Domain","Line","Found"]].rename(columns={"Domain":"Crawled Domain"})
            st.download_button("⬇️ Flat list CSV",
                data=flat.to_csv(index=False).encode(),
                file_name=f"flat_{slug}_{ts}.csv", mime="text/csv",
                use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# TAB 2 — Network Finder
# ─────────────────────────────────────────────────────────────────────────────

with tab2:

    st.markdown(
        "Enter an ad-network domain and relation. "
        "**Leave seller IDs blank** to find all entries. "
        "**Upload / paste seller IDs** to check specific ones — "
        "the app builds `network, seller_id, relation` lines automatically."
    )

    # Network + relation row
    nc1, nc2 = st.columns([2, 1])
    with nc1:
        net_input = st.text_input(
            "Ad Network domain", value="xapads.com",
            placeholder="xapads.com", key="net_in",
        )
    with nc2:
        rel_sel = st.selectbox("Relation", ["DIRECT", "RESELLER", "Any"], key="rel_sel")

    st.divider()

    # Seller IDs (optional)
    st.markdown("**Seller IDs to check** *(optional)*")
    sid_m = st.radio(
        "sid_m", ["📁 Upload seller IDs", "✏️ Paste seller IDs", "— Find All (no IDs)"],
        index=2, horizontal=True, label_visibility="collapsed", key="sid_m",
    )
    seller_ids_raw = ""
    if sid_m == "📁 Upload seller IDs":
        sid_up = st.file_uploader(
            "seller_ids.txt", type="txt", key="sid_up",
            label_visibility="collapsed",
            help="One seller ID per line (or comma-separated).",
        )
        if sid_up:
            seller_ids_raw = sid_up.read().decode("utf-8", errors="replace")
    elif sid_m == "✏️ Paste seller IDs":
        seller_ids_raw = st.text_area(
            "Seller IDs", key="sid_txt", height=130,
            label_visibility="collapsed",
            placeholder="seller_id_001\nseller_id_002\nseller_id_003",
        )

    seller_ids = parse_seller_ids(seller_ids_raw) if seller_ids_raw.strip() else []

    # Mode preview
    if seller_ids:
        rel_label = rel_sel if rel_sel != "Any" else "ANY"
        example   = f"{normalize_domain(net_input)}, {seller_ids[0]}, {rel_label}"
        st.markdown(
            f'<span class="mode-tag">🎯 Check Specific IDs mode — '
            f'{len(seller_ids)} seller ID(s)</span><br>'
            f'<small style="color:#666">Example line: <code>{example}</code></small>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<span class="mode-tag">🔍 Find All mode — list every seller ID found</span>',
            unsafe_allow_html=True,
        )

    if st.button(
        f"🔎 Run Network Finder  [{file_type}]",
        type="primary", key="run_finder", use_container_width=True,
    ):
        if not domain_list:
            st.error("Add domains in the sidebar first.")
        elif not net_input.strip():
            st.error("Enter an ad-network domain.")
        else:
            _net = normalize_domain(net_input)
            _rel = "" if rel_sel == "Any" else rel_sel
            _to  = timeout
            _ft  = file_type

            if seller_ids:
                # ── Specific-ID mode: build queries, use crawl_lines ──────────
                # Always match 3 fields (domain + seller_id + relation)
                # because the user explicitly specified the relation
                _mf = 3 if _rel else 2
                queries = [
                    {"domain": _net, "seller_id": sid,
                     "relation": _rel if _rel else "DIRECT",
                     "tag": ""}
                    for sid in seller_ids
                ]
                _q = queries[:]
                raw = run_parallel(
                    domain_list,
                    lambda d: crawl_lines(d, _q, _to, _ft, _mf),
                    workers,
                )
                st.session_state.finder_df   = raw           # list of rows
                st.session_state.finder_meta = (_net, rel_sel, _ft)
                st.session_state.finder_mode = "specific"
                st.session_state.finder_keys = [_qkey(q) for q in _q]
            else:
                # ── Find-All mode ─────────────────────────────────────────────
                rows = run_parallel(
                    domain_list,
                    lambda d: crawl_network_all(d, _net, _rel, _to, _ft),
                    workers,
                )
                st.session_state.finder_df   = pd.DataFrame(rows)
                st.session_state.finder_meta = (_net, rel_sel, _ft)
                st.session_state.finder_mode = "all"

    # ── Results ───────────────────────────────────────────────────────────────
    if st.session_state.finder_df is not None:
        net_lbl, rel_lbl, ft_lbl = st.session_state.finder_meta or ("", "", "")
        mode = st.session_state.finder_mode

        stale_warn(ft_lbl, file_type)

        # ── Specific-ID mode — pivot ──────────────────────────────────────────
        if mode == "specific":
            raw_rows     = st.session_state.finder_df     # list
            ordered_keys = st.session_state.finder_keys or []
            df_long      = pd.DataFrame(raw_rows)

            yes_n   = (df_long["Found"] == "Yes").sum()
            no_n    = (df_long["Found"] == "No").sum()
            err_n   = (df_long["Found"] == "Error").sum()
            total_n = len(df_long)

            st.markdown(tiles_html([
                ("TOTAL",    f"{total_n:,}", ""),
                ("YES",      f"{yes_n:,}",  "green"),
                ("NO",       f"{no_n:,}",   "red"),
                ("ERRORS",   f"{err_n:,}",  "amber"),
            ]), unsafe_allow_html=True)

            try:
                disp_f = build_pivot(raw_rows, ordered_keys)
                val_cols_f = [c for c in disp_f.columns if c != "Crawled Domain"]
                st.divider()
                st.subheader(f"Results — {net_lbl} · {rel_lbl}")
                st.dataframe(
                    styled_df(disp_f, val_cols_f),
                    use_container_width=True, hide_index=True,
                )
                st.caption(
                    "Rows = crawled domains  ·  "
                    "Columns = seller IDs checked  ·  Values = **Yes** / **No** / **Error**"
                )
            except Exception as exc:
                st.warning(f"Pivot failed ({exc}) — showing flat view.")
                disp_f = (df_long[["Domain","Line","Found"]]
                          .rename(columns={"Domain":"Crawled Domain"}))
                st.dataframe(styled_df(disp_f, ["Found"]),
                             use_container_width=True, hide_index=True)

            errs_f = (df_long[df_long["Found"]=="Error"][["Domain","_err"]]
                      .drop_duplicates().rename(columns={"_err":"Error reason"}))
            if not errs_f.empty:
                with st.expander(f"⚠️ {len(errs_f)} fetch error(s)"):
                    st.dataframe(errs_f, use_container_width=True, hide_index=True)

            ts3 = datetime.now().strftime("%Y%m%d_%H%M%S")
            st.download_button(
                "⬇️ Export CSV",
                data=disp_f.to_csv(index=False).encode(),
                file_name=f"finder_{net_lbl}_{ts3}.csv",
                mime="text/csv", use_container_width=True,
            )

        # ── Find-All mode — list ──────────────────────────────────────────────
        else:
            df_a    = st.session_state.finder_df
            yes_a   = (df_a["Found"] == "Yes").sum()
            no_a    = (df_a["Found"] == "No").sum()
            err_a   = (df_a["Found"] == "Error").sum()
            total_a = len(df_a)

            st.markdown(tiles_html([
                ("DOMAINS",  f"{total_a:,}", ""),
                ("YES",      f"{yes_a:,}",   "green"),
                ("NO",       f"{no_a:,}",    "red"),
                ("ERRORS",   f"{err_a:,}",   "amber"),
            ]), unsafe_allow_html=True)

            st.divider()
            st.subheader(f"Results — {net_lbl} · {rel_lbl}")

            fa1, fa2 = st.columns(2)
            with fa1:
                sf_a = st.multiselect("Filter Found", ["Yes","No","Error"],
                                      default=["Yes","No","Error"], key="ff_sa")
            with fa2:
                df_a_filt = st.multiselect(
                    "Filter domain",
                    sorted(df_a["Domain"].unique().tolist()),
                    default=[], key="ff_da",
                )

            view_a = df_a[df_a["Found"].isin(sf_a)].copy()
            if df_a_filt:
                view_a = view_a[view_a["Domain"].isin(df_a_filt)]

            COLS_A = ["Domain", "Seller IDs", "Count", "Found"]
            disp_a = view_a[COLS_A].copy()
            st.dataframe(
                styled_df(disp_a, ["Found"]),
                use_container_width=True, hide_index=True,
                column_config={
                    "Seller IDs": st.column_config.TextColumn(
                        f"Seller IDs  [{rel_lbl}]  — comma-separated",
                        width="large",
                    ),
                    "Count": st.column_config.NumberColumn("Count", width="small"),
                    "Found": st.column_config.TextColumn("Found",   width="small"),
                },
            )
            st.caption(f"Showing {len(view_a):,} of {total_a:,} domains")

            errs_a = (df_a[df_a["Found"]=="Error"][["Domain","_err"]]
                      .drop_duplicates().rename(columns={"_err":"Error reason"}))
            if not errs_a.empty:
                with st.expander(f"⚠️ {len(errs_a)} fetch error(s)"):
                    st.dataframe(errs_a, use_container_width=True, hide_index=True)

            ts4 = datetime.now().strftime("%Y%m%d_%H%M%S")
            st.download_button(
                "⬇️ Export CSV",
                data=disp_a.to_csv(index=False).encode(),
                file_name=f"finder_all_{net_lbl}_{ts4}.csv",
                mime="text/csv", use_container_width=True,
            )
