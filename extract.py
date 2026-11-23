"""Reading the invoice. Two extractors, same output schema:
  - LLM (Claude): handles any layout and scanned/image PDFs. Used when an API key is set.
  - Rules (regex on the text layer): offline fallback, digital PDFs only.
The extractor only READS. It never decides anything.
"""
import base64
import io
import re
from datetime import datetime

import pdfplumber

from email import policy
from email.parser import BytesParser

from config import ANTHROPIC_MODEL, PRICE_IN_PER_MTOK, PRICE_OUT_PER_MTOK, USD_TO_INR, get_api_key

FIELDS = ["vendor_name", "vendor_gstin", "buyer_gstin", "invoice_number", "invoice_date",
          "po_number", "currency", "line_items", "subtotal", "tax_total", "total"]

SCHEMA = {
    "type": "object",
    "properties": {
        "vendor_name": {"type": ["string", "null"], "description": "Seller / supplier legal name"},
        "vendor_gstin": {"type": ["string", "null"], "description": "Seller GSTIN (15 chars)"},
        "buyer_gstin": {"type": ["string", "null"], "description": "Bill-to / buyer GSTIN"},
        "invoice_number": {"type": ["string", "null"], "description": "Exactly as printed"},
        "invoice_date": {"type": ["string", "null"], "description": "ISO YYYY-MM-DD"},
        "po_number": {"type": ["string", "null"], "description": "Only if a PO / order reference is explicitly printed"},
        "currency": {"type": ["string", "null"]},
        "line_items": {"type": "array", "items": {"type": "object", "properties": {
            "description": {"type": "string"}, "quantity": {"type": ["number", "null"]},
            "unit_price": {"type": ["number", "null"]}, "amount": {"type": ["number", "null"]}}}},
        "subtotal": {"type": ["number", "null"], "description": "Taxable value before GST, only if printed"},
        "tax_total": {"type": ["number", "null"], "description": "Sum of GST components printed on the invoice; null if no tax line is printed"},
        "total": {"type": ["number", "null"], "description": "Grand total payable"},
    },
    "required": FIELDS,
}

PROMPT = """Extract the fields from this vendor invoice by calling record_invoice.
Rules: copy values exactly as printed; never guess or compute a missing value - use null;
dates as YYYY-MM-DD; amounts as plain numbers (no commas or currency);
po_number only if the invoice explicitly prints a PO / purchase order / order reference."""


