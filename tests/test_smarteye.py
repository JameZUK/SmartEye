import base64
import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from smarteye import config, stats
from smarteye.db import DB
from smarteye.engine import Engine, EventError, EventIn, FaceIn, parse_event

rng = np.random.default_rng(7)


def person_vec() -> np.ndarray:
    v = rng.normal(size=512).astype(np.float32)
    return v / np.linalg.norm(v)


def near(v: np.ndarray, noise: float = 0.6) -> np.ndarray:
    """Another photo of the same face: cosine to v around 0.8."""
    w = v + noise * rng.normal(size=512).astype(np.float32) / np.sqrt(512) * 1.0
    return (w / np.linalg.norm(w)).astype(np.float32)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTEYE_DATA", str(tmp_path))
    monkeypatch.setenv("SMARTEYE_CAMERAS", "FDoor")
    s = config.load()
    return Engine(s, DB(tmp_path / "smarteye.db"))


def face(v, px=100, det=0.85, label=None, crop=b"\xff\xd8jpeg"):
    return FaceIn(box=(10, 10, 10 + px, 10 + px), det=det, embedding=v, label=label, score=0.6 if label else 0.2,
                  crop=crop)


def ev(engine, ts, faces, camera="FDoor", eid=None):
    return engine.process(EventIn(id=eid or f"e{ts}-{rng.integers(1e9)}", ts=ts, camera=camera, faces=faces))


def visits(engine):
    return engine.db.q("SELECT v.*, i.name, i.kind FROM visits v LEFT JOIN identities i ON i.id = v.identity_id ORDER BY start_ts")


def test_frames_of_one_arrival_make_one_visit(engine):
    p = person_vec()
    t = 1_800_000_000
    for i in range(5):
        ev(engine, t + i * 3, [face(near(p))])
    engine.close_all()
    vs = visits(engine)
    assert len(vs) == 1
    assert vs[0]["how"] == "new" and vs[0]["kind"] == "unknown"
    assert engine.db.one("SELECT COUNT(*) AS n FROM sightings")["n"] == 5


def test_returning_visitor_matches_and_stranger_does_not(engine):
    p, q = person_vec(), person_vec()
    t = 1_800_000_000
    for i in range(3):
        ev(engine, t + i, [face(near(p))])
    ev(engine, t + 86400, [face(near(p))])              # a day later: same person
    ev(engine, t + 2 * 86400, [face(near(q))])          # a stranger
    engine.close_all()
    vs = visits(engine)
    assert [v["how"] for v in vs] == ["new", "matched", "new"]
    assert vs[0]["identity_id"] == vs[1]["identity_id"] != vs[2]["identity_id"]


def test_new_frame_after_gap_closes_previous_visit(engine):
    p = person_vec()
    t = 1_800_000_000
    ev(engine, t, [face(near(p))])
    ev(engine, t + engine.s.visit_gap_s + 5, [face(near(p))])
    vs = visits(engine)
    assert len(vs) == 2 and vs[0]["closed"] == 1 and vs[1]["closed"] == 0


def test_immicheye_name_wins_and_label_spellings_merge(engine):
    p = person_vec()
    t = 1_800_000_000
    ev(engine, t, [face(near(p), label="cal_smith")])
    ev(engine, t + 2, [face(near(p))])                  # same visit, unnamed frame
    ev(engine, t + 86400, [face(near(p), label="Cal Smith")])
    engine.close_all()
    vs = visits(engine)
    assert len(vs) == 2
    assert {v["how"] for v in vs} == {"named"}
    assert vs[0]["identity_id"] == vs[1]["identity_id"]
    assert vs[0]["name"] == "Cal Smith" and vs[0]["kind"] == "known"
    assert len(vs[0:1]) and engine.db.one("SELECT COUNT(*) AS n FROM sightings WHERE visit_id = ?", (vs[0]["id"],))["n"] == 2


def test_two_people_in_one_frame_get_two_visits(engine):
    p, q = person_vec(), person_vec()
    t = 1_800_000_000
    ev(engine, t, [face(near(p)), face(near(q))])
    ev(engine, t + 2, [face(near(p)), face(near(q))])
    engine.close_all()
    vs = visits(engine)
    assert len(vs) == 2
    assert all(engine.db.one("SELECT COUNT(*) AS n FROM sightings WHERE visit_id = ?", (v["id"],))["n"] == 2 for v in vs)


def test_small_faces_join_visits_but_never_make_identities(engine):
    p = person_vec()
    t = 1_800_000_000
    ev(engine, t, [face(near(p), px=20)])
    engine.close_all()
    vs = visits(engine)
    assert len(vs) == 1 and vs[0]["identity_id"] is None and vs[0]["how"] == "none"
    assert engine.db.one("SELECT COUNT(*) AS n FROM identities")["n"] == 0
    # ...and a tiny face during a real visit counts towards it
    ev(engine, t + 1000, [face(near(p))])
    ev(engine, t + 1002, [face(person_vec(), px=15)])
    engine.close_all()
    assert len(visits(engine)) == 2


def test_disabled_camera_stores_nothing(engine):
    assert ev(engine, 1_800_000_000, [face(person_vec())], camera="FtGrd") == 0
    assert engine.db.one("SELECT COUNT(*) AS n FROM sightings")["n"] == 0
    cam = engine.db.one("SELECT * FROM cameras WHERE name = 'FtGrd'")
    assert cam["enabled"] == 0 and cam["events"] == 1


def test_duplicate_event_ignored(engine):
    f = [face(person_vec())]
    assert ev(engine, 1_800_000_000, f, eid="same") == 1
    assert ev(engine, 1_800_000_000, f, eid="same") == -1


def test_merge_assign_and_forget(engine, tmp_path):
    p, q = person_vec(), person_vec()
    t = 1_800_000_000
    ev(engine, t, [face(near(p))])
    ev(engine, t + 1000, [face(near(q))])
    engine.close_all()
    a, b = [v["identity_id"] for v in visits(engine)]
    engine.merge(a, b)
    assert {v["identity_id"] for v in visits(engine)} == {b}
    first = visits(engine)[0]["id"]
    new = engine.assign_visit(first, "new")
    assert new != b
    crops = list(tmp_path.glob("crops/*/*.jpg"))
    assert len(crops) == 2
    engine.forget(new)
    assert len(list(tmp_path.glob("crops/*/*.jpg"))) == 1
    assert engine.db.one("SELECT 1 FROM identities WHERE id = ?", (new,)) is None


def test_suggestions_find_split_identity(engine):
    p = person_vec()
    t = 1_800_000_000
    for d in range(2):
        ev(engine, t + d * 86400, [face(near(p, 0.9))])
        engine.close_all()
    # Force a split: move the second visit to its own visitor
    second = visits(engine)[1]["id"]
    engine.assign_visit(second, "new")
    pairs = engine.suggestions()
    assert pairs and pairs[0]["score"] > 0.5
    engine.dismiss(pairs[0]["a"], pairs[0]["b"])
    assert engine.suggestions() == []


