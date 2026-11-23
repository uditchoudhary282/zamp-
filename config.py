"""Business rules in one place. Change a number here, not in the logic."""
import os
from pathlib import Path

BASE = Path(__file__).parent
DATA = BASE / "data"
DB_PATH = Path(os.getenv("AP_DB_PATH", BASE / "runs.db"))

# Our company (the buyer)
BUYER_NAME = "Zentra Foods Pvt Ltd"
BUYER_GSTIN = "27AAACZ4321B1ZR"

# A3: overage over PO balance allowed up to min(2% of PO, INR 5,000)
PO_TOLERANCE_PCT = 0.02
PO_TOLERANCE_CAP = 5000.0

# Arithmetic checks (lines -> subtotal, subtotal + tax -> total)
MATH_PASS_ABS = 1.0        # rounding
MATH_WARN_ABS = 10.0       # digital PDFs: small rounding drift = warning
SCANNED_MATH_ABS = 1.0     # A7: scans get no slack beyond rounding

VALID_GST_RATES = [0.05, 0.12, 0.18, 0.28]
INCLUSIVE_GST_RATE = 0.18   # used to back out GST when no tax line is printed
GST_RATE_SLACK = 0.005

VENDOR_NAME_MATCH = 88     # rapidfuzz token_set_ratio threshold
PO_INFER_MIN_SCORE = 0.60
PO_INFER_MIN_MARGIN = 0.15

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")

# Cost display only. Set these to your model's current list price (USD per million tokens).
PRICE_IN_PER_MTOK = float(os.getenv("PRICE_IN_PER_MTOK", 3.0))
PRICE_OUT_PER_MTOK = float(os.getenv("PRICE_OUT_PER_MTOK", 15.0))
USD_TO_INR = float(os.getenv("USD_TO_INR", 84.0))


def _secret(name):
    if os.getenv(name):
        return os.getenv(name)
    try:  # Streamlit Cloud secrets
        import streamlit as st
        return st.secrets.get(name)
    except Exception:
        return None


def get_api_key():
    return _secret("ANTHROPIC_API_KEY")


def get_admin_code():
    """If set, Reset / add data / reviewer actions need this passcode. Unset = open (local dev)."""
    return _secret("ADMIN_CODE")
