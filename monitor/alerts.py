"""Alert rules, daily digest and ntfy delivery (messages in Italian)."""
import datetime as dt
import hashlib
import json
import os
import urllib.request
from zoneinfo import ZoneInfo

HOUR = 3600
GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def topic_of(cfg):
    return os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic") or ""


def pkey(raw, cfg):
    """Opaque key for personal alerts: the state file is public, the position numbers are not."""
    salt = topic_of(cfg) or "radar-saero"
    return "p:" + hashlib.sha256((salt + "|" + raw).encode()).hexdigest()[:20]


def fmt_n(x):
    if x is None:
        return "—"
    a = abs(x)
    if a >= 1e6:
        s = f"{x / 1e6:.1f}M"
    elif a >= 1e4:
        s = f"{x / 1e3:.0f}k"
    elif a >= 1e3:
        s = f"{x / 1e3:.1f}k"
    else:
        s = f"{x:.0f}"
    return s.replace(".", ",")


def fmt_usd(x):
    if x is None:
        return "—"
    if x >= 1e6:
        return "$" + f"{x / 1e6:.2f}M".replace(".", ",")
    return "$" + f"{x:,.0f}".replace(",", ".")


def fmt_pct(x, d=0):
    return "—" if x is None else f"{x:.{d}f}%".replace(".", ",")


def fmt_time(ts, tz, now):
    t = dt.datetime.fromtimestamp(ts, tz)
    n = dt.datetime.fromtimestamp(now, tz)
    if t.date() == n.date():
        return t.strftime("%H:%M")
    if (t.date() - n.date()).days == 1:
        return "domani " + t.strftime("%H:%M")
    return f"{GIORNI[t.weekday()]} {t.strftime('%H:%M')}"


