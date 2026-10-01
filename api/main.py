"""Vyasa POC - API and join page host (Section 6.7).

Endpoints
  GET  /                              join page
  POST /token                         choose room, check capacity, return LiveKit token, dispatch vyasa-poc
  GET  /records/{learner_id}          record JSON (needs X-Drona-Secret)
  PUT  /records/{learner_id}          save record JSON (needs X-Drona-Secret)
  GET  /records/{learner_id}/summary.md   Cowork summary (Markdown)
  GET  /health                        which configuration values are missing
"""
import hmac
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from livekit import api
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

REQUIRED = ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "DRONA_API_SECRET"]
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

app = FastAPI(title="Vyasa POC API")


# ---------- config helpers ----------
def cfg(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def record_dir() -> Path:
    p = Path(cfg("DRONA_RECORD_PATH", "./records"))
    if not p.is_absolute():
        p = BASE_DIR / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def http_url(url: str) -> str:
    """LiveKit server API wants http(s); the browser wants ws(s)."""
    if url.startswith("wss://"):
        return "https://" + url[len("wss://"):]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://"):]
    return url


def check_id(value: str, label: str) -> str:
    if not value or not SAFE_ID.match(value):
        raise HTTPException(400, f"{label} must be 1-40 letters, digits, '-' or '_'")
    return value


def require_secret(provided: Optional[str]) -> None:
    expected = cfg("DRONA_API_SECRET")
    if not expected:
        raise HTTPException(503, "DRONA_API_SECRET is not configured")
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(401, "Missing or wrong X-Drona-Secret")


def record_path(learner_id: str) -> Path:
    return record_dir() / f"{check_id(learner_id, 'learner_id')}.json"


def livekit_api() -> api.LiveKitAPI:
    return api.LiveKitAPI(http_url(cfg("LIVEKIT_URL")), cfg("LIVEKIT_API_KEY"), cfg("LIVEKIT_API_SECRET"))


# ---------- room logic (Section 6.9) ----------
def room_name(mode: str, learner_id: str, lesson_id: str, slot_id: Optional[str]) -> str:
    if mode == "1to1":
        return f"drona-1to1-{learner_id}-{lesson_id}"
    return f"drona-group-{lesson_id}-{slot_id}"


async def room_status(room: str) -> dict:
    """Who is in the room right now. Learners are STANDARD participants; Vyasa
    and the LiveAvatar face are both AGENT participants."""
    async with livekit_api() as lk:
        try:
            resp = await lk.room.list_participants(api.ListParticipantsRequest(room=room))
        except api.TwirpError as e:
            if e.code == "not_found":
                return {"exists": False, "learners": set(), "has_agent": False}
            raise
    learners = {p.identity for p in resp.participants if p.kind == api.ParticipantInfo.Kind.STANDARD}
    has_agent = any(p.kind == api.ParticipantInfo.Kind.AGENT for p in resp.participants)
    return {"exists": True, "learners": learners, "has_agent": has_agent}


async def has_dispatch(room: str) -> bool:
    """True if Vyasa has already been dispatched to this room (even if it hasn't joined yet)."""
    name = cfg("DRONA_AGENT_NAME", "vyasa-poc")
    async with livekit_api() as lk:
        dispatches = await lk.agent_dispatch.list_dispatch(room_name=room)
    return any(d.agent_name == name for d in dispatches)


async def dispatch_agent(room: str, metadata: str) -> None:
    """Explicitly send Vyasa into a room that already exists. The dispatch in
    the token only fires when the token's holder creates the room."""
    async with livekit_api() as lk:
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=cfg("DRONA_AGENT_NAME", "vyasa-poc"), room=room, metadata=metadata
            )
        )


# ---------- routes ----------
class TokenRequest(BaseModel):
    learner_id: str
    lesson_id: str
    mode: str
    slot_id: Optional[str] = None


