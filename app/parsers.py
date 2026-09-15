"""Parsers for CSV, OFX, and PDF bank/book statements."""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import datetime
from typing import Optional

from .models import ParseResult, Side, Transaction

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _parse_amount_to_cents(raw: str) -> int:
    s = str(raw).strip()
    if not s:
        raise ValueError("empty amount")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    if s.startswith("-"):
        neg = True
        s = s[1:]
    if s.startswith("+"):
        s = s[1:]
    # allow integers without decimals
    val = round(float(s) * 100)
    return -val if neg else val


def _normalize_date(raw: str, default_year: Optional[int] = None) -> str:
    raw = str(raw).strip().replace(",", "")
    if not raw:
        raise ValueError("empty date")

    # Excel / openpyxl datetime string: "2022-01-01 00:00:00"
    if re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}", raw):
        return raw[:10]

    # Already ISO date
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw

    # Excel serial number as string (e.g. 44562)
    if re.fullmatch(r"\d{5}(\.\d+)?", raw):
        try:
            from datetime import timedelta
            serial = float(raw)
            dt = datetime(1899, 12, 30) + timedelta(days=serial)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    # RBC glued or spaced: 24Dec / 24 Dec / 2Jan
    m = re.match(
        r"^(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*$",
        raw,
        re.I,
    )
    if m:
        day = int(m.group(1))
        mon = m.group(2).title()[:3]
        year = default_year or datetime.now().year
        return f"{year:04d}-{MONTHS[mon]:02d}-{day:02d}"

    # 24-Dec-2018 / 24-Dec-18
    m = re.match(
        r"^(\d{1,2})[-\s](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-\s](\d{2,4})$",
        raw,
        re.I,
    )
    if m:
        day = int(m.group(1))
        mon = m.group(2).title()[:3]
        year = int(m.group(3))
        if year < 100:
            year += 2000
        return f"{year:04d}-{MONTHS[mon]:02d}-{day:02d}"

    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%m/%d/%y",   # 12/21/18 (US / this cashbook)
        "%m/%d/%Y",   # 12/21/2018
        "%d/%m/%y",
        "%d/%m/%Y",
        "%m-%d-%y",
        "%m-%d-%Y",
        "%d-%m-%y",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d %Y",
        "%B %d %Y",
        "%d-%b-%Y",
        "%d-%b-%y",
        "%b %d, %Y",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date: {raw}")


def _detect_csv_columns(headers: list[str]) -> dict:
    lower = [h.strip().lower() for h in headers]
    mapping: dict = {}

    def find(*names: str) -> Optional[int]:
        for n in names:
            for i, h in enumerate(lower):
                if n == h or n in h:
                    return i
        return None

    mapping["date"] = find("date", "posted", "transaction date", "value date")
    mapping["description"] = find(
        "description", "memo", "narrative", "details", "payee", "particulars"
    )
    mapping["amount"] = find("amount", "value", "transaction amount")
    # Prefer explicit withdrawal/deposit names before generic debit/credit
    mapping["withdrawal"] = find("withdrawal", "withdrawals", "money out")
    mapping["deposit"] = find("deposit", "deposits", "money in")
    mapping["debit"] = find("debit", "debits")
    mapping["credit"] = find("credit", "credits")
    mapping["reference"] = find("reference", "ref", "cheque", "check", "serial")
    mapping["balance"] = find("balance", "running")

    # Passbook style: Debits = money in, Credits = money out (accounting cashbook)
    # Bank statement style: Withdrawals / Deposits
    mapping["passbook_style"] = (
        mapping.get("debit") is not None
        and mapping.get("credit") is not None
        and mapping.get("withdrawal") is None
        and mapping.get("deposit") is None
    )

    if mapping.get("date") is None:
        raise ValueError("CSV must include a Date column")
    if mapping.get("description") is None:
        mapping["description"] = -1
    has_amount = mapping.get("amount") is not None
    has_wd = mapping.get("withdrawal") is not None or mapping.get("deposit") is not None
    has_dc = mapping.get("debit") is not None or mapping.get("credit") is not None
    if not (has_amount or has_wd or has_dc):
        raise ValueError("CSV must include Amount or Debit/Credit or Withdrawal/Deposit columns")
    return mapping


