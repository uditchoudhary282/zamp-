"""The AP process: 8 stages, each returns checks. Decision = deterministic roll-up of checks.

Every check has: result (pass / warn / fail), what happens on fail (review / reject),
a plain-English detail, and optionally what to ask the vendor for.
"""
import hashlib
import re
import time
from datetime import date, datetime, timedelta

from rapidfuzz import fuzz

import db
from config import (BUYER_GSTIN, GST_RATE_SLACK, MATH_PASS_ABS, MATH_WARN_ABS, PO_INFER_MIN_MARGIN,
                    PO_INFER_MIN_SCORE, PO_TOLERANCE_CAP, PO_TOLERANCE_PCT, SCANNED_MATH_ABS,
                    VALID_GST_RATES, VENDOR_NAME_MATCH, INCLUSIVE_GST_RATE)
from extract import extract, extract_rules, inspect_pdf, ocr_text


# ---------------- helpers ----------------
def inr(x):
    """Indian digit grouping: 141600 -> 'INR 1,41,600.00'."""
    if x is None:
        return "-"
    neg, x = x < 0, abs(float(x))
    whole, frac = f"{x:.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    head = ",".join(re.findall(r"\d{1,2}", head[::-1]))[::-1] if head else ""
    return f"{'-' if neg else ''}INR {head + ',' if head else ''}{tail}.{frac}"


def check(id, label, result, detail, on_fail="review", ask=None):
    return {"id": id, "label": label, "result": result, "detail": detail, "on_fail": on_fail, "ask": ask}


def norm_invoice_no(s):
    """'INV/001042' and 'inv-1042' -> 'INV1042'."""
    s = re.sub(r"[^A-Z0-9]", "", (s or "").upper())
    return re.sub(r"(?<=[A-Z])0+(?=\d)", "", s)


def norm_po(s):
    m = re.search(r"(\d{3,})", s or "")
    return f"PO-{m.group(1)}" if m else None


def norm_name(s):
    s = (s or "").lower()
    s = re.sub(r"\b(pvt|private|ltd|limited|llp|inc|co|the)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9 ]", " ", s).split()


def tokens(s):
    return {w.rstrip("s") for w in re.findall(r"[a-z]{4,}", (s or "").lower())} - {"with", "billed", "unit"}


GST_CH = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_valid(g):
    if not g or not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", g):
        return False
    t = 0
    for i, c in enumerate(g[:14]):
        p = GST_CH.index(c) * (1 if i % 2 == 0 else 2)
        t += p // 36 + p % 36
    return GST_CH[(36 - t % 36) % 36] == g[14]


def tolerance(po_amount):
    return min(PO_TOLERANCE_PCT * po_amount, PO_TOLERANCE_CAP)


# ---------------- context ----------------
class Ctx:
    def __init__(self, filename, pdf_bytes, source=None):
        self.filename, self.pdf = filename, pdf_bytes
        self.source = source or {}   # e.g. {"from": ..., "subject": ...} when received by email
        self.usage = {"tokens_in": 0, "tokens_out": 0, "cost_inr": 0.0}
        self.hash = hashlib.sha256(pdf_bytes).hexdigest()
        self.text, self.doc_type, self.method = "", None, None
        self.f = {}            # extracted fields
        self.subtotal = None
        self.vendor = None     # dict row from vendor master
        self.po, self.po_source = None, None
        self.abort = None      # reason string if we cannot continue
        self.stages = []


# ---------------- stages ----------------
def s_intake(c: Ctx):
    try:
        c.text, pages = inspect_pdf(c.pdf)
    except Exception as e:
        c.abort = "File is not a readable PDF"
        return "Unreadable file", [check("pdf", "PDF opens", "fail", f"Could not open file: {e}",
                                         ask="Please resend the invoice as a PDF.")], {}
    c.doc_type = "digital" if len(c.text) > 30 else "scanned"
    chk = [check("pdf", "PDF opens", "pass", f"{pages} page(s), sha256 {c.hash[:12]}...")]
    if c.doc_type == "scanned":
        chk.append(check("text", "Text layer", "warn", "No text layer: scanned/image PDF. Vision/OCR extraction + stricter math rules apply."))
    else:
        chk.append(check("text", "Text layer", "pass", f"{len(c.text)} characters of machine-readable text"))
    return f"{c.doc_type.title()} PDF, {pages} page(s)", chk, {"sha256": c.hash, "pages": pages}