def test_retention_purges_old_visits_and_empty_visitors(engine):
    t = time.time() - 400 * 86400
    ev(engine, t, [face(person_vec())])
    engine.close_all()
    assert engine.purge() == 1
    assert engine.db.one("SELECT COUNT(*) AS n FROM identities")["n"] == 0


def test_model_mismatch_refused(engine):
    with pytest.raises(EventError):
        engine.process(EventIn(id="m", ts=1.0, camera="FDoor", faces=[], model="antelopev2"))


def test_parse_event_roundtrip():
    v = person_vec()
    body = {"v": 1, "id": "x", "ts": 1.5, "camera": "FDoor", "model": "buffalo_l", "faces": [{
        "box": [1, 2, 50, 60], "det": 0.9, "result": "unknown", "score": 0.2, "top": [["a", 0.2]],
        "embedding": base64.b64encode(v.astype("<f4").tobytes()).decode(),
        "crop": base64.b64encode(b"jpg").decode()}]}
    e = parse_event(body)
    assert e.faces[0].label is None and e.faces[0].crop == b"jpg"
    assert np.allclose(e.faces[0].embedding, v, atol=1e-6)
    with pytest.raises(EventError):
        parse_event({**body, "faces": [{**body["faces"][0], "embedding": base64.b64encode(b"1234").decode()}]})


def test_typical_window_and_regularity(engine):
    base = 1_800_000_000 - 1_800_000_000 % 86400   # midnight UTC
    # Ten weekly visits around 10:30 London time, one at 22:00
    starts = [base + d * 7 * 86400 + 10 * 3600 + (d % 3) * 600 for d in range(10)] + [base + 3 * 86400 + 21 * 3600]
    w = stats.typical_window(engine, starts)
    assert w and w["share"] >= 0.6
    r = stats.regularity(engine, starts, now=max(starts) + 86400)
    assert r and not r["overdue"]
    assert engine.unusual(base + 70 * 86400 + 3 * 3600, starts)        # 03:00 is new for them
    assert not engine.unusual(base + 70 * 86400 + 10 * 3600, starts)


def test_api_ingest_and_read(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTEYE_DATA", str(tmp_path))
    monkeypatch.setenv("SMARTEYE_INGEST_TOKEN", "tok")
    monkeypatch.setenv("SMARTEYE_MQTT_URL", "")
    import importlib
    import smarteye.server as server
    importlib.reload(server)
    v = person_vec()
    body = {"v": 1, "id": "a1", "ts": time.time(), "camera": "FDoor", "faces": [{
        "box": [0, 0, 120, 120], "det": 0.9, "result": "unknown", "score": 0.1,
        "embedding": base64.b64encode(v.astype("<f4").tobytes()).decode(),
        "crop": base64.b64encode(b"\xff\xd8x").decode()}]}
    with TestClient(server.app) as c:
        assert c.post("/api/events", json=body).status_code == 401
        r = c.post("/api/events", json=body, headers={"Authorization": "Bearer tok"})
        assert r.status_code == 202 and r.json() == {"accepted": 1}
        assert c.post("/api/events", json=body, headers={"Authorization": "Bearer tok"}).json() == {"duplicate": True}
        s = c.get("/api/summary").json()
        assert s["today"]["visits"] == 1 and len(s["open"]) == 1
        server.app.state.engine.close_all()
        ident = c.get("/api/identities").json()[0]
        assert ident["name"] == "Visitor 1" and ident["visits"] == 1
        d = c.get(f"/api/identities/{ident['id']}").json()
        assert d["stats"]["visits"] == 1
        assert c.patch(f"/api/identities/{ident['id']}", json={"name": "Postie"}).json()["name"] == "Postie"
        sid = d["faces"][0]["id"]
        assert c.get(f"/img/s/{sid}.jpg").content == b"\xff\xd8x"
        assert c.get(f"/img/s/{sid}.jpg?k=wrong").status_code == 403
        assert c.get(f"/img/s/{sid}.jpg?k={server.sign(sid)}").status_code == 200


def test_mqtt_events_and_discovery(engine, monkeypatch):
    from smarteye import mqtt as m
    monkeypatch.setattr(engine, "s", engine.s.__class__(**{**engine.s.__dict__, "mqtt_url": "mqtt://u:p@broker:1883"}))
    bus = m.Mqtt(engine, lambda sid: f"http://se/img/s/{sid}.jpg?k=x")
    sent = []
    bus.pub = lambda topic, payload, retain=False: sent.append((topic, payload))
    engine.listeners.append(bus.on_engine)
    bus._on_connect(None, None, None, 0)
    assert any(t == "homeassistant/event/smarteye_visitor/config" for t, _ in sent)
    sent.clear()
    p = person_vec()
    t = time.time()
    ev(engine, t, [face(near(p))])
    arrival = [pl for tp, pl in sent if tp == "smarteye/event"]
    assert arrival[0]["event_type"] == "unknown_arrival"
    assert any(tp == "smarteye/present" and pl["state"] == "ON" for tp, pl in sent)
    sent.clear()
    engine.close_all()
    ended = [pl for tp, pl in sent if tp == "smarteye/event"]
    assert ended[0]["event_type"] == "new_visitor" and ended[0]["image"].startswith("http://se/img/s/")
    assert ended[0]["named"] is False
    # Naming a visitor marks them named, so HA can tell "Postie" from "Visitor 1"
    ident = visits(engine)[0]["identity_id"]
    engine.update_identity(ident, name="Postie")
    assert any(tp == "homeassistant/sensor/smarteye_person_%d/config" % ident for tp, _ in sent)
    assert [pl for tp, pl in sent if tp == f"smarteye/person/{ident}"][-1]["named"] is True
    sent.clear()
    ev(engine, t + 1000, [face(near(p))])
    arrival = [pl for tp, pl in sent if tp == "smarteye/event"][0]
    assert arrival["event_type"] == "visitor_arrival" and arrival["provisional"] and arrival["named"] is True
    engine.close_all()
    # Ignored identities (the household) fire household_arrival and nothing else
    engine.update_identity(ident, ignored=True)
    sent.clear()
    ev(engine, t + 2000, [face(near(p))])
    ev(engine, t + 2005, [face(near(p))])
    engine.close_all()
    assert [pl["event_type"] for tp, pl in sent if tp == "smarteye/event"] == ["household_arrival"]
    assert not [pl for tp, pl in sent if tp == "smarteye/camera/FDoor/last_visitor"]


def _client(tmp_path, monkeypatch, **env):
    import importlib
    import smarteye.server as server
    monkeypatch.setenv("SMARTEYE_DATA", str(tmp_path))
    for k in ("SMARTEYE_AUTH", "SMARTEYE_AUTH_MODE", "SMARTEYE_TRUSTED_PROXIES", "SMARTEYE_MQTT_URL"):
        monkeypatch.setenv(k, "")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    importlib.reload(server)
    return server


def test_authelia_mode_trusts_only_the_proxy(tmp_path, monkeypatch):
    server = _client(tmp_path, monkeypatch, SMARTEYE_AUTH_MODE="authelia", SMARTEYE_TRUSTED_PROXIES="172.25.0.10")
    # TestClient connects from "testclient": not a trusted proxy, so even a forged header is refused
    with TestClient(server.app) as c:
        assert c.get("/api/summary", headers={"Remote-User": "alex"}).status_code == 403
        assert c.get("/healthz").status_code == 200
        assert c.post("/api/events", json={"v": 1, "ts": 1, "camera": "FDoor", "faces": []}).status_code == 202
    monkeypatch.setattr(server, "_client_in", lambda addr, cidrs: True)   # as if from nginx
    with TestClient(server.app) as c:
        assert c.get("/api/summary").status_code == 401                    # proxy without a user
        r = c.get("/api/status", headers={"Remote-User": "alex"})
        assert r.status_code == 200 and r.json()["user"] == "alex" and r.json()["settings"]["auth_mode"] == "authelia"


def test_client_in():
    from smarteye.server import _client_in
    assert _client_in("172.25.0.10", ("172.25.0.10",))
    assert _client_in("::ffff:172.25.0.10", ("172.25.0.10/32",))
    assert not _client_in("192.0.2.50", ("172.25.0.10",))
    assert not _client_in("testclient", ("0.0.0.0/0",))


def test_writes_refused_cross_site(tmp_path, monkeypatch):
    server = _client(tmp_path, monkeypatch)
    with TestClient(server.app) as c:
        ident = server.app.state.engine.new_visitor()
        url = f"/api/identities/{ident}"
        # A text/plain POST can be sent cross-site without a preflight: refused
        assert c.post(f"{url}/merge", content='{"into": 1}', headers={"Content-Type": "text/plain"}).status_code == 403
        assert c.patch(url, json={"name": "x"}, headers={"Origin": "http://evil.example"}).status_code == 403
        assert c.patch(url, json={"name": "Postie"}, headers={"Origin": "http://testserver"}).status_code == 200
        assert c.patch(url, json={"name": "Postie"}).status_code == 200
        # A form POST is refused; the UI's bodiless DELETE is allowed, but not from another site
        assert c.post(f"{url}/merge", data={"into": "1"}).status_code == 403
        assert c.delete(url, headers={"Origin": "http://evil.example"}).status_code == 403
        assert c.delete(url).status_code == 200


def test_basic_mode(tmp_path, monkeypatch):
    server = _client(tmp_path, monkeypatch, SMARTEYE_AUTH="alex:pw")
    with TestClient(server.app) as c:
        assert c.get("/api/summary").status_code == 401
        r = c.get("/api/status", auth=("alex", "pw"))
        assert r.status_code == 200 and r.json()["user"] == "alex"


def test_health_checks_kept_out_of_access_log():
    import logging
    from smarteye.server import QuietHealthChecks
    f = QuietHealthChecks()
    rec = lambda path: logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                         ("127.0.0.1:1", "GET", path, "1.1", 200), None)
    assert not f.filter(rec("/healthz"))
    assert f.filter(rec("/api/summary"))
    assert QuietHealthChecks in {type(x) for x in logging.getLogger("uvicorn.access").filters}