def _row_amount_cents(row: list[str], mapping: dict) -> Optional[int]:
    """Return signed amount (+ in, − out) or None if no amount cells."""

    def cell(key: str) -> str:
        idx = mapping.get(key)
        if idx is None or idx < 0 or idx >= len(row):
            return ""
        return (row[idx] or "").strip()

    if mapping.get("amount") is not None:
        raw = cell("amount")
        if not raw:
            return None
        return _parse_amount_to_cents(raw)

    # Withdrawals / Deposits (bank statement)
    if mapping.get("withdrawal") is not None or mapping.get("deposit") is not None:
        w_raw, d_raw = cell("withdrawal"), cell("deposit")
        w = abs(_parse_amount_to_cents(w_raw)) if w_raw else 0
        d = abs(_parse_amount_to_cents(d_raw)) if d_raw else 0
        if not w and not d:
            return None
        return d - w

    # Debits / Credits
    deb_raw, cred_raw = cell("debit"), cell("credit")
    deb = abs(_parse_amount_to_cents(deb_raw)) if deb_raw else 0
    cred = abs(_parse_amount_to_cents(cred_raw)) if cred_raw else 0
    if not deb and not cred:
        return None
    if mapping.get("passbook_style"):
        # Accounting cash/bank book: Debit increases cash, Credit decreases
        return deb - cred
    # Fallback: treat credit as inflow (common bank export)
    return cred - deb


def parse_csv(content: bytes | str, side: Side, filename: str = "upload.csv") -> ParseResult:
    if isinstance(content, bytes):
        text = content.decode("utf-8-sig", errors="replace")
    else:
        text = content

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t|;")
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)
    if not rows:
        return ParseResult(transactions=[], warnings=["Empty CSV"])

    header_idx = 0
    for i, row in enumerate(rows[:15]):
        joined = " ".join(row).lower()
        if "date" in joined and (
            "description" in joined
            or "amount" in joined
            or "debit" in joined
            or "credit" in joined
            or "withdrawal" in joined
            or "deposit" in joined
        ):
            header_idx = i
            break

    headers = rows[header_idx]
    mapping = _detect_csv_columns(headers)
    txs: list[Transaction] = []
    warnings: list[str] = []
    skip_desc = re.compile(
        r"^(balance\s*forward|balance\s*forwarded|opening\s*balance|closing\s*balance|totals?\s*$)",
        re.I,
    )

    for row in rows[header_idx + 1 :]:
        if not row or all(not (c or "").strip() for c in row):
            continue
        try:
            date_raw = row[mapping["date"]].strip() if mapping["date"] < len(row) else ""
            if not date_raw:
                continue
            date_s = _normalize_date(date_raw)

            desc = ""
            if mapping["description"] is not None and mapping["description"] >= 0:
                desc = row[mapping["description"]].strip() if mapping["description"] < len(row) else ""
            if desc and skip_desc.match(desc):
                continue

            amt = _row_amount_cents(row, mapping)
            if amt is None:
                warnings.append(f"Skipped row (no amount): {date_raw} {desc[:40]}")
                continue

            ref = None
            if mapping.get("reference") is not None and mapping["reference"] < len(row):
                ref = row[mapping["reference"]].strip() or None

            bal = None
            if mapping.get("balance") is not None and mapping["balance"] < len(row):
                b = row[mapping["balance"]].strip()
                if b:
                    try:
                        bal = _parse_amount_to_cents(b)
                    except ValueError:
                        pass

            txs.append(
                Transaction(
                    id=_uid(side.value[0]),
                    side=side,
                    date=date_s,
                    description=desc or "(no description)",
                    amount_cents=amt,
                    reference=ref,
                    balance_cents=bal,
                    source_file=filename,
                )
            )
        except Exception as e:
            warnings.append(f"Skipped row: {e}")

    return ParseResult(transactions=txs, warnings=warnings)



