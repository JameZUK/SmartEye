"""SmartEye web server: event ingest, JSON API, images and the web UI."""
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, stats
from .caption import Captioner
from .db import DB
from .engine import Engine, EventError, emb_from, parse_event
from .mqtt import Mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class QuietHealthChecks(logging.Filter):
    """Docker's health check hits /healthz every 30 s; that's half the access log otherwise."""
    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(QuietHealthChecks())
log = logging.getLogger("smarteye")
settings = config.load()
STATIC = Path(__file__).parent / "static"
state: dict = {"started": time.time(), "mqtt": None, "last_event": None}


def image_key(db: DB) -> bytes:
    k = db.get("image_key")
    if not k:
        k = secrets.token_hex(16)
        db.put("image_key", k)
    return k.encode()


def sign(key: int | str) -> str:
    """Signs a sighting id, or an event id prefixed "e:" (so one can't pass for the other)."""
    return hmac.new(state["image_key"], str(key).encode(), hashlib.sha256).hexdigest()[:20]


def image_url(sid: int) -> str | None:
    """A crop URL that works without the UI password, for Home Assistant notifications."""
    if not settings.public_url:
        return None
    return f"{settings.public_url}/img/s/{sid}.jpg?k={sign(sid)}"


def frame_url(eid: str) -> str | None:
    """The same for an event's whole frame."""
    if not settings.public_url:
        return None
    return f"{settings.public_url}/img/e/{eid}.jpg?k={sign('e:' + eid)}"


