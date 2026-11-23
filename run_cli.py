"""Regression check: runs every sample in demo order and asserts the expected decision.
Usage: python run_cli.py [--reset]     (exit code 1 if any decision changed)
"""
import sys
from pathlib import Path

import db
from extract import parse_email
from pipeline import process

EXPECTED = {
    "01_happy_path.pdf": "APPROVED",
    "02_ec2_disguised_duplicate.pdf": "REJECTED",
    "03_ec1_split_part1.pdf": "APPROVED",
    "04_ec1_split_part2_over_po.pdf": "NEEDS_REVIEW",
    "05_ec3_missing_po.pdf": "NEEDS_REVIEW",
    "06_ec4_scanned_clean.pdf": "APPROVED",
    "07_ec4_scanned_total_mismatch.pdf": "NEEDS_REVIEW",
    "08_extra_gstin_impostor.pdf": "NEEDS_REVIEW",
    "09_extra_unknown_vendor.pdf": "REJECTED",
    "10_extra_missing_invoice_no.pdf": "NEEDS_REVIEW",
    "11_extra_within_tolerance.pdf": "APPROVED",
    "12_extra_tax_inclusive.pdf": "APPROVED",
    "13_email_with_invoice.eml > APX-7811.pdf": "APPROVED",
}

if "--reset" in sys.argv:
    db.reset()
elif len(db.list_runs()):
    print("Note: run history is not empty, so duplicate/PO-balance results may differ. Use --reset for a clean check.\n")

failed = 0
for p in sorted(p for p in (Path(__file__).parent / "samples").iterdir() if p.suffix in (".pdf", ".eml")):
    if p.suffix == ".eml":
        meta, pdfs = parse_email(p.read_bytes())
        jobs = [(f"{p.name} > {n}", b, meta) for n, b in pdfs]
    else:
        jobs = [(p.name, p.read_bytes(), None)]
    for name, data, meta in jobs:
        run = process(name, data, source=meta)
        want = EXPECTED.get(name)
        ok = want is None or run["decision"] == want
        failed += not ok
        tag = "PASS" if ok else "FAIL"
        extra = "" if ok else f"  (expected {want})"
        print(f"{tag}  #{run['id']:<3} {name:<42} {run['decision']:<13} [{run['extraction_method']}]{extra}")

print(f"\n{len(EXPECTED) - failed}/{len(EXPECTED)} passed" if not failed else f"\n{failed} FAILED")
sys.exit(1 if failed else 0)
