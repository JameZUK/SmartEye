"""A description of who's at the door, by a vision model on Ollama (SMARTEYE_CAPTION_MODEL).

When a visit starts, its frame goes to the model with what face recognition already knows
(who it is, household or visitor, how often they come), and back comes one line for a
notification, "Sam's back with the shopping and looks tired" or "Royal Mail with a large
parcel", with the visitor's likely role, what they carry, mood and weather when visible.

One background thread does the calls, so ingest never waits for the model. A visit is
described again if face recognition later settles on someone else by name; a guess that only
moves between unnamed visitors keeps the description it has. Listeners hear "caption" with
the visit as it stands, its description included.

People who arrive together get a visit each, and each is described with the others named
("Alex is home with Sam"): the model is told who else face recognition has at that door at
the same time, and the description lists them (with), so Home Assistant can send the group
one notification.

Frames leave the house when the model runs in Ollama's cloud (the default URL): see README.
"""
import base64
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

from .engine import Engine, EventError

log = logging.getLogger(__name__)
ROLES = ("household", "delivery", "postal", "food delivery", "trades", "friend or family", "neighbour",
         "canvasser or seller", "unknown")
QUEUE = 20   # visits waiting for a description; more are dropped (a burst the model can't keep up with)
TOGETHER_S = 5   # visits at one door this close in time are people arriving together

SYSTEM = """You write the line a smart doorbell sends to the phones of the people who live in a
house when someone comes to the door. You get one still frame from the door camera and what
face recognition already knows about the person.

Write it the way one person in the house would tell another, for example:
- "Sam's back with the shopping and looks tired."
- "The window cleaner, bucket in hand."
- "The postman, ringing the bell."
- "A man in a hi-vis jacket with a clipboard, probably a meter reader."
- "Alex is heading in with a pink bag; it looks wet out there."

These show the style, not what to say: describe only this frame.

Rules:
- Face recognition's name is who it is: don't second-guess it. Use a person's first name
  (Alex Carter -> Alex). Other names are the household's own labels for regular visitors
  (Amazon, royal mail, Milk man): use them as they'd say them (Royal Mail, the milkman).
  A label says who someone is, not what they have with them: a milkman needn't have
  crates, nor "dog woman" a dog. Only the frame says that.
- "Probably" from face recognition means a guess: keep the "probably".
- The line is about the person face recognition names first, even when someone else is
  nearer or better known: start with them. Anyone listed as also at the door is with them:
  name them after ("A man in a cream jumper, with Sam"; "Alex is home with Sam"). Mention
  anyone else only if their face and body are plainly in the frame: blur, colour smears,
  shadows and reflections are not people.
- Someone not recognised: describe them briefly ("A man in a grey hoodie") and guess why they're
  here from what's visible.
- Say what they're carrying and anything that shows why they're here: uniform, lanyard,
  scanner, parcels, food bags, tools, leaflets, a clipboard. A courier or postman with no
  parcel in sight has none: don't give them one. Smoking only if a cigarette or vape is
  plainly in hand.
- Mood only from a clear expression or posture; weather only from visible signs (wet ground,
  rain, an umbrella, hood up, bright sun). Otherwise leave them out.
- Never mention anything you can't see. If the frame shows little, say little. Something
  you can't make out is "a black bundle", not a guess at what it might be.
- Speak about them, never as yourself: no "I" or "I can't see".
- Greyscale night (infrared) frames have no colours: don't give any.
- Under 20 words. No time of day unless it's odd (the middle of the night)."""

FIELDS = f"""Reply with only a JSON object with these keys:
- summary: string, the line itself
- role: one of {", ".join(ROLES)}
- role_confidence: low, medium or high
- carrying: array of short strings, empty if nothing
- appearance: string, a few words on clothing (for someone not recognised), else empty
- mood: string, empty unless visible
- weather: string, empty unless visible
- infrared: true for a greyscale night frame"""


def who(v: dict) -> str:
    """Visit v's person as face recognition has them, in words."""
    if v.get("identity") is None:
        return "someone not recognised"
    name = v["name"] if v.get("named") else None
    probably = "probably " if v.get("provisional") else ""
    if v.get("ignored"):
        return f"{probably}{name or 'someone who lives here'} (lives here)"
    if name and (v.get("visit_number") or 1) > 1:
        return f"{probably}{name} (a regular: visit {v['visit_number']})"
    if name:
        return f"{probably}{name}"
    if (v.get("visit_number") or 1) > 1:
        return f"{probably}an unnamed visitor seen {v['visit_number'] - 1} times before"
    return "someone not recognised, here for the first time"


def context(v: dict, door: str, when: str, others: list[dict] = ()) -> str:
    """What face recognition knows about visit v, and anyone at the door with them, for the model."""
    rang = " They rang the bell." if v.get("rang") else ""
    also = f"\nAlso at the door: {'; '.join(who(o) for o in others)}." if others else ""
    return f"Door: {door}. Time: {when}.\nThis is about (face recognition): {who(v)}.{rang}{also}"


