# SmartEye data in Home Assistant

This is a reference for everything SmartEye knows, how it reaches Home Assistant, and what you
can build with it. The example payloads were produced by SmartEye itself from its demo data
(`scripts/demo.py`); signing keys are replaced with `…`.

Contents:

0. [What SmartEye does, and what HA does](#0-what-smarteye-does-and-what-ha-does)
1. [How the data is made](#1-how-the-data-is-made): sightings, visits and identities
2. [The life of a visit](#2-the-life-of-a-visit): what is published, and when
3. [Entities](#3-entities): everything SmartEye creates in HA
4. [MQTT topics and payloads](#4-mqtt-topics-and-payloads): every field
5. [Images](#5-images)
6. [The doorbell](#6-the-doorbell)
7. [Things to know before automating](#7-things-to-know-before-automating)
8. [What to build with it](#8-what-to-build-with-it): automations and dashboards
9. [Limits and design choices](#9-limits-and-design-choices)

---

## 0. What SmartEye does, and what HA does

**SmartEye reports who was at the door and what it knows about them.** Only SmartEye can do
this: it matches faces to people, keeps every visit, and works out each person's habits.

**Home Assistant owns the rules.** That covers what counts as worth an alert, timing (how
long, how soon after the bell), who's home, the time of day, and what to do: notify, announce,
lights. HA already has the other signals (the doorbell, phones, zones, the alarm), and
rules there can be changed without touching SmartEye.

So SmartEye's events describe something that happened to a **visit** or a **person**: an
arrival, a better identification, the end of a visit, the first visit by someone new, an
unusual time for a regular. It doesn't publish events that are a timer over HA's own signals.
An unanswered ring or someone lingering at the door are automations (§8.3).

The one piece of presentation SmartEye does is `summary`, a sentence of facts. It's kept on
purpose: it holds nothing that isn't in the other fields, it has no tone (HA adds that), and
it saves a long template in every announcement.

The doorbell link (§6) is the one place HA sends data to SmartEye. It's there so visits
record who rang, which only SmartEye can join up. Nothing else in SmartEye depends on it.

---

## 1. How the data is made

```
Blue Iris ─► ImmichEye ─► face events (camera, time, box, embedding, crop, Immich name)
                              │  POST /api/events
                              ▼
                          SmartEye ── SQLite + JPEGs (the full history; the web UI)
                              │
                              └── MQTT, retained ──► Home Assistant (discovery: one "SmartEye" device)
                              ▲
              POST /api/doorbell ◄── Home Assistant (the doorbell button)
```

Home Assistant only sees what SmartEye publishes over MQTT, faces included. The
JSON API (`/api/...`) is behind the UI login, so HA can't query it. Everything HA can use is
listed in sections 3 to 5.

### Building blocks

| Thing | What it is | Lives |
|---|---|---|
| **Sighting** | One face in one frame: box, size in px, detector score, quality, whether it's usable (≥ 40 px and detector ≥ 0.75), ImmichEye's name for it, embedding, crop | Database. Never published, but the best one per visit becomes the door's face image |
| **Event** | One frame from ImmichEye, with its id and the whole-frame JPEG | Database. The best frame per visit becomes `frame` |
| **Visit** | One person's arrival at one camera: the sightings of the same face, until 120 s pass with no new one. Two people in one frame make two visits. A stray face or two seconds from another visit at that door is folded into it (below) | Database. Published as events and `last_visitor` payloads |
| **Identity** | Someone SmartEye has seen: an Immich person (`kind: known`) or a visitor (`kind: unknown`), "Visitor 12" until you name them | Database. Published as person sensors and in every payload's `person` block |
| **Ring** | A doorbell press, joined to the visit at that door | Database. Published as `doorbell` events, `rang`, and the `rings`/`unanswered` counts |

### How a visit gets an identity

This is decided when the visit **ends**. While it's open, SmartEye only guesses
(`provisional`).

| `how` | Meaning |
|---|---|
| `named` | ImmichEye named someone in it. The name with the most total score wins |
| `matched` | Its best faces match an existing identity (score ≥ 0.45 and 0.05 ahead of the runner-up) |
| `new` | Usable faces that match nobody. A new visitor is created: `new_visitor: true` |
| `manual` | You moved the visit to someone in the UI |
| `none` | No face good enough to identify. The visit still counts, with `identity: null` |

### Corrections: folds and merges

Two things change the data after the fact. Both are shown in the UI; neither is
published as an event of its own.

- **Fold (automatic).** A visit that ends unrecognised with only one or two faces, overlapping
  another visit at the same door or within 5 s of it (`SMARTEYE_FOLD_FACES`, `SMARTEYE_FOLD_S`),
  is a stray frame of that visit: someone looking down as they leave, say. Its faces move into
  that visit and it's deleted, so it never becomes a visitor. Never done when the two share a
  frame: two faces in one frame are two people. What HA sees is in §2. The UI shows
  "N FOLDED IN" on the visit, and marks the faces.
- **Merge (by you, in the UI).** "Merge into…" on a person page, or Merge on Review, moves one
  identity's visits, ImmichEye names and notes onto another and deletes the first. Its id is
  gone for good (ids are never reused); the one you merge **into** keeps its id, sensor and
  entity id. HA sees:
  - the merged identity's person sensor removed, and the kept one's refreshed with the
    combined visits, so its `visits`, `usual`, rhythm and `last_seen` can change at once;
  - `sensor.smarteye_last_visitor` and the door sensors republished, so a retained payload
    never names someone who no longer exists;
  - no event. Events already sent stay as they were: a `new_visitor` for someone you later
    merge into a regular isn't taken back.

  The UI keeps the record: the kept person's page lists "Merged in: Visitor 6 (date, 1
  visit)", and a moved visit says MERGED IN instead of FIRST VISIT.

### Identity attributes that matter in HA

- **`kind`**: `known` means an Immich person, `unknown` means a visitor. A visitor you've
  named in SmartEye ("Milk man") is still `kind: unknown`.
- **`named`**: true once the identity has a real name, either from Immich or one you gave it
  in the UI. False for "Visitor 12". `kind: unknown` with `named: true` is a visitor you know.
- **Household** (`ignored: true`): set with Household in the UI. They're still matched, so they never become
  "new visitors". But they fire only `household_arrival`, don't count in today's figures (they're counted
  separately as `household`), and are left out of expected, overdue and yesterday.
- **Notes**: free text you write in the UI. It's published with every event about that
  person, for example "Pay £15 cash".

### The intel SmartEye works out per person

All times are local (`SMARTEYE_TZ`, Europe/London).

| Field | How it's worked out | Needs |
|---|---|---|
| `usual` | The narrowest time of day (1, 1.5, 2, 3 or 4 h) holding ≥ 60% of their visits, rounded to quarter hours. `days` lists the weekdays they cluster on (empty if they don't) | 3 visits |
| `median_gap_days` | Median days between visit days | 4 visit days |
| `next_expected` | Last visit plus the median gap, as a date | 4 visit days |
| `overdue` | No visit in twice their usual gap (at least 2 days) | 4 visit days |
| `timing` | This visit against `usual`: `on_time` (within the window, or ≤ 30 min outside), `early`, `late`, `unusual_day` (not one of their days) or `unknown` (no window yet) | 3 visits |
| `unusual_time` | At least 5 earlier visits, none within an hour of this time of day | 5 visits |
| `avg_dwell_s` | Mean visit length | 1 visit |

---

## 2. The life of a visit

One visit fires 1 to 4 events, plus state updates. Here is a typical unrecognised caller who rings:

```
t=0      first face at FDoor
         ├─ smarteye/camera/FDoor/present        ON, visitors: [{camera, name}]
         ├─ smarteye/present                     ON
         ├─ smarteye/today                       visits +1
         ├─ smarteye/camera/FDoor/face, /frame   raw JPEG of the best face so far
         ├─ smarteye/camera/FDoor/last_visitor   arrival payload (provisional guess, or no identity)
         └─ smarteye/event                       unknown_arrival | visitor_arrival | known_arrival

t=4s     better faces now match "Window cleaner"
         ├─ smarteye/camera/FDoor/face, /frame   only if the best face changed
         ├─ smarteye/camera/FDoor/last_visitor   updated
         └─ smarteye/event                       identified   (only if the guess appears or changes)

t=9s     HA reports the bell (POST /api/doorbell)
         ├─ smarteye/camera/FDoor/last_visitor   updated, rang: true
         └─ smarteye/event                       doorbell

t=40s    last face

t≈160s   120 s with no face (checked every 10 s) → the visit closes, identity decided
         ├─ present topics                       OFF
         ├─ smarteye/today                       recounted
         ├─ smarteye/camera/FDoor/last_visitor   final payload (duration, visit_number, ...)
         ├─ smarteye/person/<id>                 their sensor refreshed
         ├─ smarteye/last_visitor                final payload (identified, non-household)
         └─ smarteye/event                       new_visitor | unusual_time | visit_ended
```

Rules:

- **Arrival comes once per visit**, as soon as the first face lands. It holds the best guess so far.
  `provisional: true` means the guess comes from matching faces, not from an ImmichEye name.
- **`identified` fires only when the guess changes**: from nothing to someone, or from one
  person to another. It never repeats.
- **The end-of-visit event is final.** If the final identity differs from the one announced,
  the event carries `changed_from: {identity, name}`.
- **A visit with no usable face** (`how: none`) fires `unknown_arrival`, then `visit_ended`
  with `identity: null` ("Someone not recognised was at the front door for 40 seconds."). It
  doesn't update `smarteye/last_visitor`.
- **A stray frame is folded, with no end event.** A visit that ends unrecognised with only one
  or two faces, overlapping another visit at the same door or within 5 s of it, is a stray
  frame of that visit (someone looking down as they leave). Its faces join that visit and it
  disappears: no `visit_ended` or `new_visitor`, no new visitor, and if the door sensor and
  images were showing it they go back to the latest real visitor. Its `unknown_arrival` has
  already been sent by then, so an alert on `unknown_arrival` can still fire for one; a visit
  id that never gets an end event was folded.
- **Ignored people** (the household) update presence, their own person sensor and the household
  count, and fire `household_arrival` once per visit. Nothing else: no other event types, and
  they don't stay on the last-visitor sensors (an arrival that starts unrecognised is there for
  a few seconds, §7.12).
- **Timer-driven updates.** Presence, today's counts, reports and person sensors are
  republished on connect and every 5 minutes. So counts reset within 5 minutes of midnight,
  and `overdue` and `days_since_last` move even when nobody visits.

---

## 3. Entities

All entities belong to one device, **SmartEye**. They go `unavailable` when SmartEye stops or
loses the broker (`smarteye/status`, with a last will).

### Live

| Entity | State | Attributes | Updates |
|---|---|---|---|
| `event.smarteye_visitor` | Time of the last event | `event_type` plus the full visit payload (§4.2) | Every event |
| `binary_sensor.smarteye_visitor_present` | `on` while any visit is open, household included | `visitors: [{camera, name}]` | Every visit change |
| `binary_sensor.smarteye_fdoor_visitor`, `_bdoor_visitor` | Same, for one door | Same | Every visit change |
| `image.smarteye_fdoor_face`, `_bdoor_face` | The best face crop of the door's latest visitor (as on its last-visitor sensor) | | When the best face changes |
| `image.smarteye_fdoor_frame`, `_bdoor_frame` | The whole frame that face came from | | When it changes |
| `sensor.smarteye_fdoor_last_visitor`, `_bdoor_last_visitor` | Name of the latest non-household visitor at that door, from the moment they arrive. `Unrecognised` if nobody was identified, `none` before anyone came | The visit payload (§4.2) | Arrival, identified, doorbell, close |
| `sensor.smarteye_last_visitor` | Name of the last **identified, finished** visit at any door, household excluded | The visit payload | Close; and on connect, rename, merge or forget |

### Counts and reports

| Entity | State | Attributes | Notes |
|---|---|---|---|
| `sensor.smarteye_visits_today` | Visits since local midnight, household excluded | `visits`, `people`, `unknown`, `household`, `rings`, `unanswered` | `total_increasing`: the midnight drop counts as a reset |
| `sensor.smarteye_unknown_today` | Visits today by anyone not an Immich person, unidentified ones included | | |
| `sensor.smarteye_people_today` | Distinct identities today | | |
| `sensor.smarteye_new_visitors_7d` | Visitors whose first visit was in the last 7 days | | Includes visitors you've since named |
| `sensor.smarteye_expected_today` | People due today | `people: [{identity, name, usual_from, usual_to, seen_today}]` | Due means their gap says so, or today is one of their usual days (5+ visits) |
| `sensor.smarteye_overdue` | Regulars past twice their usual gap | `people: [{identity, name, days_since_last, median_gap_days}]` | Dropped after 4 gaps (at least 14 days) |
| `sensor.smarteye_yesterday` | Yesterday's visits | `unknown`, `people`, `new_visitors`, `notable: [{name, time, camera, duration_s, why}]` | `why` ⊂ `new_visitor`, `unusual_time`. Filter on `time` and `duration_s` for night visits or long stays |

### People

There is one `sensor.smarteye_<name>` for each **named** identity (Immich people, and visitors
you've named) and for each unnamed visitor with **5 or more visits**.

- **State:** `last_seen`, a timestamp (`device_class: timestamp`), so HA shows "3 hours ago".
- **Attributes:** the `person` block (§4.3) plus `name`, `kind`, `named` and `ignored`.
- **`unique_id`** is `smarteye_person_<identity id>`. Renaming someone in SmartEye changes the
  friendly name but never the entity, so automations keep working. The entity id is set from
  the name the first time only.
- Forgetting someone, or merging them into someone else, removes the sensor. The person they're
  merged into keeps theirs, now counting both people's visits (§1, corrections).

For example `sensor.smarteye_milk_man` or `sensor.smarteye_window_cleaner`. Check
Settings → Entities for the exact ids. Sensors created before readable ids were added keep
`sensor.smarteye_person_<id>`.

---

## 4. MQTT topics and payloads

The prefix is `smarteye` (`SMARTEYE_MQTT_PREFIX`) and discovery uses `homeassistant/`. All
topics are QoS 1. All are **retained** except `smarteye/event`.

| Topic | Retained | Payload |
|---|---|---|
| `smarteye/status` | ✓ | `online` / `offline` (last will) |
| `smarteye/event` | ✗ | §4.2 plus `event_type` |
| `smarteye/present` | ✓ | §4.1 |
| `smarteye/camera/<cam>/present` | ✓ | §4.1, one door |
| `smarteye/last_visitor` | ✓ | §4.2, or `{}` |
| `smarteye/camera/<cam>/last_visitor` | ✓ | §4.2, or `{}` if that door has had nobody |
| `smarteye/camera/<cam>/face` | ✓ | Raw JPEG, the face crop. Empty when there's no face (forgotten) |
| `smarteye/camera/<cam>/frame` | ✓ | Raw JPEG, the whole frame. Empty when there's none |
| `smarteye/person/<id>` | ✓ | §4.3, plus `name`, `kind`, `named`, `ignored` |
| `smarteye/today` | ✓ | §4.4 |
| `smarteye/reports/expected_today` | ✓ | `{count, people: [...]}` |
| `smarteye/reports/overdue` | ✓ | `{count, people: [...]}` |
| `smarteye/reports/yesterday` | ✓ | `{visits, unknown, people, new_visitors, notable: [...]}` |
| `smarteye/reports/new_visitors_7d` | ✓ | a bare integer |

`<cam>` is the Blue Iris short name (`FDoor`, `BDoor`). Entity ids use it lower-cased.

### 4.1 Presence

```json
{"state": "ON", "visitors": [{"camera": "FDoor", "name": "Milk man"}]}
```

`name` is `"unknown"` until the visit has an identity. An open visit's provisional guess isn't
used here; that is on the door's `last_visitor`.

### 4.2 Visit payload (events and `last_visitor`)

An example `smarteye/last_visitor`, at the close of a visit:

```json
{
  "visit": 329,
  "camera": "FDoor",
  "door": "Front door",
  "identity": 3,
  "name": "Postie",
  "kind": "unknown",
  "named": true,
  "how": "matched",
  "provisional": false,
  "faces": 2,
  "visit_number": 41,
  "new_visitor": false,
  "unusual_time": false,
  "rang": true,
  "start": "2026-10-27T11:14:27.088893+00:00",
  "end": "2026-10-27T11:14:28.588893+00:00",
  "duration_s": 2,
  "local_hour": 11,
  "image": "http://smarteye.local:8090/img/s/1328.jpg?k=…",
  "frame": "http://smarteye.local:8090/img/e/40821af2-7b05-a398-88c3-8d7868b4ef75.jpg?k=…",
  "person": {
    "visits": 40, "visits_30d": 14,
    "first_seen": "2026-08-18T10:07:04.206763+00:00",
    "last_seen": "2026-10-26T11:25:08.058347+00:00",
    "days_since_last": 1,
    "usual": {"from": "10:30", "to": "11:30", "share": 0.88, "days": []},
    "median_gap_days": 1, "next_expected": "2026-10-27", "overdue": false,
    "avg_dwell_s": 4,
    "cameras": {"FDoor": 40},
    "notes": "Letters most weekdays",
    "timing": "on_time",
    "visit_number": 41
  },
  "summary": "Postie was at the front door for 2 seconds, 41st visit, usually 10:30–11:30, last here yesterday, on time, rang the bell. Note: Letters most weekdays"
}
```

On `smarteye/event`, `event_type` is added:

| `event_type` | When | Typical use |
|---|---|---|
| `known_arrival` | Arrival; best guess is an Immich person | Greet, "X is here" |
| `visitor_arrival` | Arrival; best guess is a visitor seen before | "The milkman's here" |
| `unknown_arrival` | Arrival; no guess yet | "Someone's at the door" with the face |
| `identified` | Open visit; the guess appeared or changed | Follow-up: "it's the window cleaner" |
| `doorbell` | Bell pressed during an open visit (not the household's) | "Milk man is ringing the front door" |
| `new_visitor` | Close; nobody matched, a new identity was made | First-time caller log |
| `unusual_time` | Close; a regular (5+ visits) at an hour they've never come | Something's off |
| `visit_ended` | Close; anything else, including unidentified visits (`identity: null`) | History, counters, closing a tagged notification |
| `household_arrival` | Arrival of an ignored person; or `identified`, when an arrival with no guess turns out to be one (§7.12) | Welcome home, hall lights |

Fields:

| Field | Type | Meaning |
|---|---|---|
| `visit` | int | Visit id. Never reused. Group a visit's events by it |
| `camera` / `door` | str | Blue Iris name / friendly name (`SMARTEYE_CAMERA_LABELS`) |
| `identity` | int or null | Identity id. Never reused, survives renames. **Use this in conditions, not `name`** |
| `name` | str or null | Current name. Null when nobody's identified |
| `kind` | `known` / `unknown` / null | Immich person / visitor / nobody |
| `named` | bool | Has a real name (Immich, or given in the UI), not "Visitor N". False with no identity |
| `how` | str or null | §1. Null on an open visit that has no ImmichEye name |
| `provisional` | bool | The identity is a match guess on an open visit |
| `faces` | int | Faces so far |
| `visit_number` | int or null | This visit counts as their Nth (finished earlier visits + 1). Null without an identity |
| `new_visitor` | bool or null | True only at close, on the visit that created the identity. Null without an identity |
| `unusual_time` | bool or null | See §1. Null without an identity |
| `rang` | bool | A ring is attached to this visit, including one pressed before the face was seen |
| `start` / `end` | ISO UTC | First and last face so far |
| `duration_s` | int | `end - start`. On an arrival it's about 0 |
| `local_hour` | int | Local hour of `start`, 0–23 |
| `image` | URL | Best face crop (§5). Absent if `SMARTEYE_PUBLIC_URL` is unset |
| `frame` | URL | Whole frame with the best face (§5). Absent if ImmichEye sent no frame |
| `changed_from` | `{identity, name}` | Close only: who it was announced as, when that was wrong |
| `person` | object | §4.3, describing the visits **before** this one, plus `timing` and `visit_number`. Absent without an identity |
| `summary` | str | The facts in one sentence, for TTS or an LLM prompt |

The `summary` changes with the stage:

- arrival: "Probably Window cleaner **is at** the front door, 15th visit, usually Sat 09:30–11:30, last here 4 weeks ago, on time. Note: Pay £15 cash"
- doorbell: "Milk man **is ringing at** the front door, …"
- close: "Postie **was at** the front door **for 2 seconds**, 41st visit, …, **rang the bell**." (or **didn't ring**)
- nobody identified: "Someone not recognised is at the front door."

### 4.3 The `person` block

On events this block describes the person **before this visit**. That's why the example above
shows `visits: 40` with `visit_number: 41`, and why `last_seen` is the previous visit. On person
sensors (`smarteye/person/<id>`) it covers all their visits.

| Field | Type | Meaning |
|---|---|---|
| `visits` / `visits_30d` | int | Visit count, all time / last 30 days |
| `first_seen` / `last_seen` | ISO UTC | First visit start / last visit end |
| `days_since_last` | int or null | Calendar days since `last_seen` (0 = earlier today) |
| `usual` | `{from, to, days, share}` or null | e.g. `{"from": "10:15", "to": "11:45", "days": ["Mon", "Sun"], "share": 0.6}` |
| `median_gap_days` | number or null | Usual gap between visit days |
| `next_expected` | date or null | `"2026-11-02"` |
| `overdue` | bool | §1 |
| `avg_dwell_s` | int or null | Mean time at the door |
| `cameras` | `{camera: visits}` | Which doors, most used first |
| `notes` | str | Your notes from the UI |
| `timing` | str | Events only: `on_time`, `early`, `late`, `unusual_day`, `unknown` |
| `visit_number` | int | Events only: repeated from the top level |

A person sensor adds `name`, `kind`, `named` and `ignored`:

```json
{"name": "Milk man", "kind": "unknown", "named": true, "ignored": false, "visits": 15, "visits_30d": 4,
 "first_seen": "2026-08-20T05:28:12.380901+00:00", "last_seen": "2026-10-08T05:32:58.280711+00:00",
 "days_since_last": 19, "usual": {"from": "06:15", "to": "07:15", "share": 1.0, "days": ["Mon", "Thu"]},
 "median_gap_days": 3.5, "next_expected": "2026-10-11", "overdue": true, "avg_dwell_s": 4,
 "cameras": {"FDoor": 15}, "notes": "Mon & Thu, early"}
```

### 4.4 Today

```json
{"visits": 3, "people": 2, "unknown": 3, "household": 3, "rings": 0, "unanswered": 0}
```

- `visits`, `people` and `unknown` exclude the household; `household` counts its visits.
- `rings` counts every press today.
- `unanswered` counts presses that no visit claimed within 30 s: the bell rang and nobody was
  seen.

---

## 5. Images

The pictures go to Home Assistant over MQTT as raw JPEG. SmartEye's discovery creates one face
and one frame image entity per door:

| Entity | Topic (retained) | Picture | Size |
|---|---|---|---|
| `image.smarteye_fdoor_face` | `smarteye/camera/FDoor/face` | Best face crop of the door's latest visitor | ~40 KB |
| `image.smarteye_fdoor_frame` | `smarteye/camera/FDoor/frame` | The frame that face came from, 640×480 (Blue Iris sub-stream) | ~60 KB |
| `image.smarteye_bdoor_face`, `_bdoor_frame` | `smarteye/camera/BDoor/...` | The same for the back door | |

- **Notifications use HA's image proxy:** `/api/image_proxy/image.smarteye_fdoor_face`. HA
  serves it behind its own login, so it works away from home, and HA never fetches anything
  from SmartEye. No helpers are needed.
- **Order:** for each update, the face and frame go first, then the door's `last_visitor`,
  then the event. An automation triggered by either already has the new pictures.
- **Sent only on change:** a visit's pictures are republished only when its best face or
  frame changes. An `identified` or end-of-visit message for the same face doesn't resend.
- **The household isn't kept:** the door pictures follow the door sensors, which skip it.
  An arrival that starts unrecognised and turns out to be one of the household (§7.12) is on
  the door for those few seconds. Once it's recognised, the door goes back to its previous
  visitor, pictures included, so no household face stays on the broker.
- **Forgetting:** the door moves to its previous visitor's pictures. If there's none, the
  topics are emptied, so no retained face is left on the broker. HA's image entity then holds
  no picture, and `/api/image_proxy` returns an error, so a notification using it shows no
  image.
- **Recorder:** an image entity's state is a timestamp. The JPEGs never reach HA's database.
- **Who can see them:** anyone with a login to the broker that can read `smarteye/#` gets the
  faces. If SmartEye shares a broker login with other devices, any of them can read the faces.
  Give SmartEye its own broker user, with an ACL so no other login can read `smarteye/#`.
- **A phone fetches the picture when the notification is shown,** so two visitors at one door
  seconds apart can show the second face.

The payloads still carry `image` and `frame` links
(`http://smarteye.local:8090/img/s/<sighting>.jpg?k=<hmac>`, `/img/e/<event>.jpg?k=…`). They
open the same pictures on the LAN without the UI login, never expire, and return 404 once
the face is forgotten or past retention. Notifications don't need them.

---

## 6. The doorbell

HA tells SmartEye about presses. SmartEye can't see the bell.

```yaml
rest_command:
  smarteye_doorbell:
    url: http://smarteye.local:8090/api/doorbell
    method: post
    headers:
      Authorization: !secret smarteye_doorbell_bearer   # "Bearer <SMARTEYE_DOORBELL_TOKEN>"
    content_type: application/json
    payload: '{"camera": "FDoor"}'
```

Call `rest_command.smarteye_doorbell` from the bell automation. It's only active when
`SMARTEYE_DOORBELL_TOKEN` is set.

| Bell pressed… | Result |
|---|---|
| during an open visit at that door | `doorbell` event with who's there; the visit gets `rang: true`. For the household: `rang` only, no event |
| before anyone's seen | Held 30 s. A visit starting in that window claims it, and its arrival already has `rang: true`. No `doorbell` event fires |
| and nobody appears | Counted in `today.unanswered`. No event: an unanswered ring is a timing rule, so it belongs in HA (§8.3) |

The end-of-visit `summary` then says "rang the bell" or "didn't ring". Without a doorbell
token, nothing about rings is said.

---

## 7. Things to know before automating

1. **Use `identity`, not `name`, in conditions.** Names change when you rename someone; ids
   never change or get reused.
2. **Arrivals are guesses.** For anything consequential (an alarm, a watch list), act on
   `identified` or the end-of-visit event, or check `provisional` is false. For speed (lights,
   a chime), act on the arrival.
3. **One visit, several events.** An automation on every `event.smarteye_visitor` change runs
   2–4 times per visitor. Filter on `event_type`.
4. **Use `not_from: unavailable` on event-entity triggers.** When SmartEye restarts, the event
   entity goes `unavailable` and comes back, and that state change looks like an event.
5. **Arrival latency is ImmichEye's, not SmartEye's.** Arrival fires as soon as the first face
   is posted, typically 1–3 s after Blue Iris triggers. End-of-visit fires about 2 minutes
   after the last face.
6. **Presence lasts 2 minutes after the person leaves.** The door binary sensor stays on until
   the visit closes: last face + 120 s, checked every 10 s. "On for 5 minutes" means about 3
   minutes of faces.
7. **The household is invisible to events and last-visitor sensors,** apart from
   `household_arrival`. It still shows in `*_present` and their own person sensors. For an
   arrival that starts unrecognised, see §7.12.
8. **An unidentified visitor shows as `Unrecognised`** on the door sensor; its `name`
   attribute is null. Use the `visit` and `start` attributes, not the state, to tell visits
   apart: two unrecognised visitors in a row leave the state unchanged.
9. **`sensor.smarteye_last_visitor` and the door sensors differ.** The door sensors follow
   visits live, from arrival, including unrecognised ones. The global one only changes when an
   identified visit ends.
10. **Event attributes don't persist.** The `event.` entity holds only the last event. For
    history, use the logbook, the recorder, or a counter/helper you increment.
11. **Times are UTC ISO strings,** except `local_hour`, `usual`, `next_expected` and
    `yesterday.notable[].time`, which are local. `as_local(as_datetime(...))` converts the
    ISO strings in templates.
12. **The household's arrival can come as two events.** When the first faces aren't good enough
    to recognise you, SmartEye sends `unknown_arrival` first and `household_arrival` a few
    seconds later, once it knows. Tag notifications with the `visit` id so the second replaces
    the first, or wait a few seconds before alerting on `unknown_arrival`. For those few
    seconds the door's last-visitor sensor and images show the unrecognised arrival; once
    it's recognised as the household they go back to the previous visitor.
13. **Not every `unknown_arrival` gets an end event.** A stray frame of someone already at
    the door (a face or two that matches nobody, seconds from their visit) can open a visit of
    its own and announce `unknown_arrival`. When it ends it's folded into the real visit and no
    end event is sent (§2). If an automation waits for the end of a visit, give it a timeout.
14. **A merge retires an identity id.** If an automation or a HA-side counter uses a visitor's
    `identity` and you merge that visitor into someone else, nothing matches the old id again.
    Merge into the identity your automations use, or update them to the kept id.

An alternative to the event entity is an **MQTT trigger** on `smarteye/event`. It gets every
payload directly, with no availability flaps, but isn't visible as an entity:

```yaml
triggers:
  - trigger: mqtt
    topic: smarteye/event
    value_template: "{{ value_json.event_type }}"
    payload: unknown_arrival
# trigger.payload_json.summary, .image, .identity, ...
```

---

## 8. What to build with it

The examples use the native trigger and condition forms. Templates appear only where HA has no
native form (message text and loops). Change `notify.mobile_app_phone` to your notify target.

### 8.1 Notifications

**Unknown or new visitor, with the face, while nobody's home.** This is the core alert. The
household is already excluded.

```yaml
alias: SmartEye - stranger at the door while away
mode: queued
triggers:
  - trigger: state
    entity_id: event.smarteye_visitor
    not_from: unavailable
conditions:
  - condition: state
    entity_id: event.smarteye_visitor
    attribute: event_type
    state: [unknown_arrival, visitor_arrival, new_visitor]
  - condition: state
    entity_id: zone.home
    state: "0"
actions:
  - action: notify.mobile_app_phone
    data:
      title: "{{ trigger.to_state.attributes.door }}"
      message: "{{ trigger.to_state.attributes.summary }}"
      data:
        image: /api/image_proxy/image.smarteye_fdoor_face
        tag: "smarteye-{{ trigger.to_state.attributes.visit }}"   # later events replace the same notification
```

Tagging with the visit id makes later events for the same visit **update the one
notification** instead of stacking. Only the event types the condition lets through do that:
add `identified` and `visit_ended` to the list to follow a visit to its end. Pick the image
entity by `camera` if you have both doors.

`zone.home` is often still `0` as you reach your own door. If your first faces aren't
recognised, this alert fires about you before `household_arrival` follows (§7.12). A short
delay, then a check that no `household_arrival` came for the same `visit`, avoids that.

**A smarter doorbell notification.** Fire on the `doorbell` event instead of the button. It
says who: "Milk man is ringing at the front door, usually Mon/Thu 06:00–07:00".

**Watch list.** Alert loudly when a specific identity appears, but only once the match is firm:

```yaml
conditions:
  - condition: state
    entity_id: event.smarteye_visitor
    attribute: event_type
    state: [identified, visit_ended, unusual_time, doorbell]
  - condition: state
    entity_id: event.smarteye_visitor
    attribute: identity
    state: 4
```

`known_arrival` and `visitor_arrival` are provisional. Add them only if a faster, sometimes
wrong, alert is acceptable.

**Changed its mind.** On the end-of-visit event, if `changed_from` is present, follow up with
"Correction: that was X, not Y".

### 8.2 Announcements (TTS / LLM)

`summary` is facts only, so HA supplies the tone. Use it raw with `tts.speak`, or hand it to an
LLM for something more natural:

```yaml
actions:
  - action: ai_task.generate_data
    data:
      task_name: smarteye announcement
      instructions: >
        In one short, friendly sentence for a house speaker, say who is at the door.
        Facts: {{ trigger.to_state.attributes.summary }}
    response_variable: said
  - action: tts.speak
    target:
      entity_id: tts.home_assistant_cloud
    data:
      media_player_entity_id: media_player.kitchen
      message: "{{ said.data }}"
```

`person.notes` is part of the summary ("Note: Pay £15 cash"), so reminders come for free.
Write notes as instructions to yourself.

### 8.3 Lights and security

- **Porch light on at night for any visitor.** Trigger on `binary_sensor.smarteye_fdoor_visitor`
  turning `on`, with a `sun` condition (below horizon) and `mode: restart`. Turn it off when the
  sensor goes `off`. It includes the household, which is what you want for a porch light.
- **Night visitor alert.** Trigger on the arrival `event_type`s, with a `time` condition
  (after 22:00, before 06:00). Notify with `interruption-level: time-sensitive`.
- **Lingering.** Trigger on `binary_sensor.smarteye_fdoor_visitor` being on `for: "00:04:00"`.
  Add a condition that the doorbell press event hasn't fired since the sensor turned on. Someone
  has been at the door for about 2 minutes of faces without ringing (the sensor stays on 2
  minutes after the last face). Best combined with "nobody home".
- **Unanswered ring.** Trigger on the doorbell press event (e.g. Ring's ding), with a condition that
  `binary_sensor.smarteye_fdoor_visitor` is off (if it's on, someone's been seen). Then
  `wait_for_trigger` on it turning on, with a 30 s `timeout`. If it times out
  (`wait.trigger` is none), someone rang and the camera never saw their face: they stood out
  of view, or turned away.
- **Unusual time.** `unusual_time` events mean a regular at an hour they never come, such as
  the milkman at 3 pm. Worth a low-priority notification.

### 8.4 Routine and reminders

- **Morning brief, 07:30.** Read `sensor.smarteye_expected_today` (with times and notes),
  `sensor.smarteye_overdue` and `sensor.smarteye_yesterday`'s `notable`:

  ```yaml
  message: >
    {% set exp = state_attr('sensor.smarteye_expected_today', 'people') or [] %}
    {% set late = state_attr('sensor.smarteye_overdue', 'people') or [] %}
    {% set y = states.sensor.smarteye_yesterday.attributes %}
    Yesterday: {{ states('sensor.smarteye_yesterday') }} visits, {{ y.new_visitors }} new.
    {% for n in y.notable %}{{ n.time }} {{ n.name }} ({{ n.why | join(', ') }}). {% endfor %}
    {% if exp %}Expected today: {% for p in exp %}{{ p.name }}{% if p.usual_from %} {{ p.usual_from }}–{{ p.usual_to }}{% endif %}{{ ', ' if not loop.last }}{% endfor %}.{% endif %}
    {% if late %}Overdue: {% for p in late %}{{ p.name }} ({{ p.days_since_last }} days){{ ', ' if not loop.last }}{% endfor %}.{% endif %}
  ```

- **"Leave the cash out."** When `expected_today` includes the window cleaner and the person
  sensor's `notes` mention payment, remind the night before or that morning.
- **Didn't come.** At the end of someone's window (`usual_to` in `expected_today`), if their
  `seen_today` is still false, notify "The milkman hasn't been yet". Run it every 15 minutes, or use one
  automation per regular with a `time` trigger.
- **Overdue regulars.** Notify when `sensor.smarteye_overdue` rises above 0, or give its
  `people` a dashboard card. The deliveries that stopped, a carer who missed a visit.
- **Expected while away.** When someone's due and `zone.home` is 0, tell the house phone or
  leave a note on the door display.

### 8.5 Dashboard

| Card | Shows |
|---|---|
| Picture entity, `image.smarteye_fdoor_face` | The latest face at the door, live from arrival |
| Tile, `binary_sensor.smarteye_fdoor_visitor` | Someone there now |
| Entities: `sensor.smarteye_fdoor_last_visitor` with attribute rows (`summary`, `start`) | Who, and what's known |
| Glance: `visits_today`, `unknown_today`, `people_today`, `new_visitors_7d` | Today at a glance |
| Markdown over `expected_today` / `overdue` / `yesterday` | Same loops as the morning brief |
| Entities, sorted by state: the person sensors | "Last seen 3 hours ago" for each regular |
| Statistics graph, `sensor.smarteye_visits_today`, stat `change`, period `day` | Visits per day over months (long-term statistics, kept past recorder purges). It's a `total_increasing` sensor, so `change` is the only daily stat HA keeps for it |
| Logbook filtered to `event.smarteye_visitor` | Every arrival, identification and close with its type |

Use a tap action on any card to link to the SmartEye UI
(your `SMARTEYE_LOGIN_PORT` address, e.g. `https://smarteye.local:3001`) for the full dossier, heatmap and faces.

### 8.6 Presence and automations that use who it is

- **Welcome home.** `household_arrival` fires seconds after one of the household reaches the
  door, often before the phone's zone changes. Use `identity` to tell people apart and
  `door` for which way they came in: hall lights, heating, "Alex is home". It's a face
  match, so don't use it to unlock anything.
- **Known people as an arrival hint.** For other Immich people (family, friends),
  `known_arrival` fires the same way, and works for people without the app.
- **Per-person counters.** A `counter` helper incremented on the end-of-visit events
  (`visit_ended`, `new_visitor`, `unusual_time`) with a given `identity` gives HA-side history
  per person, independent of SmartEye's retention. Not for the household: their visits fire no
  end event.
- **Do not disturb.** Mute the chime when the arrival is a regular delivery (`identity` in a
  list) during the day, but notify for everyone else.

### 8.7 What HA can't do (yet)

- See the visit log, heatmaps or a person's faces: those are UI only. Link to the UI.
- Correct an identity from HA (merge, rename, "that wasn't X"): UI only.
- Hear about folds and merges: they're recorded in the database and shown in the UI (and in
  the JSON API, `folded`, `merged_from` and `merged`), not published over MQTT.

---

## 9. Limits and design choices

- **Habits need history.** `usual` and `timing` need 3 visits, rhythm and `overdue` need 4
  visit days, `unusual_time` needs 5 visits. Until then they're null, `unknown` or false.
- **Timing rules stay in HA.** SmartEye doesn't publish events that are a timer over signals
  HA already has, such as an unanswered ring or someone lingering at the door. See §0.
- **Faces aren't proof.** Recognition is a similarity score. Use it to inform and announce,
  never to unlock a door or disarm an alarm.