def parse_xlsx(content: bytes, side: Side, filename: str = "upload.xlsx") -> ParseResult:
    """Excel (.xlsx) — first sheet, header row auto-detected like CSV."""
    import openpyxl
    from datetime import date, time, timedelta

    def _cell_to_str(c) -> str:
        if c is None:
            return ""
        # Real Excel date/datetime cells (not numeric amounts)
        if isinstance(c, datetime):
            return c.strftime("%Y-%m-%d")
        if isinstance(c, date):
            return c.strftime("%Y-%m-%d")
        if isinstance(c, time):
            return ""
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            # Keep numeric amounts as-is (do not treat as date serials)
            if float(c) == int(c):
                return str(int(c))
            return str(c)
        s = str(c).strip()
        # String form of Excel datetime
        if len(s) >= 19 and s[4] == "-" and s[10] in " T":
            return s[:10]
        return s

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    rows: list[list[str]] = []
    for row in ws.iter_rows(values_only=True):
        rows.append([_cell_to_str(c) for c in row])
    wb.close()
    if not rows:
        return ParseResult(transactions=[], warnings=["Empty spreadsheet"])

    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)
    return parse_csv(buf.getvalue(), side, filename)


def parse_ofx(content: bytes | str, side: Side, filename: str = "upload.ofx") -> ParseResult:
    from ofxparse import OfxParser

    if isinstance(content, bytes):
        fh = io.BytesIO(content)
    else:
        fh = io.StringIO(content)

    ofx = OfxParser.parse(fh)
    txs: list[Transaction] = []
    warnings: list[str] = []
    opening = None
    closing = None
    account_label = None
    period_start = None
    period_end = None

    accounts = getattr(ofx, "accounts", None) or []
    if not accounts and getattr(ofx, "account", None):
        accounts = [ofx.account]

    for acct in accounts:
        try:
            if acct.account_id:
                account_label = str(acct.account_id)
        except Exception:
            pass
        statement = getattr(acct, "statement", None)
        if statement is None:
            continue
        try:
            if statement.start_date:
                period_start = statement.start_date.strftime("%Y-%m-%d")
            if statement.end_date:
                period_end = statement.end_date.strftime("%Y-%m-%d")
        except Exception:
            pass
        try:
            if statement.balance is not None:
                closing = int(round(float(statement.balance) * 100))
        except Exception:
            pass

        for t in statement.transactions:
            try:
                amt = int(round(float(t.amount) * 100))
                d = t.date.strftime("%Y-%m-%d") if t.date else None
                if not d:
                    continue
                desc = (t.payee or t.memo or t.type or "OFX transaction").strip()
                memo = (t.memo or "").strip()
                if memo and memo not in desc:
                    desc = f"{desc} — {memo}" if desc else memo
                ref = str(t.checknum).strip() if getattr(t, "checknum", None) else None
                if not ref and getattr(t, "id", None):
                    ref = str(t.id)[:32]
                txs.append(
                    Transaction(
                        id=_uid(side.value[0]),
                        side=side,
                        date=d,
                        description=desc,
                        amount_cents=amt,
                        reference=ref,
                        source_file=filename,
                    )
                )
            except Exception as e:
                warnings.append(f"OFX tx skipped: {e}")

    if not txs:
        warnings.append("No transactions found in OFX file")

    return ParseResult(
        transactions=txs,
        opening_balance_cents=opening,
        closing_balance_cents=closing,
        account_label=account_label,
        period_start=period_start,
        period_end=period_end,
        warnings=warnings,
    )


_OUTFLOW_RE = re.compile(
    r"withdrawal|withdraw|payment|purchase|mortgage|loan\b|fee|sent\s|"
    r"insurance|bill|transfer\s+to|debit|funds\s*transfer|fundstransfer|"
    r"interac\s*-?\s*sc|\bsc-\d|service\s*charge|monthly\s*fee|cash-?back\s*withdrawal|"
    r"business\s*pad|businesspad|\bpad\b|merit\s*premium",
    re.I,
)
_INFLOW_RE = re.compile(
    r"deposit|autodeposit|received|credit|payroll|salary|refund|interest|"
    r"br\s*to\s*br|brt?obr|branch\s*transfer|direct\s*deposit",
    re.I,
)


