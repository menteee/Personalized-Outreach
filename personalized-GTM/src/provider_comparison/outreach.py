"""Outreach tracker: import prospects from a spreadsheet, send approved emails, export who was contacted.

Flow per contact (nothing is ever sent automatically):
    import  ->  Start (identity check -> research -> draft, the existing human-reviewed flow)
            ->  approve the draft on the run page  ->  "Send" (one email, one click, after a preview)

A contact's displayed state is derived live from its run, so it never goes stale:
    new | researching | confirm_identity | attention | draft_ready | approved | sent | skipped

Sending uses SMTP settings from the environment (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD,
SMTP_FROM, SMTP_FROM_NAME, SMTP_SECURITY=starttls|ssl|none, SMTP_REPLY_TO). Without SMTP_HOST the
tracker still works; only the Send button is unavailable.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
import smtplib
import socket
import ssl
import threading
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any

from starlette.exceptions import HTTPException as HTTPError

from .team import _now, _text

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000
EMAIL = re.compile(r"[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "yahoo.co.in", "outlook.com", "hotmail.com",
             "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
             "rediffmail.com", "zoho.com", "gmx.com", "mail.com"}
GENERIC_LOCAL = {"info", "hello", "contact", "sales", "support", "admin", "hr", "jobs", "careers", "recruiting",
                 "recruitment", "talent", "team", "office", "mail", "enquiries", "inquiries", "press", "marketing"}

HEADERS = {
    "email": {"email", "e mail", "email address", "e mail address", "mail", "work email", "email id", "emailid"},
    "name": {"name", "full name", "contact", "contact name", "prospect", "person", "prospect name"},
    "first": {"first name", "firstname", "first", "given name"},
    "last": {"last name", "lastname", "last", "surname", "family name"},
    "company": {"company", "company name", "organization", "organisation", "employer", "account"},
    "role": {"role", "title", "job title", "position", "designation"},
    "profile_url": {"linkedin", "linkedin url", "linkedin profile", "profile", "profile url", "url"},
}

STATE_LABELS = {
    "new": "Not contacted", "researching": "Researching", "confirm_identity": "Confirm identity",
    "attention": "Needs attention", "draft_ready": "Draft ready", "approved": "Approved to send",
    "sent": "Sent", "skipped": "Skipped",
}


# --------------------------------------------------------------------------- reading files

def _clean_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, datetime):
        value = value.isoformat(sep=" ", timespec="minutes")
    return CONTROL.sub("", str(value)).strip()


def _read_xlsx(data: bytes, max_rows: int = MAX_ROWS) -> list[list[str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 100 * 1024 * 1024:
                raise ValueError("This spreadsheet is too large to import.")
    except zipfile.BadZipFile as exc:
        raise ValueError("This file is not a valid .xlsx spreadsheet.") from exc
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("openpyxl is not installed on the server. Run: pip install openpyxl") from exc
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError("Could not read this spreadsheet. Save it as .xlsx or .csv and try again.") from exc
    try:
        for sheet in workbook.worksheets:
            rows, scanned = [], 0
            if hasattr(sheet, "reset_dimensions"):
                sheet.reset_dimensions()  # some exporters write a wrong size; read every cell instead
            for raw in sheet.iter_rows(values_only=True):
                scanned += 1
                row = [_clean_cell(v) for v in raw]
                if any(row):
                    rows.append(row)
                if len(rows) > max_rows + 1 or scanned > max(20000, max_rows * 4):
                    break
            if rows:
                return rows
    finally:
        workbook.close()
    return []


def _read_csv(data: bytes, max_rows: int = MAX_ROWS) -> list[list[str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    try:
        delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","
    rows = []
    for raw in csv.reader(io.StringIO(text), delimiter=delimiter):
        row = [_clean_cell(v) for v in raw]
        if any(row):
            rows.append(row)
        if len(rows) > max_rows + 1:
            break
    return rows


def read_rows(data: bytes, max_rows: int = MAX_ROWS) -> list[list[str]]:
    if data[:2] == b"PK":
        return _read_xlsx(data, max_rows)
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ValueError("Old .xls files are not supported. Save the sheet as .xlsx or .csv and upload it again.")
    return _read_csv(data, max_rows)


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", header.lower()).strip()


def map_columns(header: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for index, cell in enumerate(header):
        key = _norm(cell)
        for field, names in HEADERS.items():
            if key in names and field not in found:
                found[field] = index
    return found


def guess_name(email: str) -> str:
    local = email.split("@")[0].split("+")[0].lower()
    if local in GENERIC_LOCAL:
        return ""
    parts = [p for p in re.split(r"[._-]+", local) if p.isalpha()]
    return " ".join(p.capitalize() for p in parts[:3]) if len(parts) >= 2 else ""


def guess_company(email: str) -> str:
    domain = email.split("@")[-1].lower()
    if domain in FREE_MAIL:
        return ""
    labels = domain.split(".")
    if len(labels) >= 3 and labels[-2] in {"co", "com", "org", "net", "ac", "gov"} and len(labels[-1]) == 2:
        label = labels[-3]
    else:
        label = labels[-2] if len(labels) >= 2 else labels[0]
    return label.replace("-", " ").title()


def _http_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if not re.match(r"https?://[^\s]+\.[^\s]+", value, re.I) or len(value) > 2000:
        return ""
    return value


# --------------------------------------------------------------------------- state and views

def current_draft(run: dict | None) -> dict | None:
    draft = (run or {}).get("edits") or ((run or {}).get("output") or {}).get("draft")
    if isinstance(draft, dict) and str(draft.get("subject", "")).strip() and str(draft.get("body", "")).strip():
        return {"subject": str(draft["subject"]), "body": str(draft["body"])}
    return None


def contact_state(contact: dict, run: dict | None) -> str:
    if contact["status"] == "sent":
        return "sent"
    if contact["status"] == "skipped":
        return "skipped"
    if not run:
        return "new"
    if run["status"] == "running":
        return "researching"
    if run["status"] == "needs_input":
        return "confirm_identity"
    if run["status"] == "failed":
        return "attention"
    if run["stage"] == "review" and run["status"] == "ready" and current_draft(run):
        return "approved" if run.get("draft_confirmed_at") else "draft_ready"
    return "researching"


def contact_view(contact: dict, run: dict | None, *, smtp_ok: bool, admin: bool) -> dict:
    state = contact_state(contact, run)
    if state == "sent":
        message = {"subject": contact.get("subject", ""), "body": contact.get("body", "")}
    elif state in ("draft_ready", "approved"):
        message = current_draft(run)
    else:
        message = None
    view = {k: contact.get(k) for k in ("id", "email", "name", "company", "role", "profile_url", "note", "guessed",
                                         "sent_at", "sent_by_name", "send_error", "created_at")}
    view.update(
        state=state, label=STATE_LABELS[state], message=message, run_id=contact.get("run_id") if run else None,
        complete=all(contact.get(k) for k in ("name", "company", "role")),
        can_start=state == "new", can_send=state == "approved" and smtp_ok,
        imported_from=(contact.get("batch") or {}).get("file", ""), imported_at=(contact.get("batch") or {}).get("at"),
        owner_name=contact.get("owner_name") if admin else None,
    )
    return view


# --------------------------------------------------------------------------- email

def smtp_settings() -> dict:
    user = (os.environ.get("SMTP_USER") or "").strip()
    from_addr = (os.environ.get("SMTP_FROM") or user).strip()
    try:
        port = int(os.environ.get("SMTP_PORT") or 587)
    except ValueError:
        port = 587
    security = (os.environ.get("SMTP_SECURITY") or "starttls").strip().lower()
    host = (os.environ.get("SMTP_HOST") or "").strip()
    return dict(host=host, port=port, user=user, password=os.environ.get("SMTP_PASSWORD") or "",
                from_addr=from_addr, from_name=(os.environ.get("SMTP_FROM_NAME") or "Shubham Kumar").strip(),
                reply_to=(os.environ.get("SMTP_REPLY_TO") or "").strip(),
                security=security if security in ("starttls", "ssl", "none") else "starttls",
                configured=bool(host and EMAIL.fullmatch(from_addr)))


def deliver(smtp: dict, to: str, subject: str, body: str) -> str:
    if re.search(r"[\r\n]", subject + to):
        raise ValueError("The subject or address contains a line break.")
    message = EmailMessage()
    message["From"] = formataddr((smtp["from_name"], smtp["from_addr"]))
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate()
    message_id = make_msgid(domain=smtp["from_addr"].split("@")[-1])
    message["Message-ID"] = message_id
    if smtp["reply_to"] and EMAIL.fullmatch(smtp["reply_to"]):
        message["Reply-To"] = smtp["reply_to"]
    message.set_content(body)
    context = ssl.create_default_context()
    server = (smtplib.SMTP_SSL(smtp["host"], smtp["port"], timeout=30, context=context) if smtp["security"] == "ssl"
              else smtplib.SMTP(smtp["host"], smtp["port"], timeout=30))
    with server:
        if smtp["security"] == "starttls":
            server.starttls(context=context)
        if smtp["user"]:
            server.login(smtp["user"], smtp["password"])
        server.send_message(message)
    return message_id


def _send_error(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "The mail server rejected the login. Check SMTP_USER and SMTP_PASSWORD (Gmail needs an app password)."
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The mail server refused this recipient address."
    if isinstance(exc, ssl.SSLError):
        return "Secure connection to the mail server failed. Check SMTP_PORT and SMTP_SECURITY (starttls or ssl)."
    if isinstance(exc, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, ConnectionError, TimeoutError,
                        socket.gaierror)):
        return "Could not connect to the mail server. Check SMTP_HOST, SMTP_PORT and SMTP_SECURITY."
    return f"{type(exc).__name__}: {str(exc)[:160]}"


# --------------------------------------------------------------------------- service

class OutreachService:
    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._sending: set[str] = set()

    # ---- access
    def visible(self, user: dict) -> list[dict]:
        return [c for c in self.store.contacts() if user["role"] == "admin" or c["owner_id"] == user["id"]]

    def get_for(self, user: dict, cid: str) -> dict:
        contact = self.store.contact(cid)
        if not contact or not (user["role"] == "admin" or contact["owner_id"] == user["id"]):
            raise HTTPError(404, "Contact not found")
        return contact

    # ---- import
    def import_file(self, user: dict, data: bytes, filename: str) -> dict:
        rows = read_rows(data)
        if not rows:
            raise ValueError("The file has no rows.")
        columns = map_columns(rows[0])
        first_row = 2
        if "email" not in columns:
            email_cells = [i for i, cell in enumerate(rows[0]) if EMAIL.fullmatch(cell.lower())]
            if not email_cells:
                raise ValueError("No email column found. Add a header row with a column named Email.")
            columns, first_row, body = {"email": email_cells[0]}, 1, rows  # headerless list of emails
        else:
            body = rows[1:]
        if len(body) > MAX_ROWS:
            raise ValueError(f"Too many rows. Import at most {MAX_ROWS} at a time.")

        def cell(row, field):
            index = columns.get(field)
            return row[index] if index is not None and index < len(row) else ""

        batch = {"file": _text(os.path.basename(filename or "upload"), "File name", 120), "at": _now()}
        candidates, invalid, seen, repeated = [], [], set(), 0
        for offset, row in enumerate(body):
            number = first_row + offset
            email = cell(row, "email").lower()
            if not EMAIL.fullmatch(email) or len(email) > 254:
                invalid.append({"row": number, "value": cell(row, "email")[:80], "reason": "Not a valid email address"})
                continue
            if email in seen:
                repeated += 1
                continue
            seen.add(email)
            name = " ".join(cell(row, "name").split()) or " ".join(
                f"{cell(row, 'first')} {cell(row, 'last')}".split())
            company, role = " ".join(cell(row, "company").split()), " ".join(cell(row, "role").split())
            guessed = []
            if not name and (name := guess_name(email)):
                guessed.append("name")
            if not company and (company := guess_company(email)):
                guessed.append("company")
            candidates.append(dict(
                id=uuid.uuid4().hex, email=email, name=name[:160], company=company[:160], role=role[:200],
                profile_url=_http_url(cell(row, "profile_url")), guessed=guessed, status="new", run_id=None,
                owner_id=user["id"], owner_name=user["name"], batch=batch, note="", created_at=_now(),
                subject="", body="", sent_at=None, sent_by=None, sent_by_name=None, message_id=None, send_error=None))
        added, duplicates = self.store.add_contacts(candidates)
        return {
            "rows": len(body), "added": len(added), "repeated_in_file": repeated,
            "needs_details": sum(1 for c in added if not (c["name"] and c["company"] and c["role"])),
            "already_tracked": [{"email": d["email"], "yours": d.get("owner_id") == user["id"],
                                 "owner": d.get("owner_name") if user["role"] == "admin" else None}
                                for d in duplicates][:50],
            "already_tracked_count": len(duplicates),
            "invalid": invalid[:50], "invalid_count": len(invalid),
        }

    # ---- edit
    def update(self, user: dict, cid: str, changes: dict) -> dict:
        contact = self.get_for(user, cid)
        locked = contact["status"] == "sent"
        if locked and user["role"] != "admin":
            raise ValueError("Sent records are locked. Ask an admin to change them.")
        allowed = {"note"} if locked else {"email", "name", "company", "role", "profile_url", "note", "skip"}
        if set(changes) - allowed and locked:
            raise ValueError("Only the note can be changed on a sent record.")
        if "email" in changes and "email" in allowed:
            email = str(changes["email"] or "").strip().lower()
            if not EMAIL.fullmatch(email) or len(email) > 254:
                raise ValueError("Enter a valid email address.")
            contact["email"] = email
        for field, label, limit in (("name", "Name", 160), ("company", "Company", 160), ("role", "Role", 200)):
            if field in changes and field in allowed:
                contact[field] = _text(changes[field], label, limit)
                contact["guessed"] = [g for g in contact.get("guessed", []) if g != field]
        if "profile_url" in changes and "profile_url" in allowed:
            raw = str(changes["profile_url"] or "").strip()
            contact["profile_url"] = _http_url(raw)
            if raw and not contact["profile_url"]:
                raise ValueError("Enter a valid profile URL starting with https://")
        if "note" in changes:
            contact["note"] = _text(changes["note"], "Note", 2000, multiline=True)
        if "skip" in changes and "skip" in allowed:
            if changes["skip"] and contact["status"] == "new":
                contact["status"] = "skipped"
            elif not changes["skip"] and contact["status"] == "skipped":
                contact["status"] = "new"
        self.store.put_contact(contact)
        return contact

    def delete(self, user: dict, cid: str) -> None:
        contact = self.get_for(user, cid)
        if contact["status"] == "sent" and user["role"] != "admin":
            raise ValueError("Sent records are locked. Ask an admin to delete them.")
        self.store.delete_contact(cid)

    def attach_run(self, cid: str, run_id: str, fields: dict) -> dict:
        contact = self.store.contact(cid)
        contact.update({k: v for k, v in fields.items() if k in ("name", "company", "role", "profile_url")})
        contact["guessed"], contact["run_id"] = [], run_id
        self.store.put_contact(contact)
        return contact

    # ---- send
    def send(self, user: dict, cid: str, run: dict | None) -> dict:
        smtp = smtp_settings()
        if not smtp["configured"]:
            raise HTTPError(409, "Email sending is not set up on this server. Add the SMTP_* settings to .env.")
        with self._lock:
            contact = self.get_for(user, cid)
            if contact["status"] == "sent":
                raise HTTPError(409, "This email was already sent.")
            if cid in self._sending:
                raise HTTPError(409, "This email is already being sent.")
            if contact_state(contact, run) != "approved":
                raise ValueError("Confirm the draft on the run page before sending.")
            draft = current_draft(run)
            self._sending.add(cid)
        try:
            message_id = deliver(smtp, contact["email"], draft["subject"], draft["body"])
        except Exception as exc:
            logger.warning("send failed for contact %s: %s", cid, type(exc).__name__)
            message = _send_error(exc)
            contact["send_error"] = message
            self.store.put_contact(contact)
            raise HTTPError(502, f"Not sent. {message}") from exc
        finally:
            with self._lock:
                self._sending.discard(cid)
        contact.update(status="sent", subject=draft["subject"], body=draft["body"], sent_at=_now(),
                       sent_by=user["id"], sent_by_name=user["name"], message_id=message_id, send_error=None)
        try:
            self.store.put_contact(contact)
        except Exception:
            logger.exception("email to contact %s WAS SENT (message-id %s) but the record could not be saved",
                             cid, message_id)
            raise
        return contact


# --------------------------------------------------------------------------- export

def export_xlsx(views: list[dict], tz_minutes: int = 0, *, admin: bool = False) -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    tz_minutes = max(-840, min(840, int(tz_minutes)))

    def local(value):
        try:
            return (datetime.fromisoformat(value) + timedelta(minutes=tz_minutes)).replace(tzinfo=None)
        except (TypeError, ValueError):
            return None

    columns = [("Email", 30), ("Name", 22), ("Company", 22), ("Role", 26), ("Reached out", 12), ("Status", 18),
               ("Sent at (local time)", 20), ("Sent by", 18), ("Subject", 34), ("Message", 70), ("Note", 30)]
    if admin:
        columns.append(("Owner", 18))
    columns += [("Imported from", 24), ("Imported at (local time)", 20)]
    header_fill, header_font = PatternFill("solid", fgColor="1D4ED8"), Font(bold=True, color="FFFFFF")

    def put(sheet, row, col, value):
        cell = sheet.cell(row=row, column=col)
        if isinstance(value, str):
            value = ILLEGAL_CHARACTERS_RE.sub("", value)
        cell.value = value
        if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
            cell.data_type = "s"  # always text, never a formula
        return cell

    def fill(sheet, rows):
        for index, (title, width) in enumerate(columns, start=1):
            cell = put(sheet, 1, index, title)
            cell.fill, cell.font = header_fill, header_font
            sheet.column_dimensions[get_column_letter(index)].width = width
        for r, v in enumerate(rows, start=2):
            msg = v["message"] or {}
            sent = v["state"] == "sent"
            values = [v["email"], v["name"], v["company"], v["role"], "Yes" if sent else "No", v["label"],
                      local(v["sent_at"]) if sent else None, v["sent_by_name"] if sent else "",
                      msg.get("subject", ""), msg.get("body", ""), v["note"] or ""]
            if admin:
                values.append(v["owner_name"] or "")
            values += [v["imported_from"], local(v["imported_at"])]
            for c, value in enumerate(values, start=1):
                cell = put(sheet, r, c, value)
                cell.alignment = Alignment(vertical="top", wrap_text=columns[c - 1][0] in ("Message", "Note", "Subject"))
                if isinstance(value, datetime):
                    cell.number_format = "yyyy-mm-dd hh:mm"
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(len(rows) + 1, 1)}"

    reached = [v for v in views if v["state"] == "sent"]
    pending = [v for v in views if v["state"] != "sent"]
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Summary"
    stamp = (datetime.now(UTC) + timedelta(minutes=tz_minutes)).replace(tzinfo=None)
    lines = [("Generated (local time)", stamp.strftime("%Y-%m-%d %H:%M")), ("Total contacts", len(views)),
             ("Reached out (email sent)", len(reached)), ("Not reached out yet", len(pending)), ("", "")]
    for state, label in STATE_LABELS.items():
        lines.append((label, sum(1 for v in views if v["state"] == state)))
    for r, (label, value) in enumerate(lines, start=1):
        put(summary, r, 1, label).font = Font(bold=bool(label) and r <= 4)
        put(summary, r, 2, value)
    summary.column_dimensions["A"].width = 28
    summary.column_dimensions["B"].width = 18
    for title, rows in (("All contacts", views), ("Reached out", reached), ("Not reached out", pending)):
        fill(workbook.create_sheet(title), rows)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()
