"""Streamlit UI: live run view (stage by stage) + dashboard of past runs.
Run: streamlit run app.py
"""
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

import db
from config import (ANTHROPIC_MODEL, BUYER_NAME, PO_TOLERANCE_CAP, PO_TOLERANCE_PCT, get_admin_code,
                    get_api_key)
from extract import parse_email
from pipeline import STAGES, Ctx, decide, gstin_valid, inr, norm_po, run_stage

st.set_page_config(page_title="AP Invoice Agent", page_icon="🧾", layout="wide")
SAMPLES = Path(__file__).parent / "samples"
ICON = {"pass": "✅", "warn": "⚠️", "fail": "❌", "skip": "⏭️"}
DEC = {"APPROVED": ("✅", "success"), "NEEDS_REVIEW": ("🟡", "warning"), "REJECTED": ("⛔", "error"),
       "SUPERSEDED": ("↩️", "info")}


def is_admin():
    code = get_admin_code()
    return (not code) or st.session_state.get("admin_ok", False)


# ---------------- sidebar ----------------
with st.sidebar:
    st.title("🧾 AP Invoice Agent")
    st.caption(f"Accounts Payable for **{BUYER_NAME}**")
    page = st.radio("View", ["Process invoice", "Dashboard", "Reference data"], label_visibility="collapsed", key="page")
    st.divider()
    if get_api_key():
        st.success(f"Extractor: Claude ({ANTHROPIC_MODEL})", icon="🤖")
    else:
        st.info("Extractor: offline rules + OCR (no API key)", icon="🧩")
    pacing = st.toggle("Demo pacing (pause between stages)", value=True,
                       help="Visual only: adds a short pause so each stage is visible on screen.")
    st.divider()
    if get_admin_code():
        pw = st.text_input("Admin passcode", type="password",
                           help="Needed to reset, add vendors/POs, review or re-run. Anyone can process invoices.")
        st.session_state.admin_ok = pw == get_admin_code()
        st.caption("🔓 Admin unlocked" if is_admin() else "🔒 Admin actions locked")
    with st.expander("Reset demo data"):
        st.caption("Clears run history and the invoice ledger (PO balances return to full).")
        if st.button("Reset", type="secondary", disabled=not is_admin()):
            db.reset()
            st.toast("Run history cleared")
            st.rerun()


def inr_short(x):
    x = float(x or 0)
    if x >= 1e7:
        return f"INR {x / 1e7:.2f} Cr"
    if x >= 1e5:
        return f"INR {x / 1e5:.2f} L"
    return f"INR {x:,.0f}"


@st.cache_data(show_spinner=False)
def pdf_images(data):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(data)
    return [doc[i].render(scale=1.4).to_pil() for i in range(min(len(doc), 3))]


def show_pdf(data):
    try:
        for img in pdf_images(data):
            st.image(img, use_container_width=True)
    except Exception:
        st.warning("Preview unavailable for this file.")


# ---------------- shared renderers ----------------
def render_checks(checks):
    for c in checks:
        st.markdown(f"{ICON[c['result']]} **{c['label']}** — {c['detail']}")


def render_stage_body(s):
    render_checks(s["checks"])
    d = s.get("data") or {}
    if s["key"] == "extract" and d.get("fields"):
        f = d["fields"]
        left, right = st.columns([1, 2])
        left.dataframe(pd.DataFrame([(k, f.get(k)) for k in f if k != "line_items"], columns=["field", "value"]).astype(str),
                       hide_index=True, use_container_width=True)
        if f.get("line_items"):
            right.dataframe(pd.DataFrame(f["line_items"]), hide_index=True, use_container_width=True)
    if s["key"] == "po_match" and d.get("candidates"):
        st.caption("Candidate open POs for this vendor (score = 50% amount fit + 50% description overlap)")
        st.dataframe(pd.DataFrame(d["candidates"]), hide_index=True, use_container_width=True)
    if s["key"] == "reconcile" and d:
        st.dataframe(pd.DataFrame([{k: inr(v) for k, v in d.items()}]), hide_index=True, use_container_width=True)
    if s["key"] == "decision" and d.get("next_action"):
        st.markdown(f"**Next action:** {d['next_action']}")


