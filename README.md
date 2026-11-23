# AP Invoice Agent — Zamp ASA Case Study (PS-1)

Takes a vendor invoice PDF, runs 8 visible stages, and returns **APPROVED / NEEDS_REVIEW / REJECTED** with the reasons, the next action and a drafted vendor email.
Claude only **reads** the invoice. Deterministic rules make every **decision**.

## Run locally

**Windows, one click:** double-click `run.bat` (creates `.venv`, installs packages, runs the regression check, opens the app).

**Any OS, manually:**

```bash
pip install -r requirements.txt
python make_samples.py          # writes 12 test invoices + 1 email (.eml) to samples/
python run_cli.py --reset       # headless check: prints the decision for each
streamlit run app.py            # UI: live run view + dashboard
```

Optional: set `ANTHROPIC_API_KEY` (env var or `.streamlit/secrets.toml`) to use Claude for extraction, including scanned PDFs.
Without a key the app runs offline: regex on the text layer, Tesseract OCR for scans (install `tesseract-ocr` for that).

## Deploy (live link)

On share.streamlit.io: Create app → this repo, branch `main`, file `app.py` → Advanced settings: Python 3.12, Secrets below → Deploy.

Push to GitHub → share.streamlit.io → New app → `app.py`. In Secrets add `ANTHROPIC_API_KEY = "..."` and `ADMIN_CODE = "..."`. `packages.txt` installs Tesseract.

## Stages

1. Intake (hash, page count, text-layer detection)
2. Extraction (Claude → regex → OCR fallback)
3. Field validation (required fields, date, GSTIN checksum, arithmetic, GST rate)
4. Vendor verification (GSTIN, then fuzzy name; flags name/GSTIN mismatch)
5. Duplicate check (file hash, normalised invoice number, same amount + date)
6. PO matching (explicit, or inferred from vendor's open POs → always review)
7. Amount reconciliation (open PO balance from approved invoices; tolerance min(2%, INR 5,000))
8. Decision (any reject-fail → REJECTED; any fail → NEEDS_REVIEW; else APPROVED)

All thresholds live in `config.py`.

## Demo order (click "Reset demo data" first)

| # | File | Expected |
|---|---|---|
| 1 | 01_happy_path | APPROVED |
| 2 | 02_ec2_disguised_duplicate | REJECTED (duplicate of #1) |
| 3 | 03_ec1_split_part1 | APPROVED (partial) |
| 4 | 04_ec1_split_part2_over_po | NEEDS_REVIEW (PO overrun) |
| 5 | 05_ec3_missing_po | NEEDS_REVIEW (suggests PO-4503) |
| 6 | 06_ec4_scanned_clean | APPROVED |
| 7 | 07_ec4_scanned_total_mismatch | NEEDS_REVIEW |
| 8 | 08_extra_gstin_impostor | NEEDS_REVIEW |
| 9 | 09_extra_unknown_vendor | REJECTED |
| 10 | 10_extra_missing_invoice_no | NEEDS_REVIEW |
| 11 | 11_extra_within_tolerance | APPROVED (with note) |
| 12 | 12_extra_tax_inclusive | APPROVED (GST backed out of a tax-inclusive total) |
| 13 | 13_email_with_invoice.eml | APPROVED (PDF pulled from the email; sender domain checked) |

## Live extras

- Input: PDF or a saved email (.eml); every PDF attachment is processed, sender domain is checked against the vendor master.
- Reference data page: add a vendor or PO on the spot, then press "Re-run" on the run in the Dashboard (old run is marked SUPERSEDED).
- Process page shows the PDF beside the stages; Dashboard shows INR approved / held / blocked / duplicates stopped, and each run's PDF.
- "ERP posting file" button: CSV of approved invoices with taxable value, GST and due date.
- AI tokens and estimated INR cost per run (set `PRICE_IN_PER_MTOK`, `PRICE_OUT_PER_MTOK`, `USD_TO_INR` to your model's price).

## Lock the public link

Set `ADMIN_CODE = "..."` in Streamlit secrets. Then Reset, add vendor/PO, reviewer approve/reject and Re-run need that passcode (sidebar). Anyone can still process invoices.

## Known limitations

- SQLite on Streamlit Cloud resets on app restart (fine for a demo; use Postgres in production).
- The offline regex extractor only knows the two sample templates; Claude is the general extractor.