def test_ids_never_reused(engine):
    p = person_vec()
    t = 1_800_000_000
    ev(engine, t, [face(near(p))])
    engine.close_all()
    v = visits(engine)[0]
    sid = engine.db.one("SELECT MAX(id) AS m FROM sightings")["m"]
    engine.forget(v["identity_id"])
    ev(engine, t + 1000, [face(person_vec())])
    engine.close_all()
    w = visits(engine)[0]
    assert w["id"] > v["id"] and w["identity_id"] > v["identity_id"]
    assert engine.db.one("SELECT MIN(id) AS m FROM sightings")["m"] > sid


def test_old_database_migrated_keeping_rows(tmp_path):
    import sqlite3
    from smarteye import db as dbmod
    path = tmp_path / "old.db"
    old_schema = dbmod.SCHEMA.replace("PRIMARY KEY AUTOINCREMENT", "PRIMARY KEY")
    c = sqlite3.connect(path)
    c.executescript(old_schema)
    c.execute("INSERT INTO identities (id, kind, name, created_at) VALUES (5, 'unknown', 'Visitor 5', 1)")
    c.execute("INSERT INTO labels VALUES ('x', 5)")
    c.execute("INSERT INTO visits (id, camera, identity_id, start_ts, end_ts, touched_at) VALUES (9, 'FDoor', 5, 1, 2, 2)")
    c.execute("INSERT INTO sightings (id, event_id, visit_id, ts, camera, px, det, quality, usable) "
              "VALUES (40, 'e', 9, 1, 'FDoor', 50, 0.9, 0.3, 1)")
    c.commit()
    c.close()
    d = DB(path)
    for t in ("identities", "visits", "sightings"):
        assert "AUTOINCREMENT" in d.one("SELECT sql FROM sqlite_master WHERE name = ?", (t,))["sql"]
    assert d.one("SELECT name FROM identities WHERE id = 5")["name"] == "Visitor 5"
    assert d.one("SELECT identity_id FROM visits WHERE id = 9")["identity_id"] == 5
    assert d.one("SELECT visit_id FROM sightings WHERE id = 40")["visit_id"] == 9
    assert {r["name"] for r in d.q("SELECT name FROM sqlite_master WHERE type = 'index'")} >= {
        "visits_identity", "visits_start", "visits_open", "sightings_visit", "sightings_ts"}
    # Foreign keys still enforced and still cascading after the rebuild
    d.x("DELETE FROM visits WHERE id = 9")
    assert d.one("SELECT COUNT(*) AS n FROM sightings")["n"] == 0
    # New rows continue above the old ids; a second open changes nothing
    d.x("DELETE FROM identities WHERE id = 5")
    assert d.x("INSERT INTO identities (kind, name, created_at) VALUES ('unknown', 'n', 1)").lastrowid == 6
    DB(path)


# ---------- Home Assistant feed

from datetime import datetime
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")


def at(y, mo, d, h, mi=0) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=LONDON).timestamp()


def make_bus(engine, monkeypatch, **settings):
    from smarteye import mqtt as m
    monkeypatch.setattr(engine, "s", engine.s.__class__(**{**engine.s.__dict__, "mqtt_url": "mqtt://u:p@broker:1883",
                                                          **settings}))
    bus = m.Mqtt(engine, lambda sid: f"http://se/img/s/{sid}.jpg?k=x", lambda eid: f"http://se/img/e/{eid}.jpg?k=y")
    sent = []
    bus.pub = lambda topic, payload, retain=False: sent.append((topic, payload))
    engine.listeners.append(bus.on_engine)
    bus._on_connect(None, None, None, 0)
    return bus, sent


def events(sent):
    return [pl for tp, pl in sent if tp == "smarteye/event"]


def saturday_regular(engine, p, label=None, hour=10):
    """Four Saturday visits at 10:00 (or hour), 2 to 23 Jan 2027. Returns their identity."""
    for d in (2, 9, 16, 23):
        ev(engine, at(2027, 1, d, hour), [face(near(p), label=label)])
        engine.close_all()
    return visits(engine)[-1]["identity_id"]


