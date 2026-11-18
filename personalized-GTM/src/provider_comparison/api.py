"""FastAPI backend for the outreach workspace.

A drop-in alternative to the ``http.server`` handler in ``web.py``. It reuses
``Workspace`` (the run state machine and JSON artifacts) unchanged and exposes the
same routes, so ``static/app.js`` works without edits.

Run (from the repo root, after ``pip install fastapi uvicorn``):

    python -m provider_comparison.api --port 8765
    # or
    uvicorn provider_comparison.api:app_factory --factory --host 0.0.0.0 --port 8765

Storage: set MONGODB_URI (e.g. mongodb://localhost:27017) to persist runs in MongoDB
(see mongo_workspace.py); otherwise runs are stored as JSON files in --data-dir.

Run a SINGLE worker: run state lives in one process (in memory + state.json files).
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import secrets
import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import ROOT, load_project_env, read_json
from .mongo_workspace import build_workspace
from .outreach import MAX_UPLOAD_BYTES, OutreachService, contact_view, export_xlsx, smtp_settings
from .sheets import SheetService
from .team import TeamService, can_access_run, current_user, install_team_routes, team_service
from .web import (
    DEMO_PROSPECTS,
    EVALUATOR_GUIDE,
    PRODUCT_BRIEF,
    STATIC,
    Workspace,
    _https_origin,
)

logger = logging.getLogger(__name__)

RUN_ID = re.compile(r"[a-f0-9]{32}")
ACTIONS = {"confirm", "update", "retry", "research", "save", "regenerate", "approve"}
MAX_BODY_BYTES = 65536
UPLOAD_PATH = "/api/outreach/import"
SHEET_UPLOAD_PATH = "/api/sheets/import"
UPLOAD_TYPES = ("application/octet-stream", "text/csv", "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
REQUIRED_KEYS = ("FIRECRAWL_API_KEY", "EXA_API_KEY", "GEMINI_API_KEY")


# --------------------------------------------------------------------------- security

@dataclass
class Security:
    """Same trust model as web.py: localhost by default, optional public HTTPS + Basic auth."""
    public_origin: str | None = None
    demo_password: str | None = None
    trusted_origins: tuple[str, ...] = field(default_factory=tuple)


def resolve_security(public_origin_arg: str | None = None) -> Security:
    """Build the trust configuration from env vars / CLI, mirroring web.main()."""
    password = (os.environ.get("DEMO_PASSWORD") or "").strip()
    explicit = _https_origin(os.environ.get("PUBLIC_ORIGIN"))
    render = _https_origin(os.environ.get("RENDER_EXTERNAL_URL"))
    cli = _https_origin(public_origin_arg)

    if os.environ.get("PUBLIC_ORIGIN") and not explicit:
        raise ValueError("PUBLIC_ORIGIN must be an HTTPS origin, such as https://demo.example.com")
    if public_origin_arg and not cli:
        raise ValueError("--public-origin must be an HTTPS origin, such as https://demo.example.com")
    if explicit and len(password) < 16:
        raise ValueError("PUBLIC_ORIGIN is set but DEMO_PASSWORD must be at least 16 characters.")

    public = cli or explicit
    if public and len(password) >= 16:
        return Security(public_origin=public, demo_password=password)
    if render:
        # Render sets RENDER_EXTERNAL_URL; trust that host, and require login only if a password is set.
        if len(password) >= 16:
            return Security(public_origin=render, demo_password=password, trusted_origins=(render,))
        logger.warning("Render URL is open without HTTP Basic auth; set DEMO_PASSWORD (16+ chars).")
        return Security(trusted_origins=(render,))
    return Security()


def _allowed_origins(security: Security, port: int) -> dict[str, str]:
    allowed = {f"127.0.0.1:{port}": f"http://127.0.0.1:{port}",
               f"localhost:{port}": f"http://localhost:{port}"}
    if security.public_origin:
        allowed[urlparse(security.public_origin).netloc] = security.public_origin
    for origin in security.trusted_origins:
        parsed = urlparse(origin)
        if parsed.scheme == "https" and parsed.netloc:
            allowed[parsed.netloc] = origin
    return allowed


def _harden(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = CSP
    return response


def _error(status: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return _harden(JSONResponse({"error": message}, status_code=status, headers=headers))


# --------------------------------------------------------------------------- app

def get_workspace(request: Request) -> Workspace:
    return request.app.state.workspace


def valid_run_id(run_id: str) -> str:
    if not RUN_ID.fullmatch(run_id):
        raise StarletteHTTPException(404, "Run not found")
    return run_id


def create_app(data_dir: Path, security: Security | None = None, port: int = 8765,
               workspace: Workspace | None = None) -> FastAPI:
    security = security or Security()
    allowed = _allowed_origins(security, port)

    # Interactive docs are off: they load scripts from a CDN, which the CSP blocks.
    app = FastAPI(title="Personalized Reachout API", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.workspace = workspace or Workspace(data_dir)
    # Team accounts + search log (stored in MongoDB when MONGODB_URI is set, else team.json).
    app.state.team = TeamService.open(data_dir, secure_cookie=bool(security.public_origin or security.trusted_origins))
    install_team_routes(app)
    app.state.outreach = OutreachService(app.state.team.store)
    app.state.sheets = SheetService(app.state.team.store)

    # ---- guard: origin check, optional Basic auth, body limits, hardening headers
    @app.middleware("http")
    async def guard(request: Request, call_next):
        host, origin = request.headers.get("host", ""), request.headers.get("origin")
        if host not in allowed or (origin and origin != allowed[host]):
            return _error(403, "This request origin is not allowed.")
        if security.public_origin:
            expected = "Basic " + base64.b64encode(f"demo:{security.demo_password}".encode()).decode()
            supplied = request.headers.get("authorization", "")
            if not security.demo_password or not secrets.compare_digest(supplied.encode(), expected.encode()):
                return _error(401, "Enter the demo credentials to continue.", {
                    "WWW-Authenticate": 'Basic realm="Personalized Reachout demo", charset="UTF-8"'})
        if request.method == "POST":
            length = request.headers.get("content-length", "0")
            upload = request.url.path in (UPLOAD_PATH, SHEET_UPLOAD_PATH)  # spreadsheet upload: raw bytes, larger limit
            kind = request.headers.get("content-type", "")
            if (not kind.startswith(UPLOAD_TYPES if upload else "application/json") or not length.isdigit()
                    or not 0 < int(length) <= (MAX_UPLOAD_BYTES if upload else MAX_BODY_BYTES)):
                return _error(400, "Upload a spreadsheet up to 5 MB." if upload
                              else "Expected a JSON request within the size limit.")
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled error %s %s", request.method, request.url.path)
            return _error(500, "Could not save this action. Please try again.")
        return _harden(response)

    # ---- errors keep the {"error": "..."} shape that static/app.js reads
    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        message = "Not found" if exc.detail == "Not Found" else str(exc.detail)
        return _error(exc.status_code, message)

    @app.exception_handler(RequestValidationError)
    async def bad_body(_: Request, __: RequestValidationError):
        return _error(400, "Expected a JSON object.")

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError):
        # Input errors from Workspace contain no provider responses or credentials.
        return _error(404 if str(exc) == "Run not found" else 400, str(exc))

    @app.exception_handler(KeyError)
    @app.exception_handler(TypeError)
    async def invalid(_: Request, __: Exception):
        return _error(400, "Invalid request")

    # ---- read-only endpoints
    def run_for(ws: Workspace, run_id: str, user: dict) -> dict:
        """Ownership check. Another member's run is reported as not found."""
        with ws.lock:
            run = ws.runs.get(run_id)
            if not can_access_run(user, run):
                raise StarletteHTTPException(404, "Run not found")
            return {"stage": run["stage"]}

    @app.get("/api/runs")
    def list_runs(user: dict = Depends(current_user), ws: Workspace = Depends(get_workspace)):
        with ws.lock:
            return [r for r in ws.listing() if can_access_run(user, ws.runs.get(r["id"]))]

    @app.get("/api/config")
    def config(_: dict = Depends(current_user)):
        return {"missing": [k for k in REQUIRED_KEYS if not os.environ.get(k)]}

    @app.get("/api/demo")
    def demo(_: dict = Depends(current_user)):
        brief = PRODUCT_BRIEF.read_text(encoding="utf-8").split("## Outreach objective", 1)[0]
        brief = brief.replace("Zamp is the company whose hiring assessment this workspace supports. "
                              "The product described below is the product from the case study; "
                              "it is not Zamp's own product.\n\n", "")
        return {"prospects": read_json(DEMO_PROSPECTS), "product_brief": brief}

    @app.get("/api/guide")
    def guide(_: dict = Depends(current_user)):
        return read_json(EVALUATOR_GUIDE)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str = Depends(valid_run_id), user: dict = Depends(current_user),
                ws: Workspace = Depends(get_workspace)):
        run_for(ws, run_id, user)
        return ws.get(run_id)

    # ---- mutations (sync routes run in FastAPI's threadpool; Workspace has its own lock)
    def create_run_for(user: dict, payload: dict, ws: Workspace, team: TeamService) -> dict:
        """Start a run owned by `user` and record the identity search in the team log."""
        run = ws.create(payload)
        ws.update(run["id"], created_by=user["id"], created_by_name=user["username"])
        team.log_search(user, "identify", run["id"], run["prospect"])
        return ws.get(run["id"])

    @app.post("/api/runs", status_code=201)
    def create_run(payload: dict[str, Any], user: dict = Depends(current_user),
                   ws: Workspace = Depends(get_workspace), team: TeamService = Depends(team_service)):
        return create_run_for(user, payload, ws, team)

    @app.post("/api/runs/{run_id}/sources")
    async def add_source(payload: dict[str, Any], run_id: str = Depends(valid_run_id),
                         user: dict = Depends(current_user), ws: Workspace = Depends(get_workspace)):
        run_for(ws, run_id, user)
        return await ws.add_source(run_id, payload)

    @app.post("/api/runs/{run_id}/{action}")
    def run_action(action: str, payload: dict[str, Any], run_id: str = Depends(valid_run_id),
                   user: dict = Depends(current_user), ws: Workspace = Depends(get_workspace),
                   team: TeamService = Depends(team_service)):
        if action not in ACTIONS:
            raise StarletteHTTPException(404, "Not found")
        stage_before = run_for(ws, run_id, user)["stage"]
        result = ws.action(run_id, action, payload)
        # Searches start on: confirm / update (research begins), research (re-run), retry of a search stage.
        kind = "research" if action in ("confirm", "update", "research") else (
            stage_before if action == "retry" and stage_before in ("identify", "research") else None)
        if kind:
            team.log_search(user, kind, run_id, result["prospect"])
        return result

    @app.delete("/api/runs/{run_id}", status_code=204)
    def delete_run(run_id: str = Depends(valid_run_id), user: dict = Depends(current_user),
                   ws: Workspace = Depends(get_workspace)):
        run_for(ws, run_id, user)
        ws.delete(run_id)
        return Response(status_code=204)


    # ---- outreach tracker: import prospects, start searches, send approved emails, export
    def outreach_service(request: Request) -> OutreachService:
        return request.app.state.outreach

    def contact_id(cid: str) -> str:
        if not RUN_ID.fullmatch(cid):
            raise StarletteHTTPException(404, "Contact not found")
        return cid

    def views_for(user: dict, ws: Workspace, svc: OutreachService, contacts: list[dict] | None = None) -> list[dict]:
        contacts = svc.visible(user) if contacts is None else contacts
        smtp_ok, admin = smtp_settings()["configured"], user["role"] == "admin"
        with ws.lock:
            return [contact_view(c, ws.runs.get(c.get("run_id") or ""), smtp_ok=smtp_ok, admin=admin)
                    for c in contacts]

    def one_view(user: dict, ws: Workspace, svc: OutreachService, cid: str) -> dict:
        return views_for(user, ws, svc, [svc.get_for(user, cid)])[0]

    @app.get("/api/outreach/contacts")
    def contacts(user: dict = Depends(current_user), ws: Workspace = Depends(get_workspace),
                 svc: OutreachService = Depends(outreach_service)):
        smtp = smtp_settings()
        return {"contacts": views_for(user, ws, svc),
                "smtp": {"configured": smtp["configured"], "from": smtp["from_addr"], "from_name": smtp["from_name"]}}

    @app.post(UPLOAD_PATH)
    async def import_contacts(request: Request, filename: str = "upload", user: dict = Depends(current_user),
                              svc: OutreachService = Depends(outreach_service)):
        data = await request.body()
        try:
            return await run_in_threadpool(svc.import_file, user, data, filename)
        except RuntimeError as exc:  # e.g. openpyxl missing: show the real reason, not a generic error
            raise StarletteHTTPException(500, str(exc))

    @app.post("/api/outreach/contacts/{cid}")
    def edit_contact(cid: str, payload: dict[str, Any], user: dict = Depends(current_user),
                     ws: Workspace = Depends(get_workspace), svc: OutreachService = Depends(outreach_service)):
        svc.update(user, contact_id(cid), payload)
        return one_view(user, ws, svc, cid)

    @app.delete("/api/outreach/contacts/{cid}", status_code=204)
    def delete_contact(cid: str, user: dict = Depends(current_user), svc: OutreachService = Depends(outreach_service)):
        svc.delete(user, contact_id(cid))
        return Response(status_code=204)

    def start_contact(user: dict, cid: str, overrides: dict, ws: Workspace, team: TeamService,
                      svc: OutreachService) -> dict:
        contact = svc.get_for(user, cid)
        with ws.lock:
            live = ws.runs.get(contact.get("run_id") or "")
        if contact["status"] != "new":
            raise ValueError("This contact has already been handled.")
        if live:
            raise ValueError("A search already exists for this contact. Open it from the list.")
        fields = {k: str(overrides.get(k, contact.get(k)) or "") for k in ("name", "company", "role", "profile_url")}
        run = create_run_for(user, fields, ws, team)
        svc.attach_run(cid, run["id"], fields)
        return run

    @app.post("/api/outreach/contacts/{cid}/start")
    def start(cid: str, payload: dict[str, Any], user: dict = Depends(current_user),
              ws: Workspace = Depends(get_workspace), team: TeamService = Depends(team_service),
              svc: OutreachService = Depends(outreach_service)):
        start_contact(user, contact_id(cid), payload, ws, team, svc)
        return one_view(user, ws, svc, cid)

    @app.post("/api/outreach/start-batch")
    def start_batch(payload: dict[str, Any], user: dict = Depends(current_user),
                    ws: Workspace = Depends(get_workspace), team: TeamService = Depends(team_service),
                    svc: OutreachService = Depends(outreach_service)):
        ids = payload.get("ids")
        if not isinstance(ids, list) or not 0 < len(ids) <= 20:
            raise ValueError("Select between 1 and 20 contacts to start at once.")
        started, skipped = 0, []
        for cid in ids:
            try:
                contact = svc.get_for(user, contact_id(str(cid)))
                if not all(contact.get(k) for k in ("name", "company", "role")):
                    raise ValueError("Missing name, company or role")
                start_contact(user, contact["id"], {}, ws, team, svc)
                started += 1
            except (ValueError, StarletteHTTPException) as exc:
                skipped.append({"id": str(cid), "reason": str(getattr(exc, "detail", exc))})
        return {"started": started, "skipped": skipped}

    @app.post("/api/outreach/contacts/{cid}/send")
    def send_email(cid: str, user: dict = Depends(current_user), ws: Workspace = Depends(get_workspace),
                   svc: OutreachService = Depends(outreach_service)):
        contact = svc.get_for(user, contact_id(cid))
        with ws.lock:
            run = ws.runs.get(contact.get("run_id") or "")
            run = {k: run.get(k) for k in ("stage", "status", "draft_confirmed_at", "edits", "output")} if run else None
        svc.send(user, cid, run)
        return one_view(user, ws, svc, cid)

    @app.get("/api/outreach/export.xlsx")
    def export(tz: int = 0, user: dict = Depends(current_user), ws: Workspace = Depends(get_workspace),
               svc: OutreachService = Depends(outreach_service)):
        data = export_xlsx(views_for(user, ws, svc), tz, admin=user["role"] == "admin")
        return Response(data, media_type=XLSX, headers={
            "Content-Disposition": 'attachment; filename="outreach-tracker.xlsx"'})

    # ---- team spreadsheets
    def sheet_service(request: Request) -> SheetService:
        return request.app.state.sheets

    @app.get("/api/sheets")
    def sheets_list(user: dict = Depends(current_user), svc: SheetService = Depends(sheet_service)):
        return {"sheets": svc.list(user)}

    @app.post("/api/sheets", status_code=201)
    def sheets_create(payload: dict[str, Any], user: dict = Depends(current_user),
                      svc: SheetService = Depends(sheet_service)):
        return {"sheet": svc.create(user, payload.get("name"))}

    @app.post(SHEET_UPLOAD_PATH, status_code=201)
    async def sheets_import(request: Request, filename: str = "upload", user: dict = Depends(current_user),
                            svc: SheetService = Depends(sheet_service)):
        data = await request.body()
        try:
            return {"sheet": await run_in_threadpool(svc.import_file, user, data, filename)}
        except RuntimeError as exc:
            raise StarletteHTTPException(500, str(exc))

    @app.get("/api/sheets/{sid}")
    def sheets_get(sid: str, user: dict = Depends(current_user), svc: SheetService = Depends(sheet_service)):
        return {"sheet": svc.get(user, sid)}

    @app.post("/api/sheets/{sid}/rename")
    def sheets_rename(sid: str, payload: dict[str, Any], user: dict = Depends(current_user),
                      svc: SheetService = Depends(sheet_service)):
        return {"sheet": svc.rename(user, sid, payload.get("name"))}

    @app.post("/api/sheets/{sid}/cells")
    def sheets_cells(sid: str, payload: dict[str, Any], user: dict = Depends(current_user),
                     svc: SheetService = Depends(sheet_service)):
        return svc.set_cells(user, sid, payload.get("sr"), payload.get("changes"))

    @app.post("/api/sheets/{sid}/ops")
    def sheets_ops(sid: str, payload: dict[str, Any], user: dict = Depends(current_user),
                   svc: SheetService = Depends(sheet_service)):
        return {"sheet": svc.apply(user, sid, payload)}

    @app.delete("/api/sheets/{sid}", status_code=204)
    def sheets_delete(sid: str, user: dict = Depends(current_user), svc: SheetService = Depends(sheet_service)):
        svc.delete(user, sid)
        return Response(status_code=204)

    @app.get("/api/sheets/{sid}/export.xlsx")
    def sheets_export(sid: str, user: dict = Depends(current_user), svc: SheetService = Depends(sheet_service)):
        data, name = svc.export_xlsx(user, sid)
        return Response(data, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/sheets/{sid}/to-tracker")
    async def sheets_to_tracker(sid: str, user: dict = Depends(current_user),
                                svc: SheetService = Depends(sheet_service),
                                tracker: OutreachService = Depends(outreach_service)):
        data, name = svc.to_csv(user, sid)
        return await run_in_threadpool(tracker.import_file, user, data, name)

    # ---- static frontend
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/app.js", include_in_schema=False)
    def script():
        return FileResponse(STATIC / "app.js", media_type="text/javascript; charset=utf-8")

    @app.get("/style.css", include_in_schema=False)
    def stylesheet():
        return FileResponse(STATIC / "style.css", media_type="text/css; charset=utf-8")

    @app.get("/team.js", include_in_schema=False)
    def team_script():
        return FileResponse(STATIC / "team.js", media_type="text/javascript; charset=utf-8")

    @app.get("/outreach.js", include_in_schema=False)
    def outreach_script():
        return FileResponse(STATIC / "outreach.js", media_type="text/javascript; charset=utf-8")

    @app.get("/sheets.js", include_in_schema=False)
    def sheets_script():
        return FileResponse(STATIC / "sheets.js", media_type="text/javascript; charset=utf-8")

    @app.get("/team.css", include_in_schema=False)
    def team_stylesheet():
        return FileResponse(STATIC / "team.css", media_type="text/css; charset=utf-8")

    return app


# --------------------------------------------------------------------------- entry points

def _setup_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(data_dir / "workspace.log", encoding="utf-8")])


