"""Generates realistic test invoices into samples/. Two vendor layouts + scanned (image-only) PDFs.
Run: python make_samples.py
"""
import io
import random
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent / "samples"
BUYER = ("Zentra Foods Pvt Ltd", "Plot 14, MIDC Bhosari, Pune 411026", "27AAACZ4321B1ZR")


def money(x):
    whole, frac = f"{x:.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    if head:
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head:
            parts.insert(0, head)
        return ",".join(parts) + "," + tail + "." + frac
    return tail + "." + frac


def layout_a(c, v):
    """Clean corporate layout: 'Invoice No', CGST/SGST rows, 'Grand Total'."""
    W, H = A4
    c.setFont("Helvetica-Bold", 16); c.drawString(40, H - 50, v["vendor"])
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 66, v["address"])
    c.drawString(40, H - 78, f"GSTIN: {v['gstin']}")
    c.setFont("Helvetica-Bold", 13); c.drawRightString(W - 40, H - 50, "TAX INVOICE")
    c.setFont("Helvetica", 10)
    y = H - 115
    if v.get("number"):
        c.drawString(40, y, f"Invoice No: {v['number']}")
    c.drawString(300, y, f"Invoice Date: {v['date_a']}")
    if v.get("po"):
        c.drawString(40, y - 15, f"PO Number: {v['po']}")
    y -= 45
    c.setFont("Helvetica-Bold", 10); c.drawString(40, y, "Bill To:")
    c.setFont("Helvetica", 10)
    c.drawString(40, y - 14, BUYER[0]); c.drawString(40, y - 27, BUYER[1])
    c.drawString(40, y - 40, f"Buyer GSTIN: {v.get('buyer_gstin', BUYER[2])}")
    y -= 75
    c.setFont("Helvetica-Bold", 10)
    for x, t in [(40, "#"), (60, "Description"), (330, "Qty"), (400, "Rate"), (480, "Amount")]:
        c.drawString(x, y, t)
    c.line(40, y - 5, W - 40, y - 5)
    c.setFont("Helvetica", 10)
    for i, (d, q, r) in enumerate(v["lines"], 1):
        y -= 20
        c.drawString(40, y, str(i)); c.drawString(60, y, d)
        c.drawString(330, y, f"{q:,}"); c.drawString(400, y, money(r)); c.drawString(480, y, money(q * r))
    y -= 15; c.line(40, y, W - 40, y)
    rows = ([] if v.get("inclusive") else [("Subtotal", v["subtotal"])]) + v["taxes"] + [("Grand Total (INR)", v["total"])]
    for label, amt in rows:
        y -= 18
        c.setFont("Helvetica-Bold" if "Total" in label else "Helvetica", 10)
        c.drawString(330, y, label); c.drawString(480, y, money(amt))
    c.setFont("Helvetica", 8)
    c.drawString(40, 80, v.get("footer", "Payment by NEFT within agreed terms. This is a computer generated invoice."))


def layout_b(c, v):
    """Different vendor template: 'Bill No.', 'Dated', 'Order Ref', 'Taxable Value', 'Total Amount Payable'."""
    W, H = A4
    c.setFillColorRGB(0.12, 0.25, 0.45); c.rect(0, H - 95, W, 95, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 18); c.drawString(40, H - 45, v["vendor"])
    c.setFont("Helvetica", 9); c.drawString(40, H - 62, v["address"])
    c.drawString(40, H - 75, f"GSTIN: {v['gstin']}")
    c.setFillColorRGB(0, 0, 0)
    y = H - 130
    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"Bill No.: {v['number']}"); c.drawString(250, y, f"Dated: {v['date_b']}")
    if v.get("po"):
        c.drawString(420, y, f"Order Ref: {v['po']}")
    y -= 30
    c.drawString(40, y, f"Customer: {BUYER[0]}, {BUYER[1]}")
    c.drawString(40, y - 14, f"Customer GSTIN: {v.get('buyer_gstin', BUYER[2])}")
    y -= 50
    c.setFont("Helvetica-Bold", 10)
    for x, t in [(40, "Particulars"), (330, "Qty"), (390, "Unit Price"), (480, "Amount")]:
        c.drawString(x, y, t)
    c.setFont("Helvetica", 10)
    for d, q, r in v["lines"]:
        y -= 20
        c.drawString(40, y, d); c.drawString(330, y, f"{q:,}"); c.drawString(390, y, money(r)); c.drawString(480, y, money(q * r))
    y -= 30
    rows = [("Taxable Value", v["subtotal"])] + v["taxes"] + [("Total Amount Payable", v["total"])]
    for label, amt in rows:
        c.setFont("Helvetica-Bold" if "Total" in label else "Helvetica", 10)
        c.drawString(300, y, label); c.drawString(470, y, f"Rs. {money(amt)}")
        y -= 18


