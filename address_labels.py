"""Exact-match address labels for 5Mef alerts (see _address_labels.json).
Never prefix/suffix-match: poisoning addresses share first/last chars with real ones."""
import json, os, re

LABELS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_address_labels.json")
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_cache = None


def load_labels(path=LABELS_PATH):
    """-> {full_address: label} for owners and their token accounts."""
    global _cache
    if _cache is not None and path == LABELS_PATH:
        return _cache
    out = {}
    try:
        d = json.load(open(path))
    except Exception:
        d = {}
    for e in d.get("labels") or []:
        lab = (e.get("label") or "").strip()
        for a in [e.get("owner")] + list(e.get("token_accounts") or []):
            if lab and isinstance(a, str) and _B58.match(a):
                out[a] = lab
    if path == LABELS_PATH:
        _cache = out
    return out


def label_for(*addrs):
    labels = load_labels()
    for a in addrs:
        if a and a in labels:      # exact, full-string match only
            return labels[a]
    return None


def short(a):
    return f"{a[:6]}…{a[-4:]}" if a else "?"


def party(wallet=None, token_account=None):
    """'Binance (depósito da 5Mef)' if labeled, else short address."""
    lab = label_for(wallet, token_account)
    return lab if lab else short(wallet or token_account)