def s_extract(c: Ctx):
    note = None
    try:
        fields, c.method, c.usage = extract(c.pdf, c.text)
    except Exception as e:  # LLM down / quota -> fall back rather than crash
        note = f"LLM extraction failed ({type(e).__name__}); fell back to offline extraction."
        scanned_text = "" if c.text else ocr_text(c.pdf)
        if c.text:
            fields, c.method = extract_rules(c.text), "rules"
        elif scanned_text:
            fields, c.method = extract_rules(scanned_text), "ocr-rules"
        else:
            fields, c.method = None, "none"
    if not fields:
        c.abort = "Scanned invoice and no vision model configured"
        return "Could not read invoice", [check("read", "Invoice readable", "fail",
                                               "Image-only PDF and no LLM key set: needs manual keying.")], {}
    for k in ("subtotal", "tax_total", "total"):
        try:
            fields[k] = float(str(fields[k]).replace(",", "")) if fields.get(k) not in (None, "") else None
        except ValueError:
            fields[k] = None
    for k in ("vendor_gstin", "buyer_gstin"):
        if fields.get(k):
            fields[k] = re.sub(r"\s", "", fields[k]).upper()
    c.f = fields
    found = sum(1 for k, v in fields.items() if v not in (None, "", []))
    cost = f", {c.usage['tokens_in'] + c.usage['tokens_out']:,} tokens ~INR {c.usage['cost_inr']:.2f}" if c.usage["tokens_in"] else ""
    chk = [check("extract", "Fields extracted", "pass", f"{found}/{len(fields)} fields found via {c.method}{cost}")]
    if note:
        chk.append(check("fallback", "Extractor fallback", "warn", note))
    return f"{found}/{len(fields)} fields via {c.method}", chk, {"fields": fields}


def _math(label, id, expected, actual, scanned):
    diff = round(abs(expected - actual), 2)
    if diff <= (SCANNED_MATH_ABS if scanned else MATH_PASS_ABS):
        return check(id, label, "pass", f"Ties out ({inr(actual)})")
    if scanned:
        return check(id, label, "fail", f"Off by {inr(diff)} on a scanned invoice: likely OCR misread, needs a human eye.")
    if diff <= MATH_WARN_ABS:
        return check(id, label, "warn", f"Off by {inr(diff)}: rounding, accepted.")
    return check(id, label, "fail", f"Off by {inr(diff)} (expected {inr(expected)}, invoice says {inr(actual)}).",
                 ask="Line items and totals on your invoice do not add up; please send a corrected invoice.")