def build(v):
    sub = round(sum(q * r for _, q, r in v["lines"]), 2)
    v.setdefault("subtotal", sub)
    if v.get("inclusive"):
        v["taxes"] = []
    elif v.get("intra"):
        half = round(sub * v["rate"] / 2, 2)
        v["taxes"] = [(f"CGST @ {v['rate']*50:g}%", half), (f"SGST @ {v['rate']*50:g}%", half)]
    else:
        v["taxes"] = [(f"IGST @ {v['rate']*100:g}%", round(sub * v["rate"], 2))]
    v.setdefault("total", round(v["subtotal"] + sum(t for _, t in v["taxes"]), 2))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    (layout_a if v["layout"] == "A" else layout_b)(c, v)
    c.showPage(); c.save()
    return buf.getvalue()


def to_scan(pdf_bytes):
    """Rasterise, tilt, blur, add noise -> image-only PDF with no text layer."""
    img = pdfium.PdfDocument(pdf_bytes)[0].render(scale=150 / 72).to_pil().convert("L")
    img = img.rotate(0.7, expand=True, fillcolor=255).filter(ImageFilter.GaussianBlur(0.6))
    random.seed(7)
    px = img.load()
    for _ in range(6000):
        x, y = random.randrange(img.width), random.randrange(img.height)
        px[x, y] = random.choice([150, 200, 230])
    out = io.BytesIO()
    img.save(out, "PDF", resolution=150)
    return out.getvalue()


V001 = dict(vendor="Shree Ganesh Packaging Pvt Ltd", address="Gat 221, Chakan Industrial Area, Pune 410501", gstin="27AABCS1234F1ZR", intra=True, rate=0.18)
V002 = dict(vendor="Nimbus Cloud Services LLP", address="4th Floor, Embassy Tech Village, Bengaluru 560103", gstin="29AAFFN5678K1ZJ", intra=False, rate=0.18)
V003 = dict(vendor="Apex Industrial Supplies", address="B-42, Okhla Phase II, New Delhi 110020", gstin="07AAKPA9012L1ZQ", intra=False, rate=0.18)
V004 = dict(vendor="Kaveri Logistics Pvt Ltd", address="17 Ennore High Road, Chennai 600057", gstin="33AACCK3456M1ZE", intra=False, rate=0.12)

