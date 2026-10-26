"""Home Assistant over MQTT, with discovery.

Entities (one "SmartEye" device):
  event.smarteye_visitor           arrivals: known_arrival (an Immich person), visitor_arrival (a visitor
                                   seen before), unknown_arrival; identified (an open visit's guess firmed
                                   up or changed); doorbell (the bell rang during a visit); when a visit
                                   ends: new_visitor, unusual_time (a regular at an hour they never come)
                                   or visit_ended (none for a stray frame folded into another visit);
                                   household_arrival (an ignored identity, at arrival or once recognised)
  binary_sensor.smarteye_visitor_present   on while any visit is open; attributes list who
  binary_sensor.smarteye_<camera>_visitor  the same, one per enabled camera
  sensor.smarteye_last_visitor     name of the last identified visitor, household excepted; attributes with details
  sensor.smarteye_<camera>_last_visitor    the latest visitor at that door, from arrival on, with signed
                                   image and frame links
  image.smarteye_<camera>_face, _frame     that visitor's face crop and whole frame: raw JPEG over MQTT
                                   (smarteye/camera/<camera>/face, /frame, retained), so HA serves them
                                   through its own login with no fetch back to SmartEye
  sensor.smarteye_visits_today, _unknown_today, _people_today
  sensor.smarteye_expected_today, _overdue, _yesterday, _new_visitors_7d
  sensor.smarteye_<name>           one per named identity and regular visitor: last seen, with what
                                   SmartEye knows about them (usual times, regularity, notes)

Every event carries "person" (that intel, as of the visit) and "summary", a plain-English line
of facts for an announcer. Ignored identities (the household) update their own sensor and
presence, and fire only household_arrival, so other automations never hear them. They don't
count in today's figures.
"""
import json
import logging
import re
import time
from urllib.parse import unquote, urlparse

import paho.mqtt.client as mqtt

from . import stats
from .engine import Engine, EventError
from .stats import iso

log = logging.getLogger(__name__)
EVENT_TYPES = ["known_arrival", "visitor_arrival", "unknown_arrival", "identified", "doorbell",
               "new_visitor", "unusual_time", "visit_ended", "household_arrival"]
REGULAR_VISITS = 5      # an unnamed visitor gets a sensor from this many visits
BRIEF_TTL_S = 10        # arrival, identified and close can come seconds apart
# A door's last visitor: their name, "Unrecognised" if nobody was identified, "none" before anyone came.
# .get() and `in`, not attribute access: the payload is {} before anyone came, and HA warns on a missing key
LAST_VISITOR_STATE = "{{ value_json.get('name') or ('Unrecognised' if 'visit' in value_json else 'none') }}"
TIMING = {"on_time": "on time", "early": "early", "late": "late", "unusual_day": "not on their usual day"}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "person"


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def ago(days: int) -> str:
    if days == 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return f"{round(days / 30)} months ago"


def duration(s: int) -> str:
    if s < 90:
        return "1 second" if s == 1 else f"{s} seconds"
    return f"{round(s / 60)} minutes"


def describe(p: dict, stage: str, doorbell: bool) -> str:
    """The facts of an event in one line, e.g. "Window cleaner is at the front door, 15th
    visit, usually Sat 09:30–11:30, last here 4 weeks ago, on time." No tone: HA adds that."""
    door = p["door"]
    where = f"the {door[:1].lower()}{door[1:]}" if door != p["camera"] else door
    who = p["name"] if p.get("identity") is not None else "Someone not recognised"
    if p.get("provisional"):
        who = f"Probably {who}"
    verb = {"visit": "was at", "doorbell": "is ringing at"}.get(stage, "is at")
    parts = [f"{who} {verb} {where}" + (f" for {duration(p['duration_s'])}" if stage == "visit" else "")]
    if p.get("changed_from"):
        parts.append(f"not {p['changed_from']['name']} as first thought")
    per = p.get("person")
    if per:
        if p.get("new_visitor"):
            parts.append("a new visitor")
        elif p.get("visit_number"):
            parts.append("first visit" if p["visit_number"] == 1 else f"{ordinal(p['visit_number'])} visit")
        u = per.get("usual")
        if u:
            parts.append("usually " + (f"{'/'.join(u['days'])} " if u["days"] else "") + f"{u['from']}–{u['to']}")
        if per.get("days_since_last") is not None:
            parts.append(f"last here {ago(per['days_since_last'])}")
        if per.get("timing") in TIMING:
            parts.append(TIMING[per["timing"]])
        if p.get("unusual_time"):
            parts.append("at an unusual time for them")
    if p.get("rang") and stage != "doorbell":
        parts.append("rang the bell")
    elif stage == "visit" and doorbell:
        parts.append("didn't ring")
    text = ", ".join(parts) + "."
    if per and per.get("notes"):
        text += f" Note: {per['notes']}"
    return text


