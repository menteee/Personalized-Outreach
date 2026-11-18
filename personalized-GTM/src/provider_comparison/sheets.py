"""Team spreadsheets: import any .xlsx/.csv, create blank sheets, edit in the browser, export to Excel.

Every signed-in member of the team can see and edit every sheet. A sheet can be deleted by the
member who created it or by an admin. Cell values are plain text (no formulas); on export, text
that looks like a number is written as a number, and anything that could be read as a formula
(=, +, -, @ at the start) is written as text so a spreadsheet never runs it.

Edits are cell-level. ``sr`` (structure revision) changes only when rows or columns are deleted, so
a stale browser that still has the old row/column positions is refused (409) instead of writing
into the wrong cell. Adding rows or columns appends, so it never invalidates anyone.
"""
from __future__ import annotations

import csv
import io
import os
import re
import threading
import uuid
from typing import Any

from starlette.exceptions import HTTPException as HTTPError

from .outreach import CONTROL, read_rows
from .team import _now, _text

MAX_SHEETS = 200
MAX_ROWS = 5000
MAX_COLS = 40
MAX_CELL = 2000
MAX_TOTAL_CHARS = 6_000_000
MAX_CHANGES = 500
SID = re.compile(r"[a-f0-9]{32}")
NUMBER = re.compile(r"-?(?:0|[1-9]\d{0,14})(?:\.\d{1,10})?")


def column_letter(index: int) -> str:
    letters, n = "", index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _cell(value: Any) -> str:
    text = CONTROL.sub("", "" if value is None else str(value))
    if len(text) > MAX_CELL:
        raise ValueError(f"A cell can hold at most {MAX_CELL} characters.")
    return text


def _size(sheet: dict) -> int:
    return sum(len(c) for c in sheet["columns"]) + sum(len(c) for row in sheet["rows"] for c in row)


def _meta(sheet: dict, user: dict) -> dict:
    return {
        "id": sheet["id"], "name": sheet["name"], "nrows": sheet.get("nrows", len(sheet.get("rows", []))),
        "ncols": sheet.get("ncols", len(sheet.get("columns", []))), "created_by_name": sheet["created_by_name"],
        "created_at": sheet["created_at"], "updated_at": sheet["updated_at"],
        "updated_by_name": sheet["updated_by_name"], "source": sheet.get("source", ""),
        "can_delete": user["role"] == "admin" or sheet["created_by"] == user["id"],
    }