@app.post("/token")
async def token(req: TokenRequest):
    missing = [k for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET") if not cfg(k)]
    if missing:
        raise HTTPException(503, f"Server not configured: {', '.join(missing)}")

    learner_id = check_id(req.learner_id, "learner_id")
    lesson_id = check_id(req.lesson_id, "lesson_id")
    if req.mode not in ("1to1", "group"):
        raise HTTPException(400, "mode must be '1to1' or 'group'")
    slot_id = None
    if req.mode == "group":
        slot_id = check_id(req.slot_id or "", "slot_id")

    room = room_name(req.mode, learner_id, lesson_id, slot_id)
    metadata = json.dumps(
        {"learner_id": learner_id, "lesson_id": lesson_id, "mode": req.mode, "slot_id": slot_id}
    )

    if req.mode == "group":
        capacity = int(cfg("DRONA_GROUP_CAPACITY", "15") or "15")
        try:
            status = await room_status(room)
        except Exception as e:  # LiveKit unreachable or keys wrong
            raise HTTPException(502, f"Could not check the class: {type(e).__name__}")
        if learner_id in status["learners"]:
            raise HTTPException(409, f"{learner_id} is already in this class (another tab or browser?)")
        if len(status["learners"]) >= capacity:
            raise HTTPException(409, f"This group session is full ({capacity} learners)")
        # The class is running but Vyasa is missing. Vyasa takes a few seconds to join a new
        # room, so "no agent yet" is normal just after the first learner arrives. Only send
        # Vyasa in if the room has no dispatch at all; otherwise two Vyasas would teach at once.
        if status["exists"] and status["learners"] and not status["has_agent"]:
            try:
                if not await has_dispatch(room):
                    await dispatch_agent(room, metadata)
            except Exception as e:
                raise HTTPException(502, f"Could not bring Vyasa into the class: {type(e).__name__}")

    jwt = (
        api.AccessToken(cfg("LIVEKIT_API_KEY"), cfg("LIVEKIT_API_SECRET"))
        .with_identity(learner_id)
        .with_name(learner_id)
        .with_grants(api.VideoGrants(room_join=True, room=room))
        .with_room_config(
            api.RoomConfiguration(
                agents=[
                    api.RoomAgentDispatch(
                        agent_name=cfg("DRONA_AGENT_NAME", "vyasa-poc"), metadata=metadata
                    )
                ]
            )
        )
        .to_jwt()
    )
    return {"url": cfg("LIVEKIT_URL"), "token": jwt, "room": room}


@app.get("/records/{learner_id}")
async def get_record(learner_id: str, x_drona_secret: Optional[str] = Header(None)):
    require_secret(x_drona_secret)
    path = record_path(learner_id)
    if not path.exists():
        raise HTTPException(404, "No record for this learner")
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@app.put("/records/{learner_id}")
async def put_record(learner_id: str, request: Request, x_drona_secret: Optional[str] = Header(None)):
    require_secret(x_drona_secret)
    path = record_path(learner_id)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be a JSON object")
    body["learner_id"] = learner_id
    body.setdefault("lessons", [])
    # write atomically so a crash never leaves half a file
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(body, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return {"saved": learner_id}


def _bullets(items) -> str:
    items = items or []
    return "\n".join(f"- {i}" for i in items) if items else "- (none)"


def _cold_calls(calls) -> str:
    if not calls:
        return "- (not called on)"
    out = []
    for c in calls:
        if c.get("result") == "answered":
            out.append(f"- At {c.get('at', '?')}: answered \"{c.get('answer', '')}\"")
        elif c.get("result") == "dont_know":
            out.append(f"- At {c.get('at', '?')}: didn't know (\"{c.get('answer', '')}\")")
        else:
            out.append(f"- At {c.get('at', '?')}: no response")
    return "\n".join(out)


@app.get("/records/{learner_id}/summary.md", response_class=PlainTextResponse)
async def summary(learner_id: str):
    path = record_path(learner_id)
    if not path.exists():
        raise HTTPException(404, "No record for this learner")
    rec = json.loads(path.read_text(encoding="utf-8"))
    lines = [f"# Lesson summary for learner {rec.get('learner_id', learner_id)}", ""]
    for les in rec.get("lessons", []):
        att = les.get("attentiveness") or {}
        slot = f", slot {les['slot_id']}" if les.get("slot_id") else ""
        lines += [
            f"## Lesson {les.get('lesson_id', '?')} ({les.get('mode', '?')}{slot}, "
            f"{les.get('date', 'date unknown')}, {les.get('minutes', '?')} minutes)",
            "",
            "**What was taught**",
            _bullets(les.get("covered")),
            "",
            "**What was misunderstood**",
            _bullets(les.get("misconceptions")),
            "",
            "**Questions the learner asked**",
            _bullets(les.get("questions_asked")),
            "",
            "**Called on by Vyasa**",
            _cold_calls(les.get("cold_calls")),
            "",
            f"Attentiveness: called on {att.get('called', 0)} times; answered {att.get('answered', 0)}, "
            f"didn't know {att.get('dont_know', 0)}, no response {att.get('no_response', 0)}.",
            "",
            "**Left unanswered (follow up in Cowork)**",
            _bullets(les.get("unanswered")),
            "",
        ]
    if not rec.get("lessons"):
        lines.append("No lessons recorded yet.")
    return PlainTextResponse("\n".join(lines), media_type="text/markdown")


@app.get("/health")
async def health():
    missing = [k for k in REQUIRED if not cfg(k)]
    return {"ok": not missing, "missing": missing}


@app.get("/", include_in_schema=False)
async def index():
    page = BASE_DIR / "static" / "index.html"
    if page.exists() and page.stat().st_size > 0:
        return FileResponse(page)
    return HTMLResponse("<h1>Vyasa</h1><p>The join page has not been built yet.</p>")