def app_factory() -> FastAPI:
    """For ``uvicorn provider_comparison.api:app_factory --factory``. Reads PORT / DATA_DIR from env."""
    load_project_env()
    data_dir = Path(os.environ.get("DATA_DIR") or ROOT / "comparison/artifacts/workspace")
    _setup_logging(data_dir)
    return create_app(data_dir, resolve_security(), int(os.environ.get("PORT", "8765")),
                      build_workspace(data_dir))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the outreach workspace (FastAPI)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "comparison/artifacts/workspace")
    parser.add_argument("--public-origin", default=None,
                        help="Exact public HTTPS URL; with DEMO_PASSWORD enables HTTP Basic auth")
    args = parser.parse_args()
    load_project_env()
    try:
        security = resolve_security(args.public_origin)
    except ValueError as exc:
        parser.error(str(exc))
    if args.host not in LOOPBACK_HOSTS and not (security.public_origin or security.trusted_origins):
        parser.error("A non-loopback host requires a trusted public origin (and DEMO_PASSWORD for auth).")

    import uvicorn

    _setup_logging(args.data_dir)
    try:
        workspace = build_workspace(args.data_dir)  # MongoDB when MONGODB_URI is set
    except RuntimeError as exc:
        parser.error(str(exc))
    app = create_app(args.data_dir, security, args.port, workspace)
    print(f"Outreach workspace: http://127.0.0.1:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", workers=1)


if __name__ == "__main__":
    main()
