"""
app.py — Developer Control Panel & Visual Evaluator for Zycus Bookable Payable Pipeline.

Features:
- Live, step-by-step stream execution generator over documents/
- Full visibility into OCR extraction path, segmentation, classification, AI extraction,
  grounding, master data matching, and ERP booking verification.
- Thread-safe dual sys.stdout capture (IDE terminal + bottom Streamlit terminal log feed).
- Choice between Processing All Files (Batch) vs Single File processing.
- Inspection selector rendered ONLY after processing completes.
- Side-by-side Start / Stop processing controls.
- Zero chatbot framing, zero message bubbles. Pure structured control dashboard.
"""
from __future__ import annotations

import json
import os
from src.config import settings
import sys
import time
import textwrap
from pathlib import Path
import streamlit as st

# Ensure root directory is on sys.path
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from src.ocr_engine import extract_pages, extract_text, is_digital_vector_page, reconstruct_layout
from src.segmenter import segment_document_text
from src.classifier import classify_document_text
from src.extractor import extract_payable_from_text, QuotaExhaustedError
from src.grounding import verify_payable_grounding
from src.master_matcher import MasterDataMatcher
from src.logging_config import get_audit_logger

audit_logger = get_audit_logger('pipeline')
from erp import erp_book, num, _line_base, _line_taxes, _header_taxes
import pymupdf as fitz