def parse(text: str) -> dict:
    """The model's reply as a description. Raises ValueError if it isn't one."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    d = json.loads(text[text.index("{"):text.rindex("}") + 1])
    summary = str(d.get("summary") or "").strip()
    if not summary:
        raise ValueError("no summary")
    role = str(d.get("role") or "unknown").strip().lower()
    carrying = d.get("carrying") or []
    return {
        "summary": summary[:300],
        "role": role if role in ROLES else "unknown",
        "role_confidence": d.get("role_confidence") if d.get("role_confidence") in ("low", "medium", "high") else "low",
        "carrying": [str(c)[:60] for c in carrying][:6] if isinstance(carrying, list) else [],
        "appearance": str(d.get("appearance") or "")[:200],
        "mood": str(d.get("mood") or "")[:100],
        "weather": str(d.get("weather") or "")[:100],
        "infrared": bool(d.get("infrared")),
    }


def ollama(url: str, key: str, timeout: float) -> Callable[[dict], dict]:
    """POST a chat request to Ollama (ollama.com or a local server), return the reply."""
    def post(body: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(f"{url}/api/chat", json.dumps(body).encode(), headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    return post


class Captioner:
    def __init__(self, engine: Engine, post: Callable[[dict], dict] | None = None):
        self.e = engine
        s = engine.s
        self.post = post or ollama(s.caption_url, s.ollama_key, s.caption_timeout_s)
        self.q: queue.Queue[int] = queue.Queue(QUEUE)
        self.pending: set[int] = set()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.e.listeners.append(self.on_engine)
        self.thread = threading.Thread(target=self._run, name="caption", daemon=True)
        self.thread.start()

    def on_engine(self, kind: str, data: dict) -> None:
        if kind not in ("arrival", "identified", "visit") or not data.get("frame_event"):
            return
        made = (data.get("caption") or {}).get("for")
        if kind != "arrival" and data.get("caption") and made == self._key(data):
            return   # described already, as this same person
        with self.lock:
            if data["visit"] in self.pending:
                return
            try:
                self.q.put_nowait(data["visit"])
            except queue.Full:
                self.e.counters["captions_dropped"] += 1
                return
            self.pending.add(data["visit"])

    def together(self, v: dict) -> list[dict]:
        """The other visits at v's door at the same time: people who came with them."""
        rows = self.e.db.q("""SELECT id FROM visits WHERE camera = ? AND id != ?
                                AND start_ts <= ? AND end_ts >= ? ORDER BY start_ts""",
                           (v["camera"], v["visit"], v["end"] + TOGETHER_S, v["start"] - TOGETHER_S))
        seen = {v["identity"]} if v.get("identity") is not None else set()
        out = []
        for r in rows:
            o = self.e.now(r["id"])
            if o.get("identity") is None or o["identity"] not in seen:   # not themselves, in a split visit
                out.append(o)
                if o.get("identity") is not None:
                    seen.add(o["identity"])
        return out

    @staticmethod
    def _key(v: dict) -> int | None:
        """Who a description names: a named identity, or nobody (unnamed visitors read alike)."""
        return v["identity"] if v.get("identity") is not None and v.get("named") else None

    def _run(self) -> None:
        while True:
            vid = self.q.get()
            with self.lock:
                self.pending.discard(vid)
            try:
                self.describe(vid)
            except Exception:
                log.exception("describing visit %s failed", vid)

    def drain(self) -> None:
        """Describe everything queued, here and now (tests)."""
        while not self.q.empty():
            vid = self.q.get_nowait()
            self.pending.discard(vid)
            self.describe(vid)

    def describe(self, vid: int) -> dict | None:
        """Describe visit vid as it stands now, store it and tell listeners. None if the visit or
        its frame is gone, or the model gave no usable answer."""
        e = self.e
        if not e.db.one("SELECT 1 FROM visits WHERE id = ?", (vid,)):
            return None   # folded into another, or deleted
        v = e.now(vid)
        r = e.db.one("SELECT frame FROM events WHERE id = ?", (v["frame_event"],)) if v.get("frame_event") else None
        try:
            jpeg = e._inside(r["frame"]).read_bytes() if r and r["frame"] else None
        except (OSError, EventError):
            jpeg = None
        if not jpeg:
            return None
        door = e.s.camera_label(v["camera"])
        when = e.local(v["start"]).strftime("%A %H:%M")
        others = self.together(v)
        body = {"model": e.s.caption_model, "stream": False, "think": False, "options": {"temperature": 0},
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": context(v, door, when, others) + "\n\n" + FIELDS,
                              "images": [base64.b64encode(jpeg).decode()]}]}
        t = time.monotonic()
        try:
            out = self.post(body)
            cap = parse(out.get("message", {}).get("content", ""))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as err:
            e.counters["caption_errors"] += 1
            log.warning("no description for visit %s: %s", vid, err)
            return None
        cap.update({"for": self._key(v), "model": e.s.caption_model, "ts": time.time(),
                    "seconds": round(time.monotonic() - t, 1),
                    "with": [{"visit": o["visit"], "identity": o["identity"],
                              "name": o["name"] if o.get("identity") is not None else None} for o in others]})
        if not e.db.x("UPDATE visits SET caption = ? WHERE id = ?", (json.dumps(cap), vid)).rowcount:
            return None   # gone while the model was thinking
        e.counters["captions"] += 1
        now = e.now(vid)
        e._emit("caption", now)
        return now