def _guess_sign_cents(description: str, amount_abs_cents: int) -> int:
    """Keyword heuristic when PDF columns collapse to a single amount."""
    d = description
    # Inflow first for autodeposit / deposit (overrides generic "transfer")
    if re.search(r"autodeposit|atm\s*deposit|e-?transfer\s*-?\s*autodeposit", d, re.I):
        return abs(amount_abs_cents)
    if re.search(r"fee\s*credit|abm\s*fee\s*credit|interest", d, re.I):
        return abs(amount_abs_cents)
    if _OUTFLOW_RE.search(d):
        return -abs(amount_abs_cents)
    if _INFLOW_RE.search(d):
        return abs(amount_abs_cents)
    if re.search(r"\batm\b", d, re.I):
        return -abs(amount_abs_cents)
    # Default positive — review in UI
    return abs(amount_abs_cents)


def _parse_rbc_text_lines(lines: list[str], side: Side, filename: str, default_year: Optional[int]) -> tuple[list[Transaction], list[str]]:
    """Parse RBC-style statement text where dates look like 24Dec / 2Jan."""
    txs: list[Transaction] = []
    warnings: list[str] = []
    date_re = re.compile(
        r"^(\d{1,2}\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*)\s+(.*)$",
        re.I,
    )
    amount_token = re.compile(r"([\d,]+\.\d{2})")
    skip_line = re.compile(
        r"openingbalance|closingbalance|detailsofyour|date\s*description|"
        r"page\s*\d|pleasecheck|trademark|gst\s*registration|royalbank|"
        r"summaryofyour|totaldeposits|totalwithdrawals|howtoreach|"
        r"accountnumber|estatement|personal\s*banking\s*account\s*statement|"
        r"your\s*rbc\s*personal|\b\d+\s*of\s*\d+\b|\d+of\d+|"
        r"c\.?p\.?\s*6011|montreal\s*qc|station\s*a\b|rbcroyalbank|"
        r"withdrawals\s*\(\$\)|deposits\s*\(\$\)|balance\s*\(\$\)|"
        r"from\s*december|from\s*january|from\s*[a-z]+\s*\d{1,2},?\s*\d{4}|"
        r"account\s*statement|raymonde?sawers|petten",
        re.I,
    )

    current_date: Optional[str] = None
    pending_desc: list[str] = []

    def flush(desc: str, amounts: list[str]) -> None:
        nonlocal txs
        if not current_date or not amounts:
            return
        desc = re.sub(r"\s+", " ", desc).strip(" -")
        if len(desc) < 3 or skip_line.search(desc):
            return
        # Drop page chrome that survived with a real amount glued on
        if re.search(
            r"account\s*statement\s*from|your\s*rbc|montreal\s*qc|\d+of\d+",
            desc,
            re.I,
        ):
            return
        if len(desc) > 90 and re.search(r"statement|from\s*december|from\s*january", desc, re.I):
            return
        # Last amount may be running balance when 2+ amounts present
        if len(amounts) >= 2:
            tx_raw = amounts[0]
            # Sometimes multi-amount lines still only have one tx amount + balance
            # Prefer first as tx if second looks like balance continuity — always use first
        else:
            tx_raw = amounts[0]
        try:
            abs_cents = abs(_parse_amount_to_cents(tx_raw))
        except ValueError:
            return
        signed = _guess_sign_cents(desc, abs_cents)
        txs.append(
            Transaction(
                id=_uid(side.value[0]),
                side=side,
                date=current_date,
                description=desc,
                amount_cents=signed,
                source_file=filename,
                raw=desc,
            )
        )

    for line in lines:
        line = line.strip()
        if not line or skip_line.search(line):
            continue

        dm = date_re.match(line)
        if dm:
            # flush any orphan pending without amount? drop
            pending_desc = []
            try:
                current_date = _normalize_date(dm.group(1), default_year)
            except ValueError:
                continue
            rest = dm.group(2).strip()
        else:
            rest = line
            if not current_date:
                continue

        amounts = amount_token.findall(rest)
        # Strip amounts from description
        desc_part = amount_token.sub("", rest).strip()
        desc_part = re.sub(r"\s+", " ", desc_part).strip(" -")

        def _is_chrome(text: str) -> bool:
            """Page chrome / barcodes only — not merchant lines like ATM-11274101."""
            t = text.replace(" ", "")
            if re.fullmatch(r"[\d\-_/.*]{4,}", t):
                return True
            if re.fullmatch(r"[\d\-_/.*]+", t) and len(re.sub(r"\D", "", t)) >= 8:
                return True
            # RBC form codes e.g. 4105910-040_8710487_02001ADPBR
            if re.search(r"\d{6,}[-_]\d{2,}[-_]?\d*.*[A-Z]{2,}", text):
                return True
            if re.search(r"\*\d+[A-Z]*\*", text):
                return True
            if re.search(r"from\s*december|from\s*january|account\s*statement", text, re.I):
                return True
            if skip_line.search(text):
                return True
            return False

        if amounts:
            clean_pending = [p for p in pending_desc if not _is_chrome(p)]
            parts = clean_pending + ([desc_part] if desc_part and not _is_chrome(desc_part) else [])
            full_desc = re.sub(r"\s+", " ", " ".join(parts)).strip()
            pending_desc = []
            if full_desc:
                flush(full_desc, amounts)
        else:
            if desc_part and not _is_chrome(desc_part):
                pending_desc.append(desc_part)

    if not txs:
        warnings.append("RBC-style text layout: no transactions found")
    else:
        # Flag uncertain signs for neutral descriptions
        warnings.append(
            f"Parsed {len(txs)} lines from PDF text. Amount signs use description keywords — review unmatched items."
        )
    return txs, warnings



