"""Team accounts, sessions and the admin-editable search log.

Roles
    admin   manages members; the only role that can edit or delete search-log entries
    member  runs searches (own runs only); can read the team search log, never change it

Every search is recorded in the log with who ran it and when:
    identify   the identity check started for a prospect
    research   the research searches started (after the prospect was confirmed, or re-run)

Storage follows the workspace: MongoDB (collections ``users``, ``activity``, ``meta``)
when MONGODB_URI is set, otherwise ``team.json`` in the data directory.

First start with no users: the sign-in page shows "Create admin account" (one-time setup; it
closes as soon as one user exists). Alternatively ADMIN_USERNAME / ADMIN_PASSWORD in the
environment create the first admin at startup. The admin then adds members under Manage team.
Sessions are signed HttpOnly cookies; SESSION_SECRET may pin the signing key.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from starlette.exceptions import HTTPException as HTTPError

logger = logging.getLogger(__name__)

USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{2,31}")  # older accounts created before email logins
LOGIN_EMAIL = re.compile(r"[a-z0-9][a-z0-9._%+'-]{0,63}@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")


def valid_login(value: str, *, legacy: bool = True) -> bool:
    """New accounts sign in with an email address; older plain usernames keep working."""
    return (len(value) <= 254 and bool(LOGIN_EMAIL.fullmatch(value))) or (legacy and bool(USERNAME.fullmatch(value)))
ROLES = ("admin", "member")
KINDS = ("identify", "research")
MIN_PASSWORD, MAX_PASSWORD = 10, 200
SESSION_SECONDS = int(os.environ.get("SESSION_HOURS", "12")) * 3600
COOKIE = "gtm_session"
MAX_FAILS, LOCK_SECONDS = 5, 300


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: Any, label: str, limit: int, *, required: bool = False, multiline: bool = False) -> str:
    value = str(value if value is not None else "")
    value = value.strip() if multiline else " ".join(value.split())
    if required and not value:
        raise ValueError(f"{label} is required.")
    if len(value) > limit:
        raise ValueError(f"{label} must be {limit} characters or fewer.")
    return value


# --------------------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        probe = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(probe, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


DUMMY_HASH = hash_password(secrets.token_hex(8))  # equalises timing for unknown usernames


def check_password_policy(password: str, username: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise ValueError(f"Password must be {MAX_PASSWORD} characters or fewer.")
    if password.strip().lower() == username.lower():
        raise ValueError("Password must not be the same as the username.")


# --------------------------------------------------------------------------- stores

class FileStore:
    """JSON file with an in-memory copy. Fine for a team-sized workload (single process)."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {"users": {}, "activity": [], "meta": {}, "contacts": {}}
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))
        self.data.setdefault("contacts", {})
        path.parent.mkdir(parents=True, exist_ok=True)

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    def meta(self, key):
        with self.lock:
            return self.data["meta"].get(key)

    def set_meta(self, key, value):
        with self.lock:
            self.data["meta"][key] = value
            self._flush()

    def user(self, uid):
        with self.lock:
            return copy.deepcopy(self.data["users"].get(uid))

    def user_by_name(self, username):
        with self.lock:
            return next((copy.deepcopy(u) for u in self.data["users"].values() if u["username"] == username), None)

    def users(self):
        with self.lock:
            return sorted(copy.deepcopy(list(self.data["users"].values())), key=lambda u: u["created_at"])

    def add_user(self, user):
        with self.lock:
            if any(u["username"] == user["username"] for u in self.data["users"].values()):
                raise ValueError("An account with that email already exists.")
            self.data["users"][user["id"]] = copy.deepcopy(user)
            self._flush()

    def put_user(self, user):
        with self.lock:
            self.data["users"][user["id"]] = copy.deepcopy(user)
            self._flush()

    def add_activity(self, entry):
        with self.lock:
            self.data["activity"].append(copy.deepcopy(entry))
            self._flush()

    def activities(self, limit):
        with self.lock:
            return copy.deepcopy(self.data["activity"][::-1][:limit])

    def activity(self, aid):
        with self.lock:
            return copy.deepcopy(next((a for a in self.data["activity"] if a["id"] == aid), None))

    def put_activity(self, entry):
        with self.lock:
            for index, current in enumerate(self.data["activity"]):
                if current["id"] == entry["id"]:
                    self.data["activity"][index] = copy.deepcopy(entry)
                    self._flush()
                    return

    def delete_activity(self, aid):
        with self.lock:
            before = len(self.data["activity"])
            self.data["activity"] = [a for a in self.data["activity"] if a["id"] != aid]
            self._flush()
            return len(self.data["activity"]) < before

    # ---- outreach contacts (one row per prospect email; emails are unique team-wide)
    def contacts(self):
        with self.lock:
            return sorted(copy.deepcopy(list(self.data["contacts"].values())), key=lambda c: c["created_at"])

    def contact(self, cid):
        with self.lock:
            return copy.deepcopy(self.data["contacts"].get(cid))

    def contact_by_email(self, email):
        with self.lock:
            return next((copy.deepcopy(c) for c in self.data["contacts"].values() if c["email"] == email), None)

    def add_contacts(self, items):
        """Insert new contacts; returns (added, duplicates) where duplicates are existing contacts."""
        added, duplicates = [], []
        with self.lock:
            taken = {c["email"]: c for c in self.data["contacts"].values()}
            for item in items:
                if item["email"] in taken:
                    duplicates.append(copy.deepcopy(taken[item["email"]]))
                    continue
                self.data["contacts"][item["id"]] = copy.deepcopy(item)
                taken[item["email"]] = item
                added.append(item)
            if added:
                self._flush()
        return added, duplicates

    def put_contact(self, contact):
        with self.lock:
            if any(c["email"] == contact["email"] and c["id"] != contact["id"] for c in self.data["contacts"].values()):
                raise ValueError("That email address is already in the tracker.")
            self.data["contacts"][contact["id"]] = copy.deepcopy(contact)
            self._flush()

    def delete_contact(self, cid):
        with self.lock:
            existed = self.data["contacts"].pop(cid, None) is not None
            if existed:
                self._flush()
            return existed


    # ---- sheets (one JSON file per sheet, so editing a big sheet never rewrites everything else)
    def _sheet_path(self, sid):
        if not re.fullmatch(r"[a-f0-9]{32}", str(sid)):
            raise ValueError("Sheet not found")
        return self.path.parent / "sheets" / f"{sid}.json"

    def sheets(self):
        """Every sheet without its rows (for the list view)."""
        folder = self.path.parent / "sheets"
        found = []
        with self.lock:
            for file in sorted(folder.glob("*.json")) if folder.exists() else []:
                try:
                    doc = json.loads(file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                doc.pop("rows", None)
                found.append(doc)
        return sorted(found, key=lambda d: d["updated_at"], reverse=True)

    def sheet(self, sid):
        with self.lock:
            try:
                return json.loads(self._sheet_path(sid).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

    def put_sheet(self, sheet):
        with self.lock:
            path = self._sheet_path(sheet["id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sheet, ensure_ascii=False), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(path)

    def delete_sheet(self, sid):
        with self.lock:
            try:
                self._sheet_path(sid).unlink()
                return True
            except (OSError, ValueError):
                return False


class MongoStore:
    def __init__(self, uri: str, db_name: str):
        from pymongo import ASCENDING, DESCENDING, MongoClient
        from pymongo.errors import DuplicateKeyError

        self._duplicate = DuplicateKeyError
        self._client = MongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True)
        db = self._client[db_name]
        self._users, self._activity, self._meta = db["users"], db["activity"], db["meta"]
        self._contacts, self._sheets = db["contacts"], db["sheets"]
        self._contacts.create_index([("email", ASCENDING)], unique=True)
        self._users.create_index([("username", ASCENDING)], unique=True)
        self._activity.create_index([("at", DESCENDING)])

    @staticmethod
    def _clean(doc):
        if doc is not None:
            doc.pop("_id", None)
        return doc

    def meta(self, key):
        doc = self._meta.find_one({"_id": key})
        return doc["value"] if doc else None

    def set_meta(self, key, value):
        self._meta.replace_one({"_id": key}, {"_id": key, "value": value}, upsert=True)

    def user(self, uid):
        return self._clean(self._users.find_one({"_id": uid}))

    def user_by_name(self, username):
        return self._clean(self._users.find_one({"username": username}))

    def users(self):
        return [self._clean(u) for u in self._users.find().sort("created_at", 1)]

    def add_user(self, user):
        try:
            self._users.insert_one({**user, "_id": user["id"]})
        except self._duplicate as exc:
            raise ValueError("An account with that email already exists.") from exc

    def put_user(self, user):
        self._users.replace_one({"_id": user["id"]}, {**user, "_id": user["id"]}, upsert=True)

    def add_activity(self, entry):
        self._activity.insert_one({**entry, "_id": entry["id"]})

    def activities(self, limit):
        return [self._clean(a) for a in self._activity.find().sort("at", -1).limit(limit)]

    def activity(self, aid):
        return self._clean(self._activity.find_one({"_id": aid}))

    def put_activity(self, entry):
        self._activity.replace_one({"_id": entry["id"]}, {**entry, "_id": entry["id"]})

    def delete_activity(self, aid):
        return self._activity.delete_one({"_id": aid}).deleted_count > 0

    def contacts(self):
        return [self._clean(c) for c in self._contacts.find().sort("created_at", 1)]

    def contact(self, cid):
        return self._clean(self._contacts.find_one({"_id": cid}))

    def contact_by_email(self, email):
        return self._clean(self._contacts.find_one({"email": email}))

    def add_contacts(self, items):
        added, duplicates = [], []
        for item in items:
            try:
                self._contacts.insert_one({**item, "_id": item["id"]})
                added.append(item)
            except self._duplicate:
                duplicates.append(self.contact_by_email(item["email"]) or {"email": item["email"]})
        return added, duplicates

    def put_contact(self, contact):
        try:
            self._contacts.replace_one({"_id": contact["id"]}, {**contact, "_id": contact["id"]}, upsert=True)
        except self._duplicate as exc:
            raise ValueError("That email address is already in the tracker.") from exc

    def delete_contact(self, cid):
        return self._contacts.delete_one({"_id": cid}).deleted_count > 0

    def sheets(self):
        return [self._clean(d) for d in self._sheets.find({}, {"rows": 0}).sort("updated_at", -1)]

    def sheet(self, sid):
        return self._clean(self._sheets.find_one({"_id": str(sid)}))

    def put_sheet(self, sheet):
        self._sheets.replace_one({"_id": sheet["id"]}, {**sheet, "_id": sheet["id"]}, upsert=True)

    def delete_sheet(self, sid):
        return self._sheets.delete_one({"_id": str(sid)}).deleted_count > 0


# --------------------------------------------------------------------------- service

class TeamService:
    def __init__(self, store, secret: bytes, secure_cookie: bool = False):
        self.store = store
        self._secret = secret
        self.secure_cookie = secure_cookie
        self._lock = threading.RLock()
        self._attempts: dict[str, tuple[int, float]] = {}

    @classmethod
    def open(cls, data_dir: Path, secure_cookie: bool = False) -> "TeamService":
        uri = (os.environ.get("MONGODB_URI") or "").strip()
        store = (MongoStore(uri, (os.environ.get("MONGODB_DB") or "personalized_gtm").strip())
                 if uri else FileStore(data_dir / "team.json"))
        secret = (os.environ.get("SESSION_SECRET") or "").strip() or store.meta("session_secret")
        if not secret:
            secret = secrets.token_hex(32)
            store.set_meta("session_secret", secret)
        service = cls(store, secret.encode(), secure_cookie)
        service.bootstrap_admin()
        return service

    # ---- accounts
    def bootstrap_admin(self) -> None:
        """Create the first admin from the environment, if ADMIN_PASSWORD is set. Otherwise the
        first visitor creates it on the setup screen (see setup_admin)."""
        password = os.environ.get("ADMIN_PASSWORD") or ""
        if not password or self.store.users():
            return
        username = (os.environ.get("ADMIN_USERNAME") or "admin").strip().lower()
        self.create_user(None, username, "Administrator", password, "admin", legacy_ok=True)
        logger.info("first admin account created from the environment: %s", username)

    def needs_setup(self) -> bool:
        return not self.store.users()

    def setup_admin(self, username, name, password) -> dict:
        """One-time: create the first admin account. Refused once any user exists."""
        with self._lock:
            if self.store.users():
                raise HTTPError(409, "Setup is already complete. Ask your admin for an account.")
            user = self.create_user(None, username, name, password, "admin")
        logger.info("first admin account created on the setup screen: %s", user["username"])
        return user

    @staticmethod
    def public(user: dict) -> dict:
        return {k: user.get(k) for k in ("id", "username", "name", "role", "active", "created_at", "last_login")}

    def create_user(self, actor, username, name, password, role, *, legacy_ok: bool = False) -> dict:
        username = str(username or "").strip().lower()
        if not valid_login(username, legacy=legacy_ok):
            raise ValueError("Enter a valid email address. It is the member's login ID.")
        if role not in ROLES:
            raise ValueError("Role must be admin or member.")
        check_password_policy(password, username)
        user = dict(id=uuid.uuid4().hex, username=username, name=_text(name or username, "Name", 80, required=True),
                    role=role, active=True, pw=hash_password(password), ver=1, created_at=_now(),
                    created_by=actor["username"] if actor else None, last_login=None)
        self.store.add_user(user)
        return user

    def _active_admins(self) -> list[dict]:
        return [u for u in self.store.users() if u["role"] == "admin" and u["active"]]

    def update_user(self, uid, *, name=None, role=None, active=None, password=None) -> dict:
        user = self.store.user(uid)
        if not user:
            raise HTTPError(404, "Member not found")
        if name is not None:
            user["name"] = _text(name, "Name", 80, required=True)
        new_role = user["role"] if role is None else role
        new_active = user["active"] if active is None else bool(active)
        if new_role not in ROLES:
            raise ValueError("Role must be admin or member.")
        losing_admin = user["role"] == "admin" and user["active"] and (new_role != "admin" or not new_active)
        if losing_admin and len(self._active_admins()) <= 1:
            raise ValueError("Keep at least one active admin.")
        if not new_active and user["active"]:
            user["ver"] += 1  # ends any open sessions
        user["role"], user["active"] = new_role, new_active
        if password:
            check_password_policy(password, user["username"])
            user["pw"], user["ver"] = hash_password(password), user["ver"] + 1
        self.store.put_user(user)
        return user

    def change_password(self, user, current, new) -> dict:
        if not verify_password(str(current or ""), user["pw"]):
            raise ValueError("Current password is incorrect.")
        check_password_policy(new, user["username"])
        user["pw"], user["ver"] = hash_password(new), user["ver"] + 1
        self.store.put_user(user)
        return user

    # ---- login and sessions
    def authenticate(self, username, password) -> dict:
        key = str(username or "").strip().lower()
        with self._lock:
            _, locked_until = self._attempts.get(key, (0, 0.0))
            if locked_until > time.time():
                raise HTTPError(429, "Too many failed attempts. Try again in a few minutes.")
        user = self.store.user_by_name(key) if valid_login(key) else None
        matches = verify_password(str(password or ""), user["pw"] if user else DUMMY_HASH)
        if not (user and matches and user["active"]):
            with self._lock:
                if len(self._attempts) > 10000:
                    self._attempts.clear()
                fails = self._attempts.get(key, (0, 0.0))[0] + 1
                self._attempts[key] = (0, time.time() + LOCK_SECONDS) if fails >= MAX_FAILS else (fails, 0.0)
            raise HTTPError(401, "Incorrect username or password.")
        with self._lock:
            self._attempts.pop(key, None)
        user["last_login"] = _now()
        self.store.put_user(user)
        return user

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()

    def make_token(self, user: dict) -> str:
        payload = f"{user['id']}.{user['ver']}.{int(time.time()) + SESSION_SECONDS}"
        return f"{payload}.{self._sign(payload)}"

    def user_from_token(self, token: str | None) -> dict | None:
        try:
            payload, signature = (token or "").rsplit(".", 1)
            if not hmac.compare_digest(signature, self._sign(payload)):
                return None
            uid, ver, expires = payload.split(".")
            if int(expires) < time.time():
                return None
        except (ValueError, TypeError):
            return None
        user = self.store.user(uid)
        return user if user and user["active"] and user["ver"] == int(ver) else None

    # ---- search log
    def log_search(self, user: dict, kind: str, run_id: str, prospect: dict) -> None:
        """Record one search. Never raises: the search itself has already been started."""
        try:
            self.store.add_activity(dict(
                id=uuid.uuid4().hex, at=_now(), user_id=user["id"], username=user["username"],
                user_name=user["name"], kind=kind, run_id=run_id,
                prospect={k: str(prospect.get(k) or "") for k in ("name", "company", "role")},
                note="", edited_at=None, edited_by=None, edit_log=[]))
        except Exception:
            logger.exception("could not record search for %s", user.get("username"))

    def list_activity(self, limit: int) -> list[dict]:
        return self.store.activities(max(1, min(limit, 2000)))

    def edit_activity(self, admin: dict, aid: str, changes: dict) -> dict:
        entry = self.store.activity(aid)
        if not entry:
            raise HTTPError(404, "Entry not found")
        limits = {"name": ("Name", 160), "company": ("Company", 160), "role": ("Role", 200)}
        diff: dict[str, list[str]] = {}
        for field, (label, limit) in limits.items():
            if field in changes:
                value = _text(changes[field], label, limit, required=True)
                if value != entry["prospect"].get(field):
                    diff[field] = [entry["prospect"].get(field), value]
                    entry["prospect"][field] = value
        if "note" in changes:
            value = _text(changes["note"], "Note", 2000, multiline=True)
            if value != entry.get("note", ""):
                diff["note"] = [entry.get("note", ""), value]
                entry["note"] = value
        if diff:
            entry["edited_at"], entry["edited_by"] = _now(), admin["username"]
            entry["edit_log"].append(dict(at=entry["edited_at"], by=admin["username"], changes=diff))
            self.store.put_activity(entry)
        return entry

    def delete_activity(self, admin: dict, aid: str) -> None:
        if not self.store.delete_activity(aid):
            raise HTTPError(404, "Entry not found")
        logger.warning("search-log entry %s deleted by admin %s", aid, admin["username"])


# --------------------------------------------------------------------------- FastAPI glue

def team_service(request: Request) -> TeamService:
    return request.app.state.team


def current_user(request: Request) -> dict:
    user = request.app.state.team.user_from_token(request.cookies.get(COOKIE))
    if not user:
        raise HTTPError(401, "Sign in to continue.")
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPError(403, "Only an admin can do this.")
    return user


def can_access_run(user: dict, run: dict | None) -> bool:
    """Admins see every run; members see only runs they started."""
    return run is not None and (user["role"] == "admin" or run.get("created_by") == user["id"])


def _set_session(response: Response, team: TeamService, user: dict) -> None:
    response.set_cookie(COOKIE, team.make_token(user), max_age=SESSION_SECONDS, httponly=True,
                        samesite="strict", secure=team.secure_cookie, path="/")


def install_team_routes(app: FastAPI) -> None:
    @app.get("/api/setup")
    def setup_status(team: TeamService = Depends(team_service)):
        return {"needs_setup": team.needs_setup()}

    @app.post("/api/setup", status_code=201)
    def setup(payload: dict[str, Any], response: Response, team: TeamService = Depends(team_service)):
        user = team.setup_admin(payload.get("username"), payload.get("name"), payload.get("password"))
        _set_session(response, team, user)
        return {"user": team.public(user)}

    @app.post("/api/login")
    def login(payload: dict[str, Any], response: Response, team: TeamService = Depends(team_service)):
        user = team.authenticate(payload.get("username"), payload.get("password"))
        _set_session(response, team, user)
        return {"user": team.public(user)}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(user: dict = Depends(current_user), team: TeamService = Depends(team_service)):
        return {"user": team.public(user)}

    @app.post("/api/me/password")
    def my_password(payload: dict[str, Any], response: Response, user: dict = Depends(current_user),
                    team: TeamService = Depends(team_service)):
        user = team.change_password(user, payload.get("current"), payload.get("new"))
        _set_session(response, team, user)  # the version bump ended the old session; keep this one
        return {"ok": True}

    @app.get("/api/team/users")
    def list_users(_: dict = Depends(require_admin), team: TeamService = Depends(team_service)):
        return {"users": [team.public(u) for u in team.store.users()]}

    @app.post("/api/team/users", status_code=201)
    def add_user(payload: dict[str, Any], admin: dict = Depends(require_admin),
                 team: TeamService = Depends(team_service)):
        user = team.create_user(admin, payload.get("username"), payload.get("name"),
                                payload.get("password"), payload.get("role", "member"))
        return {"user": team.public(user)}

    @app.post("/api/team/users/{uid}")
    def edit_user(uid: str, payload: dict[str, Any], admin: dict = Depends(require_admin),
                  team: TeamService = Depends(team_service)):
        user = team.update_user(uid, name=payload.get("name"), role=payload.get("role"),
                                active=payload.get("active"), password=payload.get("password") or None)
        return {"user": team.public(user)}

    @app.get("/api/team/activity")
    def activity(limit: int = 500, _: dict = Depends(current_user), team: TeamService = Depends(team_service)):
        return {"entries": team.list_activity(limit)}

    @app.post("/api/team/activity/{aid}")
    def edit_activity(aid: str, payload: dict[str, Any], admin: dict = Depends(require_admin),
                      team: TeamService = Depends(team_service)):
        return {"entry": team.edit_activity(admin, aid, payload)}

    @app.delete("/api/team/activity/{aid}", status_code=204)
    def delete_activity(aid: str, admin: dict = Depends(require_admin),
                        team: TeamService = Depends(team_service)):
        team.delete_activity(admin, aid)
        return Response(status_code=204)
