"""Faces in, visits and identities out.

A face event (one frame from one camera, see ImmichEye docs/face-events.md) becomes
sightings. Each sighting joins an open visit on that camera, the same person's arrival, or
starts one. A visit closes once its camera has seen no face of that person for
SMARTEYE_VISIT_GAP_S, and is then given an identity:

- named:   ImmichEye recognised someone in it (the label with the most total score wins);
- matched: its best faces match an existing identity's best faces (top-k mean cosine,
           with a threshold and a margin over the runner-up, as ImmichEye does);
- new:     otherwise a new unknown visitor, "Visitor 12";
- none:    it has no face good enough to identify (small, blurred, tops of heads).

An unrecognised visit of only a face or two that overlaps another visit at its door, or
comes within SMARTEYE_FOLD_S of one, is a stray frame of that visit (someone looking down
as they leave): it's folded into it instead of becoming a visitor, and listeners hear
"folded". Not if the two share a frame: two faces in one frame are two people.

Only "usable" faces (big enough and confidently detected) create or match identities.
Weaker ones still count towards the visit they appear in.

While a visit is open its identity is a guess, re-made as better faces arrive. Listeners
hear "arrival" when it starts, "identified" when the guess firms up or changes, and "visit"
when it closes (with changed_from if that differs from what was last announced).
A doorbell ring during a visit is "doorbell". With SMARTEYE_CAPTION_MODEL set, a vision
model's description of the visit follows as "caption" (caption.py).
"""
import base64
import json
import logging
import math
import re
import threading
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)
DIM = 512
PER_VISIT_REFS = 5   # at most this many reference faces from any one visit, so one long visit can't dominate
# Event ids become file names (frames/<date>/<id>.jpg) and URL path segments: ImmichEye sends
# UUIDs. Nothing that could leave the folder.
EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class EventError(ValueError):
    pass


@dataclass
class FaceIn:
    box: tuple[int, int, int, int]
    det: float
    embedding: np.ndarray | None
    label: str | None                 # ImmichEye's name for the face; None if unknown
    score: float | None = None
    top: list = field(default_factory=list)
    crop: bytes | None = None

    @property
    def px(self) -> int:
        return max(0, self.box[2] - self.box[0])


@dataclass
class EventIn:
    id: str
    ts: float
    camera: str | None
    faces: list[FaceIn]
    frame: bytes | None = None
    model: str | None = None


def decode_embedding(s: str) -> np.ndarray:
    v = np.frombuffer(base64.b64decode(s), dtype="<f4").astype(np.float32)
    if v.shape != (DIM,):
        raise EventError(f"embedding has {v.size} values, expected {DIM}")
    n = float(np.linalg.norm(v))
    if not np.isfinite(n) or n == 0:
        raise EventError("embedding is zero or not finite")
    return v / n


def parse_event(body: dict) -> EventIn:
    """An ImmichEye event (v1) as an EventIn. Raises EventError on anything malformed."""
    if not isinstance(body, dict) or body.get("v") != 1:
        raise EventError("expected an object with v=1")
    try:
        faces = []
        for f in body.get("faces") or []:
            if not isinstance(f, dict):
                raise EventError("each face must be an object")
            box = tuple(int(v) for v in f["box"])
            if len(box) != 4:
                raise EventError("a face box needs 4 numbers")
            result = f.get("result")
            faces.append(FaceIn(
                box=box,
                det=float(f.get("det", 0)),
                embedding=decode_embedding(f["embedding"]) if f.get("embedding") else None,
                label=str(result) if result and result != "unknown" else None,
                score=float(f["score"]) if f.get("score") is not None else None,
                top=f.get("top") or [],
                crop=base64.b64decode(f["crop"]) if f.get("crop") else None,
            ))
        eid = str(body.get("id") or uuid.uuid4())
        if not EVENT_ID.fullmatch(eid) or ".." in eid:
            raise EventError("id may hold only letters, digits, '_', '-' and '.'")
        ts = float(body["ts"])
        if not math.isfinite(ts) or ts <= 0:
            raise EventError("ts must be a positive number of seconds")
        camera = body.get("camera") or None
        if camera is not None and not isinstance(camera, str):
            raise EventError("camera must be a string")
        return EventIn(
            id=eid,
            ts=ts,
            camera=camera,
            faces=faces,
            frame=base64.b64decode(body["frame"]) if body.get("frame") else None,
            model=body.get("model"),
        )
    except EventError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as e:
        raise EventError(f"bad event: {e}") from e