def inspect_pdf(pdf_bytes):
    """Returns (text, page_count). Empty text => scanned / image-only."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        return text.strip(), len(pdf.pages)


# ---------------- LLM extractor ----------------
def extract_llm(pdf_bytes):
    import anthropic
    # Fail fast: a slow API falls back to the offline extractor instead of freezing the demo.
    client = anthropic.Anthropic(api_key=get_api_key(), timeout=30.0, max_retries=1)
    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2000,
        tools=[{"name": "record_invoice", "description": "Record extracted invoice fields",
                "input_schema": SCHEMA}],
        tool_choice={"type": "tool", "name": "record_invoice"},
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": base64.b64encode(pdf_bytes).decode()}},
            {"type": "text", "text": PROMPT}]}],
    )
    u = getattr(msg, "usage", None)
    tin, tout = getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0
    usage = {"tokens_in": tin, "tokens_out": tout,
             "cost_inr": round((tin * PRICE_IN_PER_MTOK + tout * PRICE_OUT_PER_MTOK) / 1e6 * USD_TO_INR, 2)}
    for block in msg.content:
        if block.type == "tool_use":
            return {k: block.input.get(k) for k in FIELDS}, usage
    raise RuntimeError("LLM returned no structured output")


# ---------------- Rules extractor (offline fallback) ----------------
NUM = r"([\d,]+\.\d{2})"
GSTIN_RE = r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b"


def _num(s):
    return float(s.replace(",", "")) if s else None


def _find(pattern, text, group=1, flags=re.I | re.M):
    m = re.search(pattern, text, flags)
    return m.group(group).strip() if m else None


def _date(s):
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def extract_rules(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    buyer_gstin = _find(r"(?:Buyer|Bill\s*To|Customer)\s*GSTIN\s*[:\-]?\s*" + GSTIN_RE, text)
    vendor_gstin = None
    for m in re.finditer(r"(Buyer|Bill\s*To|Customer)?\s*GSTIN\s*[:\-]?\s*" + GSTIN_RE, text, re.I):
        if not m.group(1):
            vendor_gstin = m.group(2)
            break

    items = []
    for l in lines:
        m = re.match(r"^\d*\.?\s*(.+?)\s+(\d[\d,]*)\s+" + NUM + r"\s+" + NUM + r"$", l)
        if m:
            items.append({"description": m.group(1), "quantity": _num(m.group(2)),
                          "unit_price": _num(m.group(3)), "amount": _num(m.group(4))})

    tax = [_num(m.group(1)) for m in re.finditer(r"^(?:CGST|SGST|IGST|GST)\b.*?" + NUM + r"\s*$", text, re.I | re.M)]

    return {
        "vendor_name": re.sub(r"\s*TAX\s+INVOICE\s*", " ", lines[0], flags=re.I).strip() if lines else None,
        "vendor_gstin": vendor_gstin,
        "buyer_gstin": buyer_gstin,
        "invoice_number": _find(r"(?:Invoice\s*(?:No|Number|#)|Bill\s*No)\.?\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-]*\d)", text),
        "invoice_date": _date(_find(r"(?:Invoice\s*Date|Dated|Date\s*of\s*Issue)\s*[:\-]?\s*([0-9]{1,2}[\-/ .][A-Za-z0-9]{2,3}[\-/ .][0-9]{4}|\d{4}-\d{2}-\d{2})", text)),
        "po_number": _find(r"(?:PO\s*(?:No|Number|#|Ref)?|Purchase\s*Order|Order\s*Ref)\.?\s*[:\-]?\s*(PO-?\d{3,})", text),
        "currency": "INR" if re.search(r"INR|Rs\.?|₹", text) else None,
        "line_items": items,
        "subtotal": _num(_find(r"(?:Sub\s*-?\s*total|Taxable\s*Value)\s*(?:\(INR\))?\s*[:]?\s*(?:INR|Rs\.?)?\s*" + NUM, text)),
        "tax_total": round(sum(tax), 2) if tax else None,
        "total": _num(_find(r"(?:Grand\s*Total|Total\s*Amount\s*Payable|Total\s*Due|Invoice\s*Total)\s*(?:\(INR\))?\s*[:]?\s*(?:INR|Rs\.?)?\s*" + NUM, text)),
    }


def ocr_text(pdf_bytes):
    """Offline OCR for scanned PDFs (needs the tesseract binary). Returns '' if unavailable."""
    try:
        import pypdfium2 as pdfium
        import pytesseract
        doc = pdfium.PdfDocument(pdf_bytes)
        pages = [doc[i].render(scale=300 / 72).to_pil() for i in range(len(doc))]
        return "\n".join(pytesseract.image_to_string(p, config="--psm 6") for p in pages).strip()
    except Exception:
        return ""


NO_USAGE = {"tokens_in": 0, "tokens_out": 0, "cost_inr": 0.0}


def extract(pdf_bytes, text):
    """Returns (fields, method, usage). method in {'llm-text', 'llm-vision', 'rules', 'ocr-rules', 'none'}."""
    if get_api_key():
        fields, usage = extract_llm(pdf_bytes)
        return fields, ("llm-text" if text else "llm-vision"), usage
    if text:
        return extract_rules(text), "rules", NO_USAGE
    scanned = ocr_text(pdf_bytes)
    if scanned:
        return extract_rules(scanned), "ocr-rules", NO_USAGE
    return None, "none", NO_USAGE


def parse_email(eml_bytes):
    """Saved email (.eml) -> (meta, [(filename, pdf_bytes), ...])."""
    msg = BytesParser(policy=policy.default).parsebytes(eml_bytes)
    meta = {"from": str(msg.get("From", "")), "subject": str(msg.get("Subject", "")), "date": str(msg.get("Date", ""))}
    pdfs = []
    for part in msg.iter_attachments():
        name = part.get_filename() or "attachment.pdf"
        if part.get_content_type() == "application/pdf" or name.lower().endswith(".pdf"):
            pdfs.append((name, part.get_payload(decode=True)))
    return meta, pdfs
