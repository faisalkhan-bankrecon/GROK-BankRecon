"""Bank reconciliation matching engine.

Scores bank vs book transactions by amount, date proximity, reference,
and description overlap. Supports 1:1 and 1:N / N:1 combinations.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from itertools import combinations
from typing import Iterable

from .models import (
    Match,
    MatchMethod,
    MatchOptions,
    MatchStatus,
    Side,
    Transaction,
)

STOP_WORDS = {
    "the", "and", "for", "from", "with", "payment", "transfer", "online",
    "banking", "purchase", "contactless", "interac", "sent", "received",
    "debit", "credit", "card", "visa", "mastercard", "pos", "retail",
}


def _uid(prefix: str = "m") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date: {s}")


def days_between(a: str, b: str) -> int:
    return abs((parse_date(a) - parse_date(b)).days)


def tokenize(text: str) -> set[str]:
    """Split glued bank text: ContactlessInterac → contactless, interac; PETERSPIZZA kept whole."""
    s = text.lower()
    # camelCase / PascalCase boundaries
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower()
    s = re.sub(r"([a-z])(\d)", r"\1 \2", s)
    s = re.sub(r"(\d)([a-z])", r"\1 \2", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = {t for t in s.split() if len(t) > 2 and t not in STOP_WORDS}
    return tokens


def compact_alpha(text: str) -> str:
    """Letters only, lower — PETERSPIZZA / PETER PIZZA → peterspizza / peterpizza."""
    return re.sub(r"[^a-z]", "", text.lower())


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def description_similarity(bank_desc: str, book_desc: str) -> tuple[float, str | None]:
    """
    Fuzzy description score in [0, 1] with a short reason label.
    Handles glued bank merchants (PETERSPIZZA) vs spaced books (PETER PIZZA).
    """
    tb = tokenize(bank_desc)
    tk = tokenize(book_desc)
    jac = jaccard(tb, tk)

    cb = compact_alpha(bank_desc)
    ck = compact_alpha(book_desc)

    # Exact compact equality (rare)
    if cb and ck and cb == ck:
        return 1.0, "Description exact"

    # Substring containment either way (min length to avoid noise)
    contain = 0.0
    if cb and ck and len(cb) >= 4 and len(ck) >= 4:
        if ck in cb or cb in ck:
            contain = 0.85
        else:
            # Token-in-compact: book token "peter" inside bank compact "peterspizza"
            hits = 0
            meaningful = [t for t in tk if len(t) >= 4]
            if meaningful:
                for t in meaningful:
                    if t in cb:
                        hits += 1
                contain = max(contain, hits / len(meaningful) * 0.9)
            # Also bank tokens inside book compact
            meaningful_b = [t for t in tb if len(t) >= 4 and not t.isdigit()]
            if meaningful_b and ck:
                hits_b = sum(1 for t in meaningful_b if t in ck)
                contain = max(contain, hits_b / len(meaningful_b) * 0.75)

    # Shared long prefix/suffix of compact strings (PETERSPIZZA vs PETERPIZZA)
    prefix = 0.0
    if cb and ck and len(cb) >= 5 and len(ck) >= 5:
        # longest common substring (bounded)
        short, long = (ck, cb) if len(ck) <= len(cb) else (cb, ck)
        best = 0
        for i in range(len(short)):
            for j in range(i + 4, len(short) + 1):
                if short[i:j] in long:
                    best = max(best, j - i)
        if best >= 4:
            prefix = min(0.95, best / max(len(short), 1) * 0.95)

    score = max(jac, contain, prefix)
    if score >= 0.75:
        return score, "Strong description match"
    if score >= 0.4:
        return score, "Description overlap"
    if score > 0:
        return score, "Weak description match"
    return 0.0, None


def normalize_ref(ref: str | None) -> str | None:
    if not ref:
        return None
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", ref).upper()
    return cleaned or None


def score_pair(
    bank: Transaction,
    book: Transaction,
    window: int,
) -> tuple[int, list[str]] | None:
    """
    Primary keys: exact amount + date within window.
    Description / reference boost confidence and break ties.
    """
    if bank.amount_cents != book.amount_cents:
        return None
    d = days_between(bank.date, book.date)
    if d > window:
        return None

    reasons: list[str] = ["Amount exact"]
    score = 45  # amount is the anchor

    if d == 0:
        score += 30
        reasons.append("Same day")
    else:
        score += max(0, 28 - d * 7)
        reasons.append(f"Date {d}d apart")

    ref_b = normalize_ref(bank.reference)
    ref_k = normalize_ref(book.reference)
    if ref_b and ref_k and ref_b == ref_k:
        score += 18
        reasons.append(f"Ref {ref_b}")
    elif ref_b and ref_b.lower() in book.description.lower():
        score += 12
        reasons.append(f"Ref {ref_b} in books")
    elif ref_k and ref_k.lower() in bank.description.lower():
        score += 12
        reasons.append(f"Ref {ref_k} in bank")

    desc_score, desc_reason = description_similarity(bank.description, book.description)
    if desc_score > 0:
        score += round(desc_score * 30)  # up to +30
        if desc_reason:
            reasons.append(desc_reason)
    else:
        # Still proposable on amount+date alone — flag for human review
        reasons.append("No description match — verify")

    return min(100, score), reasons


def _sum_cents(txs: Iterable[Transaction]) -> int:
    return sum(t.amount_cents for t in txs)


def _combo_candidates(
    anchor: Transaction,
    pool: list[Transaction],
    max_size: int,
    window: int,
) -> list[tuple[list[Transaction], int, list[str]]]:
    """Find subsets of pool that sum to anchor.amount_cents within date window."""
    results: list[tuple[list[Transaction], int, list[str]]] = []
    sign = 1 if anchor.amount_cents >= 0 else -1
    candidates = [
        t
        for t in pool
        if days_between(t.date, anchor.date) <= window
        and (t.amount_cents == 0 or (t.amount_cents > 0) == (sign > 0))
    ]
    # Prefer same-sign non-zero; allow mixed only if needed — keep simple
    candidates = [t for t in candidates if (t.amount_cents >= 0) == (anchor.amount_cents >= 0)]

    for size in range(2, max_size + 1):
        if size > len(candidates):
            break
        for combo in combinations(candidates, size):
            if _sum_cents(combo) != anchor.amount_cents:
                continue
            # Score: base high for exact sum, penalize date spread, reward shared tokens
            dates = [parse_date(t.date) for t in combo] + [parse_date(anchor.date)]
            spread = (max(dates) - min(dates)).days
            score = 70 - min(spread * 4, 20)
            shared = tokenize(anchor.description)
            for t in combo:
                shared &= tokenize(t.description) if False else tokenize(t.description)
            # union overlap average
            overlaps = [
                jaccard(tokenize(anchor.description), tokenize(t.description))
                for t in combo
            ]
            avg_jac = sum(overlaps) / len(overlaps) if overlaps else 0
            score += round(avg_jac * 20)

            # reference hint
            ref = normalize_ref(anchor.reference)
            if ref and any(ref.lower() in t.description.lower() or normalize_ref(t.reference) == ref for t in combo):
                score += 10

            reasons = [
                f"Sum of {size} items = {anchor.amount_cents / 100:.2f}",
                f"Date spread {spread}d",
            ]
            if avg_jac >= 0.2:
                reasons.append("Shared description tokens")
            results.append((list(combo), min(100, score), reasons))
    return results


def run_auto_match(
    transactions: list[Transaction],
    existing_matches: list[Match],
    options: MatchOptions | None = None,
) -> list[Match]:
    """Propose new matches. Does not mutate matched transactions from accepted matches."""
    opts = options or MatchOptions()
    by_id = {t.id: t for t in transactions}

    locked: set[str] = set()
    kept: list[Match] = []
    for m in existing_matches:
        if m.status == MatchStatus.MATCHED:
            kept.append(m)
            locked.update(m.bank_ids)
            locked.update(m.book_ids)
        elif not opts.clear_existing_proposals:
            kept.append(m)
            locked.update(m.bank_ids)
            locked.update(m.book_ids)

    open_bank = [
        t for t in transactions
        if t.side == Side.BANK and t.id not in locked and t.status != MatchStatus.MATCHED
    ]
    open_book = [
        t for t in transactions
        if t.side == Side.BOOK and t.id not in locked and t.status != MatchStatus.MATCHED
    ]

    proposals: list[tuple[int, Match]] = []

    # 1:1 pairs
    for bank in open_bank:
        for book in open_book:
            scored = score_pair(bank, book, opts.date_window_days)
            if not scored:
                continue
            conf, reasons = scored
            if conf < opts.min_confidence:
                continue
            m = Match(
                id=_uid("p"),
                bank_ids=[bank.id],
                book_ids=[book.id],
                status=MatchStatus.PROPOSED,
                method=MatchMethod.AUTO,
                confidence=conf,
                reasons=reasons,
                bank_total_cents=bank.amount_cents,
                book_total_cents=book.amount_cents,
            )
            proposals.append((conf, m))

    # Greedy assign 1:1 by confidence
    used: set[str] = set()
    new_matches: list[Match] = []
    for conf, m in sorted(proposals, key=lambda x: -x[0]):
        ids = set(m.bank_ids + m.book_ids)
        if ids & used:
            continue
        used |= ids
        new_matches.append(m)

    remaining_bank = [t for t in open_bank if t.id not in used]
    remaining_book = [t for t in open_book if t.id not in used]

    # 1:N bank -> books
    combo_props: list[tuple[int, Match]] = []
    for bank in remaining_bank:
        for combo, conf, reasons in _combo_candidates(
            bank, remaining_book, opts.max_combo_size, opts.date_window_days
        ):
            if conf < opts.min_combo_confidence:
                continue
            m = Match(
                id=_uid("p"),
                bank_ids=[bank.id],
                book_ids=[t.id for t in combo],
                status=MatchStatus.PROPOSED,
                method=MatchMethod.AUTO,
                confidence=conf,
                reasons=reasons,
                bank_total_cents=bank.amount_cents,
                book_total_cents=_sum_cents(combo),
            )
            combo_props.append((conf, m))

    # N:1 books -> bank (multiple bank lines to one book) — rarer
    for book in remaining_book:
        for combo, conf, reasons in _combo_candidates(
            book, remaining_bank, opts.max_combo_size, opts.date_window_days
        ):
            if conf < opts.min_combo_confidence:
                continue
            m = Match(
                id=_uid("p"),
                bank_ids=[t.id for t in combo],
                book_ids=[book.id],
                status=MatchStatus.PROPOSED,
                method=MatchMethod.AUTO,
                confidence=conf,
                reasons=reasons,
                bank_total_cents=_sum_cents(combo),
                book_total_cents=book.amount_cents,
            )
            combo_props.append((conf, m))

    for conf, m in sorted(combo_props, key=lambda x: -x[0]):
        ids = set(m.bank_ids + m.book_ids)
        if ids & used:
            continue
        # also ensure still in remaining
        if any(i not in {t.id for t in remaining_bank + remaining_book} for i in ids):
            pass
        used |= ids
        new_matches.append(m)

    return kept + new_matches


def apply_match_status(
    transactions: list[Transaction],
    matches: list[Match],
) -> list[Transaction]:
    """Return new transaction list with status/match_id synced from matches."""
    matched_ids: dict[str, str] = {}
    proposed_ids: dict[str, str] = {}
    for m in matches:
        target = matched_ids if m.status == MatchStatus.MATCHED else proposed_ids
        for tid in m.bank_ids + m.book_ids:
            target[tid] = m.id

    out: list[Transaction] = []
    for t in transactions:
        nt = t.model_copy()
        if t.id in matched_ids:
            nt.status = MatchStatus.MATCHED
            nt.match_id = matched_ids[t.id]
        elif t.id in proposed_ids:
            nt.status = MatchStatus.PROPOSED
            nt.match_id = proposed_ids[t.id]
        else:
            nt.status = MatchStatus.OPEN
            nt.match_id = None
        out.append(nt)
    return out


def accept_matches(
    matches: list[Match],
    match_ids: list[str] | None = None,
    min_confidence: int | None = None,
) -> list[Match]:
    out: list[Match] = []
    for m in matches:
        nm = m.model_copy()
        if nm.status != MatchStatus.PROPOSED:
            out.append(nm)
            continue
        if match_ids is not None and nm.id not in match_ids:
            out.append(nm)
            continue
        if min_confidence is not None and nm.confidence < min_confidence:
            out.append(nm)
            continue
        nm.status = MatchStatus.MATCHED
        out.append(nm)
    return out


def reject_matches(matches: list[Match], match_ids: list[str]) -> list[Match]:
    reject = set(match_ids)
    return [m for m in matches if m.id not in reject]


def create_manual_match(
    transactions: list[Transaction],
    bank_ids: list[str],
    book_ids: list[str],
) -> Match:
    by_id = {t.id: t for t in transactions}
    banks = [by_id[i] for i in bank_ids]
    books = [by_id[i] for i in book_ids]
    if not banks or not books:
        raise ValueError("Select at least one bank and one book line")
    if any(t.side != Side.BANK for t in banks):
        raise ValueError("bank_ids must be bank transactions")
    if any(t.side != Side.BOOK for t in books):
        raise ValueError("book_ids must be book transactions")
    bt = _sum_cents(banks)
    kt = _sum_cents(books)
    if bt != kt:
        raise ValueError(
            f"Totals must match: bank {bt / 100:.2f} vs book {kt / 100:.2f}"
        )
    return Match(
        id=_uid("m"),
        bank_ids=bank_ids,
        book_ids=book_ids,
        status=MatchStatus.MATCHED,
        method=MatchMethod.MANUAL,
        confidence=100,
        reasons=["Manual match"],
        bank_total_cents=bt,
        book_total_cents=kt,
    )


def build_report(transactions: list[Transaction], matches: list[Match]) -> dict:
    """Classic bank recon identity using unmatched lines."""
    bank = [t for t in transactions if t.side == Side.BANK]
    book = [t for t in transactions if t.side == Side.BOOK]

    matched_bank_ids: set[str] = set()
    matched_book_ids: set[str] = set()
    for m in matches:
        if m.status == MatchStatus.MATCHED:
            matched_bank_ids.update(m.bank_ids)
            matched_book_ids.update(m.book_ids)

    unmatched_bank = [t for t in bank if t.id not in matched_bank_ids]
    unmatched_book = [t for t in book if t.id not in matched_book_ids]

    bank_total = _sum_cents(bank)
    book_total = _sum_cents(book)
    um_bank = _sum_cents(unmatched_bank)
    um_book = _sum_cents(unmatched_book)

    # Without opening balances, report activity totals + unmatched breakdown
    deposits_in_transit = sum(t.amount_cents for t in unmatched_book if t.amount_cents > 0)
    outstanding_payments = sum(t.amount_cents for t in unmatched_book if t.amount_cents < 0)
    unrecorded_bank_credits = sum(t.amount_cents for t in unmatched_bank if t.amount_cents > 0)
    unrecorded_bank_charges = sum(t.amount_cents for t in unmatched_bank if t.amount_cents < 0)

    # Activity difference explained by unmatched
    activity_diff = book_total - bank_total
    explained = um_book - um_bank

    accepted = [m for m in matches if m.status == MatchStatus.MATCHED]
    proposed = [m for m in matches if m.status == MatchStatus.PROPOSED]

    return {
        "bank_count": len(bank),
        "book_count": len(book),
        "bank_activity_cents": bank_total,
        "book_activity_cents": book_total,
        "activity_difference_cents": activity_diff,
        "matched_count": len(accepted),
        "proposed_count": len(proposed),
        "unmatched_bank_count": len(unmatched_bank),
        "unmatched_book_count": len(unmatched_book),
        "deposits_in_transit_cents": deposits_in_transit,
        "outstanding_payments_cents": outstanding_payments,
        "unrecorded_bank_credits_cents": unrecorded_bank_credits,
        "unrecorded_bank_charges_cents": unrecorded_bank_charges,
        "unmatched_bank_cents": um_bank,
        "unmatched_book_cents": um_book,
        "explained_difference_cents": explained,
        "ties": activity_diff == explained,
        "unmatched_bank": [t.model_dump() for t in unmatched_bank],
        "unmatched_book": [t.model_dump() for t in unmatched_book],
    }