def s_validate(c: Ctx):
    f, chk, scanned = c.f, [], c.doc_type == "scanned"
    for k, label in [("vendor_name", "Vendor name"), ("invoice_number", "Invoice number"),
                     ("invoice_date", "Invoice date"), ("total", "Invoice total")]:
        if f.get(k) in (None, ""):
            chk.append(check(f"req_{k}", f"{label} present", "fail", f"{label} missing on invoice.",
                             ask=f"The invoice is missing the {label.lower()}. Please reissue it with this included."))
    # date
    if f.get("invoice_date"):
        try:
            d = datetime.fromisoformat(f["invoice_date"]).date()
            if d > date.today():
                chk.append(check("date", "Invoice date", "fail", f"Dated in the future ({d}).",
                                 ask="The invoice date is in the future; please correct it."))
            elif (date.today() - d).days > 180:
                chk.append(check("date", "Invoice date", "warn", f"Older than 180 days ({d})."))
            else:
                chk.append(check("date", "Invoice date", "pass", str(d)))
        except ValueError:
            chk.append(check("date", "Invoice date", "fail", f"Unparseable date '{f['invoice_date']}'."))
    # identity
    if f.get("vendor_gstin"):
        ok = gstin_valid(f["vendor_gstin"])
        chk.append(check("gstin_fmt", "Vendor GSTIN valid", "pass" if ok else "fail",
                         f"{f['vendor_gstin']} {'passes format and checksum' if ok else 'fails format/checksum'}",
                         ask=None if ok else "The GSTIN on your invoice is invalid; please confirm your GST registration."))
    else:
        chk.append(check("gstin_fmt", "Vendor GSTIN valid", "warn", "No vendor GSTIN printed; matching on name only."))
    if f.get("buyer_gstin") and f["buyer_gstin"] != BUYER_GSTIN:
        chk.append(check("buyer", "Billed to us", "fail", f"Billed to GSTIN {f['buyer_gstin']}, ours is {BUYER_GSTIN}: input tax credit at risk.",
                         ask=f"Please reissue the invoice to our GSTIN {BUYER_GSTIN}."))
    # arithmetic
    items_sum = round(sum((i.get("amount") or 0) for i in f.get("line_items") or []), 2)
    c.subtotal = f.get("subtotal")
    # Tax embedded in prices: no GST line printed and amounts add straight up to the total.
    # POs are pre-tax (A2), so back GST out before matching instead of reading it as an 18% overage.
    base = c.subtotal if c.subtotal is not None else items_sum
    if f.get("tax_total") is None and f.get("total") and base and abs(base - f["total"]) <= MATH_PASS_ABS:
        c.subtotal = round(f["total"] / (1 + INCLUSIVE_GST_RATE), 2)
        f["tax_total"] = round(f["total"] - c.subtotal, 2)
        chk.append(check("tax_inclusive", "Tax treatment", "warn",
                         f"No GST line printed; treated as tax-inclusive at {INCLUSIVE_GST_RATE:.0%}: "
                         f"taxable {inr(c.subtotal)} + GST {inr(f['tax_total'])} = {inr(f['total'])}."))
        items_sum = 0  # line amounts are gross; the line check no longer applies
    if c.subtotal is None and items_sum:
        c.subtotal = items_sum
        chk.append(check("sub_derived", "Subtotal", "warn", f"Not printed; derived from line items ({inr(items_sum)})."))
    if c.subtotal is None and f.get("total") is not None and f.get("tax_total") is not None:
        c.subtotal = round(f["total"] - f["tax_total"], 2)
        chk.append(check("sub_derived", "Subtotal", "warn", f"Not printed; derived as total - tax ({inr(c.subtotal)})."))
    if items_sum and c.subtotal is not None and f.get("subtotal") is not None:
        chk.append(_math("Line items = subtotal", "math_lines", items_sum, c.subtotal, scanned))
    if None not in (c.subtotal, f.get("tax_total"), f.get("total")):
        chk.append(_math("Subtotal + tax = total", "math_total", c.subtotal + f["tax_total"], f["total"], scanned))
        computed = round(c.subtotal + f["tax_total"], 2)
        if scanned and 0 < abs(computed - f["total"]) <= SCANNED_MATH_ABS:
            chk.append(check("ocr_fix", "Total corrected", "warn",
                             f"Scan read total as {inr(f['total'])}; posting computed {inr(computed)} (OCR digit noise)."))
            f["total"] = computed
        rate = f["tax_total"] / c.subtotal if c.subtotal else 0
        ok = any(abs(rate - r) <= GST_RATE_SLACK for r in VALID_GST_RATES)
        chk.append(check("gst_rate", "GST rate plausible", "pass" if ok else "warn", f"Effective GST {rate:.1%}"))
    fails = sum(x["result"] == "fail" for x in chk)
    return ("All fields valid" if not fails else f"{fails} validation issue(s)"), chk, {"subtotal_used": c.subtotal}


def s_vendor(c: Ctx):
    vdf = db.vendors()
    g, name = c.f.get("vendor_gstin"), c.f.get("vendor_name")
    by_g = vdf[vdf.gstin == g] if g else vdf.iloc[0:0]
    scored = [(fuzz.token_set_ratio(" ".join(norm_name(name)), " ".join(norm_name(r.vendor_name))), r)
              for r in vdf.itertuples()] if name else []
    best = max(scored, key=lambda t: t[0], default=(0, None))
    chk = []
    if len(by_g):
        v = by_g.iloc[0].to_dict()
        chk.append(check("vendor", "Approved vendor", "pass", f"GSTIN matches {v['vendor_id']} {v['vendor_name']}"))
    elif best[0] >= VENDOR_NAME_MATCH:
        v = best[1]._asdict()
        if g:  # name says it is vendor X, GSTIN says otherwise -> impersonation risk
            chk.append(check("vendor_identity", "Vendor identity consistent", "fail",
                             f"Name matches {v['vendor_id']} {v['vendor_name']} ({best[0]:.0f}%) but GSTIN {g} "
                             f"differs from master {v['gstin']}. Possible impersonation: verify via the contact on "
                             f"file ({v['contact_email']}), not the one on the invoice."))
        else:
            chk.append(check("vendor", "Approved vendor", "warn", f"Matched on name only to {v['vendor_id']} ({best[0]:.0f}%)."))
    else:
        c.vendor = None
        return "Vendor not approved", [check("vendor", "Approved vendor", "fail",
                                            f"'{name}' ({g or 'no GSTIN'}) is not in the approved vendor master.",
                                            on_fail="reject",
                                            ask="We could not find you in our approved vendor list. Please complete vendor onboarding with our procurement team before invoicing.")], {}
    v.pop("Index", None)
    sender = re.search(r"@([\w.-]+)", c.source.get("from", ""))
    if sender and v.get("contact_email"):
        known = v["contact_email"].split("@")[-1].lower()
        got = sender.group(1).lower()
        chk.append(check("sender", "Sender domain", "pass" if got == known else "warn",
                         f"Emailed from @{got}" + ("" if got == known else f"; vendor master says @{known}. Confirm before paying.")))
    if v.get("status") != "active":
        chk.append(check("vendor_status", "Vendor active", "fail", f"Vendor status is {v.get('status')}.", on_fail="reject"))
    c.vendor = v
    return f"{v['vendor_id']} {v['vendor_name']}", chk, {"vendor": v, "name_score": best[0]}