async def ticker(engine: Engine) -> None:
    last_purge = 0.0
    while True:
        await asyncio.sleep(10)
        try:
            await asyncio.to_thread(engine.close_due)
            if time.time() - last_purge > 86400:
                last_purge = time.time()
                await asyncio.to_thread(engine.purge)
            if state["mqtt"] and int(time.time()) % 300 < 10:
                await asyncio.to_thread(state["mqtt"].refresh_all)
        except Exception:
            log.exception("ticker failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = DB(settings.data_dir / "smarteye.db")
    engine = Engine(settings, db)
    app.state.engine = engine
    state["image_key"] = image_key(db)
    tasks = [asyncio.create_task(ticker(engine))]
    if settings.mqtt_url:
        state["mqtt"] = Mqtt(engine, image_url, frame_url)
        state["mqtt"].start()
    if settings.caption_model:
        Captioner(engine).start()
    yield
    for t in tasks:
        t.cancel()
    if state["mqtt"]:
        state["mqtt"].stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def _client_in(addr: str | None, cidrs: tuple[str, ...]) -> bool:
    try:
        ip = ipaddress.ip_address((addr or "").split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in ipaddress.ip_network(c, strict=False) for c in cidrs)


def who(request: Request) -> tuple[str | None, Response | None]:
    """Access, by SMARTEYE_AUTH_MODE:
      basic     HTTP basic auth against SMARTEYE_AUTH;
      authelia  only requests from a trusted proxy (nginx in front of Authelia) carrying the
                user header it sets after login; anything else is refused, so a client
                talking to this port directly can't claim to be anyone;
      none      open.
    Returns (user, None) or (None, the refusal)."""
    mode = settings.auth_mode
    if mode == "authelia":
        client = request.client.host if request.client else None
        if not _client_in(client, settings.trusted_proxies):
            return None, Response("Use the SmartEye login page.", 403)
        user = (request.headers.get(settings.user_header) or "").strip()
        if not user:
            return None, Response("Not logged in", 401)
        return user, None
    if mode == "basic":
        h = request.headers.get("authorization", "")
        if h.lower().startswith("basic "):
            try:
                creds = base64.b64decode(h[6:]).decode()
                if hmac.compare_digest(creds, settings.auth):
                    return creds.split(":", 1)[0], None
            except Exception:
                pass
        return None, Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="SmartEye"'})
    return None, None


def cross_site(request: Request) -> bool:
    """Browsers send cookies and basic auth with cross-site requests. POST is the only change a
    page on another site can send without a CORS preflight (and this server answers no
    preflight), and then only as a form or text: so a POST must be JSON. Any change must also
    come from this same site, when the browser says where it came from."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return False
    if request.method == "POST" and not request.headers.get("content-type", "").startswith("application/json"):
        return True
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        return origin.split("://", 1)[-1].rstrip("/").lower() != host.lower()
    return False


@app.middleware("http")
async def access(request: Request, call_next):
    path = request.url.path
    # Paths with their own credentials: ImmichEye's token, Home Assistant's doorbell token,
    # signed image links for Home Assistant (the image routes check k), and the health check.
    if path in ("/api/events", "/api/doorbell", "/healthz") or (
            path.startswith(("/img/s/", "/img/e/")) and request.query_params.get("k")):
        return await call_next(request)
    user, refused = who(request)
    if refused:
        return refused
    if path.startswith("/api/") and cross_site(request):
        return Response("Cross-site request refused", 403)
    request.state.user = user
    return await call_next(request)


def E(request: Request) -> Engine:
    return request.app.state.engine


# ---------- ingest

@app.post("/api/events")
async def ingest(request: Request) -> Response:
    if settings.ingest_token:
        h = request.headers.get("authorization", "")
        if not hmac.compare_digest(h, f"Bearer {settings.ingest_token}"):
            return JSONResponse({"error": "bad token"}, 401)
    try:
        ev = parse_event(await request.json())
        n = await asyncio.to_thread(E(request).process, ev)
    except json.JSONDecodeError:
        return JSONResponse({"error": "not JSON"}, 400)
    except EventError as e:
        code = 409 if "model" in str(e) else 400
        return JSONResponse({"error": str(e)}, code)
    state["last_event"] = time.time()
    if n < 0:
        return JSONResponse({"duplicate": True}, 200)
    return JSONResponse({"accepted": n}, 202)


@app.post("/api/doorbell")
async def doorbell(request: Request) -> Response:
    """Home Assistant reports a doorbell press: {"camera": "FDoor", "ts": optional epoch}."""
    if not settings.doorbell_token:
        return JSONResponse({"error": "doorbell not configured (SMARTEYE_DOORBELL_TOKEN)"}, 404)
    if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {settings.doorbell_token}"):
        return JSONResponse({"error": "bad token"}, 401)
    try:
        b = await request.json()
        camera, ts = b["camera"], float(b.get("ts") or time.time())
        if not isinstance(camera, str) or not camera:
            raise ValueError
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
        return JSONResponse({"error": 'expected {"camera": "...", "ts": optional}'}, 400)
    rid, visit = await asyncio.to_thread(E(request).ring, camera, ts)
    return JSONResponse({"ring": rid, "visit": visit["visit"] if visit else None}, 202)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


# ---------- reading

CANDIDATE_MIN = 0.25   # below this a visit-to-identity score is a stranger's; not worth offering
COVER = """(SELECT s.id FROM sightings s JOIN visits v2 ON v2.id = s.visit_id
            WHERE v2.identity_id = i.id AND s.crop IS NOT NULL ORDER BY s.usable DESC, s.quality DESC LIMIT 1)"""


def identity_cards(e: Engine, ids) -> dict[int, sqlite3.Row]:
    """Name, kind and cover of just these identities (COVER is a sorted subquery per row)."""
    ids = list(ids)
    if not ids:
        return {}
    return {r["id"]: r for r in e.db.q(f"SELECT i.id, i.name, i.kind, {COVER} AS cover FROM identities i "
                                       f"WHERE i.id IN ({','.join('?' * len(ids))})", tuple(ids))}


@app.get("/api/summary")
async def summary(request: Request) -> dict:
    e = E(request)
    out = await asyncio.to_thread(stats.summary, e)
    out["recent"] = visits_query(e, limit=12)
    out["open"] = visits_query(e, open_only=True, limit=10)
    return out


@app.get("/api/identities")
async def identities(request: Request, kind: str = "", q: str = "", ignored: str = "0",
                     sort: str = "last", limit: int = 500) -> list[dict]:
    where, args = [], []
    if kind in ("known", "unknown"):
        where.append("i.kind = ?")
        args.append(kind)
    if kind == "named":
        where.append("i.named = 1")
    if q:
        where.append("i.name LIKE ?")
        args.append(f"%{q}%")
    if ignored in ("0", "1"):
        where.append("i.ignored = ?")
        args.append(int(ignored))
    order = {"last": "last_seen DESC", "visits": "visits DESC, last_seen DESC", "first": "first_seen DESC",
             "name": "i.name COLLATE NOCASE"}.get(sort, "last_seen DESC")
    rows = E(request).db.q(f"""
        SELECT i.id, i.kind, i.name, i.named, i.ignored, i.notes,
               COUNT(v.id) AS visits, MIN(v.start_ts) AS first_seen, MAX(v.end_ts) AS last_seen,
               SUM(v.start_ts >= ?) AS visits_30d, {COVER} AS cover
        FROM identities i LEFT JOIN visits v ON v.identity_id = i.id
        {"WHERE " + " AND ".join(where) if where else ""}
        GROUP BY i.id ORDER BY {order} LIMIT ?""", (time.time() - 30 * 86400, *args, limit))
    return [dict(r) for r in rows]


def identity_row(e: Engine, ident: int) -> dict:
    r = e.db.one(f"SELECT i.*, {COVER} AS cover FROM identities i WHERE i.id = ?", (ident,))
    if r is None:
        raise HTTPException(404, "no such identity")
    return dict(r)


@app.get("/api/identities/{ident}")
async def identity(request: Request, ident: int) -> dict:
    e = E(request)
    out = identity_row(e, ident)
    out["labels"] = [r["label"] for r in e.db.q("SELECT label FROM labels WHERE identity_id = ?", (ident,))]
    out["merged"] = [dict(r) for r in e.db.q("SELECT ts, name, visits FROM merges WHERE identity_id = ? ORDER BY ts",
                                             (ident,))]
    out["stats"] = await asyncio.to_thread(stats.identity_stats, e, ident)
    faces = e.db.q("""
        SELECT id, ts, camera, px, usable, quality FROM (
            SELECT s.*, ROW_NUMBER() OVER (PARTITION BY s.visit_id ORDER BY s.quality DESC) AS n
            FROM sightings s JOIN visits v ON v.id = s.visit_id
            WHERE v.identity_id = ? AND s.crop IS NOT NULL
        ) WHERE n <= 3 ORDER BY usable DESC, quality DESC LIMIT 36""", (ident,))
    out["faces"] = [dict(r) for r in faces]
    out["similar"] = await asyncio.to_thread(similar, e, ident)
    return out


def similar(e: Engine, ident: int) -> list[dict]:
    refs, owners = e.refs()
    mine = refs[owners == ident][:10]
    if not len(mine):
        return []
    ranked = [(i, s) for i, s in e.score_identities(mine, exclude={ident})[:5] if s >= e.s.suggest_threshold]
    names = identity_cards(e, (i for i, _ in ranked))
    return [{"id": i, "score": round(s, 3), "name": names[i]["name"], "kind": names[i]["kind"],
             "cover": names[i]["cover"]} for i, s in ranked if i in names and s >= e.s.suggest_threshold]


def visits_query(e: Engine, identity: int | None = None, camera: str = "", kind: str = "", since: float = 0,
                 until: float = 0, before: float = 0, open_only: bool = False, limit: int = 50,
                 vid: int | None = None) -> list[dict]:
    where, args = [], []
    if vid is not None:
        where.append("v.id = ?")
        args.append(vid)
    if identity is not None:
        where.append("v.identity_id = ?")
        args.append(identity)
    if camera:
        where.append("v.camera = ?")
        args.append(camera)
    if kind == "known":
        where.append("i.kind = 'known'")
    elif kind == "unknown":
        where.append("i.kind = 'unknown'")
    elif kind == "unidentified":
        where.append("v.identity_id IS NULL")
    if since:
        where.append("v.start_ts >= ?")
        args.append(since)
    if until:
        where.append("v.start_ts < ?")
        args.append(until)
    if before:
        where.append("v.start_ts < ?")
        args.append(before)
    if open_only:
        where.append("v.closed = 0")
    rows = e.db.q(f"""
        SELECT v.id, v.camera, v.start_ts, v.end_ts, v.closed, v.how, v.match_score, v.identity_id, v.caption,
               i.name, i.kind, i.ignored,
               (SELECT COUNT(*) FROM sightings WHERE visit_id = v.id) AS faces,
               (SELECT COUNT(*) FROM sightings WHERE visit_id = v.id AND folded_from IS NOT NULL) AS folded,
               (SELECT name FROM merges WHERE id = v.merge_id) AS merged_from,
               (SELECT id FROM sightings WHERE visit_id = v.id AND crop IS NOT NULL
                ORDER BY usable DESC, quality DESC LIMIT 1) AS best,
               (SELECT e.id FROM sightings s JOIN events e ON e.id = s.event_id
                WHERE s.visit_id = v.id AND e.frame IS NOT NULL ORDER BY s.quality DESC LIMIT 1) AS frame
        FROM visits v LEFT JOIN identities i ON i.id = v.identity_id
        {"WHERE " + " AND ".join(where) if where else ""}
        ORDER BY v.start_ts DESC LIMIT ?""", (*args, min(limit, 500)))
    return [{**dict(r), "caption": json.loads(r["caption"]) if r["caption"] else None} for r in rows]


@app.get("/api/visits")
async def visits(request: Request, identity: int | None = None, camera: str = "", kind: str = "",
                 since: float = 0, until: float = 0, before: float = 0, limit: int = 50) -> list[dict]:
    return visits_query(E(request), identity, camera, kind, since, until, before, limit=limit)


@app.get("/api/visits/{vid}")
async def visit(request: Request, vid: int) -> dict:
    e = E(request)
    found = visits_query(e, vid=vid, limit=1)
    if not found:
        raise HTTPException(404, "no such visit")
    rows = found[0]
    sightings = e.db.q("""SELECT s.id, s.ts, s.px, s.det, s.quality, s.usable, s.label, s.score, s.top,
                                 s.crop IS NOT NULL AS has_crop, s.embedding, e.frame IS NOT NULL AS has_frame, s.event_id,
                                 s.folded_from
                          FROM sightings s JOIN events e ON e.id = s.event_id WHERE s.visit_id = ? ORDER BY s.ts""", (vid,))
    embs = [emb_from(r["embedding"]) for r in sorted(sightings, key=lambda r: -r["quality"])
            if r["usable"] and r["embedding"]][:5]
    cands = []
    if embs:
        exclude = {rows["identity_id"]} if rows["identity_id"] else set()
        ranked = [(i, s) for i, s in (await asyncio.to_thread(e.score_identities, np.stack(embs), exclude))[:6]
                  if s >= CANDIDATE_MIN]
        names = identity_cards(e, (i for i, _ in ranked))
        cands = [{"id": i, "score": round(s, 3), **{k: names[i][k] for k in ("name", "kind", "cover")}}
                 for i, s in ranked[:6] if i in names and s >= CANDIDATE_MIN]
    rows["sightings"] = [{k: (json.loads(r[k]) if k == "top" and r[k] else r[k]) for k in r.keys() if k != "embedding"}
                         for r in sightings]
    rows["candidates"] = cands
    return rows


@app.get("/api/suggestions")
async def suggestions(request: Request) -> list[dict]:
    e = E(request)
    pairs = await asyncio.to_thread(e.suggestions)
    if not pairs:
        return []
    ids = {p["a"] for p in pairs} | {p["b"] for p in pairs}
    info = {r["id"]: dict(r) for r in e.db.q(f"""
        SELECT i.id, i.name, i.kind, i.named, COUNT(v.id) AS visits, MAX(v.end_ts) AS last_seen, {COVER} AS cover
        FROM identities i LEFT JOIN visits v ON v.identity_id = i.id
        WHERE i.id IN ({",".join("?" * len(ids))}) GROUP BY i.id""", tuple(ids))}
    return [{**p, "a": info[p["a"]], "b": info[p["b"]]} for p in pairs if p["a"] in info and p["b"] in info]


@app.get("/api/cameras")
async def cameras(request: Request) -> list[dict]:
    return [dict(r) for r in E(request).db.q("""
        SELECT c.*, (SELECT COUNT(*) FROM visits WHERE camera = c.name) AS visits
        FROM cameras c ORDER BY enabled DESC, events DESC""")]


@app.get("/api/status")
async def status(request: Request) -> dict:
    e = E(request)
    s = settings
    db_path = s.data_dir / "smarteye.db"
    return {
        "user": getattr(request.state, "user", None),
        "uptime_s": round(time.time() - state["started"]),
        "last_event": state["last_event"],
        "counters": dict(e.counters),
        "totals": {k: e.db.one(f"SELECT COUNT(*) AS n FROM {k}")["n"] for k in ("identities", "visits", "sightings", "events")},
        "db_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "mqtt": {"enabled": bool(s.mqtt_url), "connected": bool(state["mqtt"] and state["mqtt"].connected)},
        "settings": {
            "tz": s.tz, "model": s.model, "min_face_px": s.min_face_px, "min_det": s.min_det,
            "visit_gap_s": s.visit_gap_s, "visit_link": s.visit_link, "match_threshold": s.match_threshold,
            "match_margin": s.match_margin, "suggest_threshold": s.suggest_threshold,
            "retention_days": s.retention_days, "ingest_token_set": bool(s.ingest_token),
            "auth_mode": s.auth_mode, "auth_set": s.auth_mode != "none", "public_url": s.public_url,
            "caption_model": s.caption_model, "caption_url": s.caption_url,
        },
    }


# ---------- edits

async def body(request: Request) -> dict:
    try:
        b = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "not JSON")
    if not isinstance(b, dict):
        raise HTTPException(400, "expected an object")
    return b


def run(fn, *args):
    try:
        return fn(*args)
    except KeyError as e:
        raise HTTPException(404, f"not found: {e}")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/identities/{ident}")
async def patch_identity(request: Request, ident: int) -> dict:
    b = await body(request)
    run(E(request).update_identity, ident, b.get("name"), b.get("notes"),
        bool(b["ignored"]) if "ignored" in b else None)
    return identity_row(E(request), ident)


@app.post("/api/identities/{ident}/merge")
async def merge(request: Request, ident: int) -> dict:
    b = await body(request)
    run(E(request).merge, ident, int(b["into"]))
    return {"merged": ident, "into": int(b["into"])}


@app.delete("/api/identities/{ident}")
async def forget(request: Request, ident: int) -> dict:
    return {"forgotten": ident, "visits": run(E(request).forget, ident)}


@app.post("/api/visits/{vid}/assign")
async def assign(request: Request, vid: int) -> dict:
    b = await body(request)
    target = b.get("identity")
    if target != "new":
        try:
            target = int(target)
        except (TypeError, ValueError):
            raise HTTPException(400, "identity must be an id or 'new'")
    return {"visit": vid, "identity": run(E(request).assign_visit, vid, target)}


@app.delete("/api/visits/{vid}")
async def delete_visit(request: Request, vid: int) -> dict:
    run(E(request).delete_visit, vid)
    return {"deleted": vid}


@app.post("/api/suggestions/dismiss")
async def dismiss(request: Request) -> dict:
    b = await body(request)
    E(request).dismiss(int(b["a"]), int(b["b"]))
    return {"dismissed": [b["a"], b["b"]]}


@app.patch("/api/cameras/{name}")
async def patch_camera(request: Request, name: str) -> dict:
    b = await body(request)
    e = E(request)
    if not e.db.one("SELECT 1 FROM cameras WHERE name = ?", (name,)):
        raise HTTPException(404, "no such camera")
    e.db.x("UPDATE cameras SET enabled = ? WHERE name = ?", (int(bool(b.get("enabled"))), name))
    return {"name": name, "enabled": bool(b.get("enabled"))}


@app.delete("/api/cameras/{name}")
async def delete_camera(request: Request, name: str) -> dict:
    e = E(request)
    if e.db.one("SELECT 1 FROM visits WHERE camera = ?", (name,)):
        raise HTTPException(409, "camera has visits; disable it instead")
    e.db.x("DELETE FROM cameras WHERE name = ?", (name,))
    return {"deleted": name}


# ---------- images

def send_file(rel: str | None) -> Response:
    if not rel:
        raise HTTPException(404)
    p = (settings.data_dir / rel).resolve()
    if not p.is_relative_to(settings.data_dir.resolve()) or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/img/s/{sid}.jpg")
async def sighting_image(request: Request, sid: int, k: str = "") -> Response:
    if k and not hmac.compare_digest(k, sign(sid)):
        raise HTTPException(403)
    r = E(request).db.one("SELECT crop FROM sightings WHERE id = ?", (sid,))
    return send_file(r["crop"] if r else None)


@app.get("/img/e/{eid}.jpg")
async def frame_image(request: Request, eid: str, k: str = "") -> Response:
    if k and not hmac.compare_digest(k, sign("e:" + eid)):
        raise HTTPException(403)
    r = E(request).db.one("SELECT frame FROM events WHERE id = ?", (eid,))
    return send_file(r["frame"] if r else None)


# ---------- web UI

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index() -> Response:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
