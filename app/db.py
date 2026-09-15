"""Postgres persistence for per-user reconciliation state."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from .models import Match, MatchMethod, MatchStatus, PeriodMeta, ReconState, Side, Transaction


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    return dsn


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        yield conn


def ensure_workspace(user_id: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO workspaces (user_id, period)
            VALUES (%s, '{}'::jsonb)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (user_id,),
        )
        conn.commit()


def load_state(user_id: str) -> ReconState:
    ensure_workspace(user_id)
    with connect() as conn:
        period_row = conn.execute(
            "SELECT period FROM workspaces WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        period_data = period_row["period"] if period_row else {}
        if isinstance(period_data, str):
            period_data = json.loads(period_data)
        period = PeriodMeta(**(period_data or {}))

        tx_rows = conn.execute(
            """
            SELECT id, side, date::text, description, amount_cents, reference,
                   balance_cents, match_id, status, source_file, raw
            FROM transactions
            WHERE user_id = %s
            ORDER BY date, id
            """,
            (user_id,),
        ).fetchall()

        match_rows = conn.execute(
            """
            SELECT id, bank_ids, book_ids, status, method, confidence,
                   reasons, bank_total_cents, book_total_cents
            FROM matches
            WHERE user_id = %s
            ORDER BY created_at, id
            """,
            (user_id,),
        ).fetchall()

    txs = [
        Transaction(
            id=r["id"],
            side=Side(r["side"]),
            date=str(r["date"])[:10],
            description=r["description"] or "",
            amount_cents=r["amount_cents"],
            reference=r["reference"],
            balance_cents=r["balance_cents"],
            match_id=r["match_id"],
            status=MatchStatus(r["status"]),
            source_file=r["source_file"],
            raw=r["raw"],
        )
        for r in tx_rows
    ]

    matches = [
        Match(
            id=r["id"],
            bank_ids=list(r["bank_ids"] or []),
            book_ids=list(r["book_ids"] or []),
            status=MatchStatus(r["status"]),
            method=MatchMethod(r["method"]),
            confidence=r["confidence"] or 0,
            reasons=list(r["reasons"] or []),
            bank_total_cents=r["bank_total_cents"] or 0,
            book_total_cents=r["book_total_cents"] or 0,
        )
        for r in match_rows
    ]

    return ReconState(transactions=txs, matches=matches, period=period)


def save_period(user_id: str, period: PeriodMeta) -> None:
    ensure_workspace(user_id)
    with connect() as conn:
        conn.execute(
            """
            UPDATE workspaces
            SET period = %s, updated_at = NOW()
            WHERE user_id = %s
            """,
            (Json(period.model_dump()), user_id),
        )
        conn.commit()


def replace_transactions_for_side(
    user_id: str,
    side: Side,
    new_txs: list[Transaction],
    *,
    replace: bool,
) -> None:
    """If replace=True, delete existing txs for side (and cascading match cleanup)."""
    with connect() as conn:
        if replace:
            old = conn.execute(
                "SELECT id FROM transactions WHERE user_id = %s AND side = %s",
                (user_id, side.value),
            ).fetchall()
            old_ids = {r["id"] for r in old}
            if old_ids:
                # Drop matches that reference removed txs
                match_rows = conn.execute(
                    "SELECT id, bank_ids, book_ids FROM matches WHERE user_id = %s",
                    (user_id,),
                ).fetchall()
                for m in match_rows:
                    used = set(m["bank_ids"] or []) | set(m["book_ids"] or [])
                    if used & old_ids:
                        conn.execute(
                            "DELETE FROM matches WHERE id = %s AND user_id = %s",
                            (m["id"], user_id),
                        )
                conn.execute(
                    "DELETE FROM transactions WHERE user_id = %s AND side = %s",
                    (user_id, side.value),
                )

        for t in new_txs:
            conn.execute(
                """
                INSERT INTO transactions (
                  id, user_id, side, date, description, amount_cents, reference,
                  balance_cents, match_id, status, source_file, raw
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                  side = EXCLUDED.side,
                  date = EXCLUDED.date,
                  description = EXCLUDED.description,
                  amount_cents = EXCLUDED.amount_cents,
                  reference = EXCLUDED.reference,
                  balance_cents = EXCLUDED.balance_cents,
                  match_id = EXCLUDED.match_id,
                  status = EXCLUDED.status,
                  source_file = EXCLUDED.source_file,
                  raw = EXCLUDED.raw
                """,
                (
                    t.id,
                    user_id,
                    t.side.value,
                    t.date,
                    t.description,
                    t.amount_cents,
                    t.reference,
                    t.balance_cents,
                    t.match_id,
                    t.status.value,
                    t.source_file,
                    t.raw,
                ),
            )
        conn.commit()


def save_full_state(user_id: str, state: ReconState) -> None:
    """Replace all transactions + matches for user with the given state."""
    ensure_workspace(user_id)
    with connect() as conn:
        conn.execute(
            "UPDATE workspaces SET period = %s, updated_at = NOW() WHERE user_id = %s",
            (Json(state.period.model_dump()), user_id),
        )
        conn.execute("DELETE FROM matches WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM transactions WHERE user_id = %s", (user_id,))

        for t in state.transactions:
            conn.execute(
                """
                INSERT INTO transactions (
                  id, user_id, side, date, description, amount_cents, reference,
                  balance_cents, match_id, status, source_file, raw
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s
                )
                """,
                (
                    t.id,
                    user_id,
                    t.side.value,
                    t.date,
                    t.description,
                    t.amount_cents,
                    t.reference,
                    t.balance_cents,
                    t.match_id,
                    t.status.value,
                    t.source_file,
                    t.raw,
                ),
            )

        for m in state.matches:
            conn.execute(
                """
                INSERT INTO matches (
                  id, user_id, bank_ids, book_ids, status, method, confidence,
                  reasons, bank_total_cents, book_total_cents
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s
                )
                """,
                (
                    m.id,
                    user_id,
                    m.bank_ids,
                    m.book_ids,
                    m.status.value,
                    m.method.value,
                    m.confidence,
                    m.reasons,
                    m.bank_total_cents,
                    m.book_total_cents,
                ),
            )
        conn.commit()


def reset_user(user_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM matches WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM transactions WHERE user_id = %s", (user_id,))
        conn.execute(
            """
            INSERT INTO workspaces (user_id, period)
            VALUES (%s, '{}'::jsonb)
            ON CONFLICT (user_id) DO UPDATE SET period = '{}'::jsonb, updated_at = NOW()
            """,
            (user_id,),
        )
        conn.commit()


def delete_transaction(user_id: str, tx_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM transactions WHERE id = %s AND user_id = %s",
            (tx_id, user_id),
        )
        # Remove matches that referenced it
        rows = conn.execute(
            "SELECT id, bank_ids, book_ids FROM matches WHERE user_id = %s",
            (user_id,),
        ).fetchall()
        for m in rows:
            used = set(m["bank_ids"] or []) | set(m["book_ids"] or [])
            if tx_id in used:
                conn.execute(
                    "DELETE FROM matches WHERE id = %s AND user_id = %s",
                    (m["id"], user_id),
                )
        conn.commit()