def test_identity_brief_describes_visits_before_this_one(engine):
    p = person_vec()
    ident = saturday_regular(engine, p, label="cleaner")
    ev(engine, at(2027, 1, 30, 10, 20), [face(near(p), label="cleaner")])
    v = visits(engine)[-1]
    b = stats.identity_brief(engine, ident, at=v["start_ts"], visit=v["id"])
    assert b["visits"] == 4 and b["days_since_last"] == 7
    assert b["last_seen"].startswith("2027-01-23T10:00")
    assert b["usual"]["days"] == ["Sat"] and b["timing"] == "on_time"
    assert b["next_expected"] == "2027-01-30" and b["overdue"] is False
    assert "heatmap" not in b and "weekly" not in b
    w = b["usual"]
    assert stats.timing(engine, w, at(2027, 1, 30, 12)) == "late"
    assert stats.timing(engine, w, at(2027, 1, 30, 8)) == "early"
    assert stats.timing(engine, w, at(2027, 1, 30, 9, 45)) == "on_time"     # within the slack
    assert stats.timing(engine, w, at(2027, 1, 31, 10)) == "unusual_day"
    assert stats.timing(engine, None, at(2027, 1, 31, 10)) == "unknown"
    night = {"from": "23:30", "to": "00:30", "days": []}
    assert stats.timing(engine, night, at(2027, 1, 30, 0, 15)) == "on_time"
    assert stats.timing(engine, night, at(2027, 1, 30, 1, 30)) == "late"
    assert stats.timing(engine, night, at(2027, 1, 29, 22, 30)) == "early"


def test_arrival_carries_person_for_named_and_provisional(engine, monkeypatch):
    p, q = person_vec(), person_vec()
    saturday_regular(engine, p, label="cleaner")
    saturday_regular(engine, q, hour=14)
    engine.update_identity(visits(engine)[-1]["identity_id"], notes="Pay £15 cash")
    bus, sent = make_bus(engine, monkeypatch, camera_labels=(("FDoor", "Front door"),))
    sent.clear()
    ev(engine, at(2027, 1, 30, 10, 5), [face(near(p), label="cleaner")])
    a = events(sent)[0]
    assert a["event_type"] == "known_arrival" and not a["provisional"]
    assert a["person"]["visit_number"] == 5 and a["person"]["timing"] == "on_time"
    assert a["summary"].startswith("Cleaner is at the front door, 5th visit, usually Sat 10:00")
    assert "last here 7 days ago" in a["summary"]
    sent.clear()
    ev(engine, at(2027, 1, 30, 14), [face(near(q))], camera="FDoor")
    b = [e for e in events(sent) if e["event_type"] != "visit_ended"][0]
    assert b["event_type"] == "visitor_arrival" and b["provisional"] and b["visit_number"] == 5
    assert b["person"]["notes"] == "Pay £15 cash" and b["summary"].startswith("Probably Visitor")
    assert b["summary"].endswith("Note: Pay £15 cash")


def test_identified_fires_once_and_survives_restart(engine, monkeypatch):
    p = person_vec()
    t = 1_800_000_000
    ev(engine, t, [face(near(p))])
    engine.close_all()
    a = visits(engine)[0]["identity_id"]
    bus, sent = make_bus(engine, monkeypatch)
    sent.clear()
    ev(engine, t + 1000, [face(near(p), px=20)])           # too small to guess from
    assert [e["event_type"] for e in events(sent)] == ["unknown_arrival"]
    ev(engine, t + 1002, [face(near(p))])                    # now we can tell
    ident = [e for e in events(sent) if e["event_type"] == "identified"]
    assert len(ident) == 1 and ident[0]["identity"] == a and ident[0]["provisional"]
    ev(engine, t + 1004, [face(near(p))])
    assert len([e for e in events(sent) if e["event_type"] == "identified"]) == 1
    # A restart (new engine on the same database) doesn't announce it again
    again = Engine(engine.s, engine.db)
    heard = []
    again.listeners.append(lambda k, d: heard.append(k))
    again.process(EventIn(id="r1", ts=t + 1006, camera="FDoor", faces=[face(near(p))]))
    assert "identified" not in heard
    # An ImmichEye name mid-visit is a firm identification
    sent.clear()
    q = person_vec()
    ev(engine, t + 5000, [face(near(q), px=20)])
    ev(engine, t + 5002, [face(near(q), label="dan_lee")])
    ident = [e for e in events(sent) if e["event_type"] == "identified"]
    assert len(ident) == 1 and ident[0]["name"] == "Dan Lee" and not ident[0]["provisional"]


def test_changed_from_when_close_differs_from_announcement(engine, monkeypatch):
    p = person_vec()
    t = 1_800_000_000
    other = engine.new_visitor()
    bus, sent = make_bus(engine, monkeypatch)
    ev(engine, t, [face(near(p))])
    vid = visits(engine)[0]["id"]
    engine.db.x("UPDATE visits SET announced_id = ? WHERE id = ?", (other, vid))   # as if we'd guessed them
    sent.clear()
    engine.close_all()
    end = events(sent)[0]
    assert end["changed_from"] == {"identity": other, "name": "Visitor 1"}
    assert "not Visitor 1 as first thought" in end["summary"]


def test_per_door_topic_before_event_with_image_and_frame(engine, monkeypatch):
    bus, sent = make_bus(engine, monkeypatch)
    sent.clear()
    engine.process(EventIn(id="f1", ts=time.time(), camera="FDoor", faces=[face(person_vec())], frame=b"\xff\xd8f"))
    topics = [tp for tp, _ in sent]
    door = topics.index("smarteye/camera/FDoor/last_visitor")
    assert door < topics.index("smarteye/event")
    pl = sent[door][1]
    assert pl["image"].startswith("http://se/img/s/") and pl["frame"] == "http://se/img/e/f1.jpg?k=y"
    assert any(tp == "smarteye/camera/FDoor/present" and p["state"] == "ON" for tp, p in sent)
    # Discovery per enabled camera, with readable ids
    bus2, sent2 = make_bus(engine, monkeypatch)
    cfgs = {tp: pl for tp, pl in sent2 if tp.endswith("/config")}
    assert cfgs["homeassistant/binary_sensor/smarteye_cam_fdoor_present/config"]["default_entity_id"] == \
        "binary_sensor.smarteye_fdoor_visitor"
    assert cfgs["homeassistant/sensor/smarteye_visits_today/config"]["state_class"] == "total_increasing"
    for key in ("expected_today", "overdue", "yesterday", "new_visitors_7d"):
        assert f"homeassistant/sensor/smarteye_{key}/config" in cfgs