class SheetService:
    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()  # one process: serialises read-modify-write per install

    # ---- helpers
    def _load(self, sid: str) -> dict:
        sheet = self.store.sheet(sid) if SID.fullmatch(str(sid)) else None
        if not sheet:
            raise HTTPError(404, "Sheet not found")
        return sheet

    def _save(self, sheet: dict, user: dict) -> None:
        if _size(sheet) > MAX_TOTAL_CHARS:
            raise ValueError("This sheet is too large. Remove some rows or shorten the text.")
        sheet.update(nrows=len(sheet["rows"]), ncols=len(sheet["columns"]), updated_at=_now(),
                     updated_by=user["id"], updated_by_name=user["name"], rev=sheet.get("rev", 0) + 1)
        self.store.put_sheet(sheet)

    def _full(self, sheet: dict, user: dict) -> dict:
        return {**_meta(sheet, user), "columns": sheet["columns"], "rows": sheet["rows"], "sr": sheet["sr"], "rev": sheet.get("rev", 0),
                "max_rows": MAX_ROWS, "max_cols": MAX_COLS}

    def _new(self, user: dict, name: str, columns: list[str], rows: list[list[str]], source: str = "") -> dict:
        if len(self.store.sheets()) >= MAX_SHEETS:
            raise ValueError(f"You can keep at most {MAX_SHEETS} sheets. Delete one first.")
        now = _now()
        sheet = dict(id=uuid.uuid4().hex, name=_text(name, "Sheet name", 80, required=True), columns=columns,
                     rows=rows, sr=0, rev=0, source=source[:120], created_by=user["id"],
                     created_by_name=user["name"], created_at=now, updated_at=now, updated_by=user["id"],
                     updated_by_name=user["name"], nrows=len(rows), ncols=len(columns))
        self._save(sheet, user)
        return sheet

    # ---- read
    def list(self, user: dict) -> list[dict]:
        return [_meta(s, user) for s in self.store.sheets()]

    def get(self, user: dict, sid: str) -> dict:
        return self._full(self._load(sid), user)

    # ---- create / import
    def create(self, user: dict, name: str) -> dict:
        with self._lock:
            columns = [column_letter(i) for i in range(6)]
            return self._full(self._new(user, name or "Untitled sheet", columns, [[""] * 6 for _ in range(20)]), user)

    def import_file(self, user: dict, data: bytes, filename: str) -> dict:
        rows = read_rows(data, MAX_ROWS)
        if not rows:
            raise ValueError("The file has no rows.")
        if len(rows) > MAX_ROWS + 1:
            raise ValueError(f"Too many rows. A sheet can hold at most {MAX_ROWS} rows (excluding the header).")
        width = max((max((i for i, c in enumerate(r) if c), default=-1) + 1 for r in rows), default=0)
        if width == 0:
            raise ValueError("The file has no data.")
        if width > MAX_COLS:
            raise ValueError(f"Too many columns. A sheet can hold at most {MAX_COLS} columns.")
        header = [_cell(c).strip() or f"Column {i + 1}" for i, c in enumerate((rows[0] + [""] * width)[:width])]
        body = [[_cell(c) for c in (r + [""] * width)[:width]] for r in rows[1:]]
        base = re.split(r"[\\/]", filename or "")[-1] or "Imported sheet"
        name = re.sub(r"\.(xlsx|csv|txt)$", "", base, flags=re.I).strip() or "Imported sheet"
        with self._lock:
            sheet = self._new(user, name[:80], header, body or [[""] * width], source=base)
        return self._full(sheet, user)

    # ---- edit
    def rename(self, user: dict, sid: str, name: str) -> dict:
        with self._lock:
            sheet = self._load(sid)
            sheet["name"] = _text(name, "Sheet name", 80, required=True)
            self._save(sheet, user)
            return _meta(sheet, user)

    def set_cells(self, user: dict, sid: str, sr: Any, changes: Any) -> dict:
        if not isinstance(changes, list) or not 0 < len(changes) <= MAX_CHANGES:
            raise ValueError(f"Send between 1 and {MAX_CHANGES} cell changes.")
        with self._lock:
            sheet = self._load(sid)
            self._check_sr(sheet, sr)
            rows, columns = sheet["rows"], sheet["columns"]
            for change in changes:
                r, c = change.get("r"), change.get("c")
                if not (isinstance(r, int) and isinstance(c, int) and not isinstance(r, bool) and not isinstance(c, bool)):
                    raise ValueError("Invalid cell position.")
                if not (0 <= c < len(columns)) or not (-1 <= r < len(rows)):
                    raise HTTPError(409, "This sheet changed. Reloading it.")
                value = _cell(change.get("v"))
                if r == -1:
                    columns[c] = _text(value, "Column name", 120) or f"Column {c + 1}"
                else:
                    rows[r][c] = value
            self._save(sheet, user)
            return {"ok": True, "sr": sheet["sr"], "rev": sheet["rev"], "updated_at": sheet["updated_at"],
                    "updated_by_name": sheet["updated_by_name"]}

    @staticmethod
    def _check_sr(sheet: dict, sr: Any) -> None:
        if sr != sheet["sr"]:
            raise HTTPError(409, "This sheet changed. Reloading it.")

    def apply(self, user: dict, sid: str, payload: dict) -> dict:
        op = payload.get("op")
        with self._lock:
            sheet = self._load(sid)
            self._check_sr(sheet, payload.get("sr"))
            rows, columns = sheet["rows"], sheet["columns"]
            if op == "add_rows":
                count = payload.get("count", 1)
                if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100:
                    raise ValueError("Add between 1 and 100 rows at a time.")
                if len(rows) + count > MAX_ROWS:
                    raise ValueError(f"A sheet can hold at most {MAX_ROWS} rows.")
                rows.extend([""] * len(columns) for _ in range(count))
            elif op == "add_col":
                if len(columns) >= MAX_COLS:
                    raise ValueError(f"A sheet can hold at most {MAX_COLS} columns.")
                columns.append(_text(payload.get("name"), "Column name", 120) or column_letter(len(columns)))
                for row in rows:
                    row.append("")
            elif op == "delete_rows":
                picks = payload.get("rows")
                if not isinstance(picks, list) or not picks or not all(
                        isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(rows) for i in picks):
                    raise ValueError("Choose the rows to delete.")
                drop = set(picks)
                sheet["rows"] = [row for i, row in enumerate(rows) if i not in drop]
                if not sheet["rows"]:
                    sheet["rows"] = [[""] * len(columns)]
                sheet["sr"] += 1
            elif op == "delete_col":
                index = payload.get("col")
                if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(columns):
                    raise ValueError("Choose a column to delete.")
                if len(columns) == 1:
                    raise ValueError("A sheet needs at least one column.")
                del columns[index]
                for row in rows:
                    del row[index]
                sheet["sr"] += 1
            else:
                raise ValueError("Unknown action.")
            self._save(sheet, user)
            return self._full(sheet, user)

    def delete(self, user: dict, sid: str) -> None:
        with self._lock:
            sheet = self._load(sid)
            if not (user["role"] == "admin" or sheet["created_by"] == user["id"]):
                raise HTTPError(403, "Only the sheet's creator or an admin can delete it.")
            self.store.delete_sheet(sid)

    # ---- hand-offs
    def to_csv(self, user: dict, sid: str) -> tuple[bytes, str]:
        sheet = self._load(sid)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(sheet["columns"])
        writer.writerows(sheet["rows"])
        return out.getvalue().encode("utf-8"), f"{sheet['name']}.csv"

    def export_xlsx(self, user: dict, sid: str) -> tuple[bytes, str]:
        from openpyxl import Workbook
        from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        sheet = self._load(sid)
        workbook = Workbook()
        ws = workbook.active
        ws.title = re.sub(r"[\[\]:*?/\\]", " ", sheet["name"]).strip()[:31] or "Sheet"

        def put(row, col, text):
            cell = ws.cell(row=row, column=col)
            text = ILLEGAL_CHARACTERS_RE.sub("", text)
            if NUMBER.fullmatch(text):
                cell.value = float(text) if "." in text else int(text)
                cell.number_format = "General" if "." in text else "0"
            else:
                cell.value = text
                if text[:1] in ("=", "+", "-", "@"):
                    cell.data_type = "s"  # always text, never a formula
            return cell

        fill, font = PatternFill("solid", fgColor="1D4ED8"), Font(bold=True, color="FFFFFF")
        for c, title in enumerate(sheet["columns"], start=1):
            cell = put(1, c, title)
            cell.fill, cell.font = fill, font
            longest = max([len(title)] + [len(r[c - 1]) for r in sheet["rows"][:300]])
            ws.column_dimensions[get_column_letter(c)].width = max(10, min(50, longest + 2))
        for r, row in enumerate(sheet["rows"], start=2):
            for c, value in enumerate(row, start=1):
                if value:
                    put(r, c, value).alignment = Alignment(vertical="top", wrap_text="\n" in value)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(sheet['columns']))}{len(sheet['rows']) + 1}"
        output = io.BytesIO()
        workbook.save(output)
        safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", sheet["name"]).strip(" .") or "sheet"
        return output.getvalue(), f"{safe}.xlsx"
