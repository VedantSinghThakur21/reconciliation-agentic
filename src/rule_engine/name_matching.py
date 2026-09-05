from __future__ import annotations
"""
Canonical customer-name matching for reconciliation.

Shared by the deterministic rule engine and the AI semantic matcher so both
tiers agree on the same similarity score for a given sender/customer pair.
"""

import re
from difflib import SequenceMatcher

# Bank narration fields truncate the payer name to a fixed width (commonly 15
# chars). Below this length a shared prefix is too weak to treat as a truncation
# of the same name — a 3-letter prefix would match many unrelated customers.
MIN_TRUNCATION_PREFIX = 6


def strip_legal_suffix(name: str) -> str:
    """
    Remove common legal suffixes for cleaner comparison.

    Each suffix optionally matches a trailing 's' — bank statements and ERP
    records often disagree on singular/plural forms of the same legal name.
    Without this, only one grammatical form got stripped and the two names
    diverged enough to miss the fuzzy-match threshold.
    """
    return re.sub(
        r"\b(pvt|ltd|private|limited|inc|corp|llc|llp|co\.?|industr(?:y|ies)|"
        r"service(?:s)?|enterprise(?:s)?|group(?:s)?|solution(?:s)?|"
        r"technolog(?:y|ies)|tech)\b",
        "", name.strip().lower(), flags=re.IGNORECASE
    ).strip()


def normalize_name(name: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — no suffix removal."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", name.strip().lower())).strip()


def name_similarity(a: str, b: str) -> float:
    """
    Similarity between a bank sender name and an invoice payer name.

    Suffix stripping alone is not enough when the bank truncated the name
    mid-suffix (e.g. a ~15-char narration clip). Comparing a fully stripped
    name against a truncated fragment can score under the fuzzy threshold and
    fall through to amount-proximity matching against unrelated invoices.

    So check truncation first — if one name is a prefix of the other at a
    meaningful length, treat them as the same party — then fall back to the
    stripped comparison for names that differ in legal form rather than length.
    """
    if not a or not b:
        return 0.0

    norm_a, norm_b = normalize_name(a), normalize_name(b)
    if not norm_a or not norm_b:
        return 0.0

    shorter, longer = (norm_a, norm_b) if len(norm_a) <= len(norm_b) else (norm_b, norm_a)
    if len(shorter) >= MIN_TRUNCATION_PREFIX and longer.startswith(shorter):
        return 1.0

    stripped_a, stripped_b = strip_legal_suffix(a), strip_legal_suffix(b)
    if not stripped_a or not stripped_b:
        # A name made up entirely of legal suffixes carries no distinguishing
        # content. SequenceMatcher scores two empty results as 1.0.
        return 0.0

    return SequenceMatcher(None, stripped_a, stripped_b).ratio()


def is_same_customer(sender: str, customer: str, fuzzy_threshold: float = 0.65) -> bool:
    return name_similarity(sender, customer) >= fuzzy_threshold