def s_duplicate(c: Ctx):
    hit = db.find_by_hash(c.hash)
    if hit:
        c.abort = f"duplicate of run #{hit['id']}"
        return "Duplicate file", [check("dup_file", "Not a duplicate", "fail",
                                        f"Identical file already processed in run #{hit['id']}.", on_fail="reject")], {}
    if not c.vendor or not c.f.get("invoice_number"):
        return "Partial check (file hash only)", [check("dup", "Not a duplicate", "pass", "No identical file seen; number check needs vendor + invoice number.")], {}
    key = norm_invoice_no(c.f["invoice_number"])
    prior = db.prior_invoices(c.vendor["vendor_id"])
    for p in prior:
        if p["invoice_no_norm"] == key:
            c.abort = f"duplicate of run #{p['id']}"
            return "Duplicate invoice", [check("dup_no", "Not a duplicate", "fail",
                f"'{c.f['invoice_number']}' normalises to {key}, same as '{p['invoice_no']}' in run #{p['id']} "
                f"({p['final_status']}, {inr(p['total'])}).", on_fail="reject",
                ask=f"Invoice {c.f['invoice_number']} appears to be a resubmission of {p['invoice_no']}, which we have already received. No action needed unless this is a new charge.")], {"matched_run": p["id"]}
    for p in prior:
        if p["total"] and c.f.get("total") and abs(p["total"] - c.f["total"]) < 1 and p["invoice_date"] == c.f.get("invoice_date"):
            return "Possible duplicate", [check("dup_soft", "Not a duplicate", "fail",
                f"Different number but same vendor, amount and date as run #{p['id']} ({p['invoice_no']}).")], {"matched_run": p["id"]}
    return "No duplicate found", [check("dup", "Not a duplicate", "pass", f"Key {key} not seen across {len(prior)} prior invoice(s) from this vendor.")], {}


def s_po_match(c: Ctx):
    if not c.vendor:
        return "Skipped (no approved vendor)", [], {}
    pos = db.purchase_orders()
    raw = c.f.get("po_number")
    if raw:
        po_no = norm_po(raw)
        row = pos[pos.po_number == po_no]
        if row.empty:
            return "PO not found", [check("po", "PO exists", "fail", f"{raw} not found in PO register.",
                                          ask=f"PO {raw} on your invoice does not exist in our system; please confirm the correct PO number.")], {}
        po = row.iloc[0].to_dict()
        chk = []
        if po["vendor_id"] != c.vendor["vendor_id"]:
            chk.append(check("po_vendor", "PO belongs to vendor", "fail", f"{po_no} was raised on {po['vendor_id']}, not {c.vendor['vendor_id']}."))
        if po["status"] != "open":
            chk.append(check("po_open", "PO open", "fail", f"{po_no} is {po['status']}."))
        if not chk:
            chk.append(check("po", "PO matched", "pass", f"{po_no} (explicit on invoice): {po['description']}"))
        c.po, c.po_source = po, "explicit"
        return f"{po_no} (explicit)", chk, {"po": po}

    # EC3: no PO printed -> infer from vendor's open POs
    sub = c.subtotal or 0
    inv_tok = tokens(" ".join(i.get("description", "") for i in c.f.get("line_items") or []))
    cands = []
    for po in pos[(pos.vendor_id == c.vendor["vendor_id"]) & (pos.status == "open")].to_dict("records"):
        rem = po["remaining"]
        fits = sub <= rem + tolerance(po["po_amount"]) and rem > 0
        amt = 1 - abs(rem - sub) / max(rem, sub) if fits else 0.0
        pt = tokens(po["description"])
        desc = len(inv_tok & pt) / len(pt) if pt else 0.0
        cands.append({"po_number": po["po_number"], "remaining": rem, "amount_fit": round(amt, 2),
                      "description_overlap": round(desc, 2), "score": round(0.5 * amt + 0.5 * desc, 2), "_po": po})
    cands.sort(key=lambda x: -x["score"])
    table = [{k: v for k, v in x.items() if k != "_po"} for x in cands]
    if cands and cands[0]["score"] >= PO_INFER_MIN_SCORE and (len(cands) == 1 or cands[0]["score"] - cands[1]["score"] >= PO_INFER_MIN_MARGIN):
        top = cands[0]
        c.po, c.po_source = top["_po"], "inferred"
        nxt = f" vs next best {cands[1]['score']}" if len(cands) > 1 else ""
        return f"{top['po_number']} inferred", [check("po_inferred", "PO reference", "fail",
            f"No PO printed. Best match {top['po_number']} (score {top['score']}{nxt}). Inferred matches are never auto-approved: confirm and post.",
            ask=None)], {"candidates": table}
    return "No PO match", [check("po_missing", "PO reference", "fail",
        f"No PO printed and no confident match among {len(cands)} open PO(s) for this vendor.",
        ask="Your invoice has no purchase order reference. Please reissue it quoting the PO number.")], {"candidates": table}