def norm_label(label: str) -> str:
    """'Alex Carter', 'alex_carter' and 'Alex_Carter' are one person under ImmichEye's label styles."""
    return re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()


def display_name(label: str) -> str:
    """'cal_smith' -> 'Cal Smith'; labels with capitals already ('Cal_Smith', 'Dan Lee DJ') keep them."""
    words = label.replace("_", " ").split()
    return " ".join(w[:1].upper() + w[1:] if label.islower() else w for w in words)


def emb_bytes(v: np.ndarray) -> bytes:
    return v.astype("<f4").tobytes()


def emb_from(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype="<f4")


class Engine:
    def __init__(self, settings: Settings, db: DB):
        self.s = settings
        self.db = db
        self.tz = ZoneInfo(settings.tz)
        self.listeners: list[Callable[[str, dict], None]] = []
        self.counters = Counter()
        self._refs: tuple[np.ndarray, np.ndarray] | None = None   # (embeddings, owner identity ids)
        self._refs_gen = 0                  # bumped by invalidate(): a slower, older query never overwrites a newer one
        self._refs_lock = threading.Lock()
        (settings.data_dir / "crops").mkdir(parents=True, exist_ok=True)
        (settings.data_dir / "frames").mkdir(parents=True, exist_ok=True)

    # ---------- helpers

    def usable(self, f: FaceIn) -> bool:
        return f.embedding is not None and f.px >= self.s.min_face_px and f.det >= self.s.min_det

    def quality(self, f: FaceIn) -> float:
        return round(min(f.px, 160) / 160 * f.det, 4)

    def _emit(self, kind: str, payload: dict) -> None:
        for fn in self.listeners:
            try:
                fn(kind, payload)
            except Exception:
                log.exception("listener failed for %s", kind)

    def _inside(self, rel: str) -> Path:
        """data_dir / rel, refusing anything that resolves outside data_dir."""
        root = self.s.data_dir.resolve()
        p = (root / rel).resolve()
        if not p.is_relative_to(root):
            raise EventError(f"path {rel!r} is outside the data folder")
        return p

    def _save(self, sub: str, ts: float, name: str, data: bytes) -> str:
        rel = f"{sub}/{time.strftime('%Y%m%d', time.gmtime(ts))}/{name}.jpg"
        p = self._inside(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return rel

    def _unlink(self, rel: str | None) -> None:
        if rel:
            try:
                self._inside(rel).unlink(missing_ok=True)
            except (OSError, EventError):
                log.warning("could not delete %s", rel)

    def invalidate(self) -> None:
        with self._refs_lock:
            self._refs = None
            self._refs_gen += 1

    def local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, self.tz)

    # ---------- identities

    def identity_for_label(self, label: str) -> int:
        key = norm_label(label)
        r = self.db.one("SELECT identity_id FROM labels WHERE label = ?", (key,))
        if r:
            return r["identity_id"]
        cur = self.db.x("INSERT INTO identities (kind, name, named, created_at) VALUES ('known', ?, 1, ?)",
                        (display_name(label), time.time()))
        self.db.x("INSERT INTO labels (label, identity_id) VALUES (?, ?)", (key, cur.lastrowid))
        return cur.lastrowid

    def new_visitor(self) -> int:
        n = int(self.db.get("visitor_seq", "0")) + 1
        self.db.put("visitor_seq", str(n))
        return self.db.x("INSERT INTO identities (kind, name, created_at) VALUES ('unknown', ?, ?)",
                         (f"Visitor {n}", time.time())).lastrowid

    def refs(self) -> tuple[np.ndarray, np.ndarray]:
        """Every identity's best usable faces, at most PER_VISIT_REFS from one visit."""
        with self._refs_lock:
            refs, gen = self._refs, self._refs_gen
        if refs is None:
            rows = self.db.q("""
                SELECT identity_id, embedding FROM (
                    SELECT identity_id, embedding,
                           ROW_NUMBER() OVER (PARTITION BY identity_id ORDER BY quality DESC) AS n
                    FROM (
                        SELECT v.identity_id, s.embedding, s.quality,
                               ROW_NUMBER() OVER (PARTITION BY s.visit_id ORDER BY s.quality DESC) AS vn
                        FROM sightings s JOIN visits v ON v.id = s.visit_id
                        WHERE v.identity_id IS NOT NULL AND s.usable = 1 AND s.embedding IS NOT NULL
                    ) WHERE vn <= ?
                ) WHERE n <= ? ORDER BY identity_id""", (PER_VISIT_REFS, self.s.ref_faces))
            if rows:
                refs = (np.stack([emb_from(r["embedding"]) for r in rows]), np.array([r["identity_id"] for r in rows]))
            else:
                refs = (np.zeros((0, DIM), np.float32), np.zeros(0, int))
            with self._refs_lock:
                if self._refs_gen == gen:   # nothing changed while querying: safe to keep
                    self._refs = refs
        return refs

    def score_identities(self, embs: np.ndarray, exclude: set[int] = frozenset()) -> list[tuple[int, float]]:
        """(identity, score) best first. An identity's score for one face is the mean of its
        top-k similarities; over several faces, the best face counts."""
        refs, owners = self.refs()
        if not len(owners) or not len(embs):
            return []
        sims = embs @ refs.T
        out = []
        for ident in np.unique(owners):
            if int(ident) in exclude:
                continue
            cols = sims[:, owners == ident]
            k = min(self.s.top_k, cols.shape[1])
            topk = -np.sort(-cols, axis=1)[:, :k].mean(axis=1)
            out.append((int(ident), float(topk.max())))
        out.sort(key=lambda t: t[1], reverse=True)
        return out

    # ---------- ingest

    def process(self, ev: EventIn) -> int:
        """Store an event's faces. Returns how many were stored."""
        if ev.model and ev.model != self.s.model:
            raise EventError(f"model {ev.model!r} doesn't match SMARTEYE_MODEL {self.s.model!r}")
        arrivals = []
        touched = set()    # existing visits a name or a usable face joined: their guess may change
        closed = []
        with self.db.tx():
            if self.db.one("SELECT 1 FROM events WHERE id = ?", (ev.id,)):
                self.counters["duplicates"] += 1
                return -1
            if not ev.camera:
                self.counters["no_camera"] += 1
                return 0
            cam = self.db.one("SELECT * FROM cameras WHERE name = ?", (ev.camera,))
            if cam is None:
                enabled = ev.camera in self.s.cameras
                self.db.x("INSERT INTO cameras (name, enabled, first_seen, last_seen, events) VALUES (?, ?, ?, ?, 1)",
                          (ev.camera, int(enabled), ev.ts, ev.ts))
            else:
                enabled = bool(cam["enabled"])
                self.db.x("UPDATE cameras SET last_seen = MAX(last_seen, ?), events = events + 1 WHERE name = ?",
                          (ev.ts, ev.camera))
            if not enabled:
                self.counters["camera_disabled"] += 1
                return 0
            if not ev.faces:
                return 0

            # Visits on this camera that this frame proves are over
            for v in self.db.q("SELECT id FROM visits WHERE camera = ? AND closed = 0 AND ? - end_ts > ?",
                               (ev.camera, ev.ts, self.s.visit_gap_s)):
                closed.append(self._finalize(v["id"]))

            frame = self._save("frames", ev.ts, ev.id, ev.frame) if ev.frame else None
            self.db.x("INSERT INTO events (id, ts, camera, frame) VALUES (?, ?, ?, ?)", (ev.id, ev.ts, ev.camera, frame))

            used: set[int] = set()
            for f in sorted(ev.faces, key=self.quality, reverse=True):
                vid, new = self._visit_for(ev.camera, ev.ts, f, used)
                used.add(vid)
                cur = self.db.x("""INSERT INTO sightings
                    (event_id, visit_id, ts, camera, x1, y1, x2, y2, px, det, quality, usable, label, score, top, embedding)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ev.id, vid, ev.ts, ev.camera, *f.box, f.px, f.det, self.quality(f), int(self.usable(f)),
                     f.label, f.score, json.dumps(f.top), emb_bytes(f.embedding) if f.embedding is not None else None))
                if f.crop:
                    self.db.x("UPDATE sightings SET crop = ? WHERE id = ?",
                              (self._save("crops", ev.ts, str(cur.lastrowid), f.crop), cur.lastrowid))
                if new:
                    arrivals.append(vid)
                elif f.label or self.usable(f):
                    touched.add(vid)
                self.counters["faces"] += 1
            for vid in arrivals:
                self._claim_rings(vid)
            self.counters["events"] += 1
        for c in closed:
            self._closed(c)
        for vid in arrivals:
            self._emit("arrival", self.arrival(vid))
        for vid in touched:
            c = self.recheck(vid)
            if c:
                self._emit("identified", c)
        return len(ev.faces)

    def _visit_for(self, camera: str, ts: float, f: FaceIn, used: set[int]) -> tuple[int, bool]:
        """The open visit this face belongs to, or a new one. Returns (visit id, created)."""
        gap = self.s.visit_gap_s
        cands = [v for v in self.db.q(
            "SELECT * FROM visits WHERE camera = ? AND closed = 0 AND ? - end_ts <= ? AND start_ts - ? <= ? "
            "ORDER BY touched_at DESC", (camera, ts, gap, ts, gap)) if v["id"] not in used]
        mine = self.identity_for_label(f.label) if f.label else None
        # Never join a visit already given to a different known person
        cands = [v for v in cands if v["identity_id"] is None or mine is None or v["identity_id"] == mine]

        chosen = None
        if mine is not None:
            chosen = next((v for v in cands if v["identity_id"] == mine), None)
        if chosen is None and f.embedding is not None and cands:
            best, best_sim = None, -1.0
            for v in cands:
                rows = self.db.q("SELECT embedding FROM sightings WHERE visit_id = ? AND embedding IS NOT NULL "
                                 "ORDER BY quality DESC LIMIT 10", (v["id"],))
                if rows:
                    sim = float(np.max(np.stack([emb_from(r["embedding"]) for r in rows]) @ f.embedding))
                    if sim > best_sim:
                        best, best_sim = v, sim
            if best is not None and best_sim >= self.s.visit_link:
                chosen = best
        if chosen is None and not self.usable(f) and cands:
            chosen = cands[0]   # too poor to compare: the face most likely belongs to whoever is already there

        now = time.time()
        if chosen is None:
            vid = self.db.x("INSERT INTO visits (camera, identity_id, start_ts, end_ts, touched_at, how) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (camera, mine, ts, ts, now, "named" if mine else None)).lastrowid
            return vid, True
        self.db.x("UPDATE visits SET start_ts = MIN(start_ts, ?), end_ts = MAX(end_ts, ?), touched_at = ? WHERE id = ?",
                  (ts, ts, now, chosen["id"]))
        if mine is not None and chosen["identity_id"] is None:
            self.db.x("UPDATE visits SET identity_id = ?, how = 'named' WHERE id = ?", (mine, chosen["id"]))
        return chosen["id"], False

    def close_due(self, now: float | None = None) -> int:
        """Close visits that haven't had a face for the visit gap (wall clock)."""
        now = now or time.time()
        done = []
        with self.db.tx():
            for v in self.db.q("SELECT id FROM visits WHERE closed = 0 AND ? - touched_at > ?", (now, self.s.visit_gap_s)):
                done.append(self._finalize(v["id"]))
        for c in done:
            self._closed(c)
        return len(done)

    def _closed(self, c: dict | None) -> None:
        if c:
            self._emit("folded" if "folded_into" in c else "visit", c)

    def close_all(self) -> int:
        return self.close_due(now=1e18)

    def _finalize(self, vid: int) -> dict | None:
        v = self.db.one("SELECT * FROM visits WHERE id = ?", (vid,))
        if v is None or v["closed"]:
            return None
        rows = self.db.q("SELECT label, score, usable, quality, embedding FROM sightings WHERE visit_id = ?", (vid,))
        named = defaultdict(float)
        for r in rows:
            if r["label"]:
                named[r["label"]] += r["score"] or 0.5
        ident, how, score = None, "none", None
        if named:
            label = max(named, key=named.get)
            ident, how = self.identity_for_label(label), "named"
            score = max((r["score"] or 0) for r in rows if r["label"] == label)
        else:
            good = sorted((r for r in rows if r["usable"] and r["embedding"]), key=lambda r: r["quality"], reverse=True)[:5]
            if good:
                ranked = self.score_identities(np.stack([emb_from(r["embedding"]) for r in good]))
                runner = ranked[1][1] if len(ranked) > 1 else -1.0
                if ranked and ranked[0][1] >= self.s.match_threshold and ranked[0][1] - runner >= self.s.match_margin:
                    ident, how, score = ranked[0][0], "matched", ranked[0][1]
            if ident is None:
                into = self._fold_target(v, len(rows))
                if into is not None:
                    self._fold(v, into)
                    return {"visit": vid, "camera": v["camera"], "folded_into": into}
                if good:
                    ident, how, score = self.new_visitor(), "new", (ranked[0][1] if ranked else None)
        self.db.x("UPDATE visits SET identity_id = ?, how = ?, match_score = ?, closed = 1 WHERE id = ?",
                  (ident, how, score, vid))
        self.invalidate()
        out = self.visit_summary(vid, with_context=True)
        was = v["announced_id"]
        r = self.db.one("SELECT name FROM identities WHERE id = ?", (was,)) if was is not None and was != ident else None
        if r:   # an announced identity since forgotten isn't a change of mind
            out["changed_from"] = {"identity": was, "name": r["name"]}
        return out

    def _fold_target(self, v, faces: int) -> int | None:
        """The visit an unrecognised visit of a face or two is a stray frame of, or None: the
        one at the same door it overlaps or nearly touches with the most faces, never one it
        shares a frame with."""
        if faces > self.s.fold_faces:
            return None
        r = self.db.one("""SELECT o.id FROM visits o WHERE o.camera = ? AND o.id != ?
                AND o.start_ts - ? <= ? AND o.end_ts + ? >= ?
                AND NOT EXISTS (SELECT 1 FROM sightings a JOIN sightings b ON a.event_id = b.event_id
                                WHERE a.visit_id = ? AND b.visit_id = o.id)
            ORDER BY (SELECT COUNT(*) FROM sightings WHERE visit_id = o.id) DESC, o.start_ts LIMIT 1""",
            (v["camera"], v["id"], self.s.fold_s, v["end_ts"], self.s.fold_s, v["start_ts"], v["id"]))
        return r["id"] if r else None

    def _fold(self, v, into: int) -> None:
        """Visit v becomes part of visit into: its faces and rings move there, and it's gone."""
        self.db.x("UPDATE sightings SET visit_id = ?, folded_from = COALESCE(folded_from, ?) WHERE visit_id = ?",
                  (into, v["id"], v["id"]))
        self.db.x("UPDATE rings SET visit_id = ? WHERE visit_id = ?", (into, v["id"]))
        self.db.x("UPDATE visits SET start_ts = MIN(start_ts, ?), end_ts = MAX(end_ts, ?) WHERE id = ?",
                  (v["start_ts"], v["end_ts"], into))
        self.db.x("DELETE FROM visits WHERE id = ?", (v["id"],))
        self.invalidate()

    # ---------- doorbell

    def ring(self, camera: str, ts: float) -> tuple[int, dict | None]:
        """A doorbell press. It joins the camera's open visit, if any: returns (ring id, that
        visit as it stands). Otherwise it waits for the next visit there (_claim_rings)."""
        with self.db.tx():
            v = self.db.one("SELECT id FROM visits WHERE camera = ? AND closed = 0 AND ? - end_ts <= ? "
                            "ORDER BY touched_at DESC LIMIT 1", (camera, ts, self.s.visit_gap_s))
            rid = self.db.x("INSERT INTO rings (camera, ts, visit_id) VALUES (?, ?, ?)",
                            (camera, ts, v["id"] if v else None)).lastrowid
        if v is None:
            return rid, None
        out = self.current(v["id"])
        self._emit("doorbell", out)
        return rid, out

    def _claim_rings(self, vid: int) -> None:
        """A new visit takes the rings on its camera in the window before it started: the bell
        is often pressed before the camera sees a face."""
        v = self.db.one("SELECT camera, start_ts FROM visits WHERE id = ?", (vid,))
        self.db.x("UPDATE rings SET visit_id = ? WHERE camera = ? AND visit_id IS NULL AND ts BETWEEN ? AND ?",
                  (vid, v["camera"], v["start_ts"] - self.s.ring_window_s, v["start_ts"]))

    # ---------- reading

    def visit_summary(self, vid: int, with_context: bool = False) -> dict:
        v = self.db.one("""SELECT v.*, i.name, i.kind, i.ignored, i.named,
                (SELECT COUNT(*) FROM sightings WHERE visit_id = v.id) AS faces,
                (SELECT id FROM sightings WHERE visit_id = v.id ORDER BY usable DESC, quality DESC LIMIT 1) AS best,
                (SELECT e.id FROM sightings s JOIN events e ON e.id = s.event_id
                 WHERE s.visit_id = v.id AND e.frame IS NOT NULL ORDER BY s.quality DESC LIMIT 1) AS frame,
                EXISTS (SELECT 1 FROM rings WHERE visit_id = v.id) AS rang
            FROM visits v LEFT JOIN identities i ON i.id = v.identity_id WHERE v.id = ?""", (vid,))
        out = {
            "visit": v["id"], "camera": v["camera"], "start": v["start_ts"], "end": v["end_ts"],
            "faces": v["faces"], "best_sighting": v["best"], "frame_event": v["frame"], "rang": bool(v["rang"]),
            "closed": bool(v["closed"]), "how": v["how"], "match_score": v["match_score"],
            "announced": v["announced_id"],
            "identity": v["identity_id"], "name": v["name"], "kind": v["kind"], "named": bool(v["named"]),
            "ignored": bool(v["ignored"]) if v["ignored"] is not None else False,
            "caption": json.loads(v["caption"]) if v["caption"] else None,
        }
        if with_context and v["identity_id"] is not None:
            out.update(self._context(v["identity_id"], vid, v["start_ts"]), new_visitor=v["how"] == "new")
        return out

    def _context(self, ident: int, vid: int, start: float) -> dict:
        prior = [r["start_ts"] for r in self.db.q(
            "SELECT start_ts FROM visits WHERE identity_id = ? AND id != ? AND closed = 1 AND start_ts < ?",
            (ident, vid, start))]
        return {"visit_number": len(prior) + 1, "new_visitor": False, "unusual_time": self.unusual(start, prior)}

    def _guess(self, vid: int) -> tuple[int, float] | None:
        """The identity an open visit's usable faces so far match, as at close: (id, score)."""
        rows = self.db.q("SELECT embedding FROM sightings WHERE visit_id = ? AND usable = 1 AND embedding IS NOT NULL "
                         "ORDER BY quality DESC LIMIT 5", (vid,))
        if not rows:
            return None
        ranked = self.score_identities(np.stack([emb_from(r["embedding"]) for r in rows]))
        runner = ranked[1][1] if len(ranked) > 1 else -1.0
        if ranked and ranked[0][1] >= self.s.match_threshold and ranked[0][1] - runner >= self.s.match_margin:
            return ranked[0]
        return None

    def current(self, vid: int) -> dict:
        """An open visit as it stands. A visit only gets its identity when it closes, so an
        unnamed one is matched now on its faces so far: "provisional" marks that guess."""
        out = self.visit_summary(vid, with_context=True)
        if out["identity"] is not None:
            return out
        g = self._guess(vid)
        if g:
            i = self.db.one("SELECT * FROM identities WHERE id = ?", (g[0],))
            out.update(identity=i["id"], name=i["name"], kind=i["kind"], named=bool(i["named"]),
                       ignored=bool(i["ignored"]), provisional=True, match_score=g[1],
                       **self._context(i["id"], vid, out["start"]))
        return out

    def now(self, vid: int) -> dict:
        """A visit as it stands: as it closed, or with the current guess while open."""
        out = self.visit_summary(vid, with_context=True)
        return out if out["closed"] else self.current(vid)

    def _announce(self, out: dict) -> dict:
        self.db.x("UPDATE visits SET announced_id = ? WHERE id = ?", (out["identity"], out["visit"]))
        out["announced"] = out["identity"]
        return out

    def arrival(self, vid: int) -> dict:
        """A new visit as it starts, with the best guess at who it is."""
        return self._announce(self.current(vid))

    def recheck(self, vid: int) -> dict | None:
        """An open visit whose identity has firmed up or changed since it was last announced
        (more faces, or an ImmichEye name mid-visit), or None. Stored, so never repeated."""
        out = self.current(vid)
        if out["closed"] or out["identity"] is None or out["identity"] == out["announced"]:
            return None
        return self._announce(out)

    def unusual(self, ts: float, prior: list[float], window_min: int = 60) -> bool:
        """At least five earlier visits, none within an hour of this time of day."""
        if len(prior) < 5:
            return False
        t = self.local(ts)
        m = t.hour * 60 + t.minute
        for p in prior:
            lp = self.local(p)
            d = abs(lp.hour * 60 + lp.minute - m)
            if min(d, 1440 - d) <= window_min:
                return False
        return True

    # ---------- edits

    def assign_visit(self, vid: int, target: int | str) -> int:
        """Move a visit to an identity, or to a new visitor ('new'). Returns the identity."""
        with self.db.tx():
            v = self.db.one("SELECT * FROM visits WHERE id = ?", (vid,))
            if v is None:
                raise KeyError(vid)
            if target == "new":
                ident = self.new_visitor()
            else:
                ident = int(target)
                if not self.db.one("SELECT 1 FROM identities WHERE id = ?", (ident,)):
                    raise KeyError(ident)
            self.db.x("UPDATE visits SET identity_id = ?, how = 'manual', match_score = NULL, closed = 1 WHERE id = ?",
                      (ident, vid))
            if v["identity_id"] is not None:
                self._drop_if_empty(v["identity_id"])
        self.invalidate()
        return ident

    def merge(self, src: int, dst: int) -> None:
        """Merge identity src into dst: dst gets src's visits and labels, and a record of it
        (with src's own merge history)."""
        if src == dst:
            raise ValueError("can't merge an identity into itself")
        with self.db.tx():
            a = self.db.one("SELECT * FROM identities WHERE id = ?", (src,))
            b = self.db.one("SELECT * FROM identities WHERE id = ?", (dst,))
            if a is None or b is None:
                raise KeyError(src if a is None else dst)
            self.db.x("UPDATE merges SET identity_id = ? WHERE identity_id = ?", (dst, src))
            mid = self.db.x("INSERT INTO merges (ts, identity_id, name, visits) VALUES (?, ?, ?, 0)",
                            (time.time(), dst, a["name"])).lastrowid
            # A visit keeps the first merge that moved it: it says who it was first seen as
            n = self.db.x("UPDATE visits SET identity_id = ?, merge_id = COALESCE(merge_id, ?) WHERE identity_id = ?",
                          (dst, mid, src)).rowcount
            self.db.x("UPDATE merges SET visits = ? WHERE id = ?", (n, mid))
            # an open visit announced as src was announced as this same person
            self.db.x("UPDATE visits SET announced_id = ? WHERE announced_id = ?", (dst, src))
            self.db.x("UPDATE labels SET identity_id = ? WHERE identity_id = ?", (dst, src))
            kind = "known" if "known" in (a["kind"], b["kind"]) else "unknown"
            name, named = (a["name"], 1) if a["named"] and not b["named"] else (b["name"], b["named"])
            notes = "\n".join(n for n in (b["notes"], a["notes"]) if n)
            self.db.x("UPDATE identities SET kind = ?, name = ?, named = ?, notes = ? WHERE id = ?",
                      (kind, name, named, notes, dst))
            self.db.x("DELETE FROM dismissed WHERE a = ? OR b = ?", (src, src))
            self.db.x("DELETE FROM identities WHERE id = ?", (src,))
        self.invalidate()
        self._emit("identity_removed", {"identity": src})
        self._emit("identity_changed", {"identity": dst})

    def update_identity(self, ident: int, name: str | None = None, notes: str | None = None,
                        ignored: bool | None = None) -> None:
        if not self.db.one("SELECT 1 FROM identities WHERE id = ?", (ident,)):
            raise KeyError(ident)
        if name is not None and name.strip():
            self.db.x("UPDATE identities SET name = ?, named = 1 WHERE id = ?", (name.strip()[:80], ident))
        if notes is not None:
            self.db.x("UPDATE identities SET notes = ? WHERE id = ?", (notes[:2000], ident))
        if ignored is not None:
            self.db.x("UPDATE identities SET ignored = ? WHERE id = ?", (int(ignored), ident))
        self._emit("identity_changed", {"identity": ident})

    def forget(self, ident: int) -> int:
        """Delete an identity with every visit, face and crop. Returns visits removed."""
        with self.db.tx():
            if not self.db.one("SELECT 1 FROM identities WHERE id = ?", (ident,)):
                raise KeyError(ident)
            n = self._delete_visits("identity_id = ?", (ident,))
            self.db.x("DELETE FROM dismissed WHERE a = ? OR b = ?", (ident, ident))
            self.db.x("DELETE FROM identities WHERE id = ?", (ident,))
        self.invalidate()
        self._emit("identity_removed", {"identity": ident})
        return n

    def delete_visit(self, vid: int) -> None:
        with self.db.tx():
            v = self.db.one("SELECT identity_id FROM visits WHERE id = ?", (vid,))
            if v is None:
                raise KeyError(vid)
            self._delete_visits("id = ?", (vid,))
            if v["identity_id"] is not None:
                self._drop_if_empty(v["identity_id"])
        self.invalidate()

    def purge(self, now: float | None = None) -> int:
        """Forget visits older than the retention period, and unnamed visitors left with none."""
        if self.s.retention_days <= 0:
            return 0
        cutoff = (now or time.time()) - self.s.retention_days * 86400
        with self.db.tx():
            n = self._delete_visits("closed = 1 AND end_ts < ?", (cutoff,))
            gone = [r["id"] for r in self.db.q(
                "SELECT id FROM identities i WHERE kind = 'unknown' AND named = 0 "
                "AND NOT EXISTS (SELECT 1 FROM visits WHERE identity_id = i.id)")]
            for ident in gone:
                self.db.x("DELETE FROM identities WHERE id = ?", (ident,))
            self.db.x("DELETE FROM events WHERE ts < ? AND NOT EXISTS (SELECT 1 FROM sightings WHERE event_id = events.id)",
                      (cutoff,))
            self.db.x("DELETE FROM rings WHERE ts < ? AND visit_id IS NULL", (cutoff,))
        if n:
            self.invalidate()
            log.info("retention: forgot %d visits, %d visitors", n, len(gone))
        for ident in gone:
            self._emit("identity_removed", {"identity": ident})
        return n

    def _delete_visits(self, where: str, args: tuple) -> int:
        vids = [r["id"] for r in self.db.q(f"SELECT id FROM visits WHERE {where}", args)]
        for vid in vids:
            rows = self.db.q("SELECT crop, event_id FROM sightings WHERE visit_id = ?", (vid,))
            self.db.x("DELETE FROM visits WHERE id = ?", (vid,))   # sightings cascade
            for r in rows:
                self._unlink(r["crop"])
                if not self.db.one("SELECT 1 FROM sightings WHERE event_id = ?", (r["event_id"],)):
                    e = self.db.one("SELECT frame FROM events WHERE id = ?", (r["event_id"],))
                    if e:
                        self._unlink(e["frame"])
                        self.db.x("UPDATE events SET frame = NULL WHERE id = ?", (r["event_id"],))
        return len(vids)

    def _drop_if_empty(self, ident: int) -> None:
        """An unnamed visitor left with no visits was only ever a mistake."""
        r = self.db.one("SELECT kind, named, (SELECT COUNT(*) FROM visits WHERE identity_id = i.id) AS n "
                        "FROM identities i WHERE id = ?", (ident,))
        if r and r["n"] == 0 and r["kind"] == "unknown" and not r["named"]:
            self.db.x("DELETE FROM identities WHERE id = ?", (ident,))
            self._emit("identity_removed", {"identity": ident})

    # ---------- merge suggestions

    def suggestions(self, limit: int = 50) -> list[dict]:
        """Pairs of identities that look like the same person, most alike first."""
        refs, owners = self.refs()
        if not len(owners):
            return []
        groups = {}
        for ident in np.unique(owners):
            groups[int(ident)] = refs[owners == ident][:10]
        ids = sorted(groups)
        dismissed = {(r["a"], r["b"]) for r in self.db.q("SELECT a, b FROM dismissed")}
        k = self.s.top_k
        out = []
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if (a, b) in dismissed:
                    continue
                sims = (groups[a] @ groups[b].T).ravel()
                score = float(np.sort(sims)[-min(k, sims.size):].mean())
                if score >= self.s.suggest_threshold:
                    out.append({"a": a, "b": b, "score": round(score, 3)})
        out.sort(key=lambda d: d["score"], reverse=True)
        return out[:limit]

    def dismiss(self, a: int, b: int) -> None:
        a, b = sorted((a, b))
        self.db.x("INSERT OR IGNORE INTO dismissed (a, b) VALUES (?, ?)", (a, b))

