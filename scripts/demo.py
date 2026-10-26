"""A demo database for screenshots and trying the UI: ten weeks of made-up visitors with
drawn faces, so no real face is ever needed.

    uv run --extra test python scripts/demo.py demo-data
    SMARTEYE_DATA=demo-data SMARTEYE_AUTH_MODE=none SMARTEYE_VISIT_GAP_S=3600 \\
        SMARTEYE_CAMERA_LABELS="FDoor:Front door,BDoor:Back door" uv run uvicorn smarteye.server:app

The long visit gap keeps the last arrival "at the door" while you look around.
"""
import io
import math
import random
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from smarteye import config
from smarteye.db import DB
from smarteye.engine import Engine, EventIn, FaceIn

R = random.Random(11)
NP = np.random.default_rng(11)
TZ = ZoneInfo("Europe/London")
DAYS = 70


class Person:
    def __init__(self, key, skin, hair, top, style="short", hat=None, glasses=False, label=None,
                 name=None, notes="", household=False):
        self.key, self.skin, self.hair, self.top, self.style = key, skin, hair, top, style
        self.hat, self.glasses, self.label = hat, glasses, label
        self.name, self.notes, self.household = name, notes, household
        v = NP.normal(size=512).astype(np.float32)
        self.vec = v / np.linalg.norm(v)

    def emb(self):
        w = self.vec + 0.55 * NP.normal(size=512).astype(np.float32) / math.sqrt(512)
        return (w / np.linalg.norm(w)).astype(np.float32)


SKIN = ["#f1c7a5", "#e0ac84", "#c68863", "#8d5a3b", "#5c3a24", "#f5d5bd"]
PEOPLE = {p.key: p for p in [
    Person("alex", SKIN[0], "#4a3426", "#3f6e8c", label="Alex Carter", household=True),
    Person("sam", SKIN[1], "#20180f", "#8c3f52", style="long", label="Sam Carter", household=True),
    Person("gran", SKIN[5], "#d9d6d0", "#6b5a8e", style="bun", glasses=True, label="Margaret Hill"),
    Person("window", SKIN[2], "#2b1d14", "#2f7d4f", hat="#2f7d4f", name="Window cleaner", notes="Pay £15 cash"),
    Person("post", SKIN[0], "#a0672d", "#c0392b", hat="#c0392b", name="Postie", notes="Letters most weekdays"),
    Person("milk", SKIN[3], "#111111", "#ecf0f1", name="Milk man", notes="Mon & Thu, early"),
    Person("courier", SKIN[4], "#111111", "#f39c12", hat="#2c3e50", name="Parcel courier"),
    Person("chris", SKIN[1], "#b5651d", "#34495e", glasses=True, name="Chris next door"),
    Person("reader", SKIN[0], "#6d4c41", "#7f8c8d", hat="#7f8c8d", notes="Meter reader?"),
    Person("leaflet", SKIN[2], "#3e2723", "#16a085", style="long"),
    Person("charity", SKIN[4], "#1b1b1b", "#2980b9", glasses=True),
    Person("survey", SKIN[5], "#c49a6c", "#8e44ad", style="bun"),
    Person("van", SKIN[3], "#0d0d0d", "#27ae60", hat="#d35400"),
]}


def draw_head(d: ImageDraw.ImageDraw, p: Person, cx: float, cy: float, s: float, turn: float = 0):
    """A head of size s centred at cx, cy; turn shifts the features sideways."""
    d.rounded_rectangle([cx - s * .95, cy + s * .55, cx + s * .95, cy + s * 2.2], radius=s * .5, fill=p.top)
    d.rectangle([cx - s * .2, cy + s * .35, cx + s * .2, cy + s * .7], fill=p.skin)
    if p.style == "long":
        d.rounded_rectangle([cx - s * .62, cy - s * .55, cx + s * .62, cy + s * .75], radius=s * .4, fill=p.hair)
    d.ellipse([cx - s * .5, cy - s * .62, cx + s * .5, cy + s * .58], fill=p.skin)
    if p.style == "bun":
        d.ellipse([cx - s * .22, cy - s * .95, cx + s * .22, cy - s * .55], fill=p.hair)
    d.chord([cx - s * .52, cy - s * .68, cx + s * .52, cy + s * .1], 180, 360, fill=p.hair)
    if p.hat:
        d.chord([cx - s * .55, cy - s * .78, cx + s * .55, cy + s * .05], 180, 360, fill=p.hat)
        d.rectangle([cx - s * .1 + turn * s * .6, cy - s * .38, cx + s * .7 + turn * s * .6, cy - s * .3], fill=p.hat)
    fx = cx + turn * s * .18
    for ex in (-.2, .2):
        d.ellipse([fx + s * (ex - .06), cy - s * .08, fx + s * (ex + .06), cy + s * .04], fill="#2b2b2b")
        if p.glasses:
            d.ellipse([fx + s * (ex - .13), cy - s * .15, fx + s * (ex + .13), cy + s * .11], outline="#222", width=max(1, int(s * .03)))
    d.arc([fx - s * .16, cy + s * .12, fx + s * .16, cy + s * .34], 20, 160, fill="#7a3b2e", width=max(1, int(s * .04)))


