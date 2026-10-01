#!/usr/bin/env python3
"""Scarecrow Watch data updater.

Pulls public data for the 11 ASEAN states and writes JSON files the dashboard reads:
  data/signals.json       recent reports and news (ReliefWeb, GDELT)  -> unverified signals
  data/displacement.json  people displaced from each country (UNHCR)
  data/meta.json          when each source last succeeded, and any error

Lab assessments (data/assessments.json) are edited by hand and never touched here.
Standard library only. If a source fails, its previous data is kept and the error is logged in meta.json.
"""
import json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
UA = "ScarecrowWatch/1.0"
NOW = datetime.now(timezone.utc)

COUNTRIES = {  # name -> ISO3
    "Myanmar": "MMR", "Philippines": "PHL", "Thailand": "THA", "Cambodia": "KHM",
    "Indonesia": "IDN", "Laos": "LAO", "Vietnam": "VNM", "Malaysia": "MYS",
    "Singapore": "SGP", "Brunei": "BRN", "Timor-Leste": "TLS",
}
ISO_TO_NAME = {v: k for k, v in COUNTRIES.items()}
SIGNAL_DAYS = 14
PER_COUNTRY = 15

# Words that make a news item worth an analyst's attention. Kept narrow on purpose.
GDELT_TERMS = ('(airstrike OR massacre OR killed OR displaced OR refugees OR "human rights" '
               'OR "ethnic cleansing" OR genocide OR "forced labor" OR "mass grave" OR clashes)')


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http_json(url, body=None, timeout=60, tries=3):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            return json.loads(raw)
        except Exception as e:  # network error, HTTP error or non-JSON reply
            last = e
            if i < tries - 1:
                time.sleep(5 * (i + 1))
    raise RuntimeError(f"{url.split('?')[0]}: {last}")