def test_doorbell_rings_join_visits(engine, monkeypatch):
    bus, sent = make_bus(engine, monkeypatch, doorbell_token="bell")
    now = time.time()
    p = person_vec()
    # Rung while someone's at the door
    ev(engine, now - 600, [face(near(p))])
    sent.clear()
    rid, v = engine.ring("FDoor", now - 590)
    assert v and v["rang"] and events(sent)[0]["event_type"] == "doorbell"
    engine.close_all()
    # Rung just before the first face: the visit claims it
    engine.ring("FDoor", now - 300)
    sent.clear()
    ev(engine, now - 290, [face(person_vec())])
    assert events(sent)[0]["rang"] is True
    engine.close_all()
    # Rung, and nobody came within the window
    engine.ring("FDoor", now - 200)
    ev(engine, now - 100, [face(person_vec())])
    engine.close_all()
    last = visits(engine)[-1]
    assert not engine.visit_summary(last["id"])["rang"]
    today = stats.summary(engine)["today"]
    if stats.midnight(engine, now) <= now - 600:
        assert today["rings"] == 3 and today["unanswered"] == 1
    ended = events(sent)[-1]
    assert "didn't ring" in ended["summary"]


def test_doorbell_api(tmp_path, monkeypatch):
    server = _client(tmp_path, monkeypatch, SMARTEYE_AUTH_MODE="authelia", SMARTEYE_TRUSTED_PROXIES="172.25.0.10")
    with TestClient(server.app) as c:
        assert c.post("/api/doorbell", json={"camera": "FDoor"}).status_code == 404     # not configured
    server = _client(tmp_path, monkeypatch, SMARTEYE_AUTH_MODE="authelia", SMARTEYE_TRUSTED_PROXIES="172.25.0.10",
                     SMARTEYE_DOORBELL_TOKEN="bell")
    with TestClient(server.app) as c:
        assert c.post("/api/doorbell", json={"camera": "FDoor"}).status_code == 401
        h = {"Authorization": "Bearer bell"}
        assert c.post("/api/doorbell", json={"nope": 1}, headers=h).status_code == 400
        r = c.post("/api/doorbell", json={"camera": "FDoor"}, headers=h)
        assert r.status_code == 202 and r.json()["visit"] is None


def test_signed_frame_links(tmp_path, monkeypatch):
    server = _client(tmp_path, monkeypatch, SMARTEYE_AUTH_MODE="authelia", SMARTEYE_TRUSTED_PROXIES="172.25.0.10",
                     SMARTEYE_PUBLIC_URL="http://se")
    with TestClient(server.app) as c:
        e = server.app.state.engine
        e.process(EventIn(id="f1", ts=time.time(), camera="FDoor", faces=[face(person_vec())], frame=b"\xff\xd8f"))
        assert server.frame_url("f1") == f"http://se/img/e/f1.jpg?k={server.sign('e:f1')}"
        assert c.get("/img/e/f1.jpg").status_code == 403                       # no login, no signature
        assert c.get("/img/e/f1.jpg?k=wrong").status_code == 403
        r = c.get(f"/img/e/f1.jpg?k={server.sign('e:f1')}")
        assert r.status_code == 200 and r.content == b"\xff\xd8f"
        sid = e.db.one("SELECT id FROM sightings")["id"]
        assert c.get(f"/img/e/f1.jpg?k={server.sign(sid)}").status_code == 403  # a crop's key isn't a frame's


def test_forget_blanks_last_visitor_topics(engine, monkeypatch):
    bus, sent = make_bus(engine, monkeypatch)
    ev(engine, time.time(), [face(person_vec())])
    engine.close_all()
    ident = visits(engine)[0]["identity_id"]
    assert [pl for tp, pl in sent if tp == "smarteye/last_visitor"][-1]["identity"] == ident
    sent.clear()
    engine.forget(ident)
    assert ("smarteye/last_visitor", {}) in sent and ("smarteye/camera/FDoor/last_visitor", {}) in sent


def test_regular_visitors_get_readable_person_sensors(engine, monkeypatch):
    p = person_vec()
    t = 1_800_000_000
    for d in range(5):
        ev(engine, t + d * 86400, [face(near(p))])
        engine.close_all()
    ident = visits(engine)[0]["identity_id"]
    bus, sent = make_bus(engine, monkeypatch)
    cfg = dict(sent)[f"homeassistant/sensor/smarteye_person_{ident}/config"]
    assert cfg["unique_id"] == f"smarteye_person_{ident}" and cfg["default_entity_id"] == "sensor.smarteye_visitor_1"
    state = dict(sent)[f"smarteye/person/{ident}"]
    assert state["visits"] == 5 and state["median_gap_days"] == 1 and "timing" not in state


def test_ignored_people_left_out_of_counts_and_reports(engine):
    now = time.time()
    p = person_vec()
    ev(engine, now - 60, [face(near(p), label="alex")])
    ev(engine, now - 30, [face(person_vec())])
    engine.close_all()
    alex = engine.db.one("SELECT identity_id FROM labels WHERE label = 'alex'")["identity_id"]
    engine.update_identity(alex, ignored=True)
    today = stats.summary(engine)["today"]
    if stats.midnight(engine, now) <= now - 60:
        assert today["visits"] == 1 and today["household"] == 1 and today["people"] == 1


def test_reports_expected_overdue_yesterday(engine):
    p, q = person_vec(), person_vec()
    saturday_regular(engine, p, label="cleaner")                 # Saturdays, weekly: due 30 Jan
    ev(engine, at(2027, 1, 29, 23, 15), [face(near(q))])        # Friday night, a stranger
    engine.close_all()
    r = stats.reports(engine, now=at(2027, 1, 30, 8))
    assert [x["name"] for x in r["expected_today"]["people"]] == ["Cleaner"]
    assert r["overdue"]["count"] == 0
    y = r["yesterday"]
    assert y["visits"] == 1 and y["new_visitors"] == 1
    assert y["notable"][0]["why"] == ["new_visitor"] and y["notable"][0]["time"] == "23:15"
    # Three weeks on, overdue; months on, lapsed off the list
    assert [x["name"] for x in stats.reports(engine, now=at(2027, 2, 13, 12))["overdue"]["people"]] == ["Cleaner"]
    assert stats.reports(engine, now=at(2027, 6, 1, 12))["overdue"]["count"] == 0
    # Ignored people are never expected or overdue
    engine.update_identity(engine.db.one("SELECT identity_id FROM labels")["identity_id"], ignored=True)
    assert stats.reports(engine, now=at(2027, 1, 30, 8))["expected_today"]["count"] == 0


def test_announced_column_added_to_old_database(tmp_path):
    import sqlite3
    from smarteye import db as dbmod
    path = tmp_path / "old.db"
    old = dbmod.SCHEMA.replace(",\n    announced_id INTEGER", "")
    assert old != dbmod.SCHEMA
    c = sqlite3.connect(path)
    c.executescript(old)
    c.close()
    d = DB(path)
    assert "announced_id" in {r[1] for r in d.q("PRAGMA table_info(visits)")}


# ---------- fixes from the 2026-10-26 code review

