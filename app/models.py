"""Shared data models for the bank reconciliation engine."""

from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class Side(str, Enum):
    BANK = "bank"
    BOOK = "book"


class MatchStatus(str, Enum):
    OPEN = "open"
    PROPOSED = "proposed"
    MATCHED = "matched"


class Transaction(BaseModel):
    id: str
    side: Side
    date: str  # YYYY-MM-DD
    description: str
    amount_cents: int  # signed: + inflow, - outflow
    reference: Optional[str] = None
    balance_cents: Optional[int] = None
    match_id: Optional[str] = None
    status: MatchStatus = MatchStatus.OPEN
    source_file: Optional[str] = None
    raw: Optional[str] = None

    @property
    def amount(self) -> float:
        return self.amount_cents / 100.0


class MatchMethod(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"
    RULE = "rule"


class Match(BaseModel):
    id: str
    bank_ids: list[str]
    book_ids: list[str]
    status: MatchStatus  # proposed or matched
    method: MatchMethod
    confidence: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    bank_total_cents: int = 0
    book_total_cents: int = 0


class PeriodMeta(BaseModel):
    company: str = ""
    account_label: str = ""
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    opening_bank_cents: Optional[int] = None
    opening_book_cents: Optional[int] = None
    closing_bank_cents: Optional[str] = None  # kept flexible


class ReconState(BaseModel):
    transactions: list[Transaction] = Field(default_factory=list)
    matches: list[Match] = Field(default_factory=list)
    period: PeriodMeta = Field(default_factory=PeriodMeta)


class ParseResult(BaseModel):
    transactions: list[Transaction]
    opening_balance_cents: Optional[int] = None
    closing_balance_cents: Optional[int] = None
    account_label: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


class MatchOptions(BaseModel):
    date_window_days: int = 3
    min_confidence: int = 48
    min_combo_confidence: int = 62
    max_combo_size: int = 3
    clear_existing_proposals: bool = True


class ManualMatchRequest(BaseModel):
    bank_ids: list[str]
    book_ids: list[str]


class AcceptMatchesRequest(BaseModel):
    match_ids: Optional[list[str]] = None  # None = all proposed
    min_confidence: Optional[int] = None


class UploadSide(str, Enum):
    BANK = "bank"
    BOOK = "book"