def render_decision(run):
    icon, kind = DEC.get(run.get("final_status"), DEC[run["decision"]])
    label = run["final_status"].replace("_", " ")
    extra = ""
    if run.get("reviewer_note"):
        extra = f"  \n_Reviewer changed {run['decision']} → {run['final_status']} ({run.get('reviewed_at') or ''}): {run['reviewer_note']}_"
    getattr(st, kind)(f"### {icon} {label}\n{run['summary']}{extra}")
    ai = (f"{(run.get('tokens_in') or 0) + (run.get('tokens_out') or 0):,} tokens · ~INR {run.get('cost_inr') or 0:.2f}"
          if run.get("tokens_in") else "INR 0 (offline)")
    st.markdown(f"**Total** {inr(run.get('total'))} · **PO** {run.get('po_number') or '—'} · "
                f"**Extractor** {run.get('extraction_method') or '—'} · **AI cost** {ai} · "
                f"**Time** {(run.get('duration_ms') or 0) / 1000:.1f}s")
    if run.get("source") and run["source"] != "upload":
        st.caption(f"📧 Received as {run['source']}")
    st.markdown(f"**Next action:** {run['next_action']}")
    if run.get("reasons"):
        st.markdown("**Why**")
        render_checks(run["reasons"])
    if run.get("email_draft"):
        st.markdown("**Vendor email (draft, not sent: a human reviews and sends)**")
        st.text_area("Vendor email", run["email_draft"], height=230, key=f"email{run['id']}", label_visibility="collapsed")


# ---------------- page: process ----------------
def run_live(name, data, source=None):
    st.subheader(f"Run: {name}")
    doc_col, run_col = st.columns([2, 3])
    with doc_col:
        st.caption("The invoice as received")
        show_pdf(data)
    ctx, t0 = Ctx(name, data, source), time.perf_counter()
    prog = run_col.progress(0.0, text="Starting")
    for i, (key, title, fn) in enumerate(STAGES):
        with run_col.status(f"{title} …", expanded=True) as box:
            s = run_stage(ctx, key, title, fn)
            render_stage_body(s)
            box.update(label=f"{ICON[s['status']]} {title} — {s['summary']}  ·  {s['ms']} ms",
                       state="error" if s["status"] == "fail" else "complete",
                       expanded=s["status"] == "fail")
        prog.progress((i + 1) / (len(STAGES) + 1), text=title)
        if pacing:
            time.sleep(0.6)
    run = decide(ctx, int((time.perf_counter() - t0) * 1000))
    prog.progress(1.0, text=f"Done · saved as run #{run['id']}")
    st.divider()
    st.subheader(f"8. Decision · run #{run['id']}")
    render_decision(run)
    return run


def page_process():
    st.header("Process an invoice")
    pending = st.session_state.pop("pending", None)
    if pending:
        st.info(f"Re-running run #{pending['from_run']} with current vendor and PO data. The old run is marked SUPERSEDED.")
        run_live(pending["name"], pending["data"], pending["source"])
        return

    st.caption("Upload an invoice PDF or a saved email (.eml) with the invoice attached, or pick a test file. "
               "Each stage runs live; the decision is made by rules, not by the AI.")
    c1, c2 = st.columns(2)
    up = c1.file_uploader("Upload invoice PDF or email (.eml)", type=["pdf", "eml"])
    samples = sorted(p.name for p in SAMPLES.iterdir() if p.suffix in (".pdf", ".eml"))
    pick = c2.selectbox("…or choose a test file", ["—"] + samples)
    if up:
        name, data = up.name, up.getvalue()
    elif pick != "—":
        name, data = pick, (SAMPLES / pick).read_bytes()
        c2.download_button("Download this file to view it", data, file_name=pick)
    else:
        return

    if name.lower().endswith(".eml"):
        meta, pdfs = parse_email(data)
        st.info(f"📧 **From:** {meta['from']}  \n**Subject:** {meta['subject']}  \n**Date:** {meta['date']}  \n"
                f"**PDF attachments:** {', '.join(n for n, _ in pdfs) or 'none'}")
        if not pdfs:
            st.error("No PDF attachment in this email, so there is nothing to process. Ask the sender to resend the invoice as a PDF.")
            return
        jobs = [(f"{name} > {n}", b, meta) for n, b in pdfs]
    else:
        jobs = [(name, data, None)]

    if st.button(f"▶ Run process on {name}" + (f" ({len(jobs)} invoices)" if len(jobs) > 1 else ""), type="primary"):
        for job in jobs:
            run_live(*job)


# ---------------- page: dashboard ----------------
def queue_rerun(run_id):
    """Button callback: supersede the old run, send its PDF back through the live view."""
    r = db.get_run(run_id)
    m = re.match(r"email from (.*?): (.*)", r.get("source") or "")
    db.resolve(run_id, "SUPERSEDED", f"Re-run requested; see the next run for {r['filename']}")
    st.session_state.pending = {"name": r["filename"], "data": r["pdf"], "from_run": run_id,
                                "source": {"from": m.group(1), "subject": m.group(2)} if m else None}
    st.session_state.page = "Process invoice"


def erp_posting_file(df):
    appr = df[df["final_status"] == "APPROVED"]
    terms = db.vendors().set_index("vendor_id")["payment_terms_days"].astype(int).to_dict()
    rows = []
    for r in appr.itertuples():
        ex = json.loads(r.extracted) if isinstance(r.extracted, str) else {}
        try:
            due = (datetime.fromisoformat(r.invoice_date) + timedelta(days=terms.get(r.vendor_id, 30))).date().isoformat()
        except Exception:
            due = ""
        rows.append({"run_id": r.id, "vendor_id": r.vendor_id, "vendor_name": r.vendor_name,
                     "vendor_gstin": ex.get("vendor_gstin"), "invoice_no": r.invoice_no, "invoice_date": r.invoice_date,
                     "po_number": r.po_number, "taxable_value": r.subtotal, "gst": r.tax, "invoice_total": r.total,
                     "currency": "INR", "due_date": due})
    return pd.DataFrame(rows).to_csv(index=False).encode()


def page_dashboard():
    st.header("Dashboard")
    df = db.list_runs()
    if df.empty:
        st.info("No runs yet. Process an invoice first.")
        return
    live = df[df["final_status"] != "SUPERSEDED"]
    counts = live["final_status"].value_counts()
    money = live.groupby("final_status")["total"].sum()
    dups = live[live["next_action"].fillna("").str.startswith("Do not pay")]
    k = st.columns(5)
    k[0].metric("Invoices", len(live), help=f"Avg {live['duration_ms'].mean() / 1000:.1f}s and "
                                         f"INR {live['cost_inr'].fillna(0).mean():.2f} AI cost per invoice")
    k[1].metric(f"Approved ({int(counts.get('APPROVED', 0))})", inr_short(money.get("APPROVED", 0)))
    k[2].metric(f"Held for review ({int(counts.get('NEEDS_REVIEW', 0))})", inr_short(money.get("NEEDS_REVIEW", 0)))
    k[3].metric(f"Blocked ({int(counts.get('REJECTED', 0))})", inr_short(money.get("REJECTED", 0)))
    k[4].metric(f"Duplicates stopped ({len(dups)})", inr_short(dups["total"].sum()))

    f1, f2 = st.columns([3, 1])
    flt = f1.multiselect("Filter by status", list(DEC), default=[])
    f2.download_button("⬇ ERP posting file (CSV)", erp_posting_file(df), file_name="ap_posting_approved.csv",
                       mime="text/csv", help="Approved invoices only, with due dates, ready to import into the ERP.",
                       use_container_width=True)
    view = df[df["final_status"].isin(flt)] if flt else df
    table = view[["id", "created_at", "vendor_name", "invoice_no", "po_number", "total",
                  "decision", "final_status", "extraction_method"]].copy()
    table["created_at"] = pd.to_datetime(table["created_at"]).dt.strftime("%d %b %H:%M")
    table["total"] = table["total"].map(inr)
    for col in ("decision", "final_status"):
        table[col] = table[col].map(lambda x: f"{DEC[x][0]} {x.replace('_', ' ')}")
    st.dataframe(table, hide_index=True, use_container_width=True,
                 column_config={"id": "Run", "created_at": "When", "vendor_name": "Vendor",
                                "invoice_no": "Invoice", "po_number": "PO", "total": "Total",
                                "decision": "System decision", "final_status": "Final status",
                                "extraction_method": "Extractor"})

    st.subheader("Inspect a run")
    queue = df[df["final_status"] == "NEEDS_REVIEW"]["id"].tolist()
    if queue:
        st.caption(f"Review queue: runs {', '.join(f'#{i}' for i in queue)}")
    idx = df.set_index("id")
    rid = st.selectbox("Run", df["id"].tolist(), format_func=lambda i: f"#{i} · {idx.loc[i, 'filename']} · {idx.loc[i, 'final_status']}")
    run = db.get_run(rid)
    left, right = st.columns([3, 2])
    with left:
        t1, t2 = st.tabs(["Stage log", "Invoice PDF"])
        with t1:
            for s in run["stages"]:
                with st.expander(f"{ICON[s['status']]} {s['title']} — {s['summary']}", expanded=False):
                    render_stage_body(s)
        with t2:
            show_pdf(run["pdf"]) if run.get("pdf") else st.caption("No PDF stored for this run.")
    with right:
        render_decision(run)
        locked = not is_admin()
        if locked:
            st.caption("🔒 Enter the admin passcode in the sidebar to review or re-run.")
        if run["final_status"] == "NEEDS_REVIEW":
            st.markdown("**Reviewer action**")
            note = st.text_input("Note (required)", key=f"note{rid}", placeholder="e.g. Confirmed PO-4503 with buyer", disabled=locked)
            a, b = st.columns(2)
            if a.button("Approve", type="primary", disabled=locked or not note, key=f"ap{rid}"):
                db.resolve(rid, "APPROVED", note); st.rerun()
            if b.button("Reject", disabled=locked or not note, key=f"rj{rid}"):
                db.resolve(rid, "REJECTED", note); st.rerun()
        if run.get("pdf") and run["final_status"] != "SUPERSEDED":
            st.button("↻ Re-run with current vendor/PO data", key=f"rr{rid}", disabled=locked,
                      on_click=queue_rerun, args=(rid,),
                      help="Use after adding a missing vendor or PO. The old run is kept and marked SUPERSEDED.")