def test_a_failed_begin_releases_the_lock(tmp_path):
    import sqlite3
    import threading
    db = DB(tmp_path / "x.db")
    db.conn.execute("PRAGMA busy_timeout = 50")
    other = sqlite3.connect(tmp_path / "x.db", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")                  # another writer holds the database
    with pytest.raises(sqlite3.OperationalError):
        with db.tx():
            pass
    other.execute("ROLLBACK")
    got = []
    t = threading.Thread(target=lambda: got.append(db.lock.acquire(timeout=1) and db.lock.release() is None))
    t.start()
    t.join(2)
    assert got == [True]                               # another thread can still use the database
    with db.tx():
        db.put("k", "v")
    assert db.get("k") == "v"


def test_event_ids_and_paths_stay_inside_the_data_folder(engine, tmp_path):
    good = {"v": 1, "ts": 1_800_000_000, "camera": "FDoor", "faces": []}
    for eid in ("3f2c9a4e-1b7d-4c1e-9a55-0d9e6b7c8a21", "imm-1792933740.674548"):
        assert parse_event({**good, "id": eid}).id == eid
    for eid in ("../../crops/20261026/123", "a/b", "..", ".hidden", "x" * 200, "a\\b"):
        with pytest.raises(EventError):
            parse_event({**good, "id": eid})
    with pytest.raises(EventError):
        engine._save("frames", 1_800_000_000, "../../../escape", b"x")
    assert not (tmp_path.parent / "escape.jpg").exists()


def test_malformed_events_are_refused_cleanly():
    base = {"v": 1, "ts": 1_800_000_000, "camera": "FDoor"}
    for bad in ({**base, "faces": [{"box": [1, 2, 3]}]},
                {**base, "faces": ["face"]},
                {**base, "ts": float("nan"), "faces": []},
                {**base, "ts": float("inf"), "faces": []},
                {**base, "camera": ["FDoor"], "faces": []}):
        with pytest.raises(EventError):
            parse_event(bad)


def test_person_sensors_go_when_retention_or_an_offline_forget_removes_them(engine, monkeypatch):
    p, q = person_vec(), person_vec()
    t = 1_800_000_000
    for d in range(5):
        ev(engine, t + d * 86400, [face(near(p))])
        engine.close_all()
    a = visits(engine)[0]["identity_id"]
    bus, sent = make_bus(engine, monkeypatch)
    assert f"smarteye/person/{a}" in dict(sent)
    sent.clear()
    engine.purge(now=t + 400 * 86400)                  # every visit past retention
    assert (f"homeassistant/sensor/smarteye_person_{a}/config", "") in sent
    assert (f"smarteye/person/{a}", "") in sent
    # Forgotten while MQTT was down, and SmartEye restarted before it came back
    for d in range(5):
        ev(engine, t + (500 + d) * 86400, [face(near(q))])
        engine.close_all()
    b = visits(engine)[-1]["identity_id"]
    bus.refresh_all()
    bus._on_disconnect()
    assert not bus.connected
    engine.forget(b)
    bus2, sent2 = make_bus(engine, monkeypatch)        # a new process: knows b's sensor from the database
    assert (f"homeassistant/sensor/smarteye_person_{b}/config", "") in sent2


def test_merge_or_forget_mid_visit_gives_no_false_changed_from(engine):
    p, q = person_vec(), person_vec()
    t = 1_800_000_000
    for d in range(3):
        ev(engine, t + d * 86400, [face(near(p))])
        engine.close_all()
    a = visits(engine)[0]["identity_id"]
    ev(engine, t + 10 * 86400, [face(near(q))])
    engine.close_all()
    b = visits(engine)[-1]["identity_id"]
    heard = []
    engine.listeners.append(lambda k, d: heard.append((k, d)))
    ev(engine, t + 20 * 86400, [face(near(p))])         # announced as a
    assert heard[-1][0] == "arrival" and heard[-1][1]["identity"] == a
    engine.merge(a, b)                                   # a and b are one person after all
    engine.close_all()
    done = [d for k, d in heard if k == "visit"][-1]
    assert done["identity"] == b and "changed_from" not in done
    heard.clear()
    ev(engine, t + 30 * 86400, [face(near(p))])
    assert heard[0][1]["identity"] == b
    engine.forget(b)                                     # forgotten mid-visit
    engine.close_all()
    done = [d for k, d in heard if k == "visit"][-1]
    assert "changed_from" not in done


def test_an_older_refs_query_never_overwrites_a_newer_cache(engine):
    p = person_vec()
    ev(engine, 1_800_000_000, [face(near(p))])
    engine.close_all()
    q = engine.db.q
    def q_then_commit_elsewhere(sql, args=()):
        rows = q(sql, args)
        engine.invalidate()            # another thread committed a change while this query ran
        return rows
    engine.db.q = q_then_commit_elsewhere
    refs, owners = engine.refs()
    assert len(owners) == 1 and engine._refs is None    # used, but not kept
    engine.db.q = q
    engine.refs()
    assert engine._refs is not None


def test_visit_counts_refresh_without_the_full_summary(engine, monkeypatch):
    bus, sent = make_bus(engine, monkeypatch)
    monkeypatch.setattr(stats, "summary", lambda e: pytest.fail("refresh ran the full summary"))
    sent.clear()
    ev(engine, time.time(), [face(person_vec())])
    assert dict(sent)["smarteye/today"]["visits"] == 1


def test_similar_and_candidates_name_only_whats_shown(engine):
    from smarteye.server import identity_cards, similar
    p = person_vec()
    t = 1_800_000_000
    for d in (0, 30):                                  # one person split into two visitors
        ev(engine, t + d * 86400, [face(near(p, 0.9))])
        engine.close_all()
        engine.db.x("UPDATE visits SET how = 'manual', identity_id = ? WHERE id = (SELECT MAX(id) FROM visits)",
                    (engine.new_visitor(),))
        engine.invalidate()
    for _ in range(3):
        engine.new_visitor()                           # strangers with no faces
    ids = sorted({v["identity_id"] for v in visits(engine)})
    out = similar(engine, ids[0])
    assert [o["id"] for o in out] == [ids[1]] and out[0]["name"].startswith("Visitor")
    assert identity_cards(engine, []) == {} and set(identity_cards(engine, ids)) == set(ids)


# ---------- Home Assistant gaps from docs/home-assistant-data.md §9

def test_household_kept_off_last_visitor(engine, monkeypatch):
    bus, sent = make_bus(engine, monkeypatch)
    t = time.time() - 1000
    ev(engine, t, [face(person_vec())])
    engine.close_all()
    visitor = visits(engine)[0]["identity_id"]
    me = person_vec()
    ev(engine, t + 500, [face(me)])
    engine.close_all()
    household = visits(engine)[1]["identity_id"]
    engine.update_identity(household, name="Alex", ignored=True)
    last = [pl for tp, pl in sent if tp == "smarteye/last_visitor"][-1]
    assert last["identity"] == visitor
    sent.clear()
    ev(engine, t + 900, [face(near(me))])
    engine.close_all()
    assert not [pl for tp, pl in sent if tp == "smarteye/last_visitor"]


def test_unrecognised_visitor_has_a_readable_door_state(engine, monkeypatch):
    from smarteye import mqtt as m
    bus, sent = make_bus(engine, monkeypatch)
    cfgs = {tp: pl for tp, pl in sent if tp.endswith("/config")}
    for oid in ("smarteye_last_visitor", "smarteye_cam_fdoor_last_visitor"):
        assert cfgs[f"homeassistant/sensor/{oid}/config"]["value_template"] == m.LAST_VISITOR_STATE
    sent.clear()
    ev(engine, time.time(), [face(person_vec(), px=20)])   # too small to identify
    door = [pl for tp, pl in sent if tp == "smarteye/camera/FDoor/last_visitor"][-1]
    assert door["name"] is None and "visit" in door   # the template shows "Unrecognised"


def test_door_pictures_go_over_mqtt_as_jpeg(engine, monkeypatch):
    bus, sent = make_bus(engine, monkeypatch)
    cfgs = {tp: pl for tp, pl in sent if tp.endswith("/config")}
    face_cfg = cfgs["homeassistant/image/smarteye_cam_fdoor_face/config"]
    assert face_cfg["image_topic"] == "smarteye/camera/FDoor/face" and face_cfg["content_type"] == "image/jpeg"
    assert face_cfg["default_entity_id"] == "image.smarteye_fdoor_face"
    assert "homeassistant/image/smarteye_cam_fdoor_frame/config" in cfgs
    sent.clear()
    p = person_vec()
    engine.process(EventIn(id="f1", ts=time.time(), camera="FDoor", faces=[face(p, crop=b"\xff\xd8face")],
                           frame=b"\xff\xd8frame"))
    topics = [tp for tp, _ in sent]
    # Pictures, then the door's topic, then the event
    assert topics.index("smarteye/camera/FDoor/face") < topics.index("smarteye/camera/FDoor/last_visitor") \
        < topics.index("smarteye/event")
    pics = dict((tp, pl) for tp, pl in sent if tp.endswith(("/face", "/frame")))
    assert pics == {"smarteye/camera/FDoor/face": b"\xff\xd8face", "smarteye/camera/FDoor/frame": b"\xff\xd8frame"}
    # The same best face again: no resend
    sent.clear()
    engine.close_all()
    assert not [tp for tp, _ in sent if tp.endswith("/face")]
    # Forgotten: the retained pictures are cleared
    sent.clear()
    engine.forget(visits(engine)[0]["identity_id"])
    assert ("smarteye/camera/FDoor/face", b"") in sent and ("smarteye/camera/FDoor/frame", b"") in sent


def test_unidentified_visit_ends_with_visit_ended(engine, monkeypatch):
    bus, sent = make_bus(engine, monkeypatch)
    sent.clear()
    ev(engine, time.time(), [face(person_vec(), px=20)])   # too small to identify
    engine.close_all()
    types = [e["event_type"] for e in events(sent)]
    assert types == ["unknown_arrival", "visit_ended"]
    ended = events(sent)[-1]
    assert ended["identity"] is None and ended["summary"].startswith("Someone not recognised was at")
    assert not [tp for tp, _ in sent if tp == "smarteye/last_visitor"]


def test_household_recognised_late_leaves_the_door(engine, monkeypatch):
    bus, sent = make_bus(engine, monkeypatch)
    t = time.time() - 2000
    ev(engine, t, [face(person_vec(), crop=b"\xff\xd8visitor")])          # a visitor, shown on the door
    engine.close_all()
    me = person_vec()
    ev(engine, t + 500, [face(me, crop=b"\xff\xd8me")])
    engine.close_all()
    household = visits(engine)[1]["identity_id"]
    engine.update_identity(household, name="Alex", ignored=True)
    sent.clear()
    # Arrives too small to recognise: announced as unrecognised, on the door
    ev(engine, t + 1500, [face(near(me), px=20, crop=b"\xff\xd8tiny")])
    assert ("smarteye/camera/FDoor/face", b"\xff\xd8tiny") in sent
    sent.clear()
    # ...then recognised: household_arrival, and the door goes back to the visitor
    ev(engine, t + 1503, [face(near(me), crop=b"\xff\xd8me2")])
    assert [e["event_type"] for e in events(sent)] == ["household_arrival"]
    door = [pl for tp, pl in sent if tp == "smarteye/camera/FDoor/last_visitor"][-1]
    assert door["visit"] == visits(engine)[0]["id"]
    assert ("smarteye/camera/FDoor/face", b"\xff\xd8visitor") in sent


def test_a_stray_frame_beside_a_visit_is_folded_into_it(engine, monkeypatch):
    bus, sent = make_bus(engine, monkeypatch)
    t = time.time() - 2000
    ev(engine, t, [face(person_vec(), crop=b"\xff\xd8before")])            # an earlier visitor on the door
    engine.close_all()
    me = person_vec()
    for i in range(3):
        ev(engine, t + 500 + i, [face(near(me))])
    engine.close_all()
    engine.update_identity(visits(engine)[1]["identity_id"], name="Sam", ignored=True)
    n_ids = engine.db.one("SELECT COUNT(*) AS n FROM identities")["n"]
    sent.clear()
    # Sam leaves; two seconds in, one frame of the top of their head matches no one
    ev(engine, t + 1000, [face(near(me))])
    ev(engine, t + 1002, [face(person_vec(), crop=b"\xff\xd8head")])
    ev(engine, t + 1004, [face(near(me))])
    assert ("smarteye/camera/FDoor/face", b"\xff\xd8head") in sent
    sent.clear()
    engine.close_all()
    vs = visits(engine)
    assert len(vs) == 3 and vs[2]["name"] == "Sam"
    assert engine.db.one("SELECT COUNT(*) AS n FROM sightings WHERE visit_id = ?", (vs[2]["id"],))["n"] == 3
    assert engine.db.one("SELECT COUNT(*) AS n FROM identities")["n"] == n_ids      # no new visitor
    folded = engine.db.q("SELECT folded_from FROM sightings WHERE visit_id = ? AND folded_from IS NOT NULL", (vs[2]["id"],))
    assert len(folded) == 1 and folded[0]["folded_from"] not in {v["id"] for v in vs}
    from smarteye import server
    assert server.visits_query(engine, vid=vs[2]["id"])[0]["folded"] == 1
    assert not events(sent)                                                       # no end event for it
    assert ("smarteye/camera/FDoor/face", b"\xff\xd8before") in sent             # the door goes back
    door = [pl for tp, pl in sent if tp == "smarteye/camera/FDoor/last_visitor"][-1]
    assert door["visit"] == vs[0]["id"]


def test_strangers_together_are_not_folded(engine):
    t = 1_800_000_000
    p = person_vec()
    for i in range(4):
        ev(engine, t + i, [face(near(p))])
    ev(engine, t + 20, [face(person_vec())])                         # well apart in time: a visitor of its own
    ev(engine, t + 200, [face(near(p)), face(person_vec())])         # in one frame: two people
    ev(engine, t + 202, [face(near(p))])
    engine.close_all()
    assert [v["how"] for v in visits(engine)] == ["new", "new", "matched", "new"]


def test_merges_are_recorded_on_the_identity_kept(engine):
    p, q, r = person_vec(), person_vec(), person_vec()
    t = 1_800_000_000
    for i, v in enumerate((p, q, q, r)):
        ev(engine, t + i * 1000, [face(near(v))])
        engine.close_all()
    a, b, _, c = (v["identity_id"] for v in visits(engine))
    names = {i: engine.db.one("SELECT name FROM identities WHERE id = ?", (i,))["name"] for i in (a, b, c)}
    engine.merge(b, a)
    engine.merge(a, c)          # a's own history goes with it
    rows = engine.db.q("SELECT identity_id, name, visits FROM merges ORDER BY id")
    assert [(x["identity_id"], x["name"], x["visits"]) for x in rows] == [(c, names[b], 2), (c, names[a], 3)]
    from smarteye import server
    shown = {v["id"]: v["merged_from"] for v in server.visits_query(engine)}
    assert sorted(shown.values(), key=str) == [None, names[a], names[b], names[b]]   # first merge that moved each
    engine.forget(c)
    assert not engine.db.q("SELECT 1 FROM merges")


def captioner(engine, monkeypatch, reply='{"summary": "A man with a parcel.", "role": "delivery", '
              '"role_confidence": "medium", "carrying": ["parcel"]}'):
    """A Captioner with a fake model: the requests it got, and replies of reply (or reply(body))."""
    from smarteye.caption import Captioner
    monkeypatch.setattr(engine, "s", engine.s.__class__(**{**engine.s.__dict__, "caption_model": "vision-test"}))
    calls = []

    def post(body):
        calls.append(body)
        return {"message": {"content": reply(body) if callable(reply) else reply}}
    cap = Captioner(engine, post=post)
    engine.listeners.append(cap.on_engine)
    return cap, calls


def framed(engine, ts, f, eid):
    return engine.process(EventIn(id=eid, ts=ts, camera="FDoor", faces=[f], frame=b"\xff\xd8frame"))


def test_a_visit_is_described_and_described_again_once_named(engine, monkeypatch):
    from smarteye import server
    cap, calls = captioner(engine, monkeypatch)
    p = person_vec()
    t = time.time()
    framed(engine, t, face(p), "c1")
    cap.drain()
    assert len(calls) == 1 and calls[0]["model"] == "vision-test"
    user = calls[0]["messages"][1]
    assert "nobody recognised" in user["content"] and user["images"] == [base64.b64encode(b"\xff\xd8frame").decode()]
    v = engine.now(visits(engine)[0]["id"])
    assert v["caption"]["summary"] == "A man with a parcel." and v["caption"]["carrying"] == ["parcel"]
    assert v["caption"]["for"] is None
    # More faces of the same stranger: nothing new to say
    framed(engine, t + 2, face(near(p)), "c2")
    cap.drain()
    assert len(calls) == 1
    # ImmichEye names them mid-visit: described again, by name
    framed(engine, t + 4, face(near(p), label="Sam Carter"), "c3")
    cap.drain()
    assert len(calls) == 2 and "Sam Carter" in calls[1]["messages"][1]["content"]
    engine.close_all()
    cap.drain()
    assert len(calls) == 2     # closed as the same person: kept
    shown = server.visits_query(engine)[0]["caption"]
    assert shown["for"] == visits(engine)[0]["identity_id"] and shown["model"] == "vision-test"


def test_a_bad_reply_leaves_no_description(engine, monkeypatch):
    from smarteye.caption import parse
    cap, calls = captioner(engine, monkeypatch, reply="I can't help with that.")
    framed(engine, time.time(), face(person_vec()), "b1")
    cap.drain()
    assert len(calls) == 1 and engine.now(visits(engine)[0]["id"])["caption"] is None
    assert engine.counters["caption_errors"] == 1
    # Fenced or chatty JSON is fine; an unknown role becomes "unknown"
    d = parse('Sure!\n```json\n{"summary": "Chris next door.", "role": "spy", "carrying": "a cake"}\n```')
    assert d["summary"] == "Chris next door." and d["role"] == "unknown" and d["carrying"] == []


def test_household_context_and_described_events(engine, monkeypatch):
    bus, sent = make_bus(engine, monkeypatch)
    cap, calls = captioner(engine, monkeypatch, reply=lambda body: json.dumps(
        {"summary": "Alex is back with the shopping." if "lives here" in body["messages"][1]["content"]
         else "Royal Mail with a large parcel.", "role": "postal", "carrying": ["parcel"], "weather": "raining"}))
    t = time.time() - 3000
    post = person_vec()
    framed(engine, t, face(post, label="royal_mail"), "d1")
    sent.clear()
    cap.drain()
    described = events(sent)
    assert [e["event_type"] for e in described] == ["described"]
    assert described[0]["description"] == "Royal Mail with a large parcel."
    assert described[0]["scene"]["weather"] == "raining" and described[0]["name"] == "Royal Mail"
    door = [pl for tp, pl in sent if tp == "smarteye/camera/FDoor/last_visitor"][-1]
    assert door["description"] == "Royal Mail with a large parcel."
    sent.clear()
    engine.close_all()
    assert events(sent)[0]["description"] == "Royal Mail with a large parcel."   # the end event carries it
    # The household gets its own event type, and stays off the door
    me = person_vec()
    framed(engine, t + 1000, face(me, label="Alex Carter"), "d2")
    engine.close_all()
    cap.drain()
    engine.update_identity(visits(engine)[1]["identity_id"], ignored=True)
    framed(engine, t + 2000, face(near(me), label="Alex Carter"), "d3")
    sent.clear()
    cap.drain()
    assert "Alex Carter (lives here)" in calls[-1]["messages"][1]["content"]
    assert [e["event_type"] for e in events(sent)] == ["household_described"]
    assert not [pl for tp, pl in sent if tp == "smarteye/camera/FDoor/last_visitor"]


def test_a_visit_folded_away_before_its_description_is_skipped(engine, monkeypatch):
    cap, calls = captioner(engine, monkeypatch)
    framed(engine, time.time(), face(person_vec()), "g1")
    engine.delete_visit(visits(engine)[0]["id"])
    cap.drain()
    assert calls == []


def test_people_arriving_together_are_described_together(engine, monkeypatch):
    cap, calls = captioner(engine, monkeypatch)
    t = time.time()
    engine.process(EventIn(id="t1", ts=t, camera="FDoor", frame=b"\xff\xd8frame",
                           faces=[face(person_vec(), label="Alex Carter"), face(person_vec(), label="Sam Carter")]))
    later = person_vec()
    framed(engine, t + 600, face(later), "t2")                 # someone else, later: not with them
    cap.drain()
    alex, sam, other = visits(engine)
    asked = {c["messages"][1]["content"].split("Face recognition: ")[1].split(".")[0]: c["messages"][1]["content"]
             for c in calls}
    assert "Also at the door: Sam Carter." in asked["Alex Carter"]
    assert "Also at the door: Alex Carter." in asked["Sam Carter"]
    assert "Also at the door" not in asked["nobody recognised"]
    assert engine.now(alex["id"])["caption"]["with"] == [{"visit": sam["id"], "identity": sam["identity_id"],
                                                         "name": "Sam Carter"}]
    assert engine.now(other["id"])["caption"]["with"] == []
