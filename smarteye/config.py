"""Settings, read once from the environment (see .env.example)."""
import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: str) -> bool:
    return _env(name, default).lower() not in ("0", "false", "no", "off", "")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    ingest_token: str                # ImmichEye sends this as a Bearer token; empty = no check
    auth: str                        # "user:password" for the web UI and API; empty = open
    auth_mode: str                   # basic | authelia | none (see server.py, "access")
    trusted_proxies: tuple[str, ...] # authelia: addresses allowed to vouch for a user
    user_header: str                 # authelia: header the proxy sets after login
    tz: str                          # local time zone for heatmaps and "today"
    cameras: tuple[str, ...]         # cameras enabled when first seen; the rest are recorded but ignored
    camera_labels: tuple[tuple[str, str], ...]   # camera -> friendly name ("Front door"), for HA
    model: str                       # face model; embeddings from another model are refused
    # Quality gate: smaller or less certain faces join visits but never make or match identities
    min_face_px: int
    min_det: float
    # Visits
    visit_gap_s: int                 # a camera's visit ends this long after its last face
    visit_link: float                # cosine for a face to join an open visit
    fold_faces: int                  # an unrecognised visit with this many faces or fewer...
    fold_s: int                      # ...this close to another visit at its door is part of that one
    # Identities
    match_threshold: float           # a visit joins an identity at this score...
    match_margin: float              # ...and this far ahead of the runner-up
    top_k: int                       # identity score = mean of the k best similarities
    ref_faces: int                   # best faces per identity used for matching
    suggest_threshold: float         # identities this similar are offered for merging
    retention_days: int              # forget visits older than this (0 = keep forever)
    # MQTT / Home Assistant
    mqtt_url: str                    # mqtt://user:pass@host:1883; empty = off
    mqtt_prefix: str
    ha_discovery: str                # Home Assistant discovery prefix; empty = no discovery
    public_url: str                  # base URL used in links sent to Home Assistant
    # Doorbell: Home Assistant reports presses
    doorbell_token: str              # Bearer token for /api/doorbell; empty = endpoint off
    ring_window_s: int               # a ring before anyone's seen waits this long for a visit

    def camera_label(self, camera: str) -> str:
        return dict(self.camera_labels).get(camera, camera)


def load() -> Settings:
    auth = _env("SMARTEYE_AUTH", "")
    mode = (_env("SMARTEYE_AUTH_MODE", "") or ("basic" if auth else "none")).lower()
    if mode not in ("basic", "authelia", "none"):
        raise ValueError(f"SMARTEYE_AUTH_MODE must be basic, authelia or none, not {mode!r}")
    proxies = tuple(p.strip() for p in _env("SMARTEYE_TRUSTED_PROXIES", "").split(",") if p.strip())
    if mode == "basic" and not auth:
        raise ValueError("SMARTEYE_AUTH_MODE=basic needs SMARTEYE_AUTH=user:password")
    if mode == "authelia" and not proxies:
        raise ValueError("SMARTEYE_AUTH_MODE=authelia needs SMARTEYE_TRUSTED_PROXIES (the login proxy's address)")
    return Settings(
        data_dir=Path(_env("SMARTEYE_DATA", "/data")),
        ingest_token=_env("SMARTEYE_INGEST_TOKEN", ""),
        auth=auth,
        auth_mode=mode,
        trusted_proxies=proxies,
        user_header=_env("SMARTEYE_USER_HEADER", "Remote-User"),
        tz=_env("SMARTEYE_TZ", _env("TZ", "Europe/London")),
        cameras=tuple(c.strip() for c in _env("SMARTEYE_CAMERAS", "FDoor,BDoor").split(",") if c.strip()),
        camera_labels=tuple(
            (c.split(":", 1)[0].strip(), c.split(":", 1)[1].strip())
            for c in _env("SMARTEYE_CAMERA_LABELS", "").split(",") if ":" in c),
        model=_env("SMARTEYE_MODEL", "buffalo_l"),
        min_face_px=int(_env("SMARTEYE_MIN_FACE_PX", "40")),
        min_det=float(_env("SMARTEYE_MIN_DET", "0.75")),
        visit_gap_s=int(_env("SMARTEYE_VISIT_GAP_S", "120")),
        visit_link=float(_env("SMARTEYE_VISIT_LINK", "0.30")),
        fold_faces=int(_env("SMARTEYE_FOLD_FACES", "2")),
        fold_s=int(_env("SMARTEYE_FOLD_S", "5")),
        match_threshold=float(_env("SMARTEYE_MATCH_THRESHOLD", "0.45")),
        match_margin=float(_env("SMARTEYE_MATCH_MARGIN", "0.05")),
        top_k=int(_env("SMARTEYE_TOP_K", "3")),
        ref_faces=int(_env("SMARTEYE_REF_FACES", "30")),
        suggest_threshold=float(_env("SMARTEYE_SUGGEST_THRESHOLD", "0.33")),
        retention_days=int(_env("SMARTEYE_RETENTION_DAYS", "365")),
        mqtt_url=_env("SMARTEYE_MQTT_URL", ""),
        mqtt_prefix=_env("SMARTEYE_MQTT_PREFIX", "smarteye").strip("/"),
        ha_discovery=_env("SMARTEYE_HA_DISCOVERY", "homeassistant").strip("/"),
        public_url=_env("SMARTEYE_PUBLIC_URL", "").rstrip("/"),
        doorbell_token=_env("SMARTEYE_DOORBELL_TOKEN", ""),
        ring_window_s=int(_env("SMARTEYE_RING_WINDOW_S", "30")),
    )