def fmt_in(seconds):
    m = max(0, int(seconds // 60))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d}" if h < 24 else f"{h // 24} g {h % 24} h"


def evaluate(report, state, cfg, now, new_incentives):
    tz = ZoneInfo(cfg.get("timezone", "Europe/Rome"))
    th = cfg["thresholds"]
    pools = {r["key"]: r for r in report["pools"]}
    out = []
    day = dt.datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d")

    # 1. my positions
    for m in report["mine"]:
        if m.get("missing") or not m.get("ends"):
            continue
        where = ", ".join(f"{n} ({fmt_n(a)})" for n, a in m["pools"][:3]) or "nessun pool"
        if m["free"]:
            out.append({"key": pkey(f"my:{m['id']}:{m['ends']}", cfg), "prio": 4, "tags": ["unlock"],
                        "private": True,
                        "title": f"Posizione #{m['id']} libera",
                        "body": f"Il cooldown su Base è finito alle {fmt_time(m['ends'], tz, now)}: puoi riallocare "
                                f"{fmt_n(m['saero'])} sAERO. Ora è su {where}."})
        elif m["ends"] - now <= HOUR:
            out.append({"key": pkey(f"mysoon:{m['id']}:{m['ends']}", cfg), "prio": 3, "tags": ["hourglass"],
                        "private": True,
                        "title": f"Posizione #{m['id']}: cooldown finisce tra {fmt_in(m['ends'] - now)}",
                        "body": f"Alle {fmt_time(m['ends'], tz, now)} potrai riallocare {fmt_n(m['saero'])} sAERO."})

    # 2. cooldown wave in the next few hours
    win = th["wave_window_h"]
    wave = sum(w["active"] for w in report["waves"] if now < w["h"] + HOUR and w["h"] < now + win * HOUR)
    if wave >= th["wave_min_saero"]:
        losers = sorted(((r["free_3h_active"], r["name"]) for r in report["pools"]
                         if r.get("free_3h_active")), reverse=True)[:3]
        detail = "; ".join(f"{n} {fmt_n(a)}" for a, n in losers)
        out.append({"key": f"wave:{now // (win * HOUR)}", "prio": 3, "tags": ["ocean"],
                    "title": f"Tra {win} ore si liberano {fmt_n(wave)} sAERO attivi",
                    "body": f"Posizioni che si sono già spostate almeno una volta escono dal cooldown. "
                            f"Possono lasciare: {detail}. I pool che riceveranno i voti verranno diluiti."})

    # 3. new incentive programs
    for pid in new_incentives:
        inc = next((u for u in report["incentives_upcoming"] if u["id"] == pid), None)
        if not inc or (inc["usd"] or 0) < th["incentive_min_usd"]:
            continue
        r = pools.get(inc["key"], {})
        hours = (inc["end"] - inc["start"]) / HOUR
        apr = (f" APR attuale {fmt_pct(r.get('apr'))}, con gli incentivi {fmt_pct(r.get('apr_next'))}."
               if r.get("apr_next") is not None and r.get("ap") else
               " Nessuno è ancora allocato: il primo prende tutto." if not r.get("ap") else "")
        out.append({"key": f"inc:{pid}", "prio": 3, "tags": ["moneybag"],
                    "title": f"Incentivi su {inc['pool']}: {fmt_usd(inc['usd'])} da {fmt_time(inc['start'], tz, now)}",
                    "body": f"{fmt_n(inc['amount'])} {inc['token']} in {hours:.0f} ore. "
                            f"Allocazione attuale {fmt_n(inc['ap'])} sAERO.{apr}"})

    # 4. allocation flows
    for r in report["pools"]:
        d6, ap = r.get("d6h"), r["ap"]
        if d6 is None or ap - d6 <= 0:
            continue
        pct = d6 / (ap - d6) * 100
        if abs(d6) >= th["flow_min_saero"] and abs(pct) >= th["flow_min_pct"]:
            up = d6 > 0
            out.append({"key": f"flow:{r['key']}:{'+' if up else '-'}:{now // (6 * HOUR)}", "prio": 3,
                        "tags": ["chart_with_upwards_trend" if up else "chart_with_downwards_trend"],
                        "title": f"{r['name']}: {'+' if up else '−'}{fmt_n(abs(d6))} sAERO in 6 ore "
                                 f"({'+' if up else '−'}{fmt_pct(abs(pct))})",
                        "body": (f"Allocazione ora {fmt_n(ap)}. " +
                                 ("Il rendimento per chi è dentro si diluisce." if up else
                                  "Chi resta prende una quota più grande delle fee.") +
                                 (f" APR 48h {fmt_pct(r.get('apr'))}." if r.get("rev48h") else ""))})

    # 5. routing share changes
    for r in report["pools"]:
        for series, v in (r.get("routing") or {}).items():
            if not series.endswith(":10000") or v.get("aero_24h") is None:
                continue
            delta = (v["pool"] - (v["pool_24h"] if v.get("pool_24h") is not None else v["pool"])) * 100
            if abs(delta) >= th["routing_change_pts"]:
                agg = "KyberSwap" if series.startswith("kyber") else "Velora"
                out.append({"key": f"route:{r['key']}:{series}:{day}", "prio": 3, "tags": ["twisted_rightwards_arrows"],
                            "title": f"{r['name']}: quota di routing {fmt_pct(v['pool'] * 100)} (ieri {fmt_pct(v['pool_24h'] * 100)})",
                            "body": f"Su uno swap da $10.000, {agg} ora instrada dal pool Aero il "
                                    f"{fmt_pct(v['pool'] * 100)} del flusso. "
                                    + ("Più flusso dagli aggregatori significa più fee nelle prossime ore."
                                       if delta > 0 else "Il flusso si sta spostando su altre sedi.")})

    # 6. outlier yield
    med = report["totals"].get("median_apr_1m")
    if med:
        for r in report["pools"]:
            if r["ap"] < th["apr_alert_min_alloc"] or r.get("apr") is None or r.get("apr_next") is None:
                continue
            if r["apr"] >= th["apr_alert_multiple"] * med and r["apr_next"] >= th["apr_alert_multiple"] * med:
                out.append({"key": f"apr:{r['key']}:{day}", "prio": 3, "tags": ["star"],
                            "title": f"{r['name']} rende il {fmt_pct(r['apr'])} annuo",
                            "body": f"La mediana dei pool sopra 1M sAERO è {fmt_pct(med)}. "
                                    f"Fee 48h {fmt_usd(r.get('fees48h'))}, incentivi in arrivo {fmt_usd(r.get('inc_next48h'))}, "
                                    f"allocazione {fmt_n(r['ap'])}."})
    # 7. protocol parameter changes on the Base leaf voter
    for ts, name, value in state.get("param_events", []):
        if name == "max_reduction":
            title = ("Aero ha attivato le riduzioni del cooldown" if value else "Aero ha disattivato le riduzioni del cooldown")
            body = (f"Ogni posizione può ora accumulare fino a {value / 3600:.1f} ore di riduzione, concesse da moduli "
                    f"autorizzati. Alcune posizioni potranno riallocare prima della fine del cooldown."
                    if value else "Le posizioni tornano a riallocare solo a fine cooldown.")
        else:
            title = f"Il cooldown di Aero ora è di {value / 3600:.1f} ore".replace(".", ",")
            body = "Il calendario dei cooldown usa già il nuovo valore."
        out.append({"key": f"param:{name}:{ts}", "prio": 4, "tags": ["gear"], "title": title, "body": body})
    sent = state.setdefault("alerts", {})
    fresh = [a for a in out if a["key"] not in sent]
    return fresh


def digest(report, cfg, now):
    tz = ZoneInfo(cfg.get("timezone", "Europe/Rome"))
    lines = []
    top = sorted((r for r in report["pools"] if r["ap"] >= 1_000_000 and r.get("apr_next") is not None),
                 key=lambda r: -r["apr_next"])[:5]
    if top:
        lines.append("Rendimento atteso (fee 48h + incentivi in arrivo):")
        lines += [f"• {r['name']}: {fmt_pct(r['apr_next'])} ({fmt_n(r['ap'])} sAERO)" for r in top]
    peak = max(report["waves"][:24], key=lambda w: w["active"], default=None)
    total24 = sum(w["active"] for w in report["waves"][:24])
    if total24:
        lines.append(f"Cooldown: {fmt_n(total24)} sAERO attivi si liberano nelle prossime 24 ore, "
                     f"picco alle {fmt_time(peak['h'], tz, now)} ({fmt_n(peak['active'])}).")
    inc = [u for u in report["incentives_upcoming"] if u["start"] > now and (u["usd"] or 0) >= 100][:4]
    if inc:
        lines.append("Incentivi in arrivo: " + "; ".join(
            f"{u['pool']} {fmt_usd(u['usd'])} alle {fmt_time(u['start'], tz, now)}" for u in inc))
    personal = []
    for m in report["mine"]:
        if m.get("ends"):
            personal.append(f"Posizione #{m['id']}: " + ("libera" if m["free"] else
                                                          f"cooldown fino alle {fmt_time(m['ends'], tz, now)}"))
    public = "\n".join(lines) or "Nessuna novità rilevante."
    return {"key": f"digest:{dt.datetime.fromtimestamp(now, tz):%Y-%m-%d}", "prio": 2, "tags": ["sunrise"],
            "title": "Monitor sAERO: il punto del mattino", "body": "\n".join(lines + personal) or public,
            "public_body": public}


def page_url(cfg):
    if cfg.get("page_url"):
        return cfg["page_url"]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return None


def send(alerts, cfg, log, now, state, max_per_run=5):
    topic = topic_of(cfg)
    server = cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    click = page_url(cfg)
    private = [a for a in alerts if a.get("private")]
    digests = [a for a in alerts if a["key"].startswith("digest:")]
    public = [a for a in alerts if not a.get("private") and not a["key"].startswith("digest:")]
    if len(public) > max_per_run:
        extra = public[max_per_run - 1:]
        public = public[:max_per_run - 1] + [{
            "key": "bundle", "prio": 3, "tags": ["bell"],
            "title": f"Altri {len(extra)} avvisi", "body": "\n".join("• " + a["title"] for a in extra),
            "members": [a["key"] for a in extra]}]
    delivered = 0
    for a in private + public + digests:
        ok = bool(topic)
        if topic:
            payload = {"topic": topic, "title": a["title"], "message": a["body"], "priority": a.get("prio", 3),
                       "tags": a.get("tags", [])}
            if click:
                payload["click"] = click
            try:
                req = urllib.request.Request(server, data=json.dumps(payload).encode(),
                                             headers={"content-type": "application/json"})
                urllib.request.urlopen(req, timeout=15).read()
                delivered += 1
            except Exception as e:
                ok = False
                log(f"ntfy error: {e}")
        if ok:
            for k in a.get("members", [a["key"]]):
                state["alerts"][k] = now
        if not a.get("private"):
            state.setdefault("alert_log", []).append({"ts": now, "title": a["title"],
                                                      "body": a.get("public_body", a["body"])})
    state["alerts"] = {k: v for k, v in state["alerts"].items() if v > now - 4 * 24 * HOUR}
    state["alert_log"] = state.get("alert_log", [])[-60:]
    log(f"notifications: {delivered} delivered" + ("" if topic else " (NTFY_TOPIC secret not set)"))
    return delivered
