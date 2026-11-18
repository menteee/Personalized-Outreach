"""MongoDB persistence for the outreach workspace.

``MongoWorkspace`` extends ``Workspace`` (web.py) without modifying it. MongoDB is the
durable store; the data directory becomes a working cache:

* startup  - every run and artifact is loaded from MongoDB into the data directory,
             then ``Workspace`` loads them exactly as it always did.
* save()   - writes to disk as before, then upserts the run and any new or changed
             artifact files (lookup / research / draft / review ... JSON) into MongoDB.
* delete() - removes the run from disk and from MongoDB.

Collections (database ``MONGODB_DB``, default ``personalized_gtm``):

    runs       {_id: run_id, prospect, stage, status, created_at, updated_at, state: <json>}
    artifacts  {_id: "run_id/file.json", run_id, name, content: <json text>, updated_at}

Runs that exist only on disk (e.g. created before MongoDB was enabled) are copied into
MongoDB on first start. A MongoDB outage during a run is logged and retried on the next
save; it never fails the run, because the local copy is always written first.

Configuration: ``MONGODB_URI`` (e.g. mongodb://localhost:27017). If it is unset,
``build_workspace`` returns the plain file-based ``Workspace``.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from .web import Workspace

logger = logging.getLogger(__name__)

STATE_FILE = "state.json"
RUN_ID = re.compile(r"[a-f0-9]{32}")
DEFAULT_DB = "personalized_gtm"


def _redact(uri: str) -> str:
    return re.sub(r"://[^@/]*@", "://***@", uri)


def _fingerprint(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


class MongoWorkspace(Workspace):
    def __init__(self, directory: Path, uri: str, db_name: str = DEFAULT_DB):
        try:
            from pymongo import ASCENDING, DESCENDING, MongoClient
        except ImportError as exc:
            raise RuntimeError("pymongo is not installed. Run: pip install pymongo") from exc

        self._uri = uri
        self._client = MongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True)
        try:
            self._client.admin.command("ping")
        except Exception as exc:
            raise RuntimeError(
                f"Cannot reach MongoDB at {_redact(uri)} ({type(exc).__name__}). "
                "Is the container running? Try: docker ps"
            ) from exc

        database = self._client[db_name]
        self._runs = database["runs"]
        self._artifacts = database["artifacts"]
        self._runs.create_index([("created_at", DESCENDING)])
        self._artifacts.create_index([("run_id", ASCENDING), ("name", ASCENDING)], unique=True)

        self._synced: dict[tuple[str, str], tuple[int, int]] = {}
        directory.mkdir(parents=True, exist_ok=True)
        self._hydrate(directory)
        super().__init__(directory)  # loads the hydrated state.json files
        self._backfill()
        logger.info("storage: MongoDB %s db=%s runs=%d", _redact(uri), db_name, len(self.runs))

    # ------------------------------------------------------------------ startup

    def _hydrate(self, directory: Path) -> None:
        """Copy runs and artifacts from MongoDB into the local working directory."""
        run_ids = set()
        for doc in self._runs.find({}, {"state": 1}):
            run_id = str(doc["_id"])
            if not RUN_ID.fullmatch(run_id) or not isinstance(doc.get("state"), str):
                logger.warning("skipping malformed run document %r", run_id)
                continue
            folder = directory / run_id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / STATE_FILE).write_text(doc["state"], encoding="utf-8")
            run_ids.add(run_id)

        for doc in self._artifacts.find():
            run_id, name = str(doc.get("run_id", "")), str(doc.get("name", ""))
            # Names come from our own writes, but never trust the database for a file path.
            if run_id not in run_ids or Path(name).name != name or not name.endswith(".json") \
                    or name == STATE_FILE or not isinstance(doc.get("content"), str):
                continue
            path = directory / run_id / name
            path.write_text(doc["content"], encoding="utf-8")
            self._synced[(run_id, name)] = _fingerprint(path)

    def _backfill(self) -> None:
        """Push runs that only exist on disk into MongoDB (one-time migration)."""
        try:
            existing = {str(doc["_id"]) for doc in self._runs.find({}, {"_id": 1})}
        except Exception:
            logger.exception("MongoDB backfill skipped")
            return
        for run in list(self.runs.values()):
            if run["id"] not in existing:
                self._persist(run)

    # ------------------------------------------------------------------ writes

    def save(self, run):
        super().save(run)  # local file first: it is always the working copy
        self._persist(run)

    def _persist(self, run) -> None:
        try:
            self._runs.replace_one(
                {"_id": run["id"]},
                {
                    "prospect": run.get("prospect"),
                    "stage": run.get("stage"),
                    "status": run.get("status"),
                    "created_at": run.get("created_at"),
                    "updated_at": run.get("updated_at"),
                    # Full run kept as JSON text so any key names round-trip exactly.
                    "state": json.dumps(run, ensure_ascii=False),
                },
                upsert=True,
            )
            self._sync_artifacts(run["id"])
        except Exception:
            logger.exception("MongoDB sync failed for run %s; it will be retried on the next save", run["id"])

    def _sync_artifacts(self, run_id: str) -> None:
        folder = self.directory / run_id
        if not folder.is_dir():
            return
        for path in folder.glob("*.json"):
            if path.name == STATE_FILE:
                continue
            key = (run_id, path.name)
            try:
                fingerprint = _fingerprint(path)
                if self._synced.get(key) == fingerprint:
                    continue
                content = path.read_text(encoding="utf-8")
                json.loads(content)  # skip a file that is mid-write; the next save retries it
            except (OSError, ValueError):
                continue
            self._artifacts.replace_one(
                {"_id": f"{run_id}/{path.name}"},
                {"run_id": run_id, "name": path.name, "content": content, "updated_at": datetime.now(UTC)},
                upsert=True,
            )
            self._synced[key] = fingerprint

    def delete(self, run_id):
        super().delete(run_id)
        for key in [k for k in self._synced if k[0] == run_id]:
            del self._synced[key]
        try:
            self._runs.delete_one({"_id": run_id})
            self._artifacts.delete_many({"run_id": run_id})
        except Exception:
            logger.exception("MongoDB delete failed for run %s; it may reappear after a restart", run_id)

    def close(self) -> None:
        self._client.close()


def build_workspace(data_dir: Path) -> Workspace:
    """MongoDB-backed workspace when MONGODB_URI is set, otherwise the file-based one."""
    uri = (os.environ.get("MONGODB_URI") or "").strip()
    if not uri:
        return Workspace(data_dir)
    return MongoWorkspace(data_dir, uri, (os.environ.get("MONGODB_DB") or DEFAULT_DB).strip())
