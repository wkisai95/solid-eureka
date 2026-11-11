"""US macro calendar: the scheduled releases that move every market at once.

Fixed list: the Federal Reserve's rate decisions (FOMC, 14:00 New York), the consumer price index and the jobs
report (BLS, 08:30 New York), from the published schedules (federalreserve.gov, bls.gov/schedule) up to the end of
2027; extend EVENTS when new schedules come out. On top of it the week's high-impact US events of the free
ForexFactory feed are added when it answers (PCE, GDP, retail sales, speeches of the Fed chair...), and kept so the
past ones stay known.
"""
import calendar
from datetime import datetime

from . import marketdata as MD

HOUR, DAY = 3600, 86400
FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FEED_EVERY = 6 * HOUR
KEEP = 90 * DAY

FOMC = ["2026-10-28", "2026-12-09", "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15",
        "2027-10-27", "2027-12-08"]
CPI = ["2026-11-10", "2026-12-10", "2027-01-13", "2027-02-11", "2027-03-10", "2027-04-13", "2027-05-12", "2027-06-10",
       "2027-07-14", "2027-08-11", "2027-09-14", "2027-10-14", "2027-11-10", "2027-12-10"]
JOBS = ["2026-11-06", "2026-12-04", "2027-01-08", "2027-02-05", "2027-03-05", "2027-04-02", "2027-05-07", "2027-06-04",
        "2027-07-02", "2027-08-06", "2027-09-03", "2027-10-08", "2027-11-05", "2027-12-03"]


def _ny(day, hhmm):
    """Unix time of a New York wall-clock time (daylight saving: second Sunday of March to first of November)."""
    y, m, d = (int(x) for x in day.split("-"))
    h, mi = (int(x) for x in hhmm.split(":"))
    def nth_sunday(month, n):
        first = calendar.weekday(y, month, 1)            # Monday = 0
        return 1 + (6 - first) % 7 + 7 * (n - 1)
    dst = (m, d) >= (3, nth_sunday(3, 2)) and (m, d) < (11, nth_sunday(11, 1))
    return calendar.timegm((y, m, d, h, mi, 0)) + (4 if dst else 5) * HOUR


FIXED = sorted([(_ny(d, "14:00"), "Decisione Fed sui tassi (FOMC)") for d in FOMC]
               + [(_ny(d, "08:30"), "Inflazione USA (CPI)") for d in CPI]
               + [(_ny(d, "08:30"), "Lavoro USA (payrolls)") for d in JOBS])


def refresh(state, now, log):
    """Adds the week's high-impact US events from the feed, when it answers."""
    m = state.setdefault("macro", {"feed": {}, "at": 0})
    if now - m.get("at", 0) < FEED_EVERY:
        return
    m["at"] = now
    try:
        rows = MD._get(FEED, retries=0)
    except Exception as e:
        if not m.get("warned"):
            log(f"macro: weekly feed not read ({type(e).__name__}: {str(e)[:80]}): fixed calendar only")
            m["warned"] = 1
        return
    added = 0
    for r in rows if isinstance(rows, list) else []:
        try:
            if r.get("country") != "USD" or r.get("impact") != "High":
                continue
            ts = int(datetime.fromisoformat(r["date"]).timestamp())
        except Exception:
            continue
        if any(abs(ts - t) <= 2 * HOUR for t, _ in FIXED):
            continue
        if str(ts) not in m["feed"]:
            added += 1
        m["feed"][str(ts)] = str(r.get("title") or "Evento USA")[:60]
    m["feed"] = {t: n for t, n in m["feed"].items() if int(t) >= now - KEEP}
    m.pop("warned", None)
    if added:
        log(f"macro: {added} events of the week added from the feed")


def events(state):
    """[(time, name)] sorted: the fixed calendar and what the feed added (same-time feed events joined)."""
    feed = {}
    for t, n in ((state.get("macro") or {}).get("feed") or {}).items():
        feed.setdefault(int(t), []).append(n)
    return sorted(FIXED + [(t, ", ".join(sorted(set(ns)))[:90]) for t, ns in feed.items()])


def count(evs, t0, t1):
    return sum(1 for t, _ in evs if t0 <= t < t1)


def export(state, now):
    evs = events(state)
    return {"next": [[t, n] for t, n in evs if now - HOUR <= t < now + 21 * DAY][:12],
            "past": [[t, n] for t, n in evs if now - 45 * DAY <= t < now - HOUR],
            "feed": bool((state.get("macro") or {}).get("feed"))}


