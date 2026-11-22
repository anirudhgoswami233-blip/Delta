"""Live tab: readings and explanations from the OpenF1 race agent.

The race agent (``openf1-race`` in the openf1 project) records the official F1
live-timing feed with an F1TV subscription, runs it through OpenF1, and publishes:

- ``live.json``       timing tower, flags, race control, radio, every car's latest
                      telemetry and track position (rewritten every ~2 s)
- ``telemetry.json``  the last ~60 s of telemetry per car
- ``<session>/commentary.jsonl``, ``glossary.json``, ``debrief.md``: Claude's
                      explanations, the terms it taught, and the end-of-session debrief

This router only reads those files. When no agent is running on this machine
(for example on a hosted deployment) every endpoint says so instead of failing,
and nothing here is ever described as live unless the agent's own clock is fresh.

Provenance: timing, telemetry and positions are ``measured`` (feed values);
the commentary is an AI explanation of those values, labelled as such in the UI.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Body, HTTPException, Query

from ..config import get_settings

router = APIRouter(prefix="/api/live", tags=["live"])

# A live.json older than this is reported as stale (the agent polls every ~2 s).
STALE_AFTER_SECONDS = 15.0

NOT_RUNNING = (
    "No race agent output found on this machine. Start one in the openf1 project: "
    "`bin/openf1-live --agent` during a session, or "
    "`.venv/bin/openf1-race replay 11396 --speed 5` to replay a past race."
)


def _source() -> Path | None:
    """The agent output folder whose live.json was written most recently."""
    candidates = []
    for directory in get_settings().race_agent_dirs:
        live = directory / "live.json"
        try:
            candidates.append((live.stat().st_mtime, directory))
        except OSError:
            continue
    return max(candidates)[1] if candidates else None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _session_dir(root: Path, state: dict) -> Path | None:
    """The session folder named in live.json, confined to the agent's own folder."""
    raw = state.get("session_dir")
    if not raw:
        return None
    path = Path(raw).resolve()
    return path if path.is_relative_to(root.resolve()) and path.is_dir() else None


def _load() -> tuple[Path | None, dict | None]:
    root = _source()
    if root is None:
        return None, None
    return root, _read_json(root / "live.json")


@router.get("/state")
async def live_state() -> dict:
    root, state = _load()
    if state is None:
        return {"available": False, "reason": NOT_RUNNING}
    try:
        updated = datetime.fromisoformat(state["updated_at"])
        age = (datetime.now(timezone.utc) - updated).total_seconds()
    except (KeyError, ValueError):
        age = None
    finished = bool((state.get("agent") or {}).get("finished"))
    return {
        "available": True,
        "source": str(root),
        "age_seconds": age,
        "stale": (age is None or age > STALE_AFTER_SECONDS) and not finished,
        **state,
    }


@router.get("/commentary")
async def live_commentary(after: int = Query(0, ge=0, description="Return updates with i > after")) -> dict:
    root, state = _load()
    folder = _session_dir(root, state) if state else None
    if folder is None:
        return {"available": False, "items": []}
    items = []
    try:
        with open(folder / "commentary.jsonl") as f:
            for line in f:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("i", 0) > after:
                    items.append(item)
    except OSError:
        pass
    return {"available": True, "session_key": state.get("session_key"), "items": items}


@router.get("/glossary")
async def live_glossary() -> dict:
    root, state = _load()
    folder = _session_dir(root, state) if state else None
    glossary = _read_json(folder / "glossary.json") if folder else None
    terms = sorted((glossary or {}).items(), key=lambda kv: kv[0].lower())
    return {"available": folder is not None,
            "terms": [{"term": k, "explanation": v} for k, v in terms]}


@router.get("/telemetry")
async def live_telemetry(driver: str = Query(..., description="Driver number")) -> dict:
    root = _source()
    data = _read_json(root / "telemetry.json") if root else None
    if not data:
        return {"available": False, "driver": driver, "samples": {}}
    fields = data.get("fields") or []
    rows = (data.get("drivers") or {}).get(str(driver)) or []
    columns = {name: [row[i] if i < len(row) else None for row in rows] for i, name in enumerate(fields)}
    return {"available": True, "driver": str(driver), "updated_at": data.get("updated_at"),
            "provenance": "measured", "samples": columns}


@router.get("/debrief")
async def live_debrief() -> dict:
    root, state = _load()
    folder = _session_dir(root, state) if state else None
    try:
        text = (folder / "debrief.md").read_text() if folder else None
    except OSError:
        text = None
    return {"available": text is not None, "markdown": text}


# ---------------------------------------------------------------------------
# Control: start/stop live and replays without a terminal
# ---------------------------------------------------------------------------
#
# The race agent's control service (`openf1-race serve`, localhost only) owns the
# processes. Delta forwards these calls to it and, when it is not running, starts
# it from the openf1 project so the Agent tab works without opening a terminal.

_spawn_lock = asyncio.Lock()


async def _ping(base: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            return (await client.get(f"{base}/status")).status_code == 200
    except httpx.HTTPError:
        return False


async def _ensure_controller() -> str | None:
    """Starts the control service if it is configured for this machine. Returns a reason on failure."""
    settings = get_settings()
    base = settings.race_agent_control_url
    url = urlparse(base)
    async with _spawn_lock:
        if await _ping(base):
            return None
        if url.hostname not in ("127.0.0.1", "localhost"):
            return f"The race agent control service at {base} is not reachable."
        binary = settings.openf1_dir / ".venv" / "bin" / "openf1-race"
        if not binary.exists():
            return (f"The openf1 project was not found at {settings.openf1_dir} "
                    "(set DELTA_OPENF1_DIR). The Agent tab needs it on this machine.")
        log_dir = settings.openf1_dir / ".data" / "control"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "server.log", "a") as log:
            subprocess.Popen(
                [str(binary), "serve", "--port", str(url.port or 8010)],
                cwd=settings.openf1_dir, stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        for _ in range(40):
            await asyncio.sleep(0.5)
            if await _ping(base):
                return None
        return f"Started the race agent control service, but it did not answer. See {log_dir / 'server.log'}."


async def _control(method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> dict:
    base = get_settings().race_agent_control_url
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(method, f"{base}{path}", json=json, params=params)
            break
        except httpx.ConnectError:
            if attempt:
                return {"available": False, "reason": "The race agent control service stopped responding."}
            reason = await _ensure_controller()
            if reason:
                return {"available": False, "reason": reason}
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        raise HTTPException(status_code=response.status_code, detail=str(detail))
    return {"available": True, **response.json()}


@router.get("/control/status")
async def control_status() -> dict:
    return await _control("GET", "/status")


@router.get("/control/sessions")
async def control_sessions(year: int | None = Query(None)) -> dict:
    return await _control("GET", "/sessions", params={"year": year} if year else None)


@router.post("/control/live")
async def control_live(body: dict = Body(default={})) -> dict:
    return await _control("POST", "/live/start", json=body)


@router.post("/control/replay")
async def control_replay(body: dict = Body(...)) -> dict:
    return await _control("POST", "/replay/start", json=body)


@router.post("/control/stop")
async def control_stop() -> dict:
    return await _control("POST", "/stop")


@router.post("/control/settings")
async def control_settings(body: dict = Body(...)) -> dict:
    return await _control("POST", "/settings", json=body)


@router.post("/control/login")
async def control_login() -> dict:
    return await _control("POST", "/auth/login")


@router.get("/control/log")
async def control_log(lines: int = Query(120, ge=10, le=400)) -> dict:
    return await _control("GET", "/log", params={"lines": lines})