class Mqtt:
    def __init__(self, engine: Engine, image_url, frame_url=None):
        self.e = engine
        s = engine.s
        self.p = s.mqtt_prefix
        self.disc = s.ha_discovery
        self.image_url = image_url          # sighting id -> URL Home Assistant can fetch, or None
        self.frame_url = frame_url          # event id -> URL of its frame, or None
        self._briefs: dict[tuple[int, int | None], tuple[float, dict]] = {}
        # Identities with a person sensor. Kept in the database, so a sensor published before a
        # restart is still removed once its identity is gone or no longer a regular.
        self._people: set[int] = set(json.loads(engine.db.get("mqtt.people", "[]")))
        self._cams: set[str] = set()        # cameras with per-door entities
        self._pics: dict[str, tuple] = {}   # camera -> (sighting, event) whose JPEGs are on its image topics
        self._shown: dict[str, int] = {}    # camera -> visit its last-visitor topic shows
        url = urlparse(s.mqtt_url)
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{self.p}-server")
        if url.username:
            self.client.username_pw_set(unquote(url.username), unquote(url.password or ""))
        if url.scheme == "mqtts":
            self.client.tls_set()
        self.client.will_set(f"{self.p}/status", "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.connected = False
        self.host, self.port = url.hostname, url.port or (8883 if url.scheme == "mqtts" else 1883)

    def start(self) -> None:
        self.client.reconnect_delay_set(1, 60)
        self.client.connect_async(self.host, self.port)
        self.client.loop_start()
        self.e.listeners.append(self.on_engine)

    def stop(self) -> None:
        self.pub(f"{self.p}/status", "offline", retain=True)
        self.client.loop_stop()
        self.client.disconnect()

    def pub(self, topic: str, payload, retain: bool = False) -> None:
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload)
        self.client.publish(topic, payload, qos=1, retain=retain)

    # ---------- discovery

    @property
    def device(self) -> dict:
        return {"identifiers": [self.p], "name": "SmartEye", "manufacturer": "SmartEye", "model": "Visitor intelligence"}

    def _config(self, component: str, object_id: str, cfg: dict, entity: str | None = None) -> None:
        """Discovery for one entity. unique_id stays object_id; entity is the readable id HA
        gives it when first created (renames later never orphan it)."""
        if not self.disc:
            return
        cfg = {"unique_id": object_id, "default_entity_id": f"{component}.{entity or object_id}", "device": self.device,
               "availability_topic": f"{self.p}/status", **cfg}
        self.pub(f"{self.disc}/{component}/{object_id}/config", cfg, retain=True)

    def _on_connect(self, client, userdata, flags, rc, props=None) -> None:
        if rc != 0:
            log.warning("MQTT connect failed: %s", rc)
            return
        self.connected = True
        log.info("MQTT connected to %s:%s", self.host, self.port)
        p = self.p
        self._config("event", f"{p}_visitor", {
            "name": "Visitor", "state_topic": f"{p}/event", "event_types": EVENT_TYPES, "icon": "mdi:doorbell-video"})
        self._config("binary_sensor", f"{p}_visitor_present", {
            "name": "Visitor present", "state_topic": f"{p}/present", "device_class": "occupancy",
            "value_template": "{{ value_json.state }}", "json_attributes_topic": f"{p}/present"})
        self._config("sensor", f"{p}_last_visitor", {
            "name": "Last visitor", "state_topic": f"{p}/last_visitor",
            "value_template": LAST_VISITOR_STATE,
            "json_attributes_topic": f"{p}/last_visitor", "icon": "mdi:account-clock"})
        for key, name in (("visits", "Visits today"), ("unknown", "Unknown visits today"), ("people", "People today")):
            self._config("sensor", f"{p}_{key}_today", {
                "name": name, "state_topic": f"{p}/today", "value_template": f"{{{{ value_json.{key} }}}}",
                "state_class": "total_increasing", "icon": "mdi:counter",
                **({"json_attributes_topic": f"{p}/today"} if key == "visits" else {})})
        for key, name, icon in (("expected_today", "Expected today", "mdi:calendar-account"),
                                ("overdue", "Overdue", "mdi:account-clock-outline"),
                                ("yesterday", "Yesterday", "mdi:calendar-arrow-left")):
            self._config("sensor", f"{p}_{key}", {
                "name": name, "state_topic": f"{p}/reports/{key}",
                "value_template": "{{ value_json.%s }}" % ("visits" if key == "yesterday" else "count"),
                "json_attributes_topic": f"{p}/reports/{key}", "state_class": "measurement", "icon": icon})
        self._config("sensor", f"{p}_new_visitors_7d", {
            "name": "New visitors 7 days", "state_topic": f"{p}/reports/new_visitors_7d",
            "state_class": "measurement", "icon": "mdi:account-plus"})
        self._cams.clear()
        self._pics.clear()
        self.pub(f"{p}/status", "online", retain=True)
        self.refresh_all()
        self.republish_last()

    def _on_disconnect(self, *args) -> None:
        self.connected = False
        log.warning("MQTT disconnected")

    def _cameras(self) -> list[str]:
        """Enabled cameras, including configured ones that haven't sent an event yet."""
        rows = {r["name"]: r["enabled"] for r in self.e.db.q("SELECT name, enabled FROM cameras")}
        return sorted({c for c, on in rows.items() if on} | {c for c in self.e.s.cameras if c not in rows})

    def _camera_entities(self) -> None:
        """Per-door entities for cameras enabled since the last look; removed for disabled ones."""
        cams = set(self._cameras())
        for cam in cams - self._cams:
            s, label = slug(cam), self.e.s.camera_label(cam)
            self._config("binary_sensor", f"{self.p}_cam_{s}_present", {
                "name": f"{label} visitor", "state_topic": f"{self.p}/camera/{cam}/present", "device_class": "occupancy",
                "value_template": "{{ value_json.state }}", "json_attributes_topic": f"{self.p}/camera/{cam}/present"},
                entity=f"{self.p}_{s}_visitor")
            self._config("sensor", f"{self.p}_cam_{s}_last_visitor", {
                "name": f"{label} last visitor", "state_topic": f"{self.p}/camera/{cam}/last_visitor",
                "value_template": LAST_VISITOR_STATE,
                "json_attributes_topic": f"{self.p}/camera/{cam}/last_visitor", "icon": "mdi:account-clock"},
                entity=f"{self.p}_{s}_last_visitor")
            for pic in ("face", "frame"):
                self._config("image", f"{self.p}_cam_{s}_{pic}", {
                    "name": f"{label} {pic}", "image_topic": f"{self.p}/camera/{cam}/{pic}",
                    "content_type": "image/jpeg"}, entity=f"{self.p}_{s}_{pic}")
        for cam in self._cams - cams:
            if self.disc:
                for comp, oid in (("binary_sensor", "present"), ("sensor", "last_visitor"), ("image", "face"),
                                  ("image", "frame")):
                    self.pub(f"{self.disc}/{comp}/{self.p}_cam_{slug(cam)}_{oid}/config", "", retain=True)
            for pic in ("face", "frame"):
                self.pub(f"{self.p}/camera/{cam}/{pic}", b"", retain=True)   # no face left on the broker
            self._pics.pop(cam, None)
        self._cams = cams

    def person(self, ident: int) -> None:
        """Discovery and state for one person's sensor: named identities and regular visitors."""
        i = self.e.db.one("""SELECT i.*, COUNT(v.id) AS visits FROM identities i
                             LEFT JOIN visits v ON v.identity_id = i.id WHERE i.id = ? GROUP BY i.id""", (ident,))
        if i is None or not (i["named"] or (i["kind"] == "unknown" and i["visits"] >= REGULAR_VISITS)):
            if ident in self._people:
                self.remove_person(ident)
            return
        if not self.connected:
            return
        self._config("sensor", f"{self.p}_person_{ident}", {
            "name": f"{i['name']} last seen", "state_topic": f"{self.p}/person/{ident}",
            "device_class": "timestamp", "value_template": "{{ value_json.last_seen }}",
            "json_attributes_topic": f"{self.p}/person/{ident}", "icon": "mdi:account-eye"},
            entity=f"{self.p}_{slug(i['name'])}")
        self.pub(f"{self.p}/person/{ident}", {
            "name": i["name"], "kind": i["kind"], "named": bool(i["named"]), "ignored": bool(i["ignored"]),
            **stats.identity_brief(self.e, ident)}, retain=True)
        self._remember(self._people | {ident})

    def remove_person(self, ident: int) -> None:
        if not self.connected:
            return   # still listed: removed by the refresh after the next connect
        if self.disc:
            self.pub(f"{self.disc}/sensor/{self.p}_person_{ident}/config", "", retain=True)
        self.pub(f"{self.p}/person/{ident}", "", retain=True)
        self._remember(self._people - {ident})

    def _remember(self, people: set[int]) -> None:
        if people != self._people:
            self._people = people
            self.e.db.put("mqtt.people", json.dumps(sorted(people)))

    # ---------- state

    def refresh(self) -> None:
        """Presence and today's counts, after every visit change."""
        if not self.connected:
            return
        rows = self.e.db.q("""SELECT v.camera, i.name, i.kind FROM visits v
                              LEFT JOIN identities i ON i.id = v.identity_id WHERE v.closed = 0""")
        present = lambda rs: {"state": "ON" if rs else "OFF",
                              "visitors": [{"camera": r["camera"], "name": r["name"] or "unknown"} for r in rs]}
        self.pub(f"{self.p}/present", present(rows), retain=True)
        for cam in self._cams:
            self.pub(f"{self.p}/camera/{cam}/present", present([r for r in rows if r["camera"] == cam]), retain=True)
        now = time.time()
        self.pub(f"{self.p}/today", stats.today_counts(self.e, now), retain=True)
        self.pub(f"{self.p}/reports/new_visitors_7d", str(stats.new_visitors(self.e, now)), retain=True)

    def refresh_all(self) -> None:
        """Everything that changes with time alone: on connect and every 5 minutes, so counts
        roll over at midnight and "overdue" or "days since last" move with no visit."""
        if not self.connected:
            return
        self._briefs.clear()
        self._camera_entities()
        self.refresh()
        for key, value in stats.reports(self.e).items():
            self.pub(f"{self.p}/reports/{key}", value, retain=True)
        wanted = {r["id"] for r in self.e.db.q("""SELECT i.id FROM identities i WHERE i.named = 1 OR (i.kind = 'unknown'
                                 AND (SELECT COUNT(*) FROM visits WHERE identity_id = i.id) >= ?)""", (REGULAR_VISITS,))}
        for ident in sorted(wanted):
            self.person(ident)
        # Sensors of identities that are gone (retention, forgetting while offline) or that
        # dropped below a regular's visits
        for ident in self._people - wanted:
            self.remove_person(ident)

    def republish_last(self) -> None:
        """The last-visitor topics from the database: after a connect, and after a person is
        renamed, merged or forgotten, so no retained topic keeps a name or face that's gone."""
        if not self.connected:
            return
        r = self.e.db.one("""SELECT v.id FROM visits v JOIN identities i ON i.id = v.identity_id
                             WHERE v.closed = 1 AND i.ignored = 0 ORDER BY v.end_ts DESC LIMIT 1""")
        self.pub(f"{self.p}/last_visitor",
                 self._payload(self.e.visit_summary(r["id"], with_context=True), "visit") if r else {}, retain=True)
        for cam in self._cams:
            self._door_from_db(cam)

    def _door_from_db(self, cam: str) -> None:
        """A door's topics from its latest non-household visit, or emptied if there's none. An open
        visit has no identity until it closes, so its current guess decides."""
        for r in self.e.db.q("""SELECT v.id, v.closed FROM visits v LEFT JOIN identities i ON i.id = v.identity_id
                                WHERE v.camera = ? AND COALESCE(i.ignored, 0) = 0
                                ORDER BY v.start_ts DESC LIMIT 10""", (cam,)):
            v = self.e.visit_summary(r["id"], with_context=True) if r["closed"] else self.e.current(r["id"])
            if not v["ignored"]:
                self._door(cam, v, self._payload(v, "visit" if r["closed"] else "arrival"))
                return
        self._door(cam, None, {})

    def _jpeg(self, rel: str | None) -> bytes:
        """A stored JPEG's bytes; empty (which clears the retained topic) when there's none."""
        if not rel:
            return b""
        try:
            return self.e._inside(rel).read_bytes()
        except (OSError, EventError):
            return b""

    def _door(self, cam: str, v: dict | None, payload: dict) -> None:
        """A door's face and frame as raw JPEG, then its last-visitor payload, so an automation
        triggered by that (or by the event after it) finds the matching pictures. The JPEGs are
        sent only when the visit's best face or frame changed."""
        pics = (v.get("best_sighting"), v.get("frame_event")) if v else (None, None)
        if self._pics.get(cam) != pics:
            face = self.e.db.one("SELECT crop FROM sightings WHERE id = ?", (pics[0],)) if pics[0] else None
            frame = self.e.db.one("SELECT frame FROM events WHERE id = ?", (pics[1],)) if pics[1] else None
            self.pub(f"{self.p}/camera/{cam}/face", self._jpeg(face["crop"] if face else None), retain=True)
            self.pub(f"{self.p}/camera/{cam}/frame", self._jpeg(frame["frame"] if frame else None), retain=True)
            self._pics[cam] = pics
        self._shown[cam] = payload.get("visit")
        self.pub(f"{self.p}/camera/{cam}/last_visitor", payload, retain=True)

    def brief(self, ident: int, vid: int, start: float) -> dict:
        key = (ident, vid)
        hit = self._briefs.get(key)
        if hit and time.time() - hit[0] < BRIEF_TTL_S:
            return hit[1]
        b = stats.identity_brief(self.e, ident, at=start, visit=vid)
        self._briefs[key] = (time.time(), b)
        return b

    def _payload(self, v: dict, stage: str) -> dict:
        out = {k: v.get(k) for k in ("visit", "camera", "identity", "name", "kind", "named", "faces", "how",
                                     "visit_number", "new_visitor", "unusual_time", "rang")}
        out["door"] = self.e.s.camera_label(v["camera"])
        out["provisional"] = bool(v.get("provisional"))
        out["start"], out["end"] = iso(v["start"]), iso(v["end"])
        out["duration_s"] = round(v["end"] - v["start"])
        out["local_hour"] = self.e.local(v["start"]).hour
        if v.get("best_sighting") and self.image_url:
            out["image"] = self.image_url(v["best_sighting"])
        if v.get("frame_event") and self.frame_url:
            out["frame"] = self.frame_url(v["frame_event"])
        if v.get("changed_from"):
            out["changed_from"] = v["changed_from"]
        if v.get("identity") is not None:
            out["person"] = {**self.brief(v["identity"], v["visit"], v["start"]), "visit_number": v.get("visit_number")}
        out["summary"] = describe(out, stage, bool(self.e.s.doorbell_token))
        return out

    def on_engine(self, kind: str, data: dict) -> None:
        if kind in ("identity_changed", "identity_removed"):
            self._briefs.clear()
            if kind == "identity_changed":
                self.person(data["identity"])
            else:
                self.remove_person(data["identity"])
            self.republish_last()
            return
        if kind == "folded":
            # A stray frame of another visit: no end event, and the door stops showing it
            self.refresh()
            if self._shown.get(data["camera"]) == data["visit"]:
                self._door_from_db(data["camera"])
            return
        if kind not in ("arrival", "identified", "doorbell", "visit"):
            return
        self.refresh()
        payload = self._payload(data, kind)
        # The door's pictures and topic first: an automation triggered by the event then shows this face
        if not data["ignored"]:
            self._door(data["camera"], data, payload)
        elif kind in ("identified", "visit") and self._shown.get(data["camera"]) == data["visit"]:
            # An arrival announced as unrecognised turned out to be the household: the door goes
            # back to its last visitor, so no household face stays on it
            self._door_from_db(data["camera"])
        if kind == "visit" and data["identity"] is not None:
            self.person(data["identity"])
            if not data["ignored"]:
                self.pub(f"{self.p}/last_visitor", payload, retain=True)
        if data["ignored"]:
            # The household, once per visit: at arrival, or when an unrecognised arrival turns out to be them
            if kind in ("arrival", "identified"):
                self.pub(f"{self.p}/event", {"event_type": "household_arrival", **payload})
            return
        if kind == "arrival":
            etype = {"known": "known_arrival", "unknown": "visitor_arrival"}.get(data["kind"], "unknown_arrival")
        elif kind == "visit":
            etype = "new_visitor" if data.get("new_visitor") else "unusual_time" if data.get("unusual_time") else "visit_ended"
        else:
            etype = kind
        self.pub(f"{self.p}/event", {"event_type": etype, **payload})