def _parse_generic_text_lines(
    lines: list[str],
    side: Side,
    filename: str,
    default_year: Optional[int] = None,
) -> tuple[list[Transaction], list[str]]:
    """
    Bank-agnostic text fallback.
    Looks for lines with a date + at least one amount; does not assume RBC layout.
    """
    txs: list[Transaction] = []
    warnings: list[str] = []
    # 2026-02-03 | 03/02/2026 | 03-02-2026 | 02/03/2026 | 3 Feb 2026 | Feb 3, 2026
    date_lead = re.compile(
        r"^("
        r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"
        r"|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
        r"|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}"
        r"|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4}"
        r")\b\s*(.*)$"
    )
    amount_re = re.compile(r"([(+-]?\s*[\d,]+\.\d{2}\s*\)?)")
    skip = re.compile(
        r"page\s*\d|opening\s*balance|closing\s*balance|statement\s*period|"
        r"account\s*summary|total\s*deposits|total\s*withdrawals|"
        r"date\s+description|transaction\s*details|continued",
        re.I,
    )
    current_date: Optional[str] = None

    for line in lines:
        raw = line.strip()
        if not raw or skip.search(raw):
            continue
        rest = raw
        dm = date_lead.match(raw)
        if dm:
            try:
                current_date = _normalize_date(dm.group(1).replace(",", ""), default_year)
                rest = dm.group(2).strip()
            except ValueError:
                pass
        if not current_date:
            continue
        amounts = amount_re.findall(rest)
        if not amounts:
            continue
        # Prefer first amount as tx; last often running balance when 2+
        tx_raw = amounts[0].strip()
        try:
            # parentheses handled in _parse_amount_to_cents
            cents = _parse_amount_to_cents(tx_raw)
        except ValueError:
            continue
        desc = amount_re.sub(" ", rest)
        desc = re.sub(r"\s+", " ", desc).strip(" -|")
        if len(desc) < 2:
            desc = "Transaction"
        if skip.search(desc):
            continue
        # Keyword sign when amount parsed positive from text without explicit sign
        if cents > 0 and re.search(
            r"withdrawal|payment|purchase|fee|debit|cheque|check\b|pos\b",
            desc,
            re.I,
        ) and not re.search(r"deposit|credit|refund|interest", desc, re.I):
            cents = -cents
        txs.append(
            Transaction(
                id=_uid(side.value[0]),
                side=side,
                date=current_date,
                description=desc,
                amount_cents=cents,
                source_file=filename,
                raw=raw,
            )
        )

    if txs:
        warnings.append(
            f"Generic PDF text parser extracted {len(txs)} line(s). "
            "Review amounts and signs — layouts vary by bank."
        )
    return txs, warnings