def frame_and_crop(p: Person, cam: str, ts: float) -> tuple[bytes, bytes, tuple[int, int, int, int]]:
    """A 640x480 door frame with the person in it, and the face crop from it."""
    hour = datetime.fromtimestamp(ts, TZ).hour
    light = 1.0 if 8 <= hour < 17 else 0.55
    img = Image.new("RGB", (640, 480), "#9aa5a8")
    d = ImageDraw.Draw(img)
    if cam == "FDoor":
        for y in range(0, 480, 24):            # brick porch
            for x in range(-(y // 24 % 2) * 30, 640, 60):
                d.rectangle([x + 1, y + 1, x + 58, y + 22], fill="#a8553f")
        d.rectangle([380, 40, 560, 480], fill="#26394d")
        d.ellipse([530, 250, 542, 262], fill="#d4af37")
    else:
        d.rectangle([0, 0, 640, 480], fill="#dcd6c8")
        d.rectangle([60, 30, 260, 480], fill="#5d7b5a")
        d.rectangle([0, 400, 640, 480], fill="#7b7468")
    s = R.uniform(70, 95)
    cx, cy = R.uniform(170, 330), R.uniform(170, 230)
    draw_head(d, p, cx, cy, s, turn=R.uniform(-.6, .6))
    img = img.point(lambda v: int(v * light)).filter(ImageFilter.GaussianBlur(0.6))
    box = (int(cx - s * .75), int(cy - s * .95), int(cx + s * .75), int(cy + s * .85))
    crop = img.crop(box).resize((256, 307))
    return jpeg(img), jpeg(crop), box


def jpeg(img: Image.Image) -> bytes:
    b = io.BytesIO()
    img.save(b, "JPEG", quality=82)
    return b.getvalue()


def schedule(now: datetime) -> list[tuple[float, str, str]]:
    """(time, person, camera) for every visit over the last DAYS days, oldest first."""
    out = []
    def at(day, h, m, spread=20):
        t = day.replace(hour=h, minute=m, second=0) + timedelta(minutes=R.uniform(-spread, spread), seconds=R.uniform(0, 59))
        return t.timestamp()
    start = (now - timedelta(days=DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
    for n in range(DAYS + 1):
        day = start + timedelta(days=n)
        wd = day.weekday()
        last_day = n == DAYS
        for who in ("alex", "sam"):
            if R.random() < .8:
                out.append((at(day, 8, 10, 25), who, R.choice(["FDoor", "BDoor"])))
            if R.random() < .85:
                out.append((at(day, 17, 45, 50), who, R.choice(["FDoor", "FDoor", "BDoor"])))
        if wd < 5 and R.random() < .75:
            out.append((at(day, 11, 0, 35), "post", "FDoor"))
        if wd in (0, 3) and n < DAYS - 16:     # the milk man stopped coming two weeks ago: overdue
            out.append((at(day, 6, 30, 10), "milk", "FDoor"))
        if wd == 5 and (n // 7) % 2 == 0:
            out.append((at(day, 10, 15, 30), "window", "FDoor"))
        if wd == 6 and R.random() < .8:
            out.append((at(day, 14, 30, 40), "gran", "FDoor"))
        if R.random() < .3:
            out.append((at(day, 15, 0, 120), "courier", "FDoor"))
        if R.random() < .08:
            out.append((at(day, 12, 0, 240), "chris", R.choice(["FDoor", "BDoor"])))
    for key, n in (("reader", 12), ("leaflet", 30), ("charity", 44), ("survey", 57)):
        out.append((at(start + timedelta(days=n), 13, 0, 120), key, "FDoor"))
    out = [o for o in out if o[0] < now.timestamp() - 1200]
    out.append(((now - timedelta(minutes=40)).timestamp(), "van", "FDoor"))      # someone new today
    out.append(((now - timedelta(seconds=50)).timestamp(), "courier", "FDoor"))  # at the door now
    return sorted(out)


def main(dest: str) -> None:
    import os
    os.environ.update(SMARTEYE_DATA=dest, SMARTEYE_CAMERAS="FDoor,BDoor", SMARTEYE_MQTT_URL="")
    data = Path(dest)
    if (data / "smarteye.db").exists():
        sys.exit(f"{data / 'smarteye.db'} exists: use an empty folder")
    s = config.load()
    e = Engine(s, DB(data / "smarteye.db"))
    now = datetime.now(TZ)
    plan = schedule(now)
    first_event: dict[str, str] = {}
    for i, (ts, key, cam) in enumerate(plan):
        p = PEOPLE[key]
        for k in range(R.randint(2, 6)):
            frame, crop, box = frame_and_crop(p, cam, ts + k * 1.5)
            px = box[2] - box[0]
            eid = str(uuid.UUID(int=R.getrandbits(128)))
            first_event.setdefault(key, eid)
            e.process(EventIn(id=eid, ts=ts + k * 1.5, camera=cam, frame=frame, faces=[FaceIn(
                box=box, det=round(R.uniform(.8, .95), 3), embedding=p.emb(), crop=crop,
                label=p.label if p.label and R.random() < .8 else None, score=round(R.uniform(.5, .7), 3) if p.label else None)]))
        if key in ("post", "courier", "window") and R.random() < .5:
            e.ring(cam, ts + 2)
        if i < len(plan) - 1:
            e.close_all()
    for key, eid in first_event.items():
        p = PEOPLE[key]
        r = e.db.one("SELECT v.identity_id FROM sightings s JOIN visits v ON v.id = s.visit_id WHERE s.event_id = ?", (eid,))
        if r and r["identity_id"] and (p.name or p.notes or p.household):
            e.update_identity(r["identity_id"], name=p.name, notes=p.notes or None, ignored=p.household or None)
    n = e.db.one("SELECT COUNT(*) AS n FROM visits")["n"]
    print(f"{len(plan)} arrivals, {n} visits, "
          f"{e.db.one('SELECT COUNT(*) AS n FROM identities')['n']} identities in {data}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo-data")
