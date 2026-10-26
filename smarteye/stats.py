"""The intel: when people come, how often, how regularly. All times local (SMARTEYE_TZ)."""
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from .engine import Engine

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SLACK_MIN = 30          # this close outside someone's usual window still counts as on time
LAPSED = 4              # overdue people drop off the list after this many usual gaps (at least 14 days)


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def heatmap(e: Engine, starts: list[float]) -> list[list[int]]:
    """Visits by weekday (Mon first) and hour."""
    grid = [[0] * 24 for _ in range(7)]
    for ts in starts:
        t = e.local(ts)
        grid[t.weekday()][t.hour] += 1
    return grid


def typical_window(e: Engine, starts: list[float]) -> dict | None:
    """The narrowest time of day (up to 4 h) holding at least 60% of visits, and the days
    they cluster on. None until there are three visits."""
    n = len(starts)
    if n < 3:
        return None
    mins = sorted(e.local(ts).hour * 60 + e.local(ts).minute for ts in starts)
    best = None
    for width in (60, 90, 120, 180, 240):
        for m0 in mins:   # windows starting at a visit, wrapping past midnight
            inside = sum(1 for m in mins if (m - m0) % 1440 < width)
            if best is None or inside > best[2] or (inside == best[2] and width < best[1]):
                best = (m0, width, inside)
        if best[2] / n >= 0.6:
            break
    m0, width, inside = best
    m0 -= m0 % 15   # start on a quarter hour
    end = (m0 + width + 14) // 15 * 15
    days = Counter(e.local(ts).weekday() for ts in starts)
    top = [d for d, c in sorted(days.items()) if c >= max(1, 0.15 * n)]
    clustered = len(top) <= 4 and sum(days[d] for d in top) >= 0.8 * n
    return {
        "from": f"{m0 // 60 % 24:02d}:{m0 % 60:02d}",
        "to": f"{end // 60 % 24:02d}:{end % 60:02d}",
        "share": round(inside / n, 2),
        "days": [DAYS[d] for d in top] if clustered else [],
    }


def regularity(e: Engine, starts: list[float], now: float | None = None) -> dict | None:
    """Median days between visits (one per day at most), when the next is due, and whether
    they're overdue (no visit in twice their usual gap). None under four visit days."""
    now = now or time.time()
    days = sorted({e.local(ts).date().toordinal() for ts in starts})
    if len(days) < 4:
        return None
    gaps = [b - a for a, b in zip(days, days[1:])]
    med = statistics.median(gaps)
    last = max(starts)
    return {
        "median_gap_days": med,
        "next_expected": last + med * 86400,
        "overdue": now - last > max(2 * med, 2) * 86400,
    }


def identity_stats(e: Engine, ident: int) -> dict:
    rows = e.db.q("SELECT start_ts, end_ts, camera FROM visits WHERE identity_id = ? ORDER BY start_ts", (ident,))
    starts = [r["start_ts"] for r in rows]
    dwell = [r["end_ts"] - r["start_ts"] for r in rows if r["end_ts"] > r["start_ts"]]
    now = time.time()
    weeks = weekly(e, starts, 12, now)
    return {
        "visits": len(rows),
        "first_seen": starts[0] if starts else None,
        "last_seen": max(r["end_ts"] for r in rows) if rows else None,
        "visits_30d": sum(1 for ts in starts if ts >= now - 30 * 86400),
        "cameras": dict(Counter(r["camera"] for r in rows).most_common()),
        "avg_dwell_s": round(statistics.mean(dwell)) if dwell else None,
        "heatmap": heatmap(e, starts),
        "weekly": weeks,
        "window": typical_window(e, starts),
        "regularity": regularity(e, starts, now),
    }


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def timing(e: Engine, window: dict | None, ts: float) -> str:
    """How a visit at ts compares with someone's usual window: on_time, early, late,
    unusual_day (not one of the days they cluster on) or unknown (no window yet)."""
    if not window:
        return "unknown"
    t = e.local(ts)
    if window["days"] and DAYS[t.weekday()] not in window["days"]:
        return "unusual_day"
    m, start, end = t.hour * 60 + t.minute, _mins(window["from"]), _mins(window["to"])
    if (m - start) % 1440 <= (end - start) % 1440:
        return "on_time"
    early, late = (start - m) % 1440, (m - end) % 1440
    if min(early, late) <= SLACK_MIN:
        return "on_time"
    return "early" if early < late else "late"