def parse_pdf(content: bytes, side: Side, filename: str = "upload.pdf") -> ParseResult:
    """Heuristic PDF statement parser. Tuned for text PDFs including RBC."""
    import pdfplumber

    warnings: list[str] = []
    all_text_lines: list[str] = []
    table_rows: list[list[str]] = []

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            all_text_lines.extend(text.splitlines())
            tables = page.extract_tables() or []
            for table in tables:
                for row in table:
                    if row:
                        table_rows.append([(c or "").strip() for c in row])

    full_text = "\n".join(all_text_lines)
    # Normalize common PDF extractions that strip spaces
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", full_text)
    spaced = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", spaced)

    opening = None
    closing = None
    period_start = None
    period_end = None
    account_label = None

    m = re.search(
        r"opening\s*balance[^\d$]*\$?\s*([\d,]+\.\d{2})",
        full_text,
        re.I,
    )
    if m:
        opening = _parse_amount_to_cents(m.group(1))

    m = re.search(
        r"closing\s*balance[^\d$=]*[=]?\s*\$?\s*([\d,]+\.\d{2})",
        full_text,
        re.I,
    )
    if m:
        closing = _parse_amount_to_cents(m.group(1))

    m = re.search(
        r"From\s*([A-Za-z]+\s*\d{1,2},?\s*\d{4})\s*to\s*([A-Za-z]+\s*\d{1,2},?\s*\d{4})",
        full_text,
        re.I,
    )
    if m:
        try:
            period_start = _normalize_date(re.sub(r"\s+", " ", m.group(1)).replace(",", ""))
            period_end = _normalize_date(re.sub(r"\s+", " ", m.group(2)).replace(",", ""))
        except ValueError:
            pass

    # Glued: FromDecember21,2018toJanuary21,2019
    if not period_start:
        m = re.search(
            r"From([A-Za-z]+(\d{1,2}),(\d{4}))to([A-Za-z]+(\d{1,2}),(\d{4}))",
            full_text,
            re.I,
        )
        if m:
            try:
                period_start = _normalize_date(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}")
                # m groups messy — try alternate
            except ValueError:
                pass
        m = re.search(
            r"From(December|January|February|March|April|May|June|July|August|September|October|November)"
            r"(\d{1,2}),(\d{4})to(December|January|February|March|April|May|June|July|August|September|October|November)"
            r"(\d{1,2}),(\d{4})",
            full_text,
            re.I,
        )
        if m:
            try:
                period_start = _normalize_date(f"{m.group(1)} {m.group(2)} {m.group(3)}")
                period_end = _normalize_date(f"{m.group(4)} {m.group(5)} {m.group(6)}")
            except ValueError:
                pass

    m = re.search(r"account\s*number[:\s]*([\d\-]+)", full_text, re.I)
    if m:
        account_label = m.group(1).strip()

    default_year = None
    if period_start:
        default_year = int(period_start[:4])
    elif period_end:
        default_year = int(period_end[:4])
    else:
        # RBC statements often span year-end; prefer year from closing line
        m = re.search(r"January\s*21[,]?(\d{4})|December\s*21[,]?(\d{4})", full_text, re.I)
        if m:
            default_year = int(m.group(1) or m.group(2))

    # Year-boundary: Dec lines use prior year if period spans
    # Handled per-line below when we have period_start/end

    txs: list[Transaction] = []
    table_txs: list[Transaction] = []

    if table_rows:
        header = None
        data_start = 0
        for i, row in enumerate(table_rows[:15]):
            joined = " ".join(row).lower()
            if "date" in joined and (
                "description" in joined or "withdrawal" in joined or "deposit" in joined
            ):
                header = row
                data_start = i + 1
                break
        if header:
            try:
                mapping = _detect_csv_columns(header)
                current_date = None
                for row in table_rows[data_start:]:
                    if not any(row):
                        continue
                    date_cell = row[mapping["date"]].strip() if mapping["date"] < len(row) else ""
                    if date_cell and not re.match(r"opening|closing", date_cell, re.I):
                        try:
                            yr = default_year
                            current_date = _normalize_date(date_cell, yr)
                        except ValueError:
                            pass
                    if not current_date:
                        continue
                    desc_idx = mapping.get("description", 1)
                    desc = row[desc_idx].strip() if desc_idx is not None and desc_idx < len(row) else ""
                    if not desc or re.match(r"opening|closing", desc, re.I):
                        continue
                    try:
                        amt = _row_amount_cents(row, mapping)
                    except ValueError:
                        continue
                    if amt is None:
                        continue
                    table_txs.append(
                        Transaction(
                            id=_uid(side.value[0]),
                            side=side,
                            date=current_date,
                            description=re.sub(r"\s+", " ", desc),
                            amount_cents=amt,
                            source_file=filename,
                            raw=" | ".join(row),
                        )
                    )
            except Exception as e:
                warnings.append(f"Table parse partial: {e}")

    # Always run text parser for RBC-style PDFs; keep whichever yields more lines
    lines = []
    for line in all_text_lines:
        line2 = re.sub(
            r"\b(\d{1,2})(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b",
            r"\1 \2",
            line,
            flags=re.I,
        )
        lines.append(line2)
    text_txs, w2 = _parse_rbc_text_lines(lines, side, filename, default_year)
    warnings.extend(w2)

    generic_txs, w3 = _parse_generic_text_lines(lines, side, filename, default_year)
    warnings.extend(w3)

    # Prefer the richest non-empty extraction among RBC text, generic text, tables
    candidates = [
        ("rbc-text", text_txs),
        ("generic-text", generic_txs),
        ("table", table_txs),
    ]
    best_name, txs = max(candidates, key=lambda x: len(x[1]))
    if txs:
        warnings.append(f"Selected PDF strategy: {best_name} ({len(txs)} lines).")
    else:
        warnings.append(
            "No transactions extracted from PDF. "
            "PDFs are bank-specific; try CSV or OFX/QFX from your bank download, "
            "or a text-based (not scanned) PDF."
        )

    if txs:
        # Fix year-end: if period spans Dec→Jan, Dec dates should use start year
        if period_start and period_end and period_start[:4] != period_end[:4]:
            start_year = int(period_start[:4])
            end_year = int(period_end[:4])
            fixed = []
            for t in txs:
                month = int(t.date[5:7])
                if month == 12:
                    nd = f"{start_year:04d}{t.date[4:]}"
                elif month == 1:
                    nd = f"{end_year:04d}{t.date[4:]}"
                else:
                    nd = t.date
                fixed.append(t.model_copy(update={"date": nd}))
            txs = fixed

    if not txs:
        pass  # warning already set by strategy selection

    return ParseResult(
        transactions=txs,
        opening_balance_cents=opening,
        closing_balance_cents=closing,
        account_label=account_label,
        period_start=period_start,
        period_end=period_end,
        warnings=warnings,
    )


def parse_file(
    content: bytes,
    filename: str,
    side: Side,
) -> ParseResult:
    """Supported: CSV/TSV/TXT, Excel (.xlsx), OFX/QFX. PDF is not supported."""
    lower = filename.lower()
    if lower.endswith((".xlsx", ".xlsm")):
        return parse_xlsx(content, side, filename)
    if lower.endswith((".csv", ".tsv", ".txt")):
        return parse_csv(content, side, filename)
    if lower.endswith((".ofx", ".qfx")):
        return parse_ofx(content, side, filename)
    if lower.endswith(".pdf"):
        raise ValueError(
            "PDF is not supported. Export CSV, Excel (.xlsx), or OFX/QFX from your bank, then import that file."
        )
    # sniff
    head = content[:256].lstrip()
    if head.startswith(b"PK"):  # zip-based xlsx
        try:
            return parse_xlsx(content, side, filename)
        except Exception:
            pass
    if b"<OFX" in head.upper() or b"OFXHEADER" in head.upper():
        return parse_ofx(content, side, filename)
    # default CSV
    return parse_csv(content, side, filename)
