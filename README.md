# SmartEye

Who comes to the door, how often, and when.

SmartEye builds a database of everyone your door cameras see. People you've named in Immich are
recognised by [ImmichEye](https://github.com/JameZUK/ImmichEye). Strangers are grouped into
**visitors** by their faces, so the courier, the window cleaner and the person who has called
three times this week each get their own record without you doing anything. A web UI shows
each person's visits, first and last sighting, the times and days they usually come, how
regularly they come, and their faces. Home Assistant gets an event for every arrival.

- **No extra inference.** ImmichEye already detects and embeds every face Blue Iris sends.
  SmartEye stores what it would otherwise throw away.
- **Unknown visitors become identities.** Visits are matched face to face (the same
  `buffalo_l` model and scoring as ImmichEye). Weak matches are offered for review rather than
  merged automatically.
- **Door cameras only by default.** Other cameras are listed but their faces are never stored.

![Overview: today's figures, who's at the door, recent visits, when people come and the regulars](docs/screenshots/overview.png)

<table>
<tr>
<td><img src="docs/screenshots/person.png" alt="A person's dossier: visits, usual time, rhythm, heatmap and visits per week"></td>
<td><img src="docs/screenshots/people.png" alt="People: known people, the household and visitors"></td>
</tr>
<tr>
<td><img src="docs/screenshots/visit.png" alt="One visit: the frame, every face, and who it might be"></td>
<td><img src="docs/screenshots/visits-dark.png" alt="The visit log in the dark theme"></td>
</tr>
</table>

<sub>Screenshots from the demo data (`scripts/demo.py`): made-up people with drawn faces.</sub>

## How it works

```
Blue Iris ─► ImmichEye ─► Immich ML                 (unchanged: names for Blue Iris)
                │
                └─ face event: camera, time, box, embedding, crop, ImmichEye's name
                       │
                       ▼
                   SmartEye ─► SQLite + crops ─► web UI
                       └─────► MQTT ─► Home Assistant
```

**Sightings → visits.** Each face in a frame is a *sighting*. It joins an open *visit* on the
same camera if it's the same person: same ImmichEye name, or cosine ≥ `SMARTEYE_VISIT_LINK`
to that visit's faces. Faces too poor to compare join whoever is already there. Two people in
one frame get two visits. A visit ends `SMARTEYE_VISIT_GAP_S` (120 s) after its last face.
A visit that ends unrecognised with only a face or two (`SMARTEYE_FOLD_FACES`), overlapping
another visit at that door or within `SMARTEYE_FOLD_S` of it, is a stray frame of that visit
(someone looking down as they leave) and is folded into it, unless the two share a frame.

**Visits → identities.** When a visit ends it gets an identity:

| How | When |
|---|---|
| **named** | ImmichEye named someone in it. The name with the most total score wins. |
| **matched** | Its best faces match an existing identity's best faces: mean of the top 3 similarities ≥ 0.45 and 0.05 ahead of the runner-up, as ImmichEye scores. Known people's CCTV faces count too, so an Immich person ImmichEye missed on a bad angle can still be matched. |
| **new** | Nobody matches: a new visitor, "Visitor 12". |
| **none** | No face good enough to identify (under 40 px, or detector score under 0.75). The visit still counts. |

ImmichEye labels in different styles (`cal_smith`, `Cal Smith`) are the same person.

**Descriptions (optional).** With `SMARTEYE_CAPTION_MODEL` set, each visit's frame goes to a
vision model on [Ollama](https://ollama.com) when it starts, with what face recognition knows
(who it is, household or visitor, how often they come). Back comes a line for a notification,
"Sam's back with the shopping and looks tired" or "Royal Mail with a large parcel", and the
visitor's likely role, what they're carrying, mood and weather when visible. It takes about a
second with `mimo-v2.6-pro` on Ollama's cloud (about $0.0004 a visit). The visit is described
again if it's later recognised as someone else by name. See Privacy: in the cloud, the frame
leaves your network.

**Intel per identity:** visit count, first and last seen, visits per week, a weekday × hour
heatmap, the usual window (the narrowest time of day, up to 4 h, holding 60% of visits, and the
days they cluster on), rhythm (median days between visits, when the next is due, overdue if
it's been twice that), time at the door, and cameras.

## The web UI

| Page | |
|---|---|
| **Overview** | Today's visits, people, unknowns and new visitors this week; who's at the door now; recent visits; 30-day heatmap and daily bars; regulars |
| **People** | Everyone, known and visitors, by last seen, visits or name |
| **Person** | The dossier: stats, heatmap, weekly visits, faces, similar identities, visit log, who was merged in and when. Rename, add notes, mark as household, merge or forget |
| **Visits** | Every arrival, by day, with its description if there is one, filterable by camera and known / visitor / unidentified. A visit with stray faces folded in says how many, and marks those faces; one that came to its person by a merge says MERGED IN (and who it was seen as) instead of FIRST VISIT |
| **Visit** (click any visit) | The description (role, carrying, mood, weather, model), every face with size and scores, the frame if sent, and "who is this?": the closest identities, someone else, a new visitor, or delete (posters, reflections) |
| **Review** | Pairs of identities that look like the same person: merge or mark as different |
| **Settings** | Cameras on/off, feed status, MQTT, the matching settings in use |

**Household** (`ignored` in the API and Home Assistant): your household are still matched (so
they never become a "new visitor") and shown in People with a Household tag. They're counted
apart in today's figures (`household`), left out of regulars, expected and overdue, and fire
only `household_arrival` in Home Assistant, so visitor automations never hear them. What to do
about any of it is up to Home Assistant.

## Installation

Requirements: ImmichEye running, and Docker with Compose on a machine ImmichEye can reach.

```sh
git clone https://github.com/JameZUK/SmartEye smarteye && cd smarteye
cp .env.example .env     # set SMARTEYE_AUTH, SMARTEYE_INGEST_TOKEN, SMARTEYE_PUBLIC_URL at least
mkdir -p data
docker compose up -d --build
```

Open `http://<host>:8090`. Then point ImmichEye at it:

```
IMMICHEYE_EVENTS_URL=http://<host>:8090/api/events
IMMICHEYE_EVENTS_TOKEN=<SMARTEYE_INGEST_TOKEN>
IMMICHEYE_EVENTS_CAMERAS=FDoor,BDoor
IMMICHEYE_EVENTS_FRAME_PX=640
```

The event format ImmichEye sends is specified in ImmichEye's [docs/face-events.md](https://github.com/JameZUK/ImmichEye/blob/main/docs/face-events.md).

### Login with Authelia

Instead of the browser's basic-auth dialog, SmartEye can sit behind
[Authelia](https://www.authelia.com): a real login page, a session cookie, rate limiting and
optional 2FA. The compose file has it as an optional profile:

```
browser ─► proxy (nginx, HTTPS :3001) ── auth_request ─► authelia
              └─► smarteye:8090, with Remote-User set by the proxy
```

1. A DNS name for the host (`SMARTEYE_HOSTNAME`). Authelia's cookie is tied to it, and the
   proxy redirects any other name or bare IP there.
2. A certificate for that name in `tls/cert.pem` and `tls/key.pem`, e.g. self-signed:
   ```sh
   mkdir -p tls && openssl req -x509 -newkey rsa:2048 -nodes -days 1190 -subj "/CN=$NAME" \
     -addext "subjectAltName=DNS:$NAME,IP:$IP" -keyout tls/key.pem -out tls/cert.pem
   ```
3. `authelia/configuration.yml` from `deploy/authelia/configuration.yml.example` (fill in the
   secrets and the hostname) and `authelia/users_database.yml`. Keep the directory mode 700.
4. In `.env`:
   ```
   COMPOSE_PROFILES=authelia
   SMARTEYE_AUTH_MODE=authelia
   SMARTEYE_TRUSTED_PROXIES=172.25.0.10
   SMARTEYE_HOSTNAME=smarteye.lan
   ```
5. `docker compose down && docker compose up -d --build` (down, because the network gets a fixed subnet).

In this mode SmartEye accepts a user only from the proxy's fixed address
(`SMARTEYE_PROXY_IP`, default `172.25.0.10`), which always overwrites `Remote-User`. Anything
else reaching port 8090 directly gets 403, apart from what carries its own credentials:
ImmichEye's token-checked `/api/events`, Home Assistant's token-checked `/api/doorbell`, signed
image and frame links for Home Assistant, and `/healthz`.
So keep `IMMICHEYE_EVENTS_URL` and `SMARTEYE_PUBLIC_URL` on port 8090.

In every mode, changes through the API must be same-site: a POST must be JSON, and a request
whose `Origin` is another site is refused, so a page elsewhere can't use your login.

## Home Assistant

Set `SMARTEYE_MQTT_URL`. With discovery on (default) a **SmartEye** device appears with the
entities below. [docs/home-assistant-data.md](docs/home-assistant-data.md) is the full
reference: every topic and field, when each is published, and automations and dashboards to
build with them.

| Entity | |
|---|---|
| `event.smarteye_visitor` | Fires on arrival: `known_arrival` (an Immich person), `visitor_arrival` (a visitor seen before), `unknown_arrival`. `identified` while the visit is open, when the guess firms up or changes. `doorbell` when the bell rings during a visit. When a visit ends: `new_visitor` (first time ever), `unusual_time` (someone with five or more visits, at an hour they've never come), or `visit_ended`. `household_arrival` for ignored people, so household automations stay apart. With descriptions on, `described` (or `household_described`) a second or two after arrival, with the description |
| `binary_sensor.smarteye_visitor_present` | On while any visit is open; attribute `visitors` |
| `binary_sensor.smarteye_<camera>_visitor` | The same for one door (one per enabled camera, e.g. `_fdoor_visitor`) |
| `sensor.smarteye_last_visitor` | Last identified visitor (household excepted), with the event attributes |
| `sensor.smarteye_<camera>_last_visitor` | The latest visitor at that door, from the moment they arrive (ignored people excepted); `Unrecognised` when nobody was identified |
| `sensor.smarteye_visits_today`, `_unknown_today` (unknown or unidentified), `_people_today` | Today's counts, household excluded. `visits_today` attributes add `household`, `rings` and `unanswered` |
| `sensor.smarteye_expected_today` | People due today (by their usual gap or usual weekday); attribute `people` with usual times and `seen_today` |
| `sensor.smarteye_overdue` | Regulars past twice their usual gap (dropped after four gaps, at least 14 days) |
| `sensor.smarteye_yesterday` | Yesterday's visits; attributes `unknown`, `people`, `new_visitors` and `notable` (new visitors and regulars at an unusual time, with time and length) |
| `sensor.smarteye_new_visitors_7d` | New visitors this week |
| `sensor.smarteye_<name>` | Last seen (timestamp) for each named identity and each unnamed visitor with five or more visits, with what SmartEye knows about them as attributes |

Event attributes: `name`, `named` (a real name, not "Visitor N"), `camera`, `door`, `visit_number`, `provisional`, `start`, `end`,
`duration_s`, `local_hour`, `rang`, `image`, `frame`, and when there's an identity:

- `person`: their visits before this one (`visits`, `visits_30d`, `first_seen`, `last_seen`,
  `days_since_last`), their `usual` window (`from`, `to`, `days`), `timing` (`on_time`, `early`,
  `late`, `unusual_day` or `unknown`), `median_gap_days`, `next_expected`, `overdue`,
  `avg_dwell_s`, `cameras` and your `notes`;
- `changed_from` on the end-of-visit event when the final identity isn't the one announced;
- `summary`: those facts as one line, for an announcer ("Window cleaner is at the front door,
  15th visit, usually Sat 09:30–11:30, last here 4 weeks ago, on time.");
- `description` and `scene` once the visit is described: the model's line, and `role` (household,
  delivery, postal, food delivery, trades, friend or family, neighbour, canvasser or seller,
  unknown), `role_confidence`, `carrying`, `appearance`, `mood`, `weather`, `infrared`. Every
  later event of the visit and the door's sensor carry them too.

For a notification with the description, trigger on `described` rather than the arrival: the
arrival goes out at once, the description a second or two later.

Arrivals are identified from the faces seen so far (`provisional: true`), and `identified`
follows as better faces arrive; the final identity comes with the end-of-visit event. Ignored
identities fire only `household_arrival`, once per visit. Person sensors keep a numeric `unique_id`, so renaming someone
in SmartEye never breaks an automation (sensors created before readable ids keep
`sensor.smarteye_person_<id>`).

`SMARTEYE_CAMERA_LABELS=FDoor:Front door,BDoor:Back door` gives doors friendly names, used in
entity names and `summary`.

### Faces in notifications

Each door gets two image entities, `image.smarteye_fdoor_face` (the best face crop) and
`image.smarteye_fdoor_frame` (the whole frame), sent as raw JPEG on the retained topics
`smarteye/camera/<camera>/face` and `/frame`. Home Assistant serves them behind its own login,
so use `/api/image_proxy/image.smarteye_fdoor_face` as the notification image; it works away
from home. The pictures are published before the door topic and the event, so an automation
triggered by either gets the new face. The household is never sent, and forgetting someone
clears their pictures from the broker. Anyone who can read `smarteye/#` on the broker can see
the faces: give SmartEye its own broker user if the login is shared.

The payloads' `image` and `frame` are signed links on `SMARTEYE_PUBLIC_URL` to the same
pictures, usable on your network without the UI login.

```yaml
triggers:
  - trigger: state
    entity_id: event.smarteye_visitor
conditions:
  - condition: template
    value_template: "{{ trigger.to_state.attributes.event_type in ['unknown_arrival', 'new_visitor'] }}"
actions:
  - action: notify.mobile_app_phone
    data:
      title: "{{ trigger.to_state.attributes.name or 'Someone' }} at {{ trigger.to_state.attributes.door }}"
      message: "{{ trigger.to_state.attributes.summary }}"
      data:
        image: /api/image_proxy/image.smarteye_fdoor_face
```

A notification fetches the picture when it's shown, so two visitors at the same door seconds
apart can show the second face.

### Doorbell

Set `SMARTEYE_DOORBELL_TOKEN` and call SmartEye from the doorbell automation:

```yaml
rest_command:
  smarteye_doorbell:
    url: http://smarteye.example.lan:8090/api/doorbell
    method: post
    headers:
      Authorization: !secret smarteye_doorbell_bearer   # "Bearer <token>"
    content_type: application/json
    payload: '{"camera": "FDoor"}'
```

A ring during a visit marks it `rang` and fires `doorbell` with who's there. A ring before
anyone's seen waits `SMARTEYE_RING_WINDOW_S` (30 s) for a visit on that camera; if none comes
it counts as `unanswered`. End-of-visit events then say whether the visitor rang.

### Forgetting

Renaming, merging or forgetting someone republishes the last-visitor topics, so no retained
topic keeps a name or face that's gone, and a forgotten face's link stops working. Home
Assistant's own history still holds the old states and attributes (names, notes); clear them
with `recorder.purge_entities`.

## Settings reference

All in `.env` (see `.env.example`).

| Setting | Default | |
|---|---|---|
| `SMARTEYE_AUTH_MODE` | `basic` if `SMARTEYE_AUTH` is set, else `none` | `basic`, `authelia` or `none`; see "Login with Authelia" |
| `SMARTEYE_AUTH` | | `user:password` for basic mode. With no login at all, anyone on the network sees every face |
| `SMARTEYE_TRUSTED_PROXIES` / `SMARTEYE_USER_HEADER` | / `Remote-User` | Authelia mode: the proxy's address(es), and the header it sets |
| `SMARTEYE_INGEST_TOKEN` | | Bearer token ImmichEye must send |
| `SMARTEYE_PUBLIC_URL` | | Base URL for the image and frame links in payloads (the pictures themselves go over MQTT) |
| `SMARTEYE_CAMERA_LABELS` | | Friendly door names, `FDoor:Front door,BDoor:Back door` |
| `SMARTEYE_DOORBELL_TOKEN` | | Bearer token for `/api/doorbell`; empty = off |
| `SMARTEYE_RING_WINDOW_S` | 30 | A ring before anyone's seen waits this long for a visit |
| `SMARTEYE_TZ` | `Europe/London` | Local time for "today", heatmaps and usual times |
| `SMARTEYE_CAMERAS` | `FDoor,BDoor` | Cameras enabled when first seen |
| `SMARTEYE_MODEL` | `buffalo_l` | Events from another model are refused (409) |
| `SMARTEYE_MIN_FACE_PX` / `_MIN_DET` | 40 / 0.75 | Quality gate for identifying faces |
| `SMARTEYE_VISIT_GAP_S` | 120 | Seconds without a face that end a visit |
| `SMARTEYE_VISIT_LINK` | 0.30 | Cosine for a face to join an open visit |
| `SMARTEYE_FOLD_FACES` / `_FOLD_S` | 2 / 5 | An unrecognised visit this small, this close to another at its door, is folded into it |
| `SMARTEYE_MATCH_THRESHOLD` / `_MARGIN` | 0.45 / 0.05 | Visit-to-identity matching |
| `SMARTEYE_TOP_K` | 3 | Similarities averaged into a score |
| `SMARTEYE_REF_FACES` | 30 | Best faces kept per identity for matching (at most 5 from one visit) |
| `SMARTEYE_SUGGEST_THRESHOLD` | 0.33 | Pairs at or above this are offered on Review |
| `SMARTEYE_RETENTION_DAYS` | 365 | Visits older than this are forgotten; 0 = never |
| `SMARTEYE_MQTT_URL` | | `mqtt://user:pass@host:1883`; empty = off |
| `SMARTEYE_MQTT_PREFIX` / `SMARTEYE_HA_DISCOVERY` | `smarteye` / `homeassistant` | Topics; empty discovery = no discovery |
| `SMARTEYE_CAPTION_MODEL` | | Vision model that describes each visit, e.g. `mimo-v2.6-pro`; empty = off |
| `SMARTEYE_CAPTION_URL` | `https://ollama.com` | Ollama's cloud, or your own Ollama server (`http://host:11434`; a cloud model through it is named `mimo-v2.6-pro:cloud`) |
| `OLLAMA_API_KEY` | | Ollama API key, for ollama.com |
| `SMARTEYE_CAPTION_TIMEOUT_S` | 30 | Give up on a description after this long |

**Tuning.** The match threshold is stricter than ImmichEye's (0.45 against 0.40) because a
wrong merge is worse than a duplicate: duplicates show up on Review and merge in one click; a
wrong merge needs visits moved by hand. If one person keeps appearing as several visitors,
lower `SMARTEYE_SUGGEST_THRESHOLD` to see more pairs, or the match threshold a little.

## API

The UI's JSON API, under the same login. `POST /api/events` (token) is the ingest.

| | |
|---|---|
| `GET /api/summary` | Overview data |
| `GET /api/identities?kind=&q=&ignored=&sort=` · `GET /api/identities/{id}` | People; one dossier (`merged`: `[{ts, name, visits}]` merged in) |
| `PATCH /api/identities/{id}` `{name, notes, ignored}` · `POST …/{id}/merge` `{into}` · `DELETE …/{id}` | Edit, merge, forget |
| `GET /api/visits?identity=&camera=&kind=&since=&until=&before=` · `GET /api/visits/{id}` | Visits (`folded`: faces folded in; `merged_from`: who it was seen as, if a merge moved it; `caption`: the description, with `summary`, the scene fields, `model` and `for`, the named identity it was written for); one with faces (`folded_from`: the stray visit's old id) and candidates |
| `POST /api/visits/{id}/assign` `{identity: id or "new"}` · `DELETE /api/visits/{id}` | Move or delete a visit |
| `GET /api/suggestions` · `POST /api/suggestions/dismiss` `{a, b}` | Review |
| `GET /api/cameras` · `PATCH /api/cameras/{name}` `{enabled}` · `GET /api/status` | Settings |
| `GET /img/s/{sighting}.jpg` · `GET /img/e/{event}.jpg` | Face crop; frame |

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest
SMARTEYE_DATA=devdata/run .venv/bin/uvicorn smarteye.server:app --port 8090
```

**Demo data.** `scripts/demo.py` builds ten weeks of made-up visitors with drawn faces, for
trying the UI or taking screenshots without a camera:

```sh
.venv/bin/python scripts/demo.py demo-data
SMARTEYE_DATA=demo-data SMARTEYE_AUTH_MODE=none SMARTEYE_VISIT_GAP_S=3600 \
  SMARTEYE_CAMERA_LABELS="FDoor:Front door,BDoor:Back door" .venv/bin/uvicorn smarteye.server:app --port 8090
```

`smarteye/engine.py` (sightings, visits, identities, edits), `stats.py` (the intel),
`server.py` (API), `mqtt.py` (Home Assistant), `caption.py` (descriptions), `static/` (the UI:
plain ES modules, no build step).

## Privacy

SmartEye stores facial embeddings and face crops of everyone at your door, including people
who never agreed to it. In the UK (and EU) a camera that covers anything beyond your own
property, such as the street or a neighbour's garden, takes you out of the domestic exemption,
and facial templates are special-category data under UK GDPR. Keep cameras pointed at your own
property, keep the retention period no longer than you need, set a UI password, and use
**Forget** when someone asks. Nothing leaves your network except what you send to Home Assistant,
and, with descriptions on and a cloud model, each visit's frame with the name face recognition
gave it. Ollama says it doesn't log or train on prompts or responses, and hosts models mainly in
the US. Run the model on your own Ollama server (`SMARTEYE_CAPTION_URL`) to keep frames at home.

## License

[MIT](LICENSE)