def s_reconcile(c: Ctx):
    if c.po is None or c.subtotal is None:
        return "Skipped (no PO or amount)", [], {}
    po_no, amt = c.po["po_number"], float(c.po["po_amount"])
    used = db.po_consumed(po_no)
    remaining, tol = amt - used, tolerance(amt)
    over = round(c.subtotal - remaining, 2)
    data = {"po_amount": amt, "previously_approved": used, "remaining_before": remaining,
            "this_invoice_subtotal": c.subtotal, "remaining_after": round(remaining - c.subtotal, 2), "tolerance": tol}
    if over <= 0:
        partial = c.subtotal < remaining - 1
        msg = (f"Partial billing: {inr(c.subtotal)} of {inr(remaining)} open; {inr(remaining - c.subtotal)} stays open on {po_no}."
               if partial else f"Matches open balance of {po_no} ({inr(remaining)}).")
        if used:
            msg += f" Earlier approved billing on this PO: {inr(used)}."
        return ("Within PO balance (partial)" if partial else "Matches PO"), [check("amount", "Amount within PO", "pass", msg)], data
    if over <= tol:
        return "Within tolerance", [check("amount", "Amount within PO", "warn",
            f"Exceeds open balance by {inr(over)}; within tolerance of {inr(tol)} (min 2% of PO, INR 5,000).")], data
    return "Exceeds PO balance", [check("amount", "Amount within PO", "fail",
        f"Exceeds open balance by {inr(over)} (tolerance {inr(tol)}). PO {inr(amt)}, already approved {inr(used)}, "
        f"open {inr(remaining)}, this invoice {inr(c.subtotal)}.",
        ask=f"This invoice exceeds the remaining value on {po_no} by {inr(over)}. Please confirm whether a revised PO was agreed, or send a credit note / revised invoice.")], data


STAGES = [
    ("intake", "1. Intake", s_intake),
    ("extract", "2. Extraction", s_extract),
    ("validate", "3. Field validation", s_validate),
    ("vendor", "4. Vendor verification", s_vendor),
    ("duplicate", "5. Duplicate check", s_duplicate),
    ("po_match", "6. PO matching", s_po_match),
    ("reconcile", "7. Amount reconciliation", s_reconcile),
]


def run_stage(c: Ctx, key, title, fn):
    t0 = time.perf_counter()
    if c.abort:
        st = {"key": key, "title": title, "status": "skip", "summary": f"Skipped: {c.abort}", "checks": [], "data": {}}
    else:
        try:
            summary, checks, data = fn(c)
        except Exception as e:  # a bug in one stage becomes a review, never a silent approve
            summary, checks, data = f"Error: {e}", [check(f"{key}_err", title, "fail", f"Stage crashed: {e}")], {}
        status = ("fail" if any(x["result"] == "fail" for x in checks)
                  else "warn" if any(x["result"] == "warn" for x in checks)
                  else "pass" if checks else "skip")
        st = {"key": key, "title": title, "status": status, "summary": summary, "checks": checks, "data": data}
    st["ms"] = int((time.perf_counter() - t0) * 1000)
    c.stages.append(st)
    return st