def read(name, default):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write(name, obj):
    path = os.path.join(DATA, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def safe_url(u):
    return u if isinstance(u, str) and u.startswith(("https://", "http://")) else None


def num(v):
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return 0


# ---------- ReliefWeb (UN OCHA) ----------
def reliefweb():
    app = os.environ.get("RELIEFWEB_APPNAME", "").strip()
    if not app:
        raise RuntimeError("RELIEFWEB_APPNAME is not set (ReliefWeb requires an approved appname)")
    since = (NOW - timedelta(days=SIGNAL_DAYS)).strftime("%Y-%m-%dT00:00:00+00:00")
    body = {
        "filter": {"operator": "AND", "conditions": [
            {"field": "primary_country.iso3", "value": [c.lower() for c in COUNTRIES.values()], "operator": "OR"},
            {"field": "date.created", "value": {"from": since}},
        ]},
        "fields": {"include": ["title", "date.created", "url_alias", "url", "source.shortname",
                               "primary_country.iso3", "format.name"]},
        "sort": ["date.created:desc"],
        "limit": 200,
    }
    res = http_json("https://api.reliefweb.int/v2/reports?appname=" + urllib.parse.quote(app), body)
    out = []
    for row in res.get("data", []):
        f = row.get("fields", {})
        pc = f.get("primary_country") or {}
        name = ISO_TO_NAME.get(str(pc.get("iso3", "")).upper())
        url = safe_url(f.get("url_alias") or f.get("url"))
        if not name or not url or not f.get("title"):
            continue
        src = f.get("source") or []
        out.append({
            "country": name,
            "date": str((f.get("date") or {}).get("created", ""))[:10],
            "title": str(f["title"])[:300],
            "url": url,
            "source": ", ".join(s.get("shortname", "") for s in src if isinstance(s, dict))[:80] or "ReliefWeb",
            "kind": ", ".join(x.get("name", "") for x in (f.get("format") or []) if isinstance(x, dict))[:60],
            "via": "ReliefWeb",
        })
    return out


# ---------- GDELT DOC 2.0 (global news) ----------
def gdelt():
    out, failures = [], 0
    for name in COUNTRIES:
        q = f'"{name}" {GDELT_TERMS} sourcelang:english'
        url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode({
            "query": q, "mode": "artlist", "format": "json", "maxrecords": 25,
            "timespan": "3d", "sort": "datedesc"})
        try:
            res = http_json(url, tries=2)
        except Exception as e:
            failures += 1
            log("gdelt", name, e)
            res = {}
        for a in res.get("articles", []) or []:
            u = safe_url(a.get("url"))
            if not u or not a.get("title"):
                continue
            d = str(a.get("seendate", ""))  # e.g. 20260930T120000Z
            out.append({
                "country": name,
                "date": f"{d[0:4]}-{d[4:6]}-{d[6:8]}" if len(d) >= 8 else "",
                "title": str(a["title"])[:300],
                "url": u,
                "source": str(a.get("domain", ""))[:80],
                "kind": "News",
                "via": "GDELT",
            })
        time.sleep(6)  # GDELT asks for at most one request every 5 seconds
    if failures == len(COUNTRIES):
        raise RuntimeError("GDELT failed for every country")
    return out


# ---------- UNHCR Refugee Data Finder ----------
FIELDS = ["refugees", "asylum_seekers", "idps", "stateless", "ooc"]


def unhcr():
    out = {}
    for name, iso in COUNTRIES.items():
        url = "https://api.unhcr.org/population/v1/population/?" + urllib.parse.urlencode({
            "limit": 100, "yearFrom": NOW.year - 6, "yearTo": NOW.year, "coo": iso, "cf_type": "ISO"})
        try:
            res = http_json(url)
        except Exception as e:
            log("unhcr", name, e)
            continue
        by_year = {}
        for it in res.get("items", []) or []:
            y = num(it.get("year"))
            if not y:
                continue
            agg = by_year.setdefault(y, {k: 0 for k in FIELDS})
            for k in FIELDS:
                agg[k] += num(it.get(k))
        if by_year:
            y = max(by_year)
            out[name] = {"year": y, **by_year[y]}
        time.sleep(1)
    if not out:
        raise RuntimeError("UNHCR returned no rows for any country")
    return out


def merge_signals(new_items, old_items):
    cutoff = (NOW - timedelta(days=SIGNAL_DAYS)).strftime("%Y-%m-%d")
    seen, merged = set(), []
    for it in sorted(new_items + old_items, key=lambda x: x.get("date", ""), reverse=True):
        key = it["url"].split("?")[0].rstrip("/")
        if key in seen or it.get("date", "") < cutoff:
            continue
        seen.add(key)
        merged.append(it)
    per, final = {}, []
    for it in merged:
        per[it["country"]] = per.get(it["country"], 0) + 1
        if per[it["country"]] <= PER_COUNTRY:
            final.append(it)
    return final


def main():
    os.makedirs(DATA, exist_ok=True)
    meta = read("meta.json", {"sources": {}})
    meta.setdefault("sources", {})
    old_sig = read("signals.json", {"items": []})
    stamp = NOW.isoformat(timespec="seconds")
    fresh = []

    for key, fn in (("reliefweb", reliefweb), ("gdelt", gdelt)):
        try:
            items = fn()
            fresh += items
            meta["sources"][key] = {"status": "ok", "updated": stamp, "count": len(items), "error": None}
            log(key, "ok", len(items))
        except Exception as e:
            prev = meta["sources"].get(key, {})
            meta["sources"][key] = {**prev, "status": "error", "checked": stamp, "error": str(e)[:300]}
            log(key, "FAILED", e)

    write("signals.json", {"generated": stamp, "window_days": SIGNAL_DAYS,
                           "items": merge_signals(fresh, old_sig.get("items", []))})

    try:
        disp = unhcr()
        old = read("displacement.json", {}).get("countries", {})
        write("displacement.json", {"generated": stamp, "source": "UNHCR Refugee Data Finder",
                                    "countries": {**old, **disp}})
        meta["sources"]["unhcr"] = {"status": "ok", "updated": stamp, "count": len(disp), "error": None}
        log("unhcr ok", len(disp))
    except Exception as e:
        prev = meta["sources"].get("unhcr", {})
        meta["sources"]["unhcr"] = {**prev, "status": "error", "checked": stamp, "error": str(e)[:300]}
        log("unhcr FAILED", e)

    meta["last_run"] = stamp
    write("meta.json", meta)
    # Exit 0 even on partial failure, so the workflow still commits what worked.


if __name__ == "__main__":
    main()
