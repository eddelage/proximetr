"""
Proximetr v2 — Brute force EDGAR scanner + Claude synthesis
Full document, per-filing extraction, final synthesis.
"""

import streamlit as st
import requests
import json
import re
from datetime import datetime, timedelta
from typing import Optional
import anthropic

# ── Config ────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Proximetr", page_icon="📡", layout="wide")

EDGAR_HEADERS = {"User-Agent": "Proximetr research@proximetr.io"}
EDGAR_BASE = "https://data.sec.gov"
API_KEY = st.secrets["ANTHROPIC_API_KEY"]

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');

html, body, [data-testid="stAppViewContainer"] {
    background-color: #080c14;
    color: #c9d1e0;
    font-family: 'Inter', sans-serif;
}
[data-testid="stAppViewContainer"] { max-width: 860px; margin: 0 auto; }
h1 { font-family: 'JetBrains Mono', monospace; color: #f0f4ff; letter-spacing: -1px; }
h2, h3 { color: #e2e8f0; font-weight: 600; }
.stTextInput input {
    background: #0f1623 !important;
    border: 1px solid #1e2d45 !important;
    border-radius: 8px !important;
    color: #e2e8f0 !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 14px !important;
    padding: 12px 16px !important;
}
.stTextInput input:focus { border-color: #3b82f6 !important; box-shadow: 0 0 0 2px rgba(59,130,246,0.15) !important; }
[data-testid="stMultiSelect"] > div > div {
    background: #0f1623 !important;
    border: 1px solid #1e2d45 !important;
    border-radius: 8px !important;
}
[data-baseweb="tag"] { background: #1e3a5f !important; border-radius: 4px !important; }
[data-testid="stRadio"] label { color: #94a3b8 !important; font-size: 13px !important; }
.stButton > button {
    background: #3b82f6 !important;
    color: #fff !important;
    border: none !important;
    border-radius: 8px !important;
    font-weight: 600 !important;
    font-size: 14px !important;
    padding: 10px 24px !important;
    width: 100% !important;
}
.stButton > button:hover { background: #2563eb !important; }
.stDownloadButton > button {
    background: #0f1623 !important;
    color: #94a3b8 !important;
    border: 1px solid #1e2d45 !important;
    border-radius: 8px !important;
    font-size: 13px !important;
}
.filing-row {
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    color: #64748b;
    padding: 6px 0;
    border-bottom: 1px solid #0f1a2a;
    display: flex;
    gap: 12px;
    align-items: center;
}
hr { border-color: #1a2540 !important; margin: 24px 0 !important; }
.tag {
    display: inline-block;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    font-weight: 600;
    padding: 2px 7px;
    border-radius: 3px;
    margin-right: 5px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}
.tag-blue { background: #1e3a5f; color: #60a5fa; }
</style>
""", unsafe_allow_html=True)

# ── Constants ─────────────────────────────────────────────────────────────────

ALL_FORMS = {
    "10-K": "Annual report",
    "10-Q": "Quarterly report",
    "8-K": "Material events",
    "4": "Insider trades",
    "3": "Initial ownership",
    "SC 13G": "Large holder (passive)",
    "SC 13D": "Large holder (activist)",
    "13F-HR": "Institutional holdings",
    "D": "Private placement",
    "S-1": "IPO registration",
    "S-1/A": "Amended S-1",
    "424B4": "Final prospectus",
    "DEF 14A": "Proxy statement",
}

ALL_SECTIONS = ["Company Snapshot", "Buying Appetite", "Related Companies",
                "Investing Insights", "Insider Activity", "Material Events",
                "Risk Factors", "Shareholder Moves", "Management Tone"]

# ── EDGAR ─────────────────────────────────────────────────────────────────────

def get_company_by_cik(cik: str) -> Optional[dict]:
    cik_padded = str(cik).strip().lstrip("0").zfill(10)
    try:
        resp = requests.get(
            f"{EDGAR_BASE}/submissions/CIK{cik_padded}.json",
            headers=EDGAR_HEADERS, timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "cik": cik_padded,
                "cik_raw": str(int(cik_padded)),
                "name": data.get("name", "Unknown"),
                "ticker": (data.get("tickers") or [None])[0],
                "sic_description": data.get("sicDescription", ""),
                "state": data.get("stateOfIncorporation", ""),
                "filings": data.get("filings", {}).get("recent", {}),
            }
    except Exception as e:
        st.error(f"EDGAR lookup failed: {e}")
    return None


def get_filings_in_range(company: dict, form_types: list, start_date: str, end_date: str) -> list[dict]:
    recent = company.get("filings", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    results = []
    for i, form in enumerate(forms):
        if form not in form_types:
            continue
        date = dates[i] if i < len(dates) else ""
        if date < start_date or date > end_date:
            continue
        results.append({
            "form_type": form,
            "accession_no": accessions[i] if i < len(accessions) else "",
            "date": date,
            "cik": company["cik_raw"],
        })
    return sorted(results, key=lambda x: x["date"], reverse=True)


def clean_edgar_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_exhibit(name: str, doc_type: str) -> bool:
    name_lower = name.lower()
    type_lower = doc_type.lower()
    if type_lower.startswith("ex-") or type_lower.startswith("ex "):
        return True
    if re.search(r"dex\d|ex\d|ex-\d|ex_\d", name_lower):
        return True
    if any(k in name_lower for k in ["_htm.xml", "xbrl", ".xsd", "taxonomy"]):
        return True
    return False


def get_index_docs(base: str, accession_no: str) -> list[dict]:
    docs = []
    try:
        resp = requests.get(f"{base}/{accession_no}-index.htm", headers=EDGAR_HEADERS, timeout=15)
        if resp.status_code != 200:
            return docs
        rows = re.findall(r"<tr[^>]*>.*?</tr>", resp.text, re.IGNORECASE | re.DOTALL)
        for row in rows:
            seq_match = re.search(r"<td[^>]*>\s*(\d+)\s*</td>", row)
            href_match = re.search(r'href="([^"]+\.(htm|html|txt))"', row, re.IGNORECASE)
            if not seq_match or not href_match:
                continue
            raw_href = href_match.group(1)
            if "ix?doc=" in raw_href:
                raw_href = raw_href.split("ix?doc=")[-1]
            fname = raw_href.split("/")[-1]
            type_match = re.search(r"<td[^>]*>([^<]{1,30})</td>\s*<td[^>]*>\s*\d", row)
            doc_type = type_match.group(1).strip() if type_match else ""
            docs.append({"seq": int(seq_match.group(1)), "name": fname, "type": doc_type})
    except Exception:
        pass
    return sorted(docs, key=lambda x: x["seq"])


def fetch_doc(url: str) -> str:
    try:
        resp = requests.get(url, headers=EDGAR_HEADERS, timeout=30)
        if resp.status_code == 200 and len(resp.text) > 300:
            return clean_edgar_text(resp.text)
    except Exception:
        pass
    return ""


def fetch_filing_text(cik: str, accession_no: str) -> tuple[str, str]:
    acc_clean = accession_no.replace("-", "")
    cik_int = int(cik)
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}"

    docs = get_index_docs(base, accession_no)
    primary_text = ""
    primary_name = ""
    exhibit_texts = []

    for doc in docs:
        fname = doc["name"]
        doc_type = doc["type"].lower()
        seq = doc["seq"]
        url = f"{base}/{fname}"

        if seq == 1 and not is_exhibit(fname, doc_type):
            primary_text = fetch_doc(url)
            primary_name = fname
        elif "ex-99" in doc_type or "ex 99" in doc_type or fname.lower().startswith("ex99") or "99.1" in fname:
            text = fetch_doc(url)
            if text:
                exhibit_texts.append(text)

    parts = []
    status_parts = []
    if primary_text:
        parts.append(primary_text)
        status_parts.append(primary_name)
    for i, et in enumerate(exhibit_texts):
        parts.append("=== EXHIBIT 99." + str(i+1) + " ===\n" + et)
        status_parts.append("ex99." + str(i+1))

    if parts:
        return "\n\n".join(parts), "✓ " + " + ".join(status_parts)

    text = fetch_doc(f"{base}/{accession_no}.txt")
    if text:
        return text, "✓ bundle"

    return "", "✗ failed"


# ── Claude ────────────────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """You are a senior analyst reading one SEC filing. Your job is to flag only what matters to an investor — not summarize the document.

Think like a highlighter, not a transcriber. If something is routine, boilerplate, or already well-known, skip it.

Filing: {form_type} filed {date} by {company} (CIK: {cik})

{text}

Return ONLY valid JSON, no markdown, no preamble:
{{
  "form_type": "{form_type}",
  "date": "{date}",
  "key_facts": [
    "Max 15 items. Prioritize: ALL dollar amounts (contracts, grants, raises, revenue), ALL named government awards, ALL partnership announcements, ALL milestones hit or missed, headcount, guidance changes, surprises. Never omit a specific dollar figure or grant award."
  ],
  "companies_mentioned": [
    {{
      "name": "string",
      "ticker": "string or null",
      "is_private": true,
      "relation": "customer|partner|competitor|investor|vendor|acquirer",
      "context": "one sentence — why this relationship matters"
    }}
  ],
  "insider_transactions": [
    {{
      "name": "string",
      "title": "string",
      "type": "buy|sell|grant",
      "shares": 0,
      "price_per_share": null,
      "total_value": null,
      "date": "string",
      "is_discretionary": true
    }}
  ],
  "material_events": [
    {{
      "event_type": "string",
      "summary": "one sentence max",
      "significance": "one sentence — investment angle only"
    }}
  ],
  "risk_signals": ["Max 5. Only new, specific, or elevated risks — skip standard boilerplate"],
  "positive_signals": ["Max 5. Only concrete positives with evidence"],
  "financial_highlights": ["Max 5. Actual numbers: revenue, margins, cash, guidance"],
  "management_language": "one word + one sentence evidence"
}}

Hard rules:
- companies_mentioned: NO law firms, auditors, transfer agents, custodians, index funds. Only strategic relationships.
- insider_transactions: is_discretionary=true ONLY for open-market trades outside a 10b5-1 plan
- key_facts: ALWAYS capture dollar amounts. EXCLUDE auditor changes, equity plan setup, routine governance.
- positive_signals: commercial traction only. NOT auditor upgrades or compensation setup.
- If nothing notable in a category, return empty array
- Total response must be concise — quality over quantity"""


SYNTHESIS_PROMPT = """You are a senior investment analyst writing a concise brief for a sophisticated investor.

You have per-filing extractions for {company} covering {date_range} across {filing_count} filings ({form_types}).

Extractions:
{extractions}

Return ONLY valid JSON, no markdown, no preamble.

{{
  "header": {{
    "name": "string",
    "ticker": "string or null",
    "sector": "string",
    "sub_sector": "string",
    "status": "public|private|pre-ipo",
    "stage": "string",
    "one_liner": "one sentence — what this company does, for whom, and why it matters"
  }},
  "growth_insights": [
    {{
      "headline": "6-8 words",
      "detail": "2-3 sentences. Commercial signals only — contract wins with $ values, government grants with $ values, new partnerships, new markets, product launches, technology milestones. NEVER: auditor changes, equity plan setup, SPAC governance, advisory fees, admin."
    }}
  ],
  "key_relationships": [
    {{
      "name": "string",
      "ticker": "string or null",
      "is_private": false,
      "role": "customer|partner|competitor|investor|vendor|acquirer",
      "one_liner": "one sentence on what this relationship means for the investment thesis"
    }}
  ],
  "insider_activity": ["FIRST STRING: verdict starting with 'Net signal: [strongly bearish/bearish/neutral/bullish]' — pattern summary. Then 3-4 more strings: crisp bullets — name, action, dollar amount, discretionary or not, one-line significance. 5 strings max total."],
  "buying_appetite": {{
    "rating": "strong|moderate|weak|insufficient_data",
    "score": 0,
    "verdict": "one punchy sentence — the bottom line based on everything in the filings"
  }}
}}

Rules:
- growth_insights: 3-5 items grounded in actual filing data. No speculation.
- key_relationships: 4-8 companies. Strategic commercial only. EXCLUDE: SPACs, shell entities, SPAC sponsors, auditors, law firms, one-time advisors.
- buying_appetite score: 0-3 weak, 4-6 moderate, 7-8 strong, 9-10 very strong.
- No repetition across sections."""


def extract_filing(filing: dict, text: str, company: dict) -> dict:
    client = anthropic.Anthropic(api_key=API_KEY)
    prompt = EXTRACTION_PROMPT.format(
        form_type=filing["form_type"],
        date=filing["date"],
        company=company["name"],
        cik=company["cik_raw"],
        text=text,
    )
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Try salvaging truncated JSON
            last_brace = raw.rfind("}")
            if last_brace > 0:
                try:
                    return json.loads(raw[:last_brace+1])
                except Exception:
                    pass
            return {"error": "JSON parse failed: " + raw[:200], "form_type": filing["form_type"], "date": filing["date"]}
    except Exception as e:
        return {"error": str(e), "form_type": filing["form_type"], "date": filing["date"]}


def synthesize(company: dict, extractions: list, date_range: str, form_types: str) -> dict:
    client = anthropic.Anthropic(api_key=API_KEY)
    valid = [e for e in extractions if "error" not in e]
    if not valid:
        return {"error": "No filing text could be extracted."}

    slim = []
    for e in valid:
        slim.append({
            "form_type": e.get("form_type"),
            "date": e.get("date"),
            "key_facts": e.get("key_facts", [])[:15],
            "companies_mentioned": e.get("companies_mentioned", [])[:10],
            "insider_transactions": e.get("insider_transactions", []),
            "material_events": e.get("material_events", []),
            "risk_signals": e.get("risk_signals", [])[:8],
            "positive_signals": e.get("positive_signals", [])[:8],
            "financial_highlights": e.get("financial_highlights", [])[:8],
            "management_language": e.get("management_language", ""),
        })
    extractions_text = json.dumps(slim, indent=1)[:80000]

    prompt = SYNTHESIS_PROMPT.format(
        company=company["name"],
        date_range=date_range,
        filing_count=len(valid),
        form_types=form_types,
        extractions=extractions_text,
    )
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            last_brace = raw.rfind("}")
            if last_brace > 0:
                try:
                    return json.loads(raw[:last_brace+1])
                except Exception:
                    pass
            raise
    except Exception as e:
        return {"error": str(e)}


# ── Render ────────────────────────────────────────────────────────────────────

def section_label(text):
    st.markdown(
        '<div style="font-size:12px;font-weight:700;color:#94a3b8;text-transform:uppercase;'
        'letter-spacing:0.14em;margin:32px 0 14px 0;border-left:3px solid #3b82f6;padding-left:10px">'
        + text + '</div>',
        unsafe_allow_html=True
    )

def divider():
    st.markdown('<hr style="border-color:#1a2540;margin:4px 0">', unsafe_allow_html=True)


def render_brief(result: dict, company: dict, filing_count: int, form_types: str):
    header = result.get("header", {})
    ticker = header.get("ticker") or company.get("ticker") or ""

    # 1. Header
    ticker_html = '<span style="color:#3b82f6;font-size:20px;margin-left:10px">' + ticker + '</span>' if ticker else ""
    st.markdown(
        '<div style="padding:28px 0 20px 0">'
        '<div style="font-size:26px;font-weight:700;color:#f0f4ff;font-family:JetBrains Mono,monospace;margin-bottom:6px">'
        + header.get("name", "") + " " + ticker_html +
        '</div>'
        '<div style="color:#475569;font-size:13px;margin-bottom:6px">'
        + header.get("sector", "") + " · " + header.get("sub_sector", "") + " · " + header.get("stage", "") +
        '</div>'
        '<div style="color:#94a3b8;font-size:14px;margin-bottom:8px;line-height:1.5">'
        + header.get("one_liner", "") +
        '</div>'
        '<div style="color:#1e3a5f;font-size:11px;font-family:JetBrains Mono,monospace">'
        + str(filing_count) + " filings · " + form_types +
        '</div></div>',
        unsafe_allow_html=True
    )

    divider()

    # 2. Growth Insights
    insights = result.get("growth_insights", [])
    if insights:
        section_label("Growth Insights")
        for ins in insights:
            st.markdown(
                '<div style="padding:12px 0;border-bottom:1px solid #0d1526">'
                '<div style="color:#e2e8f0;font-weight:600;font-size:13px;margin-bottom:4px">' + ins.get("headline", "") + '</div>'
                '<div style="color:#64748b;font-size:13px;line-height:1.7">' + ins.get("detail", "") + '</div>'
                '</div>',
                unsafe_allow_html=True
            )

    divider()

    # 3. Key Relationships
    relationships = result.get("key_relationships", [])
    if relationships:
        section_label("Key Relationships")
        for co in relationships:
            ticker_co = co.get("ticker") or ""
            is_private = co.get("is_private", False)
            name = co.get("name", "")
            role = co.get("role", "").upper()
            one_liner = co.get("one_liner", "")

            if is_private:
                badge = '<span style="font-family:JetBrains Mono,monospace;font-size:9px;font-weight:700;color:#a78bfa;background:#1e1040;padding:1px 5px;border-radius:3px;margin-right:6px">PRIVATE</span>'
            elif ticker_co:
                badge = '<span style="font-family:JetBrains Mono,monospace;font-size:9px;font-weight:700;color:#60a5fa;background:#1e3a5f;padding:1px 5px;border-radius:3px;margin-right:6px">' + ticker_co + '</span>'
            else:
                badge = ""

            st.markdown(
                '<div style="display:flex;gap:12px;padding:12px 0;border-bottom:1px solid #0d1526;align-items:flex-start">'
                '<div style="width:220px;flex-shrink:0;display:flex;align-items:center;flex-wrap:wrap">'
                + badge +
                '<span style="color:#e2e8f0;font-size:13px;font-weight:600">' + name + '</span>'
                '</div>'
                '<div style="width:80px;flex-shrink:0;padding-top:1px">'
                '<span style="color:#334155;font-size:10px;font-family:JetBrains Mono,monospace">' + role + '</span>'
                '</div>'
                '<div style="color:#64748b;font-size:12px;line-height:1.6">' + one_liner + '</div>'
                '</div>',
                unsafe_allow_html=True
            )

    divider()

    # 4. Insider Activity
    insider = result.get("insider_activity", [])
    if insider:
        section_label("Insider Activity")
        items = insider if isinstance(insider, list) else [insider]
        for bullet in items:
            st.markdown(
                '<div style="display:flex;gap:10px;padding:8px 0;border-bottom:1px solid #0d1526">'
                '<span style="color:#334155;flex-shrink:0;margin-top:2px">—</span>'
                '<span style="color:#94a3b8;font-size:13px;line-height:1.6">' + str(bullet) + '</span>'
                '</div>',
                unsafe_allow_html=True
            )

    divider()

    # 5. Buying Appetite
    appetite = result.get("buying_appetite", {})
    rating = appetite.get("rating", "insufficient_data")
    score = appetite.get("score", 0)
    verdict = appetite.get("verdict", "")
    rating_config = {
        "strong":            ("🟢", "#34d399", "Strong"),
        "moderate":          ("🟡", "#fbbf24", "Moderate"),
        "weak":              ("🔴", "#f87171", "Weak"),
        "insufficient_data": ("⚪", "#64748b", "Insufficient Data"),
    }
    bar_icon, bar_color, bar_label = rating_config.get(rating, ("⚪", "#64748b", rating))
    section_label("Buying Appetite")
    st.markdown(
        '<div style="background:#0d1526;border:1px solid #1a2540;border-left:3px solid ' + bar_color + ';border-radius:0 8px 8px 0;padding:16px 20px;margin-bottom:32px">'
        '<div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">'
        '<span style="font-size:22px;font-weight:700;color:' + bar_color + '">' + bar_icon + ' ' + bar_label + '</span>'
        '<span style="font-family:JetBrains Mono,monospace;color:#475569;font-size:12px">' + str(score) + '/10</span>'
        '</div>'
        '<p style="color:#94a3b8;font-size:13px;line-height:1.7;margin:0">' + verdict + '</p>'
        '</div>',
        unsafe_allow_html=True
    )


def build_txt(result: dict, company: dict, start_str: str, end_str: str, form_types_str: str) -> str:
    sep = "=" * 60
    lines = [
        "PROXIMETR",
        company["name"] + ("  (" + company["ticker"] + ")" if company.get("ticker") else ""),
        "CIK " + company["cik_raw"] + "  |  " + start_str + " to " + end_str,
        "Forms: " + form_types_str,
        "Generated " + datetime.now().strftime("%Y-%m-%d %H:%M"),
        "",
    ]

    # Header
    header = result.get("header", {})
    if header:
        lines += [sep, "COMPANY", sep]
        lines.append(header.get("sector", "") + " · " + header.get("sub_sector", "") + " · " + header.get("stage", ""))
        lines.append(header.get("one_liner", ""))
        lines.append("")

    # Growth Insights
    insights = result.get("growth_insights", [])
    if insights:
        lines += [sep, "GROWTH INSIGHTS", sep]
        for ins in insights:
            lines.append("▸ " + ins.get("headline", ""))
            lines.append("  " + ins.get("detail", ""))
            lines.append("")

    # Key Relationships
    relationships = result.get("key_relationships", [])
    if relationships:
        lines += [sep, "KEY RELATIONSHIPS", sep]
        for co in relationships:
            ticker_co = co.get("ticker") or ""
            is_private = co.get("is_private", False)
            label = "[PRIVATE]" if is_private else ("[" + ticker_co + "]" if ticker_co else "")
            lines.append((label + " " if label else "") + co.get("name", "") + "  —  " + co.get("role", "").upper())
            lines.append("  " + co.get("one_liner", ""))
            lines.append("")

    # Insider Activity
    insider = result.get("insider_activity", [])
    if insider:
        lines += [sep, "INSIDER ACTIVITY", sep]
        items = insider if isinstance(insider, list) else [insider]
        for bullet in items:
            lines.append("• " + str(bullet))
        lines.append("")

    # Buying Appetite
    appetite = result.get("buying_appetite", {})
    if appetite:
        rating = appetite.get("rating", "").upper()
        score = appetite.get("score", 0)
        verdict = appetite.get("verdict", "")
        lines += [sep, "BUYING APPETITE", sep]
        lines.append(rating + "  " + str(score) + "/10")
        lines.append(verdict)
        lines.append("")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    st.markdown("# 📡 proximetr")
    st.markdown('<p style="color:#475569;margin-top:-12px;margin-bottom:24px">Full-document EDGAR scanner + Claude synthesis</p>', unsafe_allow_html=True)

    # CIK input — auto-lookup on entry
    cik_input = st.text_input("", placeholder="CIK  (find at sec.gov/cgi-bin/browse-edgar)", label_visibility="collapsed")

    company = None
    if cik_input:
        if "company" not in st.session_state or st.session_state.get("last_cik") != cik_input:
            with st.spinner(""):
                company = get_company_by_cik(cik_input)
            if company:
                st.session_state["company"] = company
                st.session_state["last_cik"] = cik_input
            else:
                st.error("CIK not found.")
                st.stop()
        else:
            company = st.session_state["company"]

    if company:
        ticker_str = " · " + company["ticker"] if company.get("ticker") else ""
        st.markdown(
            '<p style="color:#34d399;font-size:13px;font-family:JetBrains Mono,monospace">✓ '
            + company["name"] + ticker_str + '</p>',
            unsafe_allow_html=True
        )

    # Timeframe
    preset = st.radio("", ["90d", "1y", "2y", "All time", "Custom"], horizontal=True, label_visibility="collapsed")
    today = datetime.now()
    if preset == "90d":
        start_date, end_date = today - timedelta(days=90), today
    elif preset == "1y":
        start_date, end_date = today - timedelta(days=365), today
    elif preset == "2y":
        start_date, end_date = today - timedelta(days=730), today
    elif preset == "All time":
        start_date, end_date = datetime(1993, 1, 1), today
    else:
        c1, c2 = st.columns(2)
        with c1:
            start_date = datetime.combine(st.date_input("From", value=today - timedelta(days=365)), datetime.min.time())
        with c2:
            end_date = datetime.combine(st.date_input("To", value=today), datetime.min.time())

    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    # Forms
    selected_forms = st.multiselect(
        "",
        list(ALL_FORMS.keys()),
        default=list(ALL_FORMS.keys()),
        format_func=lambda x: x + " — " + ALL_FORMS[x],
        label_visibility="collapsed",
        placeholder="Select forms to pull...",
    )

    if not company:
        st.markdown('<p style="color:#334155;font-size:13px;margin-top:16px">Enter a CIK to get started.</p>', unsafe_allow_html=True)
        return

    run = st.button("▶  Run Analysis")

    if not selected_forms:
        st.error("Select at least one form.")
        return

    # If cached result exists and Run wasn't just pressed — show it and stop
    if not run and "cached_result" in st.session_state:
        result = st.session_state["cached_result"]
        filings = st.session_state["cached_filings"]
        form_types_str = st.session_state["cached_form_types_str"]
        company = st.session_state.get("company", company)
        st.markdown("---")
        render_brief(result, company, len(filings), form_types_str)
        st.markdown("---")
        txt = build_txt(result, company, start_str, end_str, form_types_str)
        st.download_button("⬇ Download TXT", data=txt,
            file_name="proximetr_" + company["name"].replace(" ", "_").lower() + "_" + datetime.now().strftime("%Y%m%d") + ".txt",
            mime="text/plain")
        return

    if not run:
        return

    # ── Pipeline ──

    # 1. Get filings
    with st.spinner("Scanning filings..."):
        filings = get_filings_in_range(company, selected_forms, start_str, end_str)

    if not filings:
        st.error("No filings found for the selected forms and date range.")
        return

    st.markdown(
        '<p style="color:#475569;font-size:12px;font-family:JetBrains Mono,monospace">'
        + str(len(filings)) + ' filings found</p>',
        unsafe_allow_html=True
    )

    # 2. Fetch + extract per filing
    extractions = []
    progress = st.progress(0)

    for i, f in enumerate(filings):
        progress.progress((i + 1) / len(filings), text=f["form_type"] + " " + f["date"] + "...")
        text, fetch_status = fetch_filing_text(f["cik"], f["accession_no"])
        ok = fetch_status.startswith("✓")
        status_color = "#34d399" if ok else "#f87171"
        st.markdown(
            '<div class="filing-row">'
            '<span class="tag tag-blue">' + f["form_type"] + '</span>'
            '<span>' + f["date"] + '</span>'
            '<span style="color:' + status_color + '">' + fetch_status + '</span>'
            '<span style="color:#1e3a5f">' + f"{len(text):,}" + ' chars</span>'
            '</div>',
            unsafe_allow_html=True
        )
        if not text:
            extractions.append({"error": "no text", "form_type": f["form_type"], "date": f["date"]})
            continue
        extraction = extract_filing(f, text, company)
        if "error" in extraction:
            st.warning(f["form_type"] + " " + f["date"] + " extraction error: " + str(extraction["error"])[:120])
        extractions.append(extraction)

    progress.empty()

    # 3. Synthesize
    with st.spinner("Synthesizing..."):
        form_types_str = ", ".join(sorted(set(f["form_type"] for f in filings)))
        result = synthesize(company, extractions, f"{start_str} to {end_str}", form_types_str)

    if "error" in result:
        st.error("Synthesis failed: " + result["error"])
        return

    # Cache
    st.session_state["cached_result"] = result
    st.session_state["cached_filings"] = filings
    st.session_state["cached_form_types_str"] = form_types_str

    # Render
    st.markdown("---")
    render_brief(result, company, len(filings), form_types_str)

    # Export
    st.markdown("---")
    txt = build_txt(result, company, start_str, end_str, form_types_str)
    st.download_button(
        "⬇ Download TXT",
        data=txt,
        file_name="proximetr_" + company["name"].replace(" ", "_").lower() + "_" + datetime.now().strftime("%Y%m%d") + ".txt",
        mime="text/plain",
    )


if __name__ == "__main__":
    main()