def identity_brief(e: Engine, ident: int, at: float | None = None, visit: int | None = None,
                   now: float | None = None) -> dict:
    """What Home Assistant hears about a person: identity_stats without the charts. Given a
    visit and its start (at), everything describes the visits before it, so "last_seen" is
    the previous visit, and timing compares this one with their usual time."""
    now = now or time.time()
    ref = at or now
    i = e.db.one("SELECT notes FROM identities WHERE id = ?", (ident,))
    rows = [r for r in e.db.q("SELECT id, start_ts, end_ts, camera FROM visits WHERE identity_id = ? ORDER BY start_ts",
                              (ident,))
            if r["id"] != visit and (at is None or r["start_ts"] < at)]
    starts = [r["start_ts"] for r in rows]
    dwell = [r["end_ts"] - r["start_ts"] for r in rows if r["end_ts"] > r["start_ts"]]
    last = max((r["end_ts"] for r in rows), default=None)
    w = typical_window(e, starts)
    reg = regularity(e, starts, ref)
    out = {
        "visits": len(rows),
        "visits_30d": sum(1 for ts in starts if ts >= ref - 30 * 86400),
        "first_seen": iso(starts[0]) if starts else None,
        "last_seen": iso(last),
        "days_since_last": (e.local(ref).date() - e.local(last).date()).days if last else None,
        "usual": w,
        "median_gap_days": reg["median_gap_days"] if reg else None,
        "next_expected": e.local(reg["next_expected"]).date().isoformat() if reg else None,
        "overdue": reg["overdue"] if reg else False,
        "avg_dwell_s": round(statistics.mean(dwell)) if dwell else None,
        "cameras": dict(Counter(r["camera"] for r in rows).most_common()),
        "notes": i["notes"] if i else "",
    }
    if at is not None:
        out["timing"] = timing(e, w, at)
    return out


def midnight(e: Engine, now: float) -> float:
    t = e.local(now)
    return t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def daily(e: Engine, starts: list[float], days: int, now: float) -> list[dict]:
    today = e.local(midnight(e, now)).date()
    counts = Counter(e.local(ts).date() for ts in starts)
    out = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        out.append({"date": d.isoformat(), "count": counts.get(d, 0)})
    return out


def weekly(e: Engine, starts: list[float], weeks: int, now: float) -> list[dict]:
    """Visits per week (weeks start Monday), oldest first."""
    t = e.local(now).date()
    monday = t - timedelta(days=t.weekday())
    counts = Counter()
    for ts in starts:
        d = e.local(ts).date()
        counts[d - timedelta(days=d.weekday())] += 1
    return [{"week": (monday - timedelta(weeks=i)).isoformat(), "count": counts.get(monday - timedelta(weeks=i), 0)}
            for i in range(weeks - 1, -1, -1)]


def today_counts(e: Engine, now: float) -> dict:
    """Today's visits, people and rings. Cheap: Home Assistant gets it after every visit change."""
    t0 = midnight(e, now)
    rows = e.db.q("""SELECT v.identity_id, i.kind, i.ignored FROM visits v
                     LEFT JOIN identities i ON i.id = v.identity_id WHERE v.start_ts >= ?""", (t0,))
    today = [r for r in rows if not r["ignored"]]   # the household (ignored) is counted apart
    rings = e.db.q("SELECT visit_id, ts FROM rings WHERE ts >= ?", (t0,))
    return {
        "visits": len(today),
        "people": len({r["identity_id"] for r in today if r["identity_id"] is not None}),
        "unknown": sum(1 for r in today if r["kind"] != "known"),
        "household": len(rows) - len(today),
        "rings": len(rings),
        # rings no visit claimed; one still inside its window may yet be claimed
        "unanswered": sum(1 for r in rings if r["visit_id"] is None and now - r["ts"] > e.s.ring_window_s),
    }


def new_visitors(e: Engine, now: float, days: int = 7) -> int:
    return e.db.one("""SELECT COUNT(*) AS n FROM identities i WHERE kind = 'unknown'
                       AND (SELECT MIN(start_ts) FROM visits WHERE identity_id = i.id) >= ?""", (now - days * 86400,))["n"]