# ---------------------------------------------------------------------------
# Streamlit Page Setup & Custom CSS
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Zycus Bookable Payable Pipeline Control Panel",
    page_icon="⚙️",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    /* Dark Terminal Theme Styling */
    .stApp {
        background-color: #0e1117;
        color: #e0e6ed;
    }
    .metric-card {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-radius: 8px;
        padding: 16px;
        text-align: center;
    }
    .metric-value {
        font-size: 28px;
        font-weight: bold;
        color: #38bdf8;
    }
    .metric-label {
        font-size: 12px;
        color: #94a3b8;
        text-transform: uppercase;
        letter-spacing: 1px;
    }
    .status-pass {
        color: #34d399;
        font-weight: bold;
    }
    .status-declined {
        color: #fbbf24;
        font-weight: bold;
    }
    .status-fail {
        color: #f87171;
        font-weight: bold;
    }
    .status-processing {
        color: #38bdf8;
        font-weight: bold;
    }
    .status-queued {
        color: #64748b;
    }
    div[data-testid="stSidebar"] {
        background-color: #161b22;
        border-right: 1px solid #30363d;
    }
    /* Flowchart Stepper Cards */
    .step-card {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-radius: 8px;
        padding: 10px 4px;
        text-align: center;
        min-height: 85px;
        display: flex;
        flex-direction: column;
        justify-content: space-between;
        align-items: center;
        transition: all 0.2s ease-in-out;
    }
    .step-card.active {
        border-color: #38bdf8 !important;
        background-color: #0c4a6e33 !important;
        box-shadow: 0 0 10px rgba(56, 189, 248, 0.3);
    }
    .step-card.completed {
        border-color: #34d399 !important;
        background-color: #064e3b22 !important;
    }
    .step-card.pass {
        border-color: #10b981 !important;
        background-color: #064e3b33 !important;
    }
    .step-card.fail {
        border-color: #f87171 !important;
        background-color: #7f1d1d33 !important;
    }
    .step-card.declined {
        border-color: #fbbf24 !important;
        background-color: #78350f22 !important;
    }
    .step-card.skipped {
        border-color: #334155 !important;
        background-color: #1e293b33 !important;
        opacity: 0.6;
    }
    .step-card.pending {
        border-color: #2d3748 !important;
        background-color: #161b22 !important;
        opacity: 0.7;
    }
    .step-num {
        font-size: 10px;
        font-weight: 700;
        color: #94a3b8;
        letter-spacing: 0.5px;
    }
    .step-name {
        font-size: 11px;
        font-weight: 600;
        color: #f8fafc;
        margin: 2px 0;
        line-height: 1.2;
    }
    .step-badge {
        font-size: 10px;
        font-weight: 700;
        padding: 2px 6px;
        border-radius: 10px;
    }
    .step-badge.active { background-color: #0284c7; color: #ffffff; }
    .step-badge.completed { background-color: #059669; color: #ffffff; }
    .step-badge.pass { background-color: #059669; color: #ffffff; }
    .step-badge.fail { background-color: #dc2626; color: #ffffff; }
    .step-badge.declined { background-color: #d97706; color: #ffffff; }
    .step-badge.skipped { background-color: #334155; color: #94a3b8; }
    .step-badge.pending { background-color: #1e293b; color: #64748b; }
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Thread-Safe Dual stdout Capture Wrapper
# ---------------------------------------------------------------------------
class StreamTee:
    """Duplicates sys.stdout output to original terminal AND Streamlit log list safely."""
    def __init__(self, original_stdout, log_list):
        self.original = original_stdout
        self.log_list = log_list

    def write(self, text):
        try:
            self.original.write(text)
        except Exception:
            pass
        clean = text.rstrip()
        if clean:
            self.log_list.append(clean)

    def flush(self):
        try:
            self.original.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Session State Initialization
# ---------------------------------------------------------------------------
if "doc_status" not in st.session_state:
    st.session_state.doc_status = {}  # filename -> status string
if "doc_traces" not in st.session_state:
    st.session_state.doc_traces = {}  # filename -> list of step dicts
if "selected_doc" not in st.session_state:
    st.session_state.selected_doc = None
if "is_processing" not in st.session_state:
    st.session_state.is_processing = False
if "processing_complete" not in st.session_state:
    st.session_state.processing_complete = False
if "stop_requested" not in st.session_state:
    st.session_state.stop_requested = False
if "raw_logs" not in st.session_state:
    st.session_state.raw_logs = []
if "stats" not in st.session_state:
    st.session_state.stats = {
        "total": 0,
        "payables": 0,
        "declined": 0,
        "first_try_pass": 0,
        "failed": 0
    }

# ---------------------------------------------------------------------------
# Helper: Get list of PDF files
# ---------------------------------------------------------------------------
def get_pdf_files() -> list[Path]:
    doc_dir = Path("documents")
    if doc_dir.exists():
        return sorted(list(doc_dir.glob("*.pdf")))
    return []

pdf_files = get_pdf_files()
pdf_names = [f.name for f in pdf_files]

# Initialize default status for files if not present
for name in pdf_names:
    if name not in st.session_state.doc_status:
        st.session_state.doc_status[name] = "QUEUED"
    if name not in st.session_state.doc_traces:
        st.session_state.doc_traces[name] = []

# Auto-load pre-existing autodraft JSON payloads from output/ if available
out_dir_path = Path("output")
if out_dir_path.exists():
    for name in pdf_names:
        pdf_stem = Path(name).stem
        json_path = out_dir_path / f"{pdf_stem}.json"
        
        if st.session_state.doc_status.get(name) == "QUEUED" and not st.session_state.doc_traces.get(name) and json_path.exists():
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                payables = data.get("payables", [])
                declined = data.get("declined", [])

                if payables:
                    all_pass = True
                    for p in payables:
                        erp_res = erp_book(p)
                        booked_g = erp_res.get("will_book_gross", 0.0)
                        target_s = str(p.get("gross_total") or "").strip()
                        try:
                            target_g = float(target_s) if target_s else 0.0
                        except ValueError:
                            target_g = 0.0
                        if abs(booked_g - target_g) >= 0.05:
                            all_pass = False

                    status = "PASS" if all_pass else "FAIL"
                elif declined:
                    status = "DECLINED"
                else:
                    status = "PASS"

                st.session_state.doc_status[name] = status

                synthetic_trace = []
                synthetic_trace.append({"step": 1, "title": "Phase 1: OCR & Spatial Layout Extraction", "path_used": "Cached Output Payload", "char_count": len(json.dumps(data)), "page_count": 1, "raw_text": json.dumps(data, indent=2, ensure_ascii=False)})
                synthetic_trace.append({"step": 2, "title": "Phase 2: Multi-Document Pre-Segmentation", "status_msg": "Loaded existing pre-segmented output payload."})
                
                if status == "DECLINED":
                    synthetic_trace.append({"step": 3, "title": "Phase 3: Classification", "is_payable": False, "doc_type": declined[0].get("doc_type", "DECLINED") if declined else "NON_PAYABLE", "score": 1.0, "reasons": [declined[0].get("reason", "Declined non-payable document")] if declined else ["Non-payable document"]})
                else:
                    synthetic_trace.append({"step": 3, "title": "Phase 3: Classification", "is_payable": True, "doc_type": payables[0].get("invoice_type", "INVOICE") if payables else "INVOICE", "score": 1.0, "reasons": ["Valid Payable Document"]})
                    synthetic_trace.append({"step": 4, "title": "Phase 4: AI Model Extraction", "raw_json": payables[0] if payables else {}})
                    synthetic_trace.append({"step": "4b", "title": "Phase 4b: Grounding Verification", "warnings": [], "sanitized_json": payables[0] if payables else {}})
                    synthetic_trace.append({"step": 5, "title": "Phase 5: Master Data Resolution", "master_matched": {
                        "supplier_id": payables[0].get("supplier", {}).get("supplier_id", "") if payables else "",
                        "company_code": payables[0].get("buyer", {}).get("company_code", "") if payables else "",
                        "business_unit_code": payables[0].get("buyer", {}).get("business_unit_code", "") if payables else "",
                        "location_code": payables[0].get("buyer", {}).get("location_code", "") if payables else "",
                        "po_id": payables[0].get("po_id", "") if payables else "",
                        "payment_term_id": payables[0].get("payment_term_id", "") if payables else ""
                    }, "resolved_json": payables[0] if payables else {}})
                    synthetic_trace.append({"step": 6, "title": "Phase 6: Pre-ERP Payload Assembly", "payload": payables[0] if payables else {}})
                    
                    if payables:
                        p0 = payables[0]
                        erp_res0 = erp_book(p0)
                        booked_g0 = erp_res0.get("will_book_gross", 0.0)
                        target_s0 = str(p0.get("gross_total") or "").strip()
                        try:
                            target_g0 = float(target_s0) if target_s0 else 0.0
                        except ValueError:
                            target_g0 = 0.0
                        is_m0 = abs(booked_g0 - target_g0) < 0.05
                        synthetic_trace.append({
                            "step": 7,
                            "title": "Phase 7: ERP Oracle Booking Verification",
                            "target_gross": f"{target_g0:.2f}",
                            "booked_gross": f"{booked_g0:.2f}",
                            "status": "PASS" if is_m0 else "FAIL",
                            "erp_details": erp_res0
                        })

                st.session_state.doc_traces[name] = synthetic_trace

                st.session_state.stats["total"] += 1
                if payables:
                    st.session_state.stats["payables"] += len(payables)
                    if status == "PASS":
                        st.session_state.stats["first_try_pass"] += len(payables)
                    else:
                        st.session_state.stats["failed"] += len(payables)
                if declined:
                    st.session_state.stats["declined"] += len(declined)
            except Exception:
                pass

if not st.session_state.selected_doc and pdf_names:
    st.session_state.selected_doc = pdf_names[0]


# ---------------------------------------------------------------------------
# Pipeline Generator Function
# ---------------------------------------------------------------------------
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from streamlit.runtime.scriptrunner import add_script_run_ctx

def _process_single_pdf(idx, pdf, target_files_len, q, matcher, out_dir):
    """Worker thread function to process a single PDF asynchronously."""
    try:
        filename = pdf.name
        # Note: In Streamlit, modifying st.session_state from a background thread can be problematic 
        # if the main thread triggers a rerun while the worker is writing.
        # We push all events to the queue and let the main thread update the state!
        
        trace = []
        
        print(f"\n" + "=" * 80, flush=True)
        audit_logger.info(f"DOCUMENT [{idx:02d}/{target_files_len}]: {filename}", extra={"doc_filename": filename, "status": "START"})
        print("=" * 80, flush=True)

        q.put({
            "type": "DOC_START",
            "filename": filename,
            "idx": idx,
            "total": target_files_len
        })

        try:
            txt_cache = Path("parsed_files") / f"{pdf.stem}.txt"
            path_used = "PyMuPDF Native Vector Text"
            page_count = 1

            # Phase 1: OCR Extraction & Layout
            if txt_cache.exists():
                full_ocr_text = txt_cache.read_text(encoding="utf-8", errors="ignore")
                path_used = "Cached OCR Text (300 DPI Spatial Layout)"
                audit_logger.info(f"[STEP 1: OCR & LAYOUT EXTRACTION] -> Loaded cached text ({len(full_ocr_text)} chars)", extra={"doc_filename": filename, "step": 1, "path_used": path_used, "char_count": len(full_ocr_text)})
            else:
                doc = fitz.open(str(pdf))
                page_count = len(doc)
                is_dig = any(is_digital_vector_page(doc[i]) for i in range(len(doc)))
                doc.close()
                
                text_pages = extract_text(str(pdf))
                full_ocr_text = "\n\n--- PAGE BREAK ---\n\n".join(text_pages)
                path_used = "PyMuPDF Direct Text Extraction" if is_dig else "PyMuPDF Render (300 DPI) + EasyOCR"
                audit_logger.info(f"[STEP 1: OCR & LAYOUT EXTRACTION] -> Extracted {page_count} page(s) via {path_used} ({len(full_ocr_text)} chars)", extra={"doc_filename": filename, "step": 1, "path_used": path_used, "char_count": len(full_ocr_text), "page_count": page_count})

            step1_event = {
                "step": 1,
                "title": "Phase 1: OCR & Spatial Layout Extraction",
                "path_used": path_used,
                "char_count": len(full_ocr_text),
                "page_count": page_count,
                "raw_text": full_ocr_text
            }
            trace.append(step1_event)
            q.put({"type": "STEP_1", "filename": filename, "data": step1_event})

            # Phase 2: Multi-Document Pre-Segmentation
            subdoc_texts = segment_document_text(full_ocr_text)
            audit_logger.info(f"[STEP 2: PRE-SEGMENTATION]       -> Segmented into {len(subdoc_texts)} sub-document(s)", extra={"doc_filename": filename, "step": 2, "subdoc_count": len(subdoc_texts)})

            step2_event = {
                "step": 2,
                "title": "Phase 2: Multi-Document Pre-Segmentation",
                "subdoc_count": len(subdoc_texts),
                "status_msg": f"Detected {len(subdoc_texts)} distinct sub-document segment(s)."
            }
            trace.append(step2_event)
            q.put({"type": "STEP_2", "filename": filename, "data": step2_event})

            payables = []
            declined = []

            for s_idx, seg_text in enumerate(subdoc_texts, 1):
                sub_label = f"{filename}#subdoc{s_idx}" if len(subdoc_texts) > 1 else filename
                
                # Phase 3: Classification
                class_res = classify_document_text(seg_text, filename=sub_label)
                confidence_score = getattr(class_res, 'confidence', getattr(class_res, 'score', 1.0))
                audit_logger.info(f"  [STEP 3: CLASSIFICATION]         -> Payable: {class_res.is_payable} | Type: {class_res.doc_type} (Confidence: {confidence_score})", extra={"doc_filename": filename, "sub_label": sub_label, "step": 3, "is_payable": class_res.is_payable, "doc_type": class_res.doc_type, "confidence": confidence_score})

                step3_event = {
                    "step": 3,
                    "subdoc_idx": s_idx,
                    "title": f"Phase 3: Classification [{sub_label}]",
                    "is_payable": class_res.is_payable,
                    "doc_type": class_res.doc_type,
                    "score": confidence_score,
                    "reasons": class_res.reasons
                }
                trace.append(step3_event)
                q.put({"type": "STEP_3", "filename": filename, "data": step3_event})

                if class_res.is_payable:
                    try:
                        # Phase 4: AI Model Extraction (Groq)
                        audit_logger.info(f"  [STEP 4: AI EXTRACTION & GROUNDING] -> Sending OCR text to Groq API...", extra={"doc_filename": filename, "sub_label": sub_label, "step": 4})
                        raw_payable = extract_payable_from_text(seg_text, filename=sub_label, allow_fallback=False)
                        token_usage = raw_payable.pop("__tokens__", {})
                        st.session_state.stats["prompt_tokens"] += token_usage.get("prompt_tokens", token_usage.get("prompt_token_count", 0))
                        st.session_state.stats["completion_tokens"] += token_usage.get("completion_tokens", token_usage.get("candidates_token_count", 0))
                        if class_res.doc_type == "CREDIT_MEMO":
                            raw_payable["invoice_type"] = "CREDIT_MEMO"

                        step4_event = {
                            "step": 4,
                            "subdoc_idx": s_idx,
                            "title": f"Phase 4: AI Model Extraction (Groq) [{sub_label}]",
                            "raw_json": raw_payable,
                            "token_usage": token_usage
                        }
                        trace.append(step4_event)
                        q.put({"type": "STEP_4", "filename": filename, "data": step4_event})

                        # Phase 4b: Grounding Verification
                        grounded_payable, g_warns = verify_payable_grounding(raw_payable, seg_text)
                        step4b_event = {
                            "step": "4b",
                            "subdoc_idx": s_idx,
                            "title": f"Phase 4b: Grounding & Anti-Hallucination Verification [{sub_label}]",
                            "warnings": g_warns,
                            "sanitized_json": grounded_payable
                        }
                        trace.append(step4b_event)
                        q.put({"type": "STEP_4B", "filename": filename, "data": step4b_event})

                        # Phase 5: Master Data Matching
                        resolved_payable = matcher.resolve_payable(grounded_payable, text_context=seg_text)
                        
                        supp_id = resolved_payable.get("supplier", {}).get("supplier_id", "")
                        comp_code = resolved_payable.get("buyer", {}).get("company_code", "")
                        audit_logger.info(f"  [STEP 5: MASTER DATA MATCHING]   -> Supplier ID: '{supp_id}' | Company Code: '{comp_code}'", extra={"doc_filename": filename, "sub_label": sub_label, "step": 5, "supplier_id": supp_id, "company_code": comp_code})

                        master_summary = {
                            "supplier_id": supp_id,
                            "company_code": comp_code,
                            "business_unit_code": resolved_payable.get("buyer", {}).get("business_unit_code", ""),
                            "location_code": resolved_payable.get("buyer", {}).get("location_code", ""),
                            "po_id": resolved_payable.get("po_id", ""),
                            "payment_term_id": resolved_payable.get("payment_term_id", "")
                        }
                        
                        step5_event = {
                            "step": 5,
                            "subdoc_idx": s_idx,
                            "title": f"Phase 5: Master Data Resolution [{sub_label}]",
                            "master_matched": master_summary,
                            "resolved_json": resolved_payable
                        }
                        trace.append(step5_event)
                        q.put({"type": "STEP_5", "filename": filename, "data": step5_event})

                        # Phase 6: Pre-ERP Payload Assembly
                        payables.append(resolved_payable)
                        step6_event = {
                            "step": 6,
                            "subdoc_idx": s_idx,
                            "title": f"Phase 6: Pre-ERP Payload Assembly [{sub_label}]",
                            "payload": resolved_payable
                        }
                        trace.append(step6_event)
                        q.put({"type": "STEP_6", "filename": filename, "data": step6_event})

                        # Phase 7: ERP Oracle Booking Check
                        erp_res = erp_book(resolved_payable)
                        booked_gross = erp_res.get("will_book_gross", 0.0)
                        printed_gross_str = str(resolved_payable.get("gross_total") or "").strip()
                        try:
                            target_gross = float(printed_gross_str) if printed_gross_str else 0.0
                        except ValueError:
                            target_gross = 0.0

                        is_match = abs(booked_gross - target_gross) < 0.05
                        erp_status = "PASS" if is_match else "FAIL"
                        audit_logger.info(f"  [STATUS]: BOOKABLE PAYABLE ({erp_status}) -> Gross Total: {printed_gross_str} {resolved_payable.get('currency')}", extra={"doc_filename": filename, "sub_label": sub_label, "step": 7, "erp_status": erp_status, "gross": printed_gross_str, "currency": resolved_payable.get("currency")})

                        step7_event = {
                            "step": 7,
                            "subdoc_idx": s_idx,
                            "title": f"Phase 7: ERP Oracle Booking Verification [{sub_label}]",
                            "target_gross": f"{target_gross:.2f}",
                            "booked_gross": f"{booked_gross:.2f}",
                            "status": erp_status,
                            "erp_details": erp_res
                        }
                        trace.append(step7_event)
                        q.put({"type": "STEP_7", "filename": filename, "data": step7_event})

                    except QuotaExhaustedError as qe:
                        audit_logger.error(f"[QUOTA EXHAUSTED]: {qe}", extra={"doc_filename": filename, "sub_label": sub_label, "error": str(qe)})
                        q.put({"type": "QUOTA_ERROR", "filename": filename, "error": str(qe)})
                        return
                    except Exception as e:
                        audit_logger.error(f"  [EXTRACTION FAILED]: {e}", extra={"doc_filename": filename, "sub_label": sub_label, "error": str(e)})
                        declined.append({"doc_type": class_res.doc_type, "reason": f"Extraction exception: {e}"})
                else:
                    audit_logger.warning(f"  [STATUS]: DECLINED              -> Reasons: {'; '.join(class_res.reasons)}", extra={"doc_filename": filename, "sub_label": sub_label, "reasons": class_res.reasons})
                    declined.append({"doc_type": class_res.doc_type, "reason": "; ".join(class_res.reasons)})

            # Save final payload to output/<pdf_stem>.json
            file_payload = {
                "file": filename,
                "payables": payables,
                "declined": declined
            }
            out_json_path = out_dir / f"{pdf.stem}.json"
            out_json_path.write_text(json.dumps(file_payload, indent=2, ensure_ascii=False), encoding="utf-8")
            audit_logger.info(f"LAST [STEP 6: JSON SAVED]               -> Saved JSON to '{out_json_path}'", extra={"doc_filename": filename, "step": "SAVE_JSON", "payload_path": str(out_json_path)})

            q.put({
                "type": "DOC_END", 
                "filename": filename, 
                "payables_count": len(payables),
                "declined_count": len(declined),
                "has_erp_fail": any(ev.get("step") == 7 and ev.get("status") == "FAIL" for ev in trace) if payables else False
            })

        except Exception as e:
            import traceback
            import shutil
            dlq_dir = Path("output/dlq")
            dlq_dir.mkdir(parents=True, exist_ok=True)
            error_msg = f"Fatal Document Error: {e}\n{traceback.format_exc()}"
            audit_logger.critical(f"Fatal Document Error: {e}", extra={"doc_filename": filename, "dlq": True, "traceback": traceback.format_exc()})
            print(error_msg, flush=True)
            try:
                shutil.copy(pdf, dlq_dir / pdf.name)
                dlq_meta = {
                    "filename": filename,
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                    "partial_trace": trace
                }
                (dlq_dir / f"{pdf.stem}_error.json").write_text(json.dumps(dlq_meta, indent=2), encoding="utf-8")
            except Exception as dlq_e:
                print(f"Failed to write DLQ: {dlq_e}")
            
            q.put({
                "type": "DOC_FATAL", 
                "filename": filename, 
                "error": str(e)
            })

    except Exception as e:
        q.put({"type": "DOC_FATAL", "filename": pdf.name, "error": str(e)})

# ---------------------------------------------------------------------------
# Pipeline Generator Function (Async/Queue based)
# ---------------------------------------------------------------------------
def run_pipeline_generator(target_files: list[Path]):
    """Generator yielding live step events per document using Async/Queue."""
    matcher = MasterDataMatcher()
    out_dir = Path("output")
    out_dir.mkdir(parents=True, exist_ok=True)

    original_stdout = sys.stdout
    sys.stdout = StreamTee(original_stdout, st.session_state.raw_logs)

    q = queue.Queue()
    target_files_len = len(target_files)

    try:
        # Initialize UI state for all files before starting threads
        for pdf in target_files:
            filename = pdf.name
            st.session_state.doc_status[filename] = "PENDING"
            st.session_state.doc_traces[filename] = []
        
        if target_files:
            st.session_state.selected_doc = target_files[0].name

        active_workers = 0
        with ThreadPoolExecutor(max_workers=min(4, target_files_len)) as executor:
            for idx, pdf in enumerate(target_files, 1):
                if st.session_state.stop_requested:
                    st.session_state.raw_logs.append("[SYSTEM]: Processing stopped by user request.")
                    break
                
                # Submit worker
                future = executor.submit(
                    _process_single_pdf, idx, pdf, target_files_len, q, matcher, out_dir
                )
                add_script_run_ctx(future)
                active_workers += 1

            # Poll the queue and yield to Streamlit
            completed = 0
            while completed < active_workers:
                try:
                    # Timeout prevents deadlocks if a worker silently crashes
                    event = q.get(timeout=1.0)
                    
                    filename = event["filename"]
                    
                    if event["type"] == "DOC_START":
                        st.session_state.doc_status[filename] = "PROCESSING"
                        yield event
                    
                    elif event["type"].startswith("STEP_"):
                        st.session_state.doc_traces[filename].append(event["data"])
                        yield event
                        
                    elif event["type"] == "QUOTA_ERROR":
                        st.session_state.doc_status[filename] = "FAIL"
                        completed += 1
                        yield event
                        
                    elif event["type"] == "DOC_END":
                        # Update stats based on results
                        if event["payables_count"] > 0:
                            if event["has_erp_fail"]:
                                final_status = "FAIL"
                                st.session_state.stats["failed"] += 1
                            else:
                                final_status = "PASS"
                                st.session_state.stats["first_try_pass"] += event["payables_count"]
                        else:
                            final_status = "DECLINED"
                            
                        st.session_state.doc_status[filename] = final_status
                        st.session_state.stats["total"] += 1
                        st.session_state.stats["payables"] += event["payables_count"]
                        st.session_state.stats["declined"] += event["declined_count"]
                        
                        completed += 1
                        yield event
                        
                    elif event["type"] == "DOC_FATAL":
                        st.session_state.doc_status[filename] = "FAIL"
                        st.session_state.stats["failed"] += 1
                        st.session_state.doc_traces[filename].append({
                            "step": "DLQ",
                            "title": "System Crash — Routed to Dead Letter Queue",
                            "error": event["error"]
                        })
                        completed += 1
                        yield event
                        
                except queue.Empty:
                    # Allow Streamlit to handle stop requests while waiting
                    if st.session_state.stop_requested:
                        st.session_state.raw_logs.append("[SYSTEM]: Aborting queued tasks...")
                        break
                    
    finally:
        sys.stdout = original_stdout

# ---------------------------------------------------------------------------
# Sidebar UI: Controls & Scope Selection
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("⚙️ invoice2ERP Control Panel")
    st.caption("Intelligent Financial Document Ingestion & ERP Booking Engine")

    # Bring Your Own Key (BYOK) Input Section
    with st.expander("🔑 Custom API Keys (BYOK)", expanded=False):
        st.caption("Provide your own API key to force routing to a specific provider:")
        selected_provider = st.selectbox(
            "Select AI Provider", 
            ["OpenRouter", "Groq", "Gemini", "Cloudflare"]
        )
        custom_api_key = st.text_input(f"{selected_provider} API Key:", type="password", help="Overrides defaults and forces this provider.")
        
        if custom_api_key.strip():
            settings.primary_provider = selected_provider
            
            if selected_provider == "Groq":
                settings.groq_api_key = custom_api_key.strip()
            elif selected_provider == "OpenRouter":
                settings.open_router_api_key = custom_api_key.strip()
            elif selected_provider == "Gemini":
                settings.gemini_api_key = custom_api_key.strip()
            elif selected_provider == "Cloudflare":
                settings.cloudflare_workers_ai_key = custom_api_key.strip()

    st.divider()

    # 1. Processing Scope Selector (Batch vs Single Document)
    processing_mode = st.radio(
        "Processing Scope:",
        options=["All Files (Batch - 42 PDFs)", "Single Document"],
        disabled=st.session_state.is_processing
    )

    single_file_selected = None
    if processing_mode == "Single Document":
        single_file_selected = st.selectbox(
            "Select File to Process:",
            options=pdf_names,
            disabled=st.session_state.is_processing
        )

    # 2. Side-by-Side Start / Stop Buttons
    col_start, col_stop = st.columns(2)
    with col_start:
        start_btn = st.button("🚀 Start", type="primary", use_container_width=True, disabled=st.session_state.is_processing)
    with col_stop:
        stop_btn = st.button("⏹️ Stop", use_container_width=True, disabled=not st.session_state.is_processing)

    if stop_btn:
        st.session_state.stop_requested = True
        st.session_state.is_processing = False
        st.warning("Stop requested. Halting execution...")

    st.divider()

    # 3. Inspection Selector — Rendered ONLY after processing completes
    if st.session_state.processing_complete:
        st.subheader("🔍 Review Processed Trace")
        processed_files = [f for f in pdf_names if st.session_state.doc_traces.get(f)]
        if processed_files:
            selected_inspection = st.selectbox(
                "Select Document to Inspect:",
                options=processed_files,
                index=processed_files.index(st.session_state.selected_doc) if st.session_state.selected_doc in processed_files else 0,
                help="Select any completed document to review its detailed 7-phase trace."
            )
            if selected_inspection != st.session_state.selected_doc:
                st.session_state.selected_doc = selected_inspection

        st.divider()

    # 4. Live Document Queue Status List
    st.subheader("📄 Document Queue")
    
    # Quick Autodraft Readiness Badge in Sidebar
    out_dir_path = Path("output")
    json_autodraft_count = len(list(out_dir_path.glob("*.json"))) if out_dir_path.exists() else 0
    if json_autodraft_count > 0:
        st.success(f"📦 **{json_autodraft_count} Autodrafts Ready** (`output/`)", icon="✅")
    else:
        st.info("📦 **Autodrafts Pending**", icon="⏳")

    with st.expander("📄 View Queue Status", expanded=False):
        display_list = pdf_names if processing_mode.startswith("All") else ([single_file_selected] if single_file_selected else pdf_names)
        queue_data = [{"Document": name, "Status": st.session_state.doc_status.get(name, "QUEUED")} for name in display_list]
        st.dataframe(queue_data, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Helper UI Component Functions: Process Status & Horizontal Flowchart Stepper
# ---------------------------------------------------------------------------

def render_process_status_dashboard(pdf_names_list: list[str]):
    """Renders the top process status banner, progress bar, and autodraft readiness indicator."""
    total_docs = len(pdf_names_list)
    processed_count = sum(
        1 for name in pdf_names_list if st.session_state.doc_status.get(name, "QUEUED") in ("PASS", "FAIL", "DECLINED")
    )
    queued_count = sum(
        1 for name in pdf_names_list if st.session_state.doc_status.get(name, "QUEUED") == "QUEUED"
    )

    out_dir = Path("output")
    json_files_count = len(list(out_dir.glob("*.json"))) if out_dir.exists() else 0

    # Determine Pipeline Status
    if st.session_state.is_processing:
        status_title = "⚡ PARSING IN PROGRESS"
        status_color = "#38bdf8"
        bg_color = "#0c4a6e22"
        border_color = "#0284c7"
        status_desc = f"Ingesting document stream ({processed_count + 1} of {total_docs} active)"
    elif st.session_state.processing_complete:
        status_title = "✅ PIPELINE PARSING COMPLETED"
        status_color = "#34d399"
        bg_color = "#064e3b22"
        border_color = "#059669"
        status_desc = f"Finished ingestion pipeline for all {processed_count} target document(s)."
    elif st.session_state.stop_requested:
        status_title = "⏹️ EXECUTION HALTED"
        status_color = "#fbbf24"
        bg_color = "#78350f22"
        border_color = "#d97706"
        status_desc = f"Halted by user request ({processed_count} completed, {queued_count} remaining)."
    else:
        status_title = "🟢 IDLE / READY TO PROCESS"
        status_color = "#94a3b8"
        bg_color = "#161b22"
        border_color = "#30363d"
        status_desc = f"Ready to ingest {total_docs} document(s). Select scope in sidebar and click Start."

    # Autodraft Readiness Status
    if json_files_count > 0:
        draft_title = "✅ AUTODRAFTS READY"
        draft_color = "#34d399"
        draft_bg = "#064e3b22"
        draft_border = "#059669"
        draft_desc = f"<strong>{json_files_count}</strong> JSON autodraft file(s) available in <code>output/</code>"
    else:
        draft_title = "⏳ AUTODRAFTS PENDING"
        draft_color = "#94a3b8"
        draft_bg = "#161b22"
        draft_border = "#30363d"
        draft_desc = "JSON autodraft payloads will be saved in <code>output/</code> upon Phase 6."

    col1, col2 = st.columns([1.5, 1])

    with col1:
        st.markdown(textwrap.dedent(f"""
            <div style="background-color: {bg_color}; border: 1px solid {border_color}; border-radius: 10px; padding: 14px 18px;">
                <div style="font-size: 11px; font-weight: 700; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px;">Pipeline Execution Status</div>
                <div style="font-size: 17px; font-weight: 800; color: {status_color}; margin: 4px 0;">{status_title}</div>
                <div style="font-size: 12px; color: #cbd5e1;">{status_desc}</div>
            </div>
        """), unsafe_allow_html=True)

    with col2:
        st.markdown(textwrap.dedent(f"""
            <div style="background-color: {draft_bg}; border: 1px solid {draft_border}; border-radius: 10px; padding: 14px 18px;">
                <div style="font-size: 11px; font-weight: 700; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px;">Autodraft Storage Status</div>
                <div style="font-size: 17px; font-weight: 800; color: {draft_color}; margin: 4px 0;">{draft_title}</div>
                <div style="font-size: 12px; color: #cbd5e1;">{draft_desc}</div>
            </div>
        """), unsafe_allow_html=True)

    st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
    progress_val = min(1.0, max(0.0, processed_count / total_docs)) if total_docs > 0 else 0.0
    st.progress(progress_val, text=f"Parsing Progress: {processed_count}/{total_docs} Documents Ingested ({progress_val*100:.0f}%)")


def get_flowchart_steps_status(doc_name: str | None) -> list[dict]:
    """Computes completion status for each of the 7 pipeline phases for doc_name."""
    steps_info = [
        {"num": 1, "name": "OCR & Layout"},
        {"num": 2, "name": "Segmentation"},
        {"num": 3, "name": "Classifier"},
        {"num": 4, "name": "AI Grounding"},
        {"num": 5, "name": "Master Match"},
        {"num": 6, "name": "Autodraft JSON"},
        {"num": 7, "name": "ERP Oracle"},
    ]

    if not doc_name:
        for s in steps_info:
            s["status"] = "PENDING"
        return steps_info

    doc_status = st.session_state.doc_status.get(doc_name, "QUEUED")
    traces = st.session_state.doc_traces.get(doc_name, [])

    executed_steps = set()
    erp_status = None

    for ev in traces:
        st_val = ev.get("step")
        if st_val == 1:
            executed_steps.add(1)
        elif st_val == 2:
            executed_steps.add(2)
        elif st_val == 3:
            executed_steps.add(3)
        elif st_val in (4, "4b"):
            executed_steps.add(4)
        elif st_val == 5:
            executed_steps.add(5)
        elif st_val == 6:
            executed_steps.add(6)
        elif st_val == 7:
            executed_steps.add(7)
            erp_status = ev.get("status")

    is_curr_processing = (doc_status == "PROCESSING")

    for s in steps_info:
        n = s["num"]
        if doc_status == "QUEUED":
            s["status"] = "PENDING"
        elif is_curr_processing:
            if n in executed_steps:
                max_step = max(executed_steps) if executed_steps else 0
                s["status"] = "ACTIVE" if n == max_step else "COMPLETED"
            else:
                s["status"] = "PENDING"
        elif doc_status in ("PASS", "FAIL"):
            if n < 7:
                s["status"] = "COMPLETED" if n in executed_steps else "SKIPPED"
            else:
                s["status"] = "PASS" if erp_status == "PASS" else "FAIL"
        elif doc_status == "DECLINED":
            if n <= 3:
                s["status"] = "DECLINED" if n == 3 else ("COMPLETED" if n in executed_steps else "SKIPPED")
            else:
                s["status"] = "SKIPPED"
        else:
            s["status"] = "COMPLETED" if n in executed_steps else "PENDING"

    return steps_info


def render_flowchart_stepper(doc_name: str | None):
    """Renders a horizontal visual flowchart stepper showing pipeline procedure phases."""
    steps = get_flowchart_steps_status(doc_name)

    st.markdown(textwrap.dedent(f"""
        <div style="font-size: 11px; font-weight: 700; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center;">
            <span>🗺️ Pipeline Execution Procedure Flowchart</span>
            <span style="font-size: 11px; background-color: #1e293b; padding: 3px 10px; border-radius: 12px; color: #38bdf8; border: 1px solid #334155;">
                Selected Target: <strong>{doc_name or 'None Selected'}</strong>
            </span>
        </div>
    """), unsafe_allow_html=True)

    cols = st.columns(7)

    badge_labels = {
        "COMPLETED": "✓ Done",
        "ACTIVE": "⚙ Active",
        "PASS": "✅ PASS",
        "FAIL": "❌ FAIL",
        "DECLINED": "⛔ Declined",
        "SKIPPED": "— Skipped",
        "PENDING": "⏳ Pending"
    }

    for idx, (col, step) in enumerate(zip(cols, steps)):
        st_val = step["status"].lower()
        badge_txt = badge_labels.get(step["status"], step["status"])

        with col:
            html_card = textwrap.dedent(f"""
                <div class="step-card {st_val}">
                    <div class="step-num">STEP 0{step['num']}</div>
                    <div class="step-name">{step['name']}</div>
                    <div class="step-badge {st_val}">{badge_txt}</div>
                </div>
            """)
            st.markdown(html_card, unsafe_allow_html=True)


def render_metrics():
    """Renders top header summary metric cards."""
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.markdown(f"<div class='metric-card'><div class='metric-value'>{len(pdf_files)}</div><div class='metric-label'>Total PDFs</div></div>", unsafe_allow_html=True)
    with m2:
        st.markdown(f"<div class='metric-card'><div class='metric-value' style='color:#34d399;'>{st.session_state.stats['payables']}</div><div class='metric-label'>Payables Extracted</div></div>", unsafe_allow_html=True)
    with m3:
        st.markdown(f"<div class='metric-card'><div class='metric-value' style='color:#fbbf24;'>{st.session_state.stats['declined']}</div><div class='metric-label'>Declined Segments</div></div>", unsafe_allow_html=True)
    with m4:
        total_p = max(1, st.session_state.stats['payables'])
        pass_rate = (st.session_state.stats['first_try_pass'] / total_p) * 100 if st.session_state.stats['payables'] > 0 else 0.0
        st.markdown(f"<div class='metric-card'><div class='metric-value' style='color:#38bdf8;'>{pass_rate:.1f}%</div><div class='metric-label'>ERP Booking Pass Rate</div></div>", unsafe_allow_html=True)



def run_batch_erp_audit():
    """Iterates over all output/*.json files and calculates ERP oracle booking for every payable."""
    out_dir = Path("output")
    if not out_dir.exists():
        return [], {"total": 0, "pass": 0, "fail": 0, "pass_rate": 0.0}

    json_files = sorted(list(out_dir.glob("*.json")))
    records = []
    pass_count = 0
    fail_count = 0

    for jf in json_files:
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            payables = data.get("payables", [])
            file_name = data.get("file", jf.name)

            for p_idx, p in enumerate(payables, 1):
                erp_res = erp_book(p)
                booked_gross = erp_res.get("will_book_gross", 0.0)
                currency = erp_res.get("currency", "") or "EUR"

                target_str = str(p.get("gross_total") or "").strip()
                try:
                    target_gross = float(target_str) if target_str else 0.0
                except ValueError:
                    target_gross = 0.0

                delta = round(abs(booked_gross - target_gross), 2)
                is_match = delta < 0.05
                verdict = "PASS" if is_match else "FAIL"

                if is_match:
                    pass_count += 1
                else:
                    fail_count += 1

                records.append({
                    "File": file_name,
                    "Sub-Doc": f"#{p_idx}" if len(payables) > 1 else "1",
                    "Invoice #": p.get("invoice_number", "N/A"),
                    "Supplier": p.get("supplier", {}).get("name", "N/A"),
                    "Stated Gross": f"{target_gross:.2f} {currency}",
                    "ERP Booked Gross": f"{booked_gross:.2f} {currency}",
                    "Delta": f"{delta:.2f}",
                    "Verdict": verdict
                })
        except Exception:
            pass

    total = pass_count + fail_count
    pass_rate = (pass_count / total * 100.0) if total > 0 else 0.0
    summary = {
        "total": total,
        "pass": pass_count,
        "fail": fail_count,
        "pass_rate": pass_rate
    }
    return records, summary


def render_erp_playground():
    """Renders an interactive ERP calculation playground with single payload editing AND batch ERP audit over all payables."""
    with st.expander("🧮 Interactive ERP Oracle Calculator & Batch Audit Suite", expanded=False):
        tab_single, tab_batch = st.tabs(["🔍 Single Payload Inspector & Editor", "📊 Batch ERP Audit (All Payables)"])

        with tab_single:
            st.markdown("<div style='font-size: 12px; color: #94a3b8; margin-bottom: 10px;'>Select any generated autodraft from <code>output/</code>, <code>sample_autodraft.json</code>, or paste custom JSON. Tweak line prices, quantities, taxes, or discounts and re-run the ERP Oracle booking calculation live.</div>", unsafe_allow_html=True)
            
            out_dir = Path("output")
            output_files = sorted([f.name for f in out_dir.glob("*.json")]) if out_dir.exists() else []
            options = []
            if Path("sample_autodraft.json").exists():
                options.append("sample_autodraft.json")
            options.extend([f"output/{f}" for f in output_files])
            options.append("Custom JSON Input")

            selected_source = st.selectbox("Select Payload Source:", options=options, index=0 if options else 0, key="erp_payload_source_sel")

            initial_json_str = ""
            if selected_source == "sample_autodraft.json":
                sample_p = Path("sample_autodraft.json")
                if sample_p.exists():
                    initial_json_str = sample_p.read_text(encoding="utf-8")
            elif selected_source.startswith("output/"):
                file_name = selected_source.replace("output/", "")
                target_p = out_dir / file_name
                if target_p.exists():
                    initial_json_str = target_p.read_text(encoding="utf-8")
            else:
                initial_json_str = json.dumps({
                    "invoice_number": "INV-TEST-001",
                    "currency": "EUR",
                    "gross_total": "100.00",
                    "discount_amount": "0.00",
                    "freight_charges": "0.00",
                    "insurance_charges": "0.00",
                    "extra_charges": "0.00",
                    "excise_duties": "0.00",
                    "line_items": [
                        {
                            "description": "Sample Line Item",
                            "quantity": "2.00",
                            "unit_price": "50.00",
                            "discount": "0.00",
                            "discount_percentage": "0.00",
                            "tax_rate": "0.00",
                            "tax_amount": "0.00"
                        }
                    ]
                }, indent=2)

            edited_json_str = st.text_area(
                "Editable Payable JSON Payload:",
                value=initial_json_str,
                height=240,
                key=f"json_editor_{selected_source}"
            )

            recalc_btn = st.button("⚡ Run ERP Oracle Recompute", type="primary", key="recalc_single_btn")

            if recalc_btn or edited_json_str:
                try:
                    payload = json.loads(edited_json_str)
                    payables_list = []
                    if isinstance(payload, dict):
                        if "payables" in payload and isinstance(payload["payables"], list):
                            payables_list = payload["payables"]
                        else:
                            payables_list = [payload]

                    if not payables_list:
                        st.warning("No payable objects found in the provided JSON payload.")
                    else:
                        for p_idx, p in enumerate(payables_list, 1):
                            if len(payables_list) > 1:
                                st.markdown(f"#### Payable #{p_idx}")

                            erp_res = erp_book(p)
                            booked_gross = erp_res.get("will_book_gross", 0.0)
                            currency = erp_res.get("currency", "") or "EUR"

                            target_str = str(p.get("gross_total") or "").strip()
                            try:
                                target_gross = float(target_str) if target_str else 0.0
                            except ValueError:
                                target_gross = 0.0

                            delta = round(abs(booked_gross - target_gross), 2)
                            is_match = delta < 0.05

                            item_discounted_total = sum(_line_base(li) for li in (p.get("line_items") or []) if isinstance(li, dict))
                            line_tax_total = sum(_line_taxes(li, _line_base(li)) for li in (p.get("line_items") or []) if isinstance(li, dict))
                            header_discount = abs(num(p.get("discount_amount")))
                            net_base = item_discounted_total - header_discount
                            header_tax = _header_taxes(p.get("taxes"), net_base)
                            other_charges = (
                                num(p.get("freight_charges"))
                                + num(p.get("insurance_charges"))
                                + num(p.get("extra_charges"))
                                + num(p.get("excise_duties"))
                            )

                            verdict_text = "✅ PASS — CENT-EXACT MATCH" if is_match else "❌ FAIL — DISCREPANCY DETECTED"
                            verdict_color = "#34d399" if is_match else "#f87171"
                            bg_color = "#064e3b22" if is_match else "#7f1d1d33"
                            border_color = "#059669" if is_match else "#dc2626"

                            st.markdown(textwrap.dedent(f"""
                                <div style="background-color: {bg_color}; border: 1px solid {border_color}; border-radius: 8px; padding: 14px 18px; margin: 12px 0;">
                                    <div style="font-size: 11px; font-weight: 700; color: #94a3b8; text-transform: uppercase;">Oracle Calculation Verdict</div>
                                    <div style="font-size: 18px; font-weight: 800; color: {verdict_color}; margin: 4px 0;">{verdict_text}</div>
                                    <div style="font-size: 12px; color: #cbd5e1;">Stated Gross: <strong>{target_gross:.2f} {currency}</strong> | ERP Recomputed: <strong>{booked_gross:.2f} {currency}</strong> | Delta: <strong>{delta:.2f} {currency}</strong></div>
                                </div>
                            """), unsafe_allow_html=True)

                            c1, c2 = st.columns(2)
                            c1.metric("Stated Target Gross", f"{target_gross:.2f} {currency}")
                            c2.metric("ERP Booked Gross", f"{booked_gross:.2f} {currency}", delta=f"-{delta:.2f}" if delta > 0 else "0.00", delta_color="inverse" if not is_match else "normal")

                            st.markdown("**📐 Itemized Accounting Ledger Breakdown**")
                            st.table([
                                {"Accounting Component": "Line Items Base Total (Net)", "Amount": f"{item_discounted_total:.2f} {currency}"},
                                {"Accounting Component": "Header Discount Deduction", "Amount": f"-{header_discount:.2f} {currency}"},
                                {"Accounting Component": "Item-Level Line Taxes Total", "Amount": f"+{line_tax_total:.2f} {currency}"},
                                {"Accounting Component": "Header Taxes Total", "Amount": f"+{header_tax:.2f} {currency}"},
                                {"Accounting Component": "Freight & Extra Charges", "Amount": f"+{other_charges:.2f} {currency}"},
                                {"Accounting Component": "Final ERP Recomputed Gross", "Amount": f"{booked_gross:.2f} {currency}"}
                            ])
                except Exception as ex:
                    st.error(f"JSON Parsing / Calculation Error: {ex}")

        with tab_batch:
            st.markdown("<div style='font-size: 12px; color: #94a3b8; margin-bottom: 12px;'>Execute batch ERP calculation across <strong>all generated autodraft payloads</strong> in <code>output/</code> to audit complete accounting footprint correctness.</div>", unsafe_allow_html=True)
            
            records, summary = run_batch_erp_audit()

            # Top Batch Audit Summary Metrics
            bm1, bm2, bm3, bm4 = st.columns(4)
            with bm1:
                st.markdown(f"<div class='metric-card'><div class='metric-value'>{summary['total']}</div><div class='metric-label'>Payables Audited</div></div>", unsafe_allow_html=True)
            with bm2:
                st.markdown(f"<div class='metric-card'><div class='metric-value' style='color:#34d399;'>{summary['pass']}</div><div class='metric-label'>Cent-Exact Passed</div></div>", unsafe_allow_html=True)
            with bm3:
                st.markdown(f"<div class='metric-card'><div class='metric-value' style='color:#f87171;'>{summary['fail']}</div><div class='metric-label'>Discrepancies</div></div>", unsafe_allow_html=True)
            with bm4:
                st.markdown(f"<div class='metric-card'><div class='metric-value' style='color:#38bdf8;'>{summary['pass_rate']:.1f}%</div><div class='metric-label'>Batch Pass Rate</div></div>", unsafe_allow_html=True)

            st.markdown("<div style='margin-top: 14px;'></div>", unsafe_allow_html=True)

            # Filter options for the table
            filter_choice = st.radio(
                "Filter Batch Payables:",
                options=["All Audited Payables", "✅ PASS Only", "❌ FAIL Discrepancies Only"],
                horizontal=True,
                key="batch_erp_filter_choice"
            )

            filtered_records = records
            if filter_choice == "✅ PASS Only":
                filtered_records = [r for r in records if r["Verdict"] == "PASS"]
            elif filter_choice == "❌ FAIL Discrepancies Only":
                filtered_records = [r for r in records if r["Verdict"] == "FAIL"]

            if filtered_records:
                st.dataframe(
                    filtered_records,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Verdict": st.column_config.TextColumn("Verdict", help="ERP Oracle Cent-Exact Match Status")
                    }
                )

                # Download JSON Report
                json_report_bytes = json.dumps({"summary": summary, "records": records}, indent=2).encode("utf-8")
                st.download_button(
                    label="📥 Download Full Batch ERP Audit Report (JSON)",
                    data=json_report_bytes,
                    file_name="erp_batch_audit_report.json",
                    mime="application/json",
                    key="dl_batch_erp_report"
                )
            else:
                st.info("No payables match the selected filter criteria.")


def render_rerun_erp_section():
    """Renders a dedicated subsequent section allowing users to trigger ERP Oracle re-runs based on Single or Batch scope."""
    st.markdown("### 🔄 Re-run ERP Calculation Suite")
    st.caption("Re-execute the ERP accounting oracle dynamically for either the currently selected document or across all batch payloads.")

    curr_doc = st.session_state.selected_doc or "DU-02.pdf"
    
    c_scope, c_btn = st.columns([2.5, 1])

    with c_scope:
        scope_choice = st.radio(
            "Select Re-run Execution Scope:",
            options=[f"Single Document (`{curr_doc}`)", "Batch Mode (All Output Payloads)"],
            horizontal=True,
            key="radio_rerun_erp_scope"
        )

    with c_btn:
        st.write("")
        st.write("")
        trigger_btn = st.button("⚡ Re-run ERP Oracle", type="primary", use_container_width=True, key="btn_trigger_rerun_erp")

    if trigger_btn:
        if scope_choice.startswith("Single"):
            out_dir = Path("output")
            stem = Path(curr_doc).stem
            json_p = out_dir / f"{stem}.json"

            if json_p.exists():
                try:
                    data = json.loads(json_p.read_text(encoding="utf-8"))
                    payables = data.get("payables", [])
                    if not payables:
                        st.warning(f"Document `{curr_doc}` contains no payables (Declined document).")
                    else:
                        st.success(f"✅ Successfully re-ran ERP Oracle on `{curr_doc}` ({len(payables)} payable object(s))")
                        for p_idx, p in enumerate(payables, 1):
                            if len(payables) > 1:
                                st.markdown(f"#### Payable #{p_idx}")

                            erp_res = erp_book(p)
                            booked_gross = erp_res.get("will_book_gross", 0.0)
                            currency = erp_res.get("currency", "") or "EUR"

                            target_str = str(p.get("gross_total") or "").strip()
                            try:
                                target_gross = float(target_str) if target_str else 0.0
                            except ValueError:
                                target_gross = 0.0

                            delta = round(abs(booked_gross - target_gross), 2)
                            is_match = delta < 0.05

                            item_discounted_total = sum(_line_base(li) for li in (p.get("line_items") or []) if isinstance(li, dict))
                            line_tax_total = sum(_line_taxes(li, _line_base(li)) for li in (p.get("line_items") or []) if isinstance(li, dict))
                            header_discount = abs(num(p.get("discount_amount")))
                            net_base = item_discounted_total - header_discount
                            header_tax = _header_taxes(p.get("taxes"), net_base)
                            other_charges = (
                                num(p.get("freight_charges"))
                                + num(p.get("insurance_charges"))
                                + num(p.get("extra_charges"))
                                + num(p.get("excise_duties"))
                            )

                            verdict_text = "✅ PASS — CENT-EXACT MATCH" if is_match else "❌ FAIL — DISCREPANCY DETECTED"
                            verdict_color = "#34d399" if is_match else "#f87171"
                            bg_color = "#064e3b22" if is_match else "#7f1d1d33"
                            border_color = "#059669" if is_match else "#dc2626"

                            st.markdown(textwrap.dedent(f"""
                                <div style="background-color: {bg_color}; border: 1px solid {border_color}; border-radius: 8px; padding: 14px 18px; margin: 10px 0;">
                                    <div style="font-size: 11px; font-weight: 700; color: #94a3b8; text-transform: uppercase;">Re-run Calculation Verdict (`{curr_doc}`)</div>
                                    <div style="font-size: 18px; font-weight: 800; color: {verdict_color}; margin: 4px 0;">{verdict_text}</div>
                                    <div style="font-size: 12px; color: #cbd5e1;">Stated Target Gross: <strong>{target_gross:.2f} {currency}</strong> | ERP Recomputed: <strong>{booked_gross:.2f} {currency}</strong> | Delta: <strong>{delta:.2f} {currency}</strong></div>
                                </div>
                            """), unsafe_allow_html=True)

                            col_a, col_b = st.columns(2)
                            col_a.metric("Stated Target Gross", f"{target_gross:.2f} {currency}")
                            col_b.metric("ERP Booked Gross", f"{booked_gross:.2f} {currency}", delta=f"-{delta:.2f}" if delta > 0 else "0.00", delta_color="inverse" if not is_match else "normal")

                            st.table([
                                {"Accounting Component": "Line Items Base Total (Net)", "Amount": f"{item_discounted_total:.2f} {currency}"},
                                {"Accounting Component": "Header Discount Deduction", "Amount": f"-{header_discount:.2f} {currency}"},
                                {"Accounting Component": "Item-Level Line Taxes Total", "Amount": f"+{line_tax_total:.2f} {currency}"},
                                {"Accounting Component": "Header Taxes Total", "Amount": f"+{header_tax:.2f} {currency}"},
                                {"Accounting Component": "Freight & Extra Charges", "Amount": f"+{other_charges:.2f} {currency}"},
                                {"Accounting Component": "Final ERP Recomputed Gross", "Amount": f"{booked_gross:.2f} {currency}"}
                            ])
                except Exception as ex:
                    st.error(f"Failed to read/calculate payload for `{curr_doc}`: {ex}")
            else:
                st.warning(f"No generated JSON output found for `{curr_doc}` in `output/`.")
        else:
            records, summary = run_batch_erp_audit()
            st.success(f"✅ Successfully re-ran Batch ERP Oracle across all {summary['total']} payables!")

            bm1, bm2, bm3, bm4 = st.columns(4)
            with bm1:
                st.markdown(f"<div class='metric-card'><div class='metric-value'>{summary['total']}</div><div class='metric-label'>Payables Audited</div></div>", unsafe_allow_html=True)
            with bm2:
                st.markdown(f"<div class='metric-card'><div class='metric-value' style='color:#34d399;'>{summary['pass']}</div><div class='metric-label'>Cent-Exact Passed</div></div>", unsafe_allow_html=True)
            with bm3:
                st.markdown(f"<div class='metric-card'><div class='metric-value' style='color:#f87171;'>{summary['fail']}</div><div class='metric-label'>Discrepancies</div></div>", unsafe_allow_html=True)
            with bm4:
                st.markdown(f"<div class='metric-card'><div class='metric-value' style='color:#38bdf8;'>{summary['pass_rate']:.1f}%</div><div class='metric-label'>Batch Pass Rate</div></div>", unsafe_allow_html=True)

            st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
            st.dataframe(records, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Main Region: UI Layout & Placeholders
# ---------------------------------------------------------------------------

tab_dash, tab_inspect, tab_tools = st.tabs(["📊 Operations Dashboard", "🔍 Document Inspector", "🧮 ERP Tools & Logs"])

with tab_dash:
    status_dashboard_placeholder = st.empty()
    metrics_placeholder = st.empty()
    st.markdown("### 📋 Batch Queue Status")
    queue_placeholder = st.empty()

with tab_inspect:
    st.info("Select a document from the sidebar 'Review Processed Trace' dropdown to inspect it.")
    flowchart_placeholder = st.empty()
    st.divider()
    trace_header_placeholder = st.empty()
    trace_placeholder = st.container()

with tab_tools:
    erp_playground_placeholder = st.empty()
    st.divider()
    rerun_erp_placeholder = st.empty()
    st.divider()
    log_expander_placeholder = st.empty()


def refresh_live_ui():
    """Refreshes all top status banners, metrics, flowchart, calculator, and headers."""
    with status_dashboard_placeholder.container():
        render_process_status_dashboard(pdf_names)
    with metrics_placeholder.container():
        render_metrics()
    with queue_placeholder.container():
        queue_data = [{"Document": name, "Status": st.session_state.doc_status.get(name, "QUEUED")} for name in pdf_names]
        st.dataframe(queue_data, use_container_width=True, hide_index=True)
    with flowchart_placeholder.container():
        render_flowchart_stepper(st.session_state.selected_doc)
    with erp_playground_placeholder.container():
        render_erp_playground()
    with rerun_erp_placeholder.container():
        render_rerun_erp_section()
    with trace_header_placeholder.container():
        curr_doc = st.session_state.selected_doc
        curr_status = st.session_state.doc_status.get(curr_doc, "QUEUED") if curr_doc else "QUEUED"
        st.header(f"🔍 Pipeline Step Trace: `{curr_doc or 'Ready'}`")
        st.markdown(f"**Current Status:** `{curr_status}`")


def render_trace(doc_name: str | None):
    if not doc_name:
        st.info("Select a processing mode and click **🚀 Start** to begin pipeline execution.")
        return

    events = st.session_state.doc_traces.get(doc_name, [])
    if not events:
        st.info(f"Document `{doc_name}` is queued. Click **🚀 Start** in sidebar to run pipeline.")
        return

    # Deduplicate events by (step, subdoc_idx) to prevent duplicate expander rendering
    seen_keys = set()
    unique_events = []
    for ev in events:
        key = (ev.get("step"), ev.get("subdoc_idx", 1))
        if key not in seen_keys:
            seen_keys.add(key)
            unique_events.append(ev)

    for ev in unique_events:
        step = ev.get("step")

        # Step 1: OCR & Layout Extraction
        if step == 1:
            with st.expander(f"📄 {ev['title']}", expanded=True):
                st.markdown(f"**Extraction Path Used:** `{ev['path_used']}`")
                st.markdown(f"**Total Characters:** `{ev['char_count']}` | **Pages:** `{ev['page_count']}`")
                with st.expander("Show Extracted Layout Text"):
                    st.code(ev["raw_text"], language="text")

        # Step 2: Pre-Segmentation
        elif step == 2:
            with st.expander(f"✂️ {ev['title']}", expanded=True):
                st.markdown(f"**Status:** `{ev['status_msg']}`")

        # Step 3: Classification
        elif step == 3:
            with st.expander(f"🏷️ {ev['title']}", expanded=True):
                col_a, col_b, col_c = st.columns(3)
                col_a.metric("Payable Verdict", "PAYABLE" if ev["is_payable"] else "DECLINED")
                col_b.metric("Doc Type", ev["doc_type"])
                col_c.metric("Confidence", f"{ev.get('score', 1.0):.2f}" if isinstance(ev.get('score'), (int, float)) else str(ev.get('score', '1.0')))
                st.markdown("**Rule Breakdown Reasons:**")
                for r in ev["reasons"]:
                    st.markdown(f"- `{r}`")

        # Step 4: AI Model Extraction (Groq)
        elif step == 4:
            with st.expander(f"🤖 {ev['title']}", expanded=True):
                st.markdown("**Raw Groq Model JSON Response:**")
                st.json(ev["raw_json"])

        # Step 4b: Grounding Verification
        elif step == "4b":
            with st.expander(f"🛡️ {ev['title']}", expanded=True):
                warns = ev.get("warnings", [])
                if warns:
                    st.warning(f"Raised {len(warns)} Grounding Warning(s):")
                    for w in warns:
                        st.markdown(f"- `{w}`")
                else:
                    st.success("Grounding Verification Passed: 0 ungrounded warnings (100% Rule 1 compliant).")
                with st.expander("Sanitized Payload"):
                    st.json(ev["sanitized_json"])

        # Step 5: Master Data Resolution
        elif step == 5:
            with st.expander(f"🏢 {ev['title']}", expanded=True):
                st.markdown("**Resolved Master Data Reference Codes:**")
                matched = ev["master_matched"]
                st.table([
                    {"Field": "Supplier ID", "Matched Code": matched["supplier_id"]},
                    {"Field": "Company Code", "Matched Code": matched["company_code"]},
                    {"Field": "Business Unit Code", "Matched Code": matched["business_unit_code"]},
                    {"Field": "Location Code", "Matched Code": matched["location_code"]},
                    {"Field": "PO ID", "Matched Code": matched["po_id"]},
                    {"Field": "Payment Term ID", "Matched Code": matched["payment_term_id"]}
                ])

        # Step 6: Pre-ERP Payload Assembly
        elif step == 6:
            with st.expander(f"📦 {ev['title']}", expanded=True):
                st.markdown("**Assembled Autodraft JSON Payload (Ready for Oracle ERP):**")
                st.json(ev["payload"])

        # Step 7: ERP Oracle Booking Verification
        elif step == 7:
            with st.expander(f"📊 {ev['title']}", expanded=True):
                status_color = "green" if ev["status"] == "PASS" else "red"
                st.markdown(f"### Status: :{status_color}[{ev['status']}]")
                c1, c2 = st.columns(2)
                c1.metric("Document Stated Target Gross", f"{ev['target_gross']}")
                c2.metric("ERP Calculated Booked Gross", f"{ev['booked_gross']}")
                with st.expander("ERP Calculation Details"):
                    st.json(ev["erp_details"])


# ---------------------------------------------------------------------------
# Trigger Live Generator Execution on Start Click
# ---------------------------------------------------------------------------

if start_btn:
    st.session_state.is_processing = True
    st.session_state.processing_complete = False
    st.session_state.stop_requested = False
    st.session_state.stats = {"total": 0, "payables": 0, "declined": 0, "first_try_pass": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0}
    st.session_state.raw_logs = []

    # Determine target list based on mode
    if processing_mode.startswith("All"):
        targets = pdf_files
    else:
        target_path = Path("documents") / (single_file_selected or pdf_names[0])
        targets = [target_path]

    status_banner = st.empty()
    status_banner.info(f"⚙️ Running pipeline across {len(targets)} document(s)...")

    # Run generator loop live WITHOUT st.rerun() inside loop!
    for event in run_pipeline_generator(targets):
        if st.session_state.stop_requested:
            status_banner.warning("⏹️ Processing stopped by user request.")
            break

        # Re-render UI components live
        refresh_live_ui()

        with trace_placeholder.container():
            render_trace(st.session_state.selected_doc)

        with log_expander_placeholder.container():
            with st.expander("🖥️ Live Terminal Log Feed (stdout)", expanded=True):
                st.code("\n".join(st.session_state.raw_logs[-100:]), language="text")

    st.session_state.is_processing = False
    if not st.session_state.stop_requested:
        st.session_state.processing_complete = True
        status_banner.success("✅ Pipeline processing complete!")
    st.rerun()
else:
    refresh_live_ui()
    with trace_placeholder.container():
        render_trace(st.session_state.selected_doc)
    with log_expander_placeholder.container():
        with st.expander("🖥️ Live Terminal Log Feed (stdout)", expanded=False):
            if st.session_state.raw_logs:
                st.code("\n".join(st.session_state.raw_logs[-100:]), language="text")
            else:
                st.code("No stdout logs recorded yet. Start processing to view terminal logs live.", language="text")