# ---------------- page: reference ----------------
def page_reference():
    st.header("Reference data")
    st.markdown(f"**Rules:** PO tolerance = min({PO_TOLERANCE_PCT:.0%} of PO, {inr(PO_TOLERANCE_CAP)}) · "
                "only approved invoices consume PO balance · inferred POs always go to review · scans get no math slack.")
    st.subheader("Purchase orders (live balances)")
    pos = db.purchase_orders()
    for col in ("po_amount", "approved_billed", "remaining"):
        pos[col] = pos[col].map(inr)
    st.dataframe(pos, hide_index=True, use_container_width=True)
    st.subheader("Approved vendor master")
    st.dataframe(db.vendors(), hide_index=True, use_container_width=True)

    st.subheader("Add reference data")
    if not is_admin():
        st.info("🔒 Enter the admin passcode in the sidebar to add vendors or POs.")
        return
    st.caption("Register a new vendor or PO, then re-run the invoice from the Dashboard (e.g. for an invoice handed over live).")
    a, b = st.columns(2)
    with a.form("add_vendor", clear_on_submit=True):
        st.markdown("**New vendor**")
        name = st.text_input("Legal name")
        gstin = st.text_input("GSTIN")
        terms = st.number_input("Payment terms (days)", 0, 180, 30)
        email = st.text_input("Contact email")
        if st.form_submit_button("Add vendor"):
            if not name or not gstin_valid(gstin.upper().strip()):
                st.error("Need a name and a valid GSTIN (format + checksum).")
            elif gstin.upper().strip() in set(db.vendors()["gstin"]):
                st.error("A vendor with this GSTIN already exists.")
            else:
                st.success(f"Added {db.add_vendor(name, gstin, terms, email)}")
                st.rerun()
    with b.form("add_po", clear_on_submit=True):
        st.markdown("**New purchase order**")
        vdf = db.vendors()
        vid = st.selectbox("Vendor", vdf["vendor_id"], format_func=lambda v: f"{v} · {vdf.set_index('vendor_id').loc[v, 'vendor_name']}")
        po_no = st.text_input("PO number", placeholder="PO-4600")
        amount = st.number_input("PO amount, pre-tax (INR)", 0.0, step=1000.0)
        desc = st.text_input("Description (used to infer POs)")
        if st.form_submit_button("Add PO"):
            po_norm = norm_po(po_no)
            if not po_norm or amount <= 0:
                st.error("Need a PO number with digits and an amount above zero.")
            elif po_norm in set(db.purchase_orders()["po_number"]):
                st.error(f"{po_norm} already exists.")
            else:
                db.add_po(po_norm, vid, amount, desc)
                st.success(f"Added {po_norm}")
                st.rerun()


{"Process invoice": page_process, "Dashboard": page_dashboard, "Reference data": page_reference}[page]()