def decide(c: Ctx, total_ms=0):
    checks = [x for s in c.stages for x in s["checks"]]
    fails = [x for x in checks if x["result"] == "fail"]
    rejects = [x for x in fails if x["on_fail"] == "reject"]
    warns = [x for x in checks if x["result"] == "warn"]
    f, v = c.f, c.vendor or {}
    who = f"{v.get('vendor_name') or f.get('vendor_name') or 'Unknown vendor'} invoice {f.get('invoice_number') or '(no number)'}"
    po = c.po["po_number"] if c.po is not None else None

    if rejects:
        decision = "REJECTED"
        summary = f"Rejected: {who}. {rejects[0]['detail']}"
        dup = any(x["id"].startswith("dup") for x in rejects)
        next_action = "Do not pay. Already in the system; no posting needed." if dup else "Send back to vendor with the email below."
    elif fails:
        decision = "NEEDS_REVIEW"
        summary = f"Needs review: {who} for {inr(f.get('total'))}. {len(fails)} issue(s) a human must resolve."
        next_action = f"AP reviewer: {fails[0]['label']} - {fails[0]['detail']}"
    else:
        decision = "APPROVED"
        due = ""
        try:
            due = (datetime.fromisoformat(f["invoice_date"]) + timedelta(days=int(v.get("payment_terms_days", 30)))).date()
            due = f" Payment due {due} ({v.get('payment_terms_days')}-day terms)."
        except Exception:
            pass
        summary = f"Approved: {who} for {inr(f.get('total'))} matches {po}." + (f" {len(warns)} note(s) recorded." if warns else "")
        next_action = f"Post to ERP against {po}.{due}"

    asks = [x["ask"] for x in (rejects or fails) if x.get("ask")]
    email = None
    if asks and decision != "APPROVED":
        to = v.get("contact_email") or "(vendor contact)"
        email = (f"To: {to}\nSubject: Invoice {f.get('invoice_number') or ''} - action needed\n\n"
                 f"Hello,\n\nWe received your invoice {f.get('invoice_number') or '(unnumbered)'} dated {f.get('invoice_date') or '-'} "
                 f"for {inr(f.get('total'))}. We cannot process it yet:\n\n"
                 + "\n".join(f"- {a}" for a in asks)
                 + f"\n\nRegards,\nAccounts Payable, Zentra Foods Pvt Ltd")

    reasons = [{"label": x["label"], "result": x["result"], "detail": x["detail"]} for x in (rejects or fails)] + \
              [{"label": x["label"], "result": "warn", "detail": x["detail"]} for x in warns]
    dec_stage = {"key": "decision", "title": "8. Decision", "status": {"APPROVED": "pass", "NEEDS_REVIEW": "warn", "REJECTED": "fail"}[decision],
                 "summary": decision, "checks": [], "data": {"next_action": next_action}, "ms": 0}
    c.stages.append(dec_stage)

    run = {
        "filename": c.filename, "file_hash": c.hash, "doc_type": c.doc_type, "extraction_method": c.method,
        "vendor_id": v.get("vendor_id"), "vendor_name": v.get("vendor_name") or f.get("vendor_name"),
        "invoice_no": f.get("invoice_number"), "invoice_no_norm": norm_invoice_no(f.get("invoice_number")) if f.get("invoice_number") else None,
        "invoice_date": f.get("invoice_date"), "po_number": po, "subtotal": c.subtotal,
        "tax": f.get("tax_total"), "total": f.get("total"), "decision": decision, "final_status": decision,
        "summary": summary, "next_action": next_action, "reasons": reasons, "stages": c.stages,
        "extracted": f, "email_draft": email, "duration_ms": total_ms,
        "pdf": c.pdf, "source": (f"email from {c.source.get('from')}: {c.source.get('subject')}" if c.source else "upload"),
        **c.usage,
    }
    run["id"] = db.save_run(run)
    return run


def process(filename, pdf_bytes, on_stage=None, source=None):
    """Headless runner. on_stage(stage_dict) is called after each stage."""
    c = Ctx(filename, pdf_bytes, source)
    t0 = time.perf_counter()
    for key, title, fn in STAGES:
        st = run_stage(c, key, title, fn)
        if on_stage:
            on_stage(st)
    return decide(c, int((time.perf_counter() - t0) * 1000))