def summary(e: Engine) -> dict:
    now = time.time()
    month = [r["start_ts"] for r in e.db.q("SELECT start_ts FROM visits WHERE start_ts >= ?", (now - 30 * 86400,))]
    counts = {r["kind"]: r["n"] for r in e.db.q("SELECT kind, COUNT(*) AS n FROM identities GROUP BY kind")}
    regulars = e.db.q("""SELECT i.id, i.name, i.kind, COUNT(*) AS n, MAX(v.end_ts) AS last,
                                (SELECT s.id FROM sightings s JOIN visits v2 ON v2.id = s.visit_id
                                 WHERE v2.identity_id = i.id AND s.crop IS NOT NULL
                                 ORDER BY s.usable DESC, s.quality DESC LIMIT 1) AS cover
                         FROM visits v JOIN identities i ON i.id = v.identity_id
                         WHERE v.start_ts >= ? AND i.ignored = 0
                         GROUP BY i.id ORDER BY n DESC, last DESC LIMIT 8""", (now - 30 * 86400,))
    open_visits = e.db.one("SELECT COUNT(*) AS n FROM visits WHERE closed = 0")["n"]
    return {
        "now": now,
        "tz": e.s.tz,
        "today": today_counts(e, now),
        "new_visitors_7d": new_visitors(e, now),
        "identities": {"known": counts.get("known", 0), "unknown": counts.get("unknown", 0)},
        "open_visits": open_visits,
        "heatmap_30d": heatmap(e, month),
        "daily_30d": daily(e, month, 30, now),
        "regulars": [dict(r) for r in regulars],
    }


def reports(e: Engine, now: float | None = None) -> dict:
    """Who's expected today, who's overdue, and yesterday's recap. Ignored people are left out."""
    now = now or time.time()
    t0 = midnight(e, now)
    today = e.local(now)
    by = defaultdict(list)
    names = {}
    for r in e.db.q("""SELECT v.identity_id, v.start_ts, v.end_ts, i.name FROM visits v
                       JOIN identities i ON i.id = v.identity_id WHERE i.ignored = 0 ORDER BY v.start_ts"""):
        by[r["identity_id"]].append(r)
        names[r["identity_id"]] = r["name"]
    expected, overdue = [], []
    for ident, rows in by.items():
        starts = [r["start_ts"] for r in rows]
        before = [ts for ts in starts if ts < t0]
        w, reg = typical_window(e, before), regularity(e, before, t0)
        due = reg is not None and not reg["overdue"] and e.local(reg["next_expected"]).date() <= today.date()
        usual_day = w is not None and len(before) >= 5 and DAYS[today.weekday()] in w["days"]
        if due or usual_day:
            expected.append({"identity": ident, "name": names[ident],
                             "usual_from": w["from"] if w else None, "usual_to": w["to"] if w else None,
                             "seen_today": any(ts >= t0 for ts in starts)})
        reg = regularity(e, starts, now)
        if reg and reg["overdue"]:
            last = max(r["end_ts"] for r in rows)
            if now - last <= max(LAPSED * reg["median_gap_days"], 14) * 86400:
                overdue.append({"identity": ident, "name": names[ident],
                                "days_since_last": (today.date() - e.local(last).date()).days,
                                "median_gap_days": reg["median_gap_days"]})
    expected.sort(key=lambda p: p["usual_from"] or "99")
    overdue.sort(key=lambda p: -p["days_since_last"])
    return {"expected_today": {"count": len(expected), "people": expected},
            "overdue": {"count": len(overdue), "people": overdue},
            "yesterday": yesterday(e, t0)}


def yesterday(e: Engine, t0: float) -> dict:
    """The day before midnight t0: counts as in summary()["today"], and the visits only SmartEye
    can call notable (a new visitor, a regular at an unusual time). Time of day and length are
    in every entry, for Home Assistant to judge."""
    y0 = midnight(e, t0 - 1)
    rows = [r for r in e.db.q("""SELECT v.*, i.name, i.kind, i.ignored FROM visits v
                                 LEFT JOIN identities i ON i.id = v.identity_id
                                 WHERE v.start_ts >= ? AND v.start_ts < ? ORDER BY v.start_ts""", (y0, t0))
            if not r["ignored"]]
    notable = []
    for r in rows:
        why = []
        if r["how"] == "new":
            why.append("new_visitor")
        if r["identity_id"] is not None:
            prior = [p["start_ts"] for p in e.db.q(
                "SELECT start_ts FROM visits WHERE identity_id = ? AND id != ? AND closed = 1 AND start_ts < ?",
                (r["identity_id"], r["id"], r["start_ts"]))]
            if e.unusual(r["start_ts"], prior):
                why.append("unusual_time")
        t = e.local(r["start_ts"])
        if why:
            notable.append({"name": r["name"] or "unknown", "time": t.strftime("%H:%M"), "camera": r["camera"],
                            "duration_s": round(r["end_ts"] - r["start_ts"]), "why": why})
    return {"visits": len(rows),
            "unknown": sum(1 for r in rows if r["kind"] != "known"),
            "people": len({r["identity_id"] for r in rows if r["identity_id"] is not None}),
            "new_visitors": sum(1 for r in rows if r["how"] == "new"),
            "notable": notable}
