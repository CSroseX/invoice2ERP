"""
app.py — invoice2ERP Read-Only Showcase.

Public, read-only Streamlit deployment. Renders pre-computed pipeline output
(output/*.json) for a curated set of documents. Does not run live processing —
the full pipeline (src/extractor.py, src/ocr_engine.py, etc.) is unaffected and
runnable locally, but this UI has no path that invokes it.

The Showcase also includes an OCR engine comparison read from the saved benchmark in
measurements/ocr_benchmark/ (src/ocr_comparison.py).

A second view, "Needs review" (sidebar, or ?view=review), lists every output item
that needs a human (src/review.py). Recording decisions is disabled unless the
INVOICE2ERP_REVIEW_EDIT=1 env var is set, so the public deployment stays read-only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.resolve()))

import pymupdf as fitz

from src import ocr_comparison, review

CURATED_FILES = [
    "DU-02.pdf",
    "INV-11.pdf",
    "DU-11.pdf",
    "INV-01.pdf",
    "DU-05s.pdf",
    "DU-08.pdf",
]


@st.cache_data(show_spinner=False)
def load_ocr_benchmark() -> list[ocr_comparison.EngineRun]:
    return ocr_comparison.load_runs()


# Translucent so the highlight reads in both light and dark themes.
_BEST = "background-color: rgba(40, 167, 69, 0.25); font-weight: 600"


def render_ocr_comparison() -> None:
    """Stage 1 evidence: how the benchmarked OCR engines compare on the saved 5-PDF benchmark."""
    runs = load_ocr_benchmark()
    if not runs:
        return
    st.markdown("## OCR Engine Comparison")
    st.caption(
        "Why Stage 1 uses EasyOCR: four engines benchmarked on five hard documents (a 12-page "
        "customs bundle, scans, multi-column tables, a utility bill). Results are read from "
        "`measurements/ocr_benchmark/`; nothing is re-run here."
    )

    rows = ocr_comparison.summary_rows(runs)
    numbers = pd.DataFrame(rows).set_index("Engine")
    time_cols = ["Median time per PDF (s)", "Slowest PDF (s)"]
    best = pd.DataFrame(False, index=numbers.index, columns=numbers.columns)
    for c in time_cols:
        best[c] = numbers[c] == numbers[c].min()
    best["Amounts read"] = numbers["Amounts read"] == numbers["Amounts read"].max()
    # Times are shown as text so the cached engine reads "cached" rather than an empty cell.
    shown = numbers.astype({c: object for c in time_cols})
    for c in time_cols:
        shown[c] = [("cached" if pd.isna(v) else f"{v:,.1f}") for v in numbers[c]]
    styled = shown.style.apply(lambda _: best.replace({True: _BEST, False: ""}), axis=None)
    st.dataframe(styled, use_container_width=True)
    st.caption(
        "Green marks the best value in each column. **Amounts read** counts distinct numbers with two "
        "decimals (e.g. 407.95) in each engine's text: an engine that drops a table reads fewer. "
        "**current** shows *cached* because the benchmark read its saved text instead of re-running "
        "OCR. Paddle's slowest PDF (HLD-10, ~13 h) looks like a stalled run, so compare its median. "
        "Field-level accuracy needs human-verified answers for each document (issue #10)."
    )

    with st.expander("Compare the extracted text for one document"):
        stems = list(runs[0].texts)
        stem = st.selectbox("Document", stems, key="ocr_doc")
        cols = st.columns(len(runs))
        for col, run in zip(cols, runs):
            with col:
                secs = run.seconds.get(stem)
                timing = "cached" if run.engine in ocr_comparison.CACHED_ENGINES else f"{secs:,.1f}s"
                st.markdown(f"**{run.engine}**")
                st.caption(f"{run.status.get(stem, 'missing')} · {timing} · "
                           f"{len(ocr_comparison.amounts_in(run.texts.get(stem, '')))} amounts")
                with st.container(height=420):
                    st.text(run.texts.get(stem, "") or "(no text)")


@st.cache_data(show_spinner=False)
def render_pdf_page_bytes(pdf_path: str, page_num: int) -> bytes:
    doc = fitz.open(pdf_path)
    page = doc.load_page(page_num)
    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
    img_bytes = pix.tobytes(output="png")
    doc.close()
    return img_bytes


@st.cache_data(show_spinner=False)
def get_pdf_page_count(pdf_path: str) -> int:
    doc = fitz.open(pdf_path)
    n = len(doc)
    doc.close()
    return n


@st.cache_data(show_spinner=False)
def load_autodraft_json_for(pdf_name: str) -> dict | None:
    p = Path("output") / f"{Path(pdf_name).stem}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def non_empty_items(d: dict) -> dict:
    """Filter a flat dict down to fields with a non-empty value, for a readable key/value view."""
    return {k: v for k, v in d.items() if str(v or "").strip()}


def render_payable(p: dict, show_gross: bool = False) -> None:
    """Header fields, line items and the full payload of one extracted payable."""
    header_fields = {
        "Invoice #": p.get("invoice_number"),
        "Type": p.get("invoice_type"),
        "Date": p.get("invoice_date"),
        "Currency": p.get("currency"),
        "Supplier": (p.get("supplier") or {}).get("name"),
        "Buyer Company Code": (p.get("buyer") or {}).get("company_code"),
    }
    if show_gross:
        header_fields |= {
            "Gross total": p.get("gross_total"),
            "Subtotal": p.get("subtotal"),
            "Total tax": p.get("total_tax_amount"),
        }
    for label, val in non_empty_items(header_fields).items():
        st.markdown(f"**{label}:** {val}")

    line_items = [li for li in (p.get("line_items") or []) if isinstance(li, dict)]
    if line_items:
        st.markdown(f"**Line Items ({len(line_items)}):**")
        display_rows = [non_empty_items(li) for li in line_items]
        st.dataframe(display_rows, use_container_width=True, hide_index=True)

    with st.expander("Full extracted payload"):
        st.json(non_empty_items(p) | {
            k: v for k, v in p.items()
            if isinstance(v, (list, dict)) and v
        })


@st.cache_data(show_spinner=False, ttl=60)
def load_review_items() -> list[review.ReviewItem]:
    return review.collect_review_items(Path("output"))


def render_review_page() -> None:
    """The "Needs review" view: every output item that needs a human, plus the decisions recorded so far."""
    st.markdown("# 🔎 Needs review")
    st.markdown(
        "Every document in `output/` with a failed extraction, a payable the pipeline flagged for "
        "review (`__review__`), or a payable the ERP oracle does not book at its printed gross."
    )

    items = load_review_items()
    counts = review.category_counts(items)
    count_cols = st.columns(len(review.CATEGORIES) + 1)
    count_cols[0].metric("Items", len(items))
    for col, (cat, label) in zip(count_cols[1:], review.CATEGORIES.items()):
        col.metric(label, counts[cat])

    if not items:
        st.success("Nothing needs review.")
        return

    decisions = review.latest_decisions()
    shown_cats = st.multiselect(
        "Categories", options=list(review.CATEGORIES), default=list(review.CATEGORIES),
        format_func=review.CATEGORIES.get, key="review_categories",
    )
    shown = [i for i in items if set(i.categories) & set(shown_cats)]
    st.dataframe(
        [
            {
                "File": i.file,
                "Item": i.label.split(" · ", 1)[1],
                "Category": ", ".join(review.CATEGORIES[c] for c in i.categories),
                "Reasons": "; ".join(i.reasons),
                "Latest decision": (decisions.get(i.key) or {}).get("decision", ""),
            }
            for i in shown
        ],
        use_container_width=True,
        hide_index=True,
    )
    if not shown:
        return

    by_key = {i.key: i for i in shown}
    item = by_key[st.selectbox("Item", list(by_key), format_func=lambda k: by_key[k].label, key="review_item")]
    st.markdown(f"**Category:** {', '.join(review.CATEGORIES[c] for c in item.categories)}")
    for reason in item.reasons:
        st.markdown(f"- {reason}")

    col_pdf, col_data = st.columns([1, 1])
    with col_pdf:
        pdf_path = Path("documents") / item.file
        if pdf_path.exists():
            page_count = get_pdf_page_count(str(pdf_path))
            page = 1
            if page_count > 1:
                page = st.number_input(
                    f"Page (of {page_count})", min_value=1, max_value=page_count, value=1,
                    key=f"review_page_{item.key}",
                )
            st.image(render_pdf_page_bytes(str(pdf_path), int(page) - 1), use_container_width=True)
        else:
            st.warning(f"`{item.file}` not found in `documents/`.")
    with col_data:
        if item.payable is not None:
            render_payable(item.payable, show_gross=True)
        else:
            st.markdown("**No payable was extracted from this segment.**")
            st.json(item.entry or {})

    st.markdown("### Decision")
    latest = decisions.get(item.key)
    if latest:
        note = f" — {latest['note']}" if latest.get("note") else ""
        st.markdown(f"**Latest:** {latest['decision']} ({latest.get('timestamp', '')}){note}")
    else:
        st.markdown("**Latest:** no decision recorded yet.")

    can_edit = review.review_edit_enabled()
    if not can_edit:
        st.caption(
            f"This is a read-only showcase, so decisions are disabled. To record them locally, "
            f"run the app with `{review.EDIT_ENV_VAR}=1`; they are appended to `review/decisions.jsonl`."
        )
    with st.form(key=f"decision_form_{item.key}", clear_on_submit=True):
        decision = st.radio("Decision", review.DECISIONS, horizontal=True, disabled=not can_edit)
        note = st.text_area("Note", disabled=not can_edit)
        submitted = st.form_submit_button("Record decision", disabled=not can_edit)
    if submitted and can_edit:
        review.record_decision(item, decision, note)
        st.rerun()


# ---------------------------------------------------------------------------
# Page Setup & Theme
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="invoice2ERP — Showcase",
    page_icon="🧾",
    layout="wide",
)

st.markdown("""
<style>
    .stApp {
        background-color: #0e1117;
        color: #e0e6ed;
        font-size: 19px;
    }
    html, body, [class*="css"] {
        font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    }
    p, li, span, label, div[data-testid="stMarkdownContainer"] {
        font-size: 19px !important;
        line-height: 1.6;
    }
    [data-testid="stCaptionContainer"] {
        font-size: 16px !important;
    }
    .banner {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-left: 3px solid #38bdf8;
        border-radius: 6px;
        padding: 14px 20px;
        font-size: 18px;
        color: #b8c2cf;
        margin-bottom: 18px;
    }
    .banner a { color: #38bdf8; }
    .stage-card {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-radius: 8px;
        padding: 14px 10px;
        text-align: center;
        min-height: 86px;
        display: flex;
        flex-direction: column;
        justify-content: center;
    }
    .stage-card.selected {
        border-color: #38bdf8;
        background-color: #0c4a6e33;
    }
    .stage-num {
        font-size: 13px;
        font-weight: 700;
        color: #7b8caa;
        letter-spacing: 0.5px;
    }
    .stage-name {
        font-size: 17px;
        font-weight: 600;
        color: #f8fafc;
        margin-top: 4px;
    }
    div[data-testid="stButton"] button {
        font-size: 16px;
        padding: 5px 12px;
        background-color: transparent;
        border: 1px solid #30363d;
        color: #b8c2cf;
    }
    div[data-testid="stButton"] button:hover {
        border-color: #38bdf8;
        color: #38bdf8;
    }
    .decision-card {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-radius: 8px;
        padding: 18px 20px;
        margin-bottom: 8px;
    }
    .decision-title {
        font-size: 18px;
        font-weight: 700;
        color: #38bdf8;
    }
    .decision-body {
        font-size: 17px;
        color: #dbe4ee;
        margin-top: 6px;
        line-height: 1.55;
    }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# VIEW SWITCH — "Needs review" renders its own page and stops here
# ---------------------------------------------------------------------------
VIEWS = ("Showcase", "Needs review")
if "view" not in st.session_state:
    st.session_state.view = VIEWS[1] if st.query_params.get("view") == "review" else VIEWS[0]
view = st.sidebar.radio("View", VIEWS, key="view")
if view == VIEWS[1]:
    st.query_params["view"] = "review"
    render_review_page()
    st.stop()
st.query_params.pop("view", None)

# ---------------------------------------------------------------------------
# HERO
# ---------------------------------------------------------------------------
st.markdown("# 🧾 invoice2ERP")
st.markdown(
    "Extracting text from a supplier invoice is easy; producing a record an ERP system can "
    "actually book is not — it requires exact itemized decomposition (line vs. header taxes, "
    "discounts, charges) with zero ungrounded values, since a single misplaced figure fails "
    "the ERP's own recomputation."
)
st.markdown(
    """
    <div class="banner">
        📖 This is a <strong>read-only showcase</strong> — no processing runs from this page.
        See the source and run the full pipeline locally:
        <a href="https://github.com/CSroseX/invoice2ERP" target="_blank">github.com/CSroseX/invoice2ERP</a>
    </div>
    """,
    unsafe_allow_html=True,
)

st.divider()

# ---------------------------------------------------------------------------
# PIPELINE FLOWCHART
# ---------------------------------------------------------------------------
st.markdown("## Pipeline")
st.caption("Seven stages, run per document. Click a stage for the design decision behind it.")

STAGES = [
    {
        "name": "OCR & Layout",
        "what": "Extracts text from each page. Native vector text is read directly from digital "
                "PDFs; scanned pages fall back to a 300 DPI render + OCR.",
        "why": "Native extraction is preferred over OCR wherever possible — it is exact, "
               "whereas OCR introduces character-level uncertainty that later stages have to "
               "account for.",
    },
    {
        "name": "Segmentation",
        "what": "Splits a multi-page PDF into individual sub-documents using page-restart "
                "markers and reference-number continuity.",
        "why": "A single PDF may bundle several unrelated invoices, or none at all — treating "
               "the whole file as one document would silently merge or miss payables.",
    },
    {
        "name": "Classifier",
        "what": "Rule-based scoring decides whether a segment is a bookable payable (invoice or "
                "credit memo) before it reaches the LLM.",
        "why": "Declined segments (quotes, delivery notes, reminders) never reach extraction — "
               "keeping non-payables out avoids the LLM inventing a payable where none exists.",
    },
    {
        "name": "AI Extraction",
        "what": "Routes OCR text to an LLM provider through a circuit-breaker cascade, cheapest "
               "model first (OpenRouter → Cloudflare → Groq → Gemini), to extract structured fields.",
        "why": "A cascade rather than a single provider means one API outage or exhausted quota "
               "doesn't stop the pipeline.",
    },
    {
        "name": "Grounding",
        "what": "Verifies every extracted field appears verbatim in the source OCR text.",
        "why": "Any field not found in the source text is blanked rather than guessed — "
               "preventing financial hallucination is worth more than a complete-looking record.",
    },
    {
        "name": "Master Match",
        "what": "Fuzzy-matches extracted supplier/buyer names and codes against local reference "
                "data (suppliers, tax codes, payment terms, PO numbers).",
        "why": "A confident but wrong master-data code is worse than a blank one — matches below "
               "an 85% similarity threshold are left unresolved rather than guessed.",
    },
    {
        "name": "ERP Oracle",
        "what": "Recomputes the gross total from raw line items, taxes, discounts, and charges, "
                "independent of any total the document prints.",
        "why": "The oracle is the actual booking logic downstream ERP systems run — matching it "
               "is what makes a record bookable, not just plausible-looking.",
    },
]

if "selected_stage" not in st.session_state:
    st.session_state.selected_stage = 0

cols = st.columns(7)
for i, (col, stage) in enumerate(zip(cols, STAGES)):
    with col:
        is_selected = st.session_state.selected_stage == i
        card_class = "stage-card selected" if is_selected else "stage-card"
        st.markdown(
            f'<div class="{card_class}"><div class="stage-num">STAGE {i + 1}</div>'
            f'<div class="stage-name">{stage["name"]}</div></div>',
            unsafe_allow_html=True,
        )
        if st.button("Details", key=f"stage_btn_{i}", use_container_width=True):
            st.session_state.selected_stage = i
            st.rerun()

active = STAGES[st.session_state.selected_stage]
st.markdown(
    f'<div class="decision-card" style="margin-top:10px;">'
    f'<div class="decision-title">Stage {st.session_state.selected_stage + 1}: {active["name"]}</div>'
    f'<div class="decision-body"><strong>What it does:</strong> {active["what"]}</div>'
    f'<div class="decision-body" style="margin-top:6px;"><strong>Why:</strong> {active["why"]}</div>'
    f'</div>',
    unsafe_allow_html=True,
)

st.divider()

# ---------------------------------------------------------------------------
# OCR ENGINE COMPARISON — evidence for Stage 1
# ---------------------------------------------------------------------------
render_ocr_comparison()

st.divider()

# ---------------------------------------------------------------------------
# PRODUCTION CHOICES
# ---------------------------------------------------------------------------
st.markdown("## Production Choices")

DECISIONS = [
    ("Async Task Queue", "Documents process concurrently via a thread pool rather than "
     "one at a time, so a burst of uploads doesn't block on a single slow document."),
    ("Circuit-Breaker LLM Routing", "Automatically fails over between LLM providers when a "
     "quota is exhausted or a provider is down, instead of stopping the batch."),
    ("Anti-Hallucination Grounding", "Every extracted field is checked against the raw OCR "
     "text; anything not found on the page is blanked instead of guessed."),
    ("Dead Letter Queue", "A document that raises a fatal error is caught and routed aside "
     "for manual review, instead of silently dropping it from the batch."),
    ("Immutable JSONL Audit Trail", "Every pipeline step is logged append-only, so processing "
     "history can be reconstructed for compliance review."),
    ("Token Cost Tracking", "Prompt/completion token usage is captured per document, making "
     "LLM cost visible per processing batch rather than only at the account level."),
    ("Master Data Fuzzy Resolution", "Supplier names, tax codes, and payment terms are matched "
     "against reference data with a similarity threshold, leaving no-match fields blank rather "
     "than guessing a code."),
]

d_cols = st.columns(2)
for i, (title, body) in enumerate(DECISIONS):
    with d_cols[i % 2]:
        st.markdown(
            f'<div class="decision-card"><div class="decision-title">{title}</div>'
            f'<div class="decision-body">{body}</div></div>',
            unsafe_allow_html=True,
        )

st.divider()

# ---------------------------------------------------------------------------
# SEE IT WORK — curated document viewer
# ---------------------------------------------------------------------------
st.markdown("## See It Work")
st.caption("Six documents, chosen to show different pipeline behavior. Page through and inspect the extracted output.")

if "page_num" not in st.session_state:
    st.session_state.page_num = {}
if "payable_idx" not in st.session_state:
    st.session_state.payable_idx = {}

selected_file = st.selectbox("Document", CURATED_FILES, key="curated_selected")

pdf_path = Path("documents") / selected_file
data = load_autodraft_json_for(selected_file)

col_pdf, col_data = st.columns([1, 1])

with col_pdf:
    if pdf_path.exists():
        page_count = get_pdf_page_count(str(pdf_path))
        current_page = st.session_state.page_num.get(selected_file, 0)
        current_page = max(0, min(current_page, page_count - 1))

        st.image(render_pdf_page_bytes(str(pdf_path), current_page), use_container_width=True)

        if page_count > 1:
            nav_prev, nav_label, nav_next = st.columns([1, 2, 1])
            with nav_prev:
                if st.button("← Prev", disabled=current_page == 0, key=f"prev_{selected_file}"):
                    st.session_state.page_num[selected_file] = current_page - 1
                    st.rerun()
            with nav_label:
                st.markdown(
                    f"<div style='text-align:center; padding-top:8px; color:#b8c2cf; font-size:17px;'>"
                    f"Page {current_page + 1} of {page_count}</div>",
                    unsafe_allow_html=True,
                )
            with nav_next:
                if st.button("Next →", disabled=current_page >= page_count - 1, key=f"next_{selected_file}"):
                    st.session_state.page_num[selected_file] = current_page + 1
                    st.rerun()
    else:
        st.warning(f"`{selected_file}` not found in `documents/`.")

with col_data:
    if data is None:
        st.info("No generated output for this document.")
    else:
        declined = data.get("declined", [])
        payables = data.get("payables", [])
        failed = data.get("failed", [])  # absent in output files written before #8

        if failed:
            st.warning(
                f"{len(failed)} segment(s) failed extraction (an LLM or processing error, "
                "not a classifier decision): "
                + "; ".join(f.get("reason", "No reason recorded.") for f in failed)
            )

        if declined and not payables:
            st.markdown("**Classifier decision: declined**")
            for d in declined:
                doc_type = d.get("doc_type", "Unknown")
                reason = d.get("reason", "No reason recorded.")
                st.markdown(f"- **Type:** `{doc_type}`")
                st.markdown(f"- **Reason:** {reason}")
        elif payables:
            if len(payables) > 1:
                idx = st.session_state.payable_idx.get(selected_file, 0)
                idx = max(0, min(idx, len(payables) - 1))
                idx = st.selectbox(
                    f"Payable ({len(payables)} segmented from this document)",
                    options=list(range(len(payables))),
                    index=idx,
                    format_func=lambda i: f"#{i + 1}",
                    key=f"payable_select_{selected_file}",
                )
                st.session_state.payable_idx[selected_file] = idx
            else:
                idx = 0

            render_payable(payables[idx])
        elif not failed:
            st.info("This document produced no payables and no decline record.")

st.divider()

# ---------------------------------------------------------------------------
# SUMMARY + RUN LOCALLY
# ---------------------------------------------------------------------------
st.markdown("## Run It Locally")
st.markdown(
    "This page demonstrates the pipeline's segmentation, extraction, and grounding behavior "
    "on pre-computed output. To run the full pipeline — OCR, LLM extraction, and ERP booking "
    "verification — against your own documents:"
)
st.code(
    "git clone https://github.com/CSroseX/invoice2ERP.git\n"
    "cd invoice2ERP\n"
    "python -m venv .venv && .venv\\Scripts\\activate  # Windows\n"
    "pip install -r requirements-full.txt\n"
    "cp .env.example .env  # add your LLM provider API key(s)\n"
    "streamlit run app.py",
    language="bash",
)