SAMPLES = {
    "01_happy_path.pdf": {**V001, "layout": "A", "number": "INV-1042", "date_a": "10-Nov-2026", "date_b": "10/11/2026", "po": "PO-4501",
                          "lines": [("5-ply corrugated boxes 18x12x10", 2000, 60.00)]},
    "02_ec2_disguised_duplicate.pdf": {**V001, "layout": "B", "number": "INV/001042", "date_a": "10-Nov-2026", "date_b": "10/11/2026", "po": "PO-4501",
                                       "lines": [("5-ply corrugated boxes 18x12x10", 2000, 60.00)]},
    "03_ec1_split_part1.pdf": {**V002, "layout": "B", "number": "NCS/2026/0871", "date_a": "01-Nov-2026", "date_b": "01/11/2026", "po": "PO-4502",
                               "lines": [("Cloud hosting - October 2026", 1, 150000.00), ("Managed database - October 2026", 1, 100000.00)]},
    "04_ec1_split_part2_over_po.pdf": {**V002, "layout": "B", "number": "NCS/2026/0934", "date_a": "15-Nov-2026", "date_b": "15/11/2026", "po": "PO-4502",
                                       "lines": [("Cloud hosting - November 2026", 1, 110000.00), ("Managed database - November 2026", 1, 70000.00)]},
    "05_ec3_missing_po.pdf": {**V003, "layout": "A", "number": "APX-7781", "date_a": "12-Nov-2026", "date_b": "12/11/2026", "po": None,
                              "lines": [("Safety helmets ISI marked", 200, 250.00), ("Nitrile gloves (pairs)", 1000, 24.00), ("Reflective vests", 100, 100.00)]},
    "06_ec4_scanned_clean.pdf": {**V001, "layout": "A", "number": "INV-1088", "date_a": "14-Nov-2026", "date_b": "14/11/2026", "po": "PO-4505",
                                 "lines": [("Printed product labels 50x25mm", 50000, 1.20)], "scan": True},
    "07_ec4_scanned_total_mismatch.pdf": {**V001, "layout": "A", "number": "INV-1091", "date_a": "16-Nov-2026", "date_b": "16/11/2026", "po": "PO-4505",
                                          "lines": [("Printed product labels 50x25mm", 2000, 1.20)], "total": 2838.00, "scan": True},
    "08_extra_gstin_impostor.pdf": {**V003, "gstin": "07AAKPA9999Q1ZK", "layout": "A", "number": "APX-7790", "date_a": "17-Nov-2026", "date_b": "17/11/2026", "po": "PO-4504",
                                    "lines": [("Hydraulic pump seal kit", 40, 2500.00)],
                                    "footer": "NOTE: We have changed our bank. Please remit to HDFC A/c ending 4471 with immediate effect."},
    "09_extra_unknown_vendor.pdf": {**V004, "vendor": "Bright Star Traders", "address": "Ring Road, Surat 395002", "gstin": "24AADCB7777H1ZG",
                                    "layout": "A", "number": "BST-221", "date_a": "18-Nov-2026", "date_b": "18/11/2026", "po": "PO-4501", "rate": 0.18,
                                    "lines": [("Stretch wrap film rolls", 300, 180.00)]},
    "10_extra_missing_invoice_no.pdf": {**V004, "layout": "A", "number": None, "date_a": "19-Nov-2026", "date_b": "19/11/2026", "po": "PO-4506",
                                        "lines": [("Freight Mumbai to Pune - full truck load", 1, 45000.00)]},
    "11_extra_within_tolerance.pdf": {**V003, "layout": "A", "number": "APX-7802", "date_a": "18-Nov-2026", "date_b": "18/11/2026", "po": "PO-4504",
                                      "lines": [("Hydraulic pump spares kit", 10, 20000.00), ("Seals and gaskets assortment", 1, 13800.00)]},
    "12_extra_tax_inclusive.pdf": {**V004, "layout": "A", "number": "KLP-5520", "date_a": "20-Nov-2026", "date_b": "20/11/2026", "po": "PO-4507",
                                   "inclusive": True, "lines": [("Loading and unloading charges - Pune warehouse", 1, 29500.00)],
                                   "footer": "All prices are inclusive of GST @ 18%."},
}

EMAIL_INVOICE = {**V003, "layout": "A", "number": "APX-7811", "date_a": "21-Nov-2026", "date_b": "21/11/2026", "po": "PO-4508",
                 "lines": [("Industrial adhesive cartridges", 400, 100.00)]}


def make_email(pdf_bytes):
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"] = "Apex Accounts <ar@apexindustrial.in>"
    m["To"] = "ap@zentrafoods.in"
    m["Subject"] = "Invoice APX-7811 for PO-4508"
    m["Date"] = "Sat, 21 Nov 2026 10:14:00 +0530"
    m.set_content("Dear team,\n\nPlease find attached our invoice APX-7811 against PO-4508.\n\nRegards,\nApex Industrial Supplies")
    m.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename="APX-7811.pdf")
    return bytes(m)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, v in SAMPLES.items():
        data = build(dict(v))
        if v.get("scan"):
            data = to_scan(data)
        (OUT / name).write_bytes(data)
        print("wrote", name)
    (OUT / "13_email_with_invoice.eml").write_bytes(make_email(build(dict(EMAIL_INVOICE))))
    print("wrote 13_email_with_invoice.eml")
