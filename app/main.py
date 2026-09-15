"""FastAPI application — multi-user bank reconciliation engine + web UI."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import AuthUser, require_user
from .db import (
    delete_transaction as db_delete_transaction,
    load_state,
    replace_transactions_for_side,
    reset_user,
    save_full_state,
    save_period,
)
from .engine import (
    accept_matches,
    apply_match_status,
    build_report,
    create_manual_match,
    reject_matches,
    run_auto_match,
)
from .export import export_csv_bytes, export_xlsx_bytes
from .models import (
    AcceptMatchesRequest,
    ManualMatchRequest,
    MatchOptions,
    MatchStatus,
    PeriodMeta,
    Side,
    Transaction,
)
from .parsers import parse_file

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Concord Bank Recon", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _snapshot_for(user_id: str) -> dict:
    state = load_state(user_id)
    txs = apply_match_status(state.transactions, state.matches)
    # Persist status updates derived from matches
    state.transactions = txs
    save_full_state(user_id, state)
    return {
        "transactions": [t.model_dump() for t in txs],
        "matches": [m.model_dump() for m in state.matches],
        "period": state.period.model_dump(),
        "report": build_report(txs, state.matches),
    }


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "auth_configured": bool(os.environ.get("SUPABASE_JWT_SECRET")),
        "db_configured": bool(os.environ.get("DATABASE_URL")),
    }


@app.get("/api/me")
def me(user: AuthUser = Depends(require_user)):
    return {"id": user.id, "email": user.email}


@app.get("/api/config")
def public_config():
    """Public keys needed by the browser for Supabase Google sign-in."""
    return {
        "supabase_url": os.environ.get("SUPABASE_URL", ""),
        "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
    }


@app.get("/api/state")
def get_state(user: AuthUser = Depends(require_user)):
    return _snapshot_for(user.id)


@app.post("/api/reset")
def reset_state(user: AuthUser = Depends(require_user)):
    reset_user(user.id)
    return _snapshot_for(user.id)


@app.post("/api/upload")
async def upload(
    side: str = Form(...),
    file: UploadFile = File(...),
    replace: bool = Form(False),
    user: AuthUser = Depends(require_user),
):
    if side not in ("bank", "book"):
        raise HTTPException(400, "side must be 'bank' or 'book'")
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    filename = file.filename or "upload"
    try:
        result = parse_file(content, filename, Side(side))
    except Exception as e:
        raise HTTPException(400, f"Parse failed: {e}") from e

    side_enum = Side(side)
    replace_transactions_for_side(
        user.id, side_enum, result.transactions, replace=replace
    )

    state = load_state(user.id)
    if side_enum == Side.BANK:
        if result.account_label:
            state.period.account_label = result.account_label
        if result.period_start:
            state.period.start_date = result.period_start
        if result.period_end:
            state.period.end_date = result.period_end
        if result.opening_balance_cents is not None:
            state.period.opening_bank_cents = result.opening_balance_cents
        save_period(user.id, state.period)

    snap = _snapshot_for(user.id)
    snap["upload"] = {
        "filename": filename,
        "side": side,
        "count": len(result.transactions),
        "warnings": result.warnings,
        "opening_balance_cents": result.opening_balance_cents,
        "closing_balance_cents": result.closing_balance_cents,
        "account_label": result.account_label,
        "period_start": result.period_start,
        "period_end": result.period_end,
    }
    return snap


class PeriodUpdate(BaseModel):
    company: Optional[str] = None
    account_label: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    opening_bank_cents: Optional[int] = None
    opening_book_cents: Optional[int] = None


@app.patch("/api/period")
def update_period(body: PeriodUpdate, user: AuthUser = Depends(require_user)):
    state = load_state(user.id)
    data = body.model_dump(exclude_unset=True)
    state.period = PeriodMeta(**{**state.period.model_dump(), **data})
    save_period(user.id, state.period)
    return _snapshot_for(user.id)


@app.post("/api/match/run")
def match_run(
    options: Optional[MatchOptions] = None,
    user: AuthUser = Depends(require_user),
):
    opts = options or MatchOptions()
    state = load_state(user.id)
    state.matches = run_auto_match(state.transactions, state.matches, opts)
    save_full_state(user.id, state)
    return _snapshot_for(user.id)


@app.post("/api/match/accept")
def match_accept(
    body: AcceptMatchesRequest,
    user: AuthUser = Depends(require_user),
):
    state = load_state(user.id)
    state.matches = accept_matches(
        state.matches,
        match_ids=body.match_ids,
        min_confidence=body.min_confidence,
    )
    save_full_state(user.id, state)
    return _snapshot_for(user.id)


@app.post("/api/match/reject")
def match_reject(
    body: AcceptMatchesRequest,
    user: AuthUser = Depends(require_user),
):
    state = load_state(user.id)
    ids = body.match_ids or [
        m.id for m in state.matches if m.status == MatchStatus.PROPOSED
    ]
    state.matches = reject_matches(state.matches, ids)
    save_full_state(user.id, state)
    return _snapshot_for(user.id)


@app.post("/api/match/manual")
def match_manual(
    body: ManualMatchRequest,
    user: AuthUser = Depends(require_user),
):
    state = load_state(user.id)
    try:
        m = create_manual_match(state.transactions, body.bank_ids, body.book_ids)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    used = set(body.bank_ids + body.book_ids)
    state.matches = [
        x for x in state.matches if not (set(x.bank_ids + x.book_ids) & used)
    ]
    state.matches.append(m)
    save_full_state(user.id, state)
    return _snapshot_for(user.id)


@app.post("/api/match/unmatch")
def match_unmatch(
    body: AcceptMatchesRequest,
    user: AuthUser = Depends(require_user),
):
    state = load_state(user.id)
    ids = set(body.match_ids or [])
    state.matches = [m for m in state.matches if m.id not in ids]
    save_full_state(user.id, state)
    return _snapshot_for(user.id)


class AddTxRequest(BaseModel):
    side: Side
    date: str
    description: str
    amount_cents: int
    reference: Optional[str] = None


@app.post("/api/transactions")
def add_transaction(body: AddTxRequest, user: AuthUser = Depends(require_user)):
    state = load_state(user.id)
    t = Transaction(
        id=f"{body.side.value[0]}-{uuid.uuid4().hex[:10]}",
        side=body.side,
        date=body.date,
        description=body.description,
        amount_cents=body.amount_cents,
        reference=body.reference,
    )
    state.transactions.append(t)
    save_full_state(user.id, state)
    return _snapshot_for(user.id)


@app.delete("/api/transactions/{tx_id}")
def delete_transaction(tx_id: str, user: AuthUser = Depends(require_user)):
    db_delete_transaction(user.id, tx_id)
    return _snapshot_for(user.id)


@app.get("/api/export/csv")
def export_csv(user: AuthUser = Depends(require_user)):
    state = load_state(user.id)
    content = export_csv_bytes(state.transactions, state.matches, state.period)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="concord-recon-report.csv"'},
    )


@app.get("/api/export/xlsx")
def export_xlsx(user: AuthUser = Depends(require_user)):
    state = load_state(user.id)
    content = export_xlsx_bytes(state.transactions, state.matches, state.period)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="concord-recon-report.xlsx"'},
    )


# Static UI
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        return {"message": "UI not found. Place static/index.html"}
    return FileResponse(index_path)
