#!/usr/bin/env python3
"""Scarecrow Watch data updater.

Pulls public data for the 11 ASEAN states and writes JSON files the dashboard reads:
  data/signals.json       recent reports and news (ReliefWeb, GDELT)  -> unverified signals
  data/displacement.json  people displaced from each country (UNHCR)
  data/indicators.json    World Bank, V-Dem (via OWID) and UCDP values used by the PREVENT layer
  data/auto_published.json  automated PREVENT scores currently public
  data/auto_review.json   automated changes held for Lab approval (scores and leader changes)
  data/factbook_auto.json population, GDP, GDP per capita and area (World Bank)
  data/leaders_seen.json  last confirmed leaders from Wikidata
  data/meta.json          when each source last succeeded, and any error

Lab assessments (data/assessments.json) are edited by hand and never touched here.
Standard library only. If a source fails, its previous data is kept and the error is logged in meta.json.
"""
import csv, email.utils, html, io, json, math, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
UA = "ScarecrowWatch/1.0 (https://github.com/scarecrow-lab; non-profit early-warning dashboard)"
NOW = datetime.now(timezone.utc)

COUNTRIES = {  # name -> ISO3
    "Myanmar": "MMR", "Philippines": "PHL", "Thailand": "THA", "Cambodia": "KHM",
    "Indonesia": "IDN", "Laos": "LAO", "Vietnam": "VNM", "Malaysia": "MYS",
    "Singapore": "SGP", "Brunei": "BRN", "Timor-Leste": "TLS",
}
ISO_TO_NAME = {v: k for k, v in COUNTRIES.items()}
SIGNAL_DAYS = 14
PER_COUNTRY = 15
CURATED = "Curated sources"

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


def http_text(url, headers=None, timeout=90, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
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
FIELDS = ["refugees", "asylum_seekers", "idps", "stateless", "ooc", "hosted_refugees", "hosted_asylum_seekers"]


def unhcr():
    out = {}
    for name, iso in COUNTRIES.items():
        url = "https://api.unhcr.org/population/v1/population/?" + urllib.parse.urlencode({
            "limit": 200, "yearFrom": NOW.year - 10, "yearTo": NOW.year, "coo": iso, "cf_type": "ISO"})
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
                if k not in ("stateless", "hosted_refugees", "hosted_asylum_seekers"):
                    agg[k] += num(it.get(k))
        # Stateless people, and refugees a country hosts, are recorded by the country where
        # they live (stateless people often have no country of origin), so ask by residence too.
        try:
            page = 1
            while page <= 20:
                res2 = http_json("https://api.unhcr.org/population/v1/population/?" + urllib.parse.urlencode({
                    "limit": 1000, "page": page, "yearFrom": NOW.year - 10, "yearTo": NOW.year,
                    "coa": iso, "coo_all": "true", "cf_type": "ISO"}))
                items = res2.get("items", []) or []
                for it in items:
                    y = num(it.get("year"))
                    if not y:
                        continue
                    agg = by_year.setdefault(y, {k: 0 for k in FIELDS})
                    agg["stateless"] += num(it.get("stateless"))
                    if str(it.get("coo_iso", "")).upper() != iso:  # people from elsewhere living here
                        agg["hosted_refugees"] += num(it.get("refugees"))
                        agg["hosted_asylum_seekers"] += num(it.get("asylum_seekers"))
                max_pages = num(res2.get("maxPages"))
                if len(items) < 1000 or (max_pages and page >= max_pages):
                    break
                page += 1
                time.sleep(1)
        except Exception as e:
            log("unhcr residence", name, e)
        if by_year:
            y = max(by_year)
            series = [{"year": yr, **by_year[yr]} for yr in sorted(by_year)]
            out[name] = {"year": y, **by_year[y], "series": series}
        time.sleep(1)
    if not out:
        raise RuntimeError("UNHCR returned no rows for any country")
    return out


# ---------- World Bank (PREVENT indicators) ----------
def worldbank():
    spec = read("prevent.json", {}).get("indicators", {})
    codes = {k: v["wb"] for k, v in spec.items() if v.get("wb")}
    isos = ";".join(COUNTRIES.values())
    out = {}
    for key, code in codes.items():
        url = (f"https://api.worldbank.org/v2/country/{isos}/indicator/{urllib.parse.quote(code)}?"
               + urllib.parse.urlencode({"format": "json", "mrnev": 1, "per_page": 100}))
        try:
            res = http_json(url)
        except Exception as e:
            log("worldbank", code, e)
            continue
        rows = res[1] if isinstance(res, list) and len(res) > 1 and isinstance(res[1], list) else []
        vals = {}
        for r in rows:
            name = ISO_TO_NAME.get(str(r.get("countryiso3code", "")).upper())
            v = r.get("value")
            if name and v is not None:
                try:
                    vals[name] = {"value": round(float(v), 2), "year": int(r.get("date"))}
                except (TypeError, ValueError):
                    pass
        if vals:
            out[key] = {"code": code, "countries": vals}
        time.sleep(1)
    if not out:
        raise RuntimeError("World Bank returned no indicator values")
    return out


# ---------- V-Dem via Our World in Data ----------
def owid():
    spec = read("prevent.json", {}).get("indicators", {})
    isos = set(COUNTRIES.values())
    out, failed = {}, []
    for key, v in spec.items():
        slug = v.get("owid")
        if not slug:
            continue
        url = f"https://ourworldindata.org/grapher/{urllib.parse.quote(slug)}.csv?v=1&csvType=full&useColumnShortNames=true"
        try:
            rows = list(csv.reader(io.StringIO(http_text(url))))
        except Exception as e:
            failed.append(f"{slug}: {e}")
            continue
        head, latest = rows[0], {}
        try:
            ci, yi = head.index("code"), head.index("year")
        except ValueError:
            ci, yi = 1, 2
        vi = yi + 1  # the main value column follows the year column
        for r in rows[1:]:
            if len(r) <= vi or r[ci] not in isos:
                continue
            try:
                y, val = int(r[yi]), float(r[vi])
            except ValueError:
                continue
            if r[ci] not in latest or y > latest[r[ci]][0]:
                latest[r[ci]] = (y, val)
        vals = {ISO_TO_NAME[iso]: {"value": round(val, 3), "year": y} for iso, (y, val) in latest.items()}
        if vals:
            out[key] = {"source": "V-Dem via Our World in Data", "slug": slug, "countries": vals}
        else:
            failed.append(f"{slug}: no rows for ASEAN")
        time.sleep(1)
    for f in failed:
        log("owid", f)
    if not out:
        raise RuntimeError("; ".join(failed)[:280] or "no V-Dem indicators configured")
    return out


# ---------- UCDP georeferenced events (needs a free token) ----------
GW = {"Myanmar": 775, "Thailand": 800, "Cambodia": 811, "Laos": 812, "Vietnam": 816, "Malaysia": 820,
      "Singapore": 830, "Brunei": 835, "Philippines": 840, "Indonesia": 850, "Timor-Leste": 860}
GW_EXTRA = [771, 750, 710, 910]  # Bangladesh, India, China, Papua New Guinea: neighbours outside ASEAN
GW_TO_NAME = {v: k for k, v in GW.items()}


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def ucdp():
    token = os.environ.get("UCDP_TOKEN", "").strip()
    if not token:
        raise RuntimeError("UCDP_TOKEN is not set (request a free token from UCDP)")
    hdr = {"x-ucdp-access-token": token, "Accept": "application/json"}
    countries = ",".join(str(c) for c in list(GW.values()) + GW_EXTRA)
    since = (NOW - timedelta(days=365)).strftime("%Y-%m-%d")
    events, versions = {}, []
    yy = NOW.year % 100
    # Monthly GED Candidate releases are versioned YY.0.N; try this year's and last year's.
    for y in (yy, yy - 1):
        for n in range(12, 0, -1):
            ver = f"{y}.0.{n}"
            url = f"https://ucdpapi.pcr.uu.se/api/gedevents/{ver}?" + urllib.parse.urlencode({"pagesize": 1000, "Country": countries, "StartDate": since})
            got = 0
            while url:
                try:
                    res = json.loads(http_text(url, hdr, tries=1))
                except Exception:
                    break
                for e in res.get("Result", []) or []:
                    if e.get("id") is not None:
                        events[e["id"]] = e
                        got += 1
                url = res.get("NextPageUrl") or None
                time.sleep(0.5)
            if got:
                versions.append(ver)
    if not versions:
        raise RuntimeError("no UCDP candidate data returned (check the token or version names)")
    bp = read("prevent.json", {}).get("border_points", {})
    deaths = {n: 0 for n in GW}
    near = {n: 0 for n in GW}
    for e in events.values():
        if str(e.get("date_start", ""))[:10] < since:
            continue
        try:
            d = int(e.get("best") or 0)
            pt = (float(e["latitude"]), float(e["longitude"]))
            cid = int(e.get("country_id"))
        except (KeyError, TypeError, ValueError):
            continue
        own = GW_TO_NAME.get(cid)
        if own:
            deaths[own] += d
        for name, pts in bp.items():
            if name != own and any(km(pt, tuple(p)) <= 200 for p in pts):
                near[name] += d
    stamp = f"12 months to {NOW.strftime('%Y-%m')}"
    return {
        "armed_conflict": {"source": "UCDP GED Candidate", "versions": versions,
                           "countries": {n: {"value": v, "year": stamp} for n, v in deaths.items()}},
        "neighbor_violence": {"source": "UCDP GED Candidate", "versions": versions,
                              "countries": {n: {"value": v, "year": stamp} for n, v in near.items()}},
    }


# ---------- exception-based review ----------
def score(spec, v):
    t = spec.get("thresholds") or []
    if v is None or len(t) != 4:
        return None
    for i in range(4):
        if (v < t[i]) if spec.get("higher_is_worse") else (v >= t[i]):
            return i + 1
    return 5


def review_auto(indicators):
    """Publish small automated changes; hold big jumps for Lab approval."""
    pv = read("prevent.json", {})
    spec, rules = pv.get("indicators", {}), pv.get("review_rules", {})
    step = int(rules.get("max_auto_step", 1))
    published = read("auto_published.json", {"countries": {}})
    approvals = read("auto_approvals.json", {"approved": [], "rejected": []})
    ok = {(a.get("country"), a.get("indicator"), a.get("score")) for a in approvals.get("approved", [])}
    no = {(a.get("country"), a.get("indicator"), a.get("score")) for a in approvals.get("rejected", [])}
    pending, stamp = [], NOW.isoformat(timespec="seconds")
    for key, block in indicators.items():
        sp = spec.get(key)
        if not sp or sp.get("type") != "auto":
            continue
        for name, rec in (block.get("countries") or {}).items():
            new = score(sp, rec.get("value"))
            if new is None:
                continue
            pub = published["countries"].setdefault(name, {})
            prev = pub.get(key, {}).get("score")
            if prev is None:  # first automated value: compare with the Lab's score it replaces
                prev = ((pv.get("countries", {}).get(name, {}) or {}).get("lab", {}) or {}).get(key)
            entry = {"score": new, "value": rec.get("value"), "year": rec.get("year"), "source": block.get("source", ""), "at": stamp}
            if prev is None or abs(new - prev) <= step or (name, key, new) in ok:
                pub[key] = entry
            else:
                pending.append({"country": name, "indicator": key, "published": prev, "proposed": new,
                                "value": rec.get("value"), "year": rec.get("year"), "source": block.get("source", ""),
                                "status": "rejected" if (name, key, new) in no else "pending"})
                if key not in pub:
                    pub[key] = {"score": prev, "value": None, "year": None, "source": "Lab score kept pending review", "at": stamp}
    write("auto_published.json", {"generated": stamp, "countries": published["countries"]})
    write("auto_review.json", {"generated": stamp, "pending": pending})
    return len(pending)


# ---------- Factbook: World Bank figures + Wikidata leaders ----------
FACT_WB = {"pop": "SP.POP.TOTL", "gdp": "NY.GDP.MKTP.PP.CD", "gdpCap": "NY.GDP.PCAP.PP.CD", "area": "AG.SRF.TOTL.K2"}
QID = {"Myanmar": "Q836", "Thailand": "Q869", "Cambodia": "Q424", "Laos": "Q819", "Vietnam": "Q881", "Malaysia": "Q833",
       "Singapore": "Q334", "Brunei": "Q921", "Philippines": "Q928", "Indonesia": "Q252", "Timor-Leste": "Q574"}


def factbook_numbers():
    isos = ";".join(COUNTRIES.values())
    out = {}
    for field, code in FACT_WB.items():
        url = f"https://api.worldbank.org/v2/country/{isos}/indicator/{code}?" + urllib.parse.urlencode({"format": "json", "mrnev": 1, "per_page": 100})
        try:
            res = http_json(url)
        except Exception as e:
            log("factbook", code, e)
            continue
        rows = res[1] if isinstance(res, list) and len(res) > 1 and isinstance(res[1], list) else []
        for r in rows:
            name = ISO_TO_NAME.get(str(r.get("countryiso3code", "")).upper())
            if name and r.get("value") is not None:
                try:
                    out.setdefault(name, {})[field] = {"value": float(r["value"]), "year": int(r["date"])}
                except (TypeError, ValueError):
                    pass
        time.sleep(1)
    if not out:
        raise RuntimeError("World Bank returned no factbook figures")
    return out


def wikidata_leaders():
    values = " ".join(f"wd:{q}" for q in QID.values())
    q = f"""SELECT ?country ?role ?personLabel WHERE {{
      VALUES ?country {{ {values} }}
      {{ ?country p:P35 ?st . ?st ps:P35 ?person . BIND("head_of_state" AS ?role) }}
      UNION
      {{ ?country p:P6 ?st . ?st ps:P6 ?person . BIND("head_of_government" AS ?role) }}
      FILTER NOT EXISTS {{ ?st pq:P582 ?end }}
      FILTER NOT EXISTS {{ ?st wikibase:rank wikibase:DeprecatedRank }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}"""
    url = "https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": q, "format": "json"})
    res = json.loads(http_text(url, {"Accept": "application/sparql-results+json"}))
    inv = {v: k for k, v in QID.items()}
    out = {}
    for b in res.get("results", {}).get("bindings", []):
        qid = b["country"]["value"].rsplit("/", 1)[-1]
        name, role, person = inv.get(qid), b["role"]["value"], b.get("personLabel", {}).get("value", "")
        if name and person and not person.startswith("Q"):
            out.setdefault(name, {}).setdefault(role, set()).add(person)
    if not out:
        raise RuntimeError("Wikidata returned no leaders")
    return {n: {r: sorted(v) for r, v in d.items()} for n, d in out.items()}


def leaders_text(d):
    hs, hg = ", ".join(d.get("head_of_state", [])), ", ".join(d.get("head_of_government", []))
    if hs and hg and hs != hg:
        return f"Head of state: {hs}; head of government: {hg}"
    return f"Head of state and government: {hs or hg}"


def review_leaders(current):
    """Detect leader changes; never publish them automatically."""
    seen = read("leaders_seen.json", {"countries": {}})["countries"]
    approvals = read("auto_approvals.json", {"approved": [], "rejected": []})
    ok = {(a.get("country"), a.get("names")) for a in approvals.get("approved", []) if a.get("indicator") == "leader"}
    no = {(a.get("country"), a.get("names")) for a in approvals.get("rejected", []) if a.get("indicator") == "leader"}
    lab = {n: (p or {}).get("leader", "") for n, p in read("context.json", {}).get("profile", {}).items()}
    pending = []
    for name, d in current.items():
        txt = leaders_text(d)
        if name not in seen:
            # First run: accept Wikidata silently only if it matches the Lab's Factbook text;
            # otherwise flag it, so a Factbook entry that is already out of date gets caught.
            people = [x for v in d.values() for x in v]
            def known(person):
                low = lab.get(name, "").lower()
                return person.lower() in low or any(len(w) >= 4 and w.lower() in low for w in person.split())
            if all(known(x) for x in people):
                seen[name] = txt
            elif (name, txt) in ok:
                seen[name] = txt
            else:
                pending.append({"kind": "leader", "country": name, "indicator": "leader", "published": lab.get(name, "") or "(not recorded)",
                                "proposed": txt, "source": "Wikidata", "status": "rejected" if (name, txt) in no else "pending"})
        elif seen[name] != txt:
            if (name, txt) in ok:
                seen[name] = txt
            else:
                pending.append({"kind": "leader", "country": name, "indicator": "leader", "published": seen[name], "proposed": txt,
                                "source": "Wikidata", "status": "rejected" if (name, txt) in no else "pending"})
    write("leaders_seen.json", {"generated": NOW.isoformat(timespec="seconds"), "countries": seen})
    return pending


# ---------- Curated sources (RSS/Atom): chosen by the Lab; listing implies no partnership ----------
COUNTRY_TERMS = {
    "Myanmar": ["myanmar", "burma", "burmese", "rakhine", "rohingya", "arakan", "kachin", "kayin", "karenni", "kayah", "sagaing",
                "magway", "mandalay", "yangon", "naypyidaw", "nay pyi taw", "chin state", "shan state", "tatmadaw"],
    "Thailand": ["thailand", "thai", "bangkok", "pattani", "yala", "narathiwat", "mae sot"],
    "Cambodia": ["cambodia", "cambodian", "khmer", "phnom penh", "hun manet", "hun sen"],
    "Laos": ["laos", "lao pdr", "vientiane"],
    "Vietnam": ["vietnam", "viet nam", "vietnamese", "hanoi", "ho chi minh", "montagnard"],
    "Malaysia": ["malaysia", "malaysian", "kuala lumpur", "sabah", "sarawak"],
    "Singapore": ["singapore", "singaporean"],
    "Brunei": ["brunei"],
    "Philippines": ["philippines", "philippine", "filipino", "manila", "mindanao", "bangsamoro", "barmm", "duterte", "marcos"],
    "Indonesia": ["indonesia", "indonesian", "jakarta", "papua", "aceh", "prabowo"],
    "Timor-Leste": ["timor-leste", "east timor", "timorese", "dili"],
}
TERM_RE = {c: re.compile(r"\b(" + "|".join(re.escape(t) for t in ts) + r")\b", re.I) for c, ts in COUNTRY_TERMS.items()}
FEED_GUESSES = ["feed/", "rss.xml", "feed", "rss", "index.xml", "atom.xml"]


def strip_html(t):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t or ""))).strip()


def parse_feed(xml_text):
    root = ET.fromstring(xml_text.encode("utf-8", "ignore"))
    items = []
    for it in root.iter():
        if it.tag.split("}")[-1] not in ("item", "entry"):
            continue
        def get(*names):
            for c in it:
                if c.tag.split("}")[-1] in names:
                    return c
            return None
        t = get("title"); title = (t.text if t is not None else "") or ""
        link_el, link = get("link"), ""
        if link_el is not None:
            link = link_el.get("href") or (link_el.text or "")
        date_el, d = get("pubDate", "published", "updated", "date"), ""
        if date_el is not None and date_el.text:
            try:
                d = email.utils.parsedate_to_datetime(date_el.text).strftime("%Y-%m-%d")
            except Exception:
                d = date_el.text.strip()[:10]
        desc_el = get("description", "summary", "encoded")
        items.append({"title": strip_html(title), "url": link.strip(), "date": d,
                      "text": strip_html(desc_el.text if desc_el is not None else "")[:1500]})
    return items


def find_feed(src, known):
    if src.get("feed"):
        return [src["feed"]]
    cands = [known] if known else []
    try:
        page = http_text(src["home"], tries=1, timeout=25)
        for m in re.finditer(r"<link[^>]+>", page, re.I):
            tag = m.group(0)
            if re.search(r"application/(rss|atom)\+xml", tag, re.I):
                h = re.search(r'href=["\']([^"\']+)', tag)
                if h:
                    cands.append(urllib.parse.urljoin(src["home"], html.unescape(h.group(1))))
    except Exception as e:
        log("curated home", src["id"], e)
    cands += [urllib.parse.urljoin(src["home"], g) for g in FEED_GUESSES]
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c); out.append(c)
    return out


def partner_sources():
    cfg = read("sources.json", {}).get("sources", [])
    status = read("sources_status.json", {"sources": {}})["sources"]
    cutoff = (NOW - timedelta(days=SIGNAL_DAYS)).strftime("%Y-%m-%d")
    out, ok = [], 0
    for src in cfg:
        sid, prev = src["id"], status.get(src["id"], {})
        got, used, err = None, None, None
        for feed in find_feed(src, prev.get("feed")):
            try:
                txt = http_text(feed, {"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}, tries=1, timeout=25)
                items = parse_feed(txt)
                if items:
                    got, used = items, feed
                    break
            except Exception as e:
                err = str(e)[:200]
        if got is None:
            status[sid] = {**prev, "status": "error", "checked": NOW.isoformat(timespec="seconds"), "error": err or "no RSS/Atom feed found"}
            continue
        ok += 1
        n = 0
        for it in got:
            url = safe_url(it["url"])
            if not url or not it["title"] or (it["date"] and it["date"] < cutoff):
                continue
            blob = f'{it["title"]} {it["text"]}'
            countries = [c for c, rx in TERM_RE.items() if rx.search(blob)]
            if not countries and src.get("default_country"):
                countries = [src["default_country"]]
            for c in countries[:3]:  # an item about several countries appears under each, up to three
                out.append({"country": c, "date": it["date"] or NOW.strftime("%Y-%m-%d"), "title": it["title"][:300], "url": url,
                            "source": src["name"][:80], "kind": src.get("kind", ""), "via": CURATED})
                n += 1
        status[sid] = {"status": "ok", "feed": used, "updated": NOW.isoformat(timespec="seconds"), "items": n, "error": None}
        time.sleep(1)
    write("sources_status.json", {"generated": NOW.isoformat(timespec="seconds"), "sources": status})
    if not ok:
        raise RuntimeError("no curated source could be read")
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
    for it in merged:  # older runs labelled curated items differently
        if it.get("via") == "Partner sources":
            it["via"] = CURATED
    # Per country, curated sources take the slots first, then broad news (GDELT, ReliefWeb).
    per, final = {}, []
    for it in sorted(merged, key=lambda x: x.get("via") != CURATED):
        per[it["country"]] = per.get(it["country"], 0) + 1
        if per[it["country"]] <= PER_COUNTRY:
            final.append(it)
    return sorted(final, key=lambda x: x.get("date", ""), reverse=True)


def main():
    os.makedirs(DATA, exist_ok=True)
    meta = read("meta.json", {"sources": {}})
    meta.setdefault("sources", {})
    old_sig = read("signals.json", {"items": []})
    stamp = NOW.isoformat(timespec="seconds")
    fresh = []

    for key, fn in (("reliefweb", reliefweb), ("gdelt", gdelt), ("partners", partner_sources)):
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

    try:
        wb = worldbank()
        old = read("indicators.json", {}).get("indicators", {})
        write("indicators.json", {"generated": stamp, "source": "World Bank WDI", "indicators": {**old, **wb}})
        meta["sources"]["worldbank"] = {"status": "ok", "updated": stamp, "count": len(wb), "error": None}
        log("worldbank ok", len(wb))
    except Exception as e:
        prev = meta["sources"].get("worldbank", {})
        meta["sources"]["worldbank"] = {**prev, "status": "error", "checked": stamp, "error": str(e)[:300]}
        log("worldbank FAILED", e)

    for key, fn in (("vdem", owid), ("ucdp", ucdp)):
        try:
            got = fn()
            old = read("indicators.json", {}).get("indicators", {})
            write("indicators.json", {"generated": stamp, "source": "World Bank WDI, V-Dem via OWID, UCDP", "indicators": {**old, **got}})
            meta["sources"][key] = {"status": "ok", "updated": stamp, "count": len(got), "error": None}
            log(key, "ok", len(got))
        except Exception as e:
            prev = meta["sources"].get(key, {})
            meta["sources"][key] = {**prev, "status": "error", "checked": stamp, "error": str(e)[:300]}
            log(key, "FAILED", e)

    try:
        held = review_auto(read("indicators.json", {}).get("indicators", {}))
        meta["auto_review"] = {"pending": held, "checked": stamp}
        log("review", held, "held for Lab review")
    except Exception as e:
        log("review FAILED", e)

    try:
        write("factbook_auto.json", {"generated": stamp, "source": "World Bank WDI", "countries": factbook_numbers()})
        meta["sources"]["factbook"] = {"status": "ok", "updated": stamp, "error": None}
    except Exception as e:
        prev = meta["sources"].get("factbook", {})
        meta["sources"]["factbook"] = {**prev, "status": "error", "checked": stamp, "error": str(e)[:300]}
        log("factbook FAILED", e)

    try:
        held = review_leaders(wikidata_leaders())
        rv = read("auto_review.json", {"pending": []})
        rv["pending"] = [x for x in rv.get("pending", []) if x.get("kind") != "leader"] + held
        write("auto_review.json", rv)
        meta["sources"]["wikidata"] = {"status": "ok", "updated": stamp, "count": len(held), "error": None}
        log("wikidata ok", len(held), "leader changes held")
    except Exception as e:
        prev = meta["sources"].get("wikidata", {})
        meta["sources"]["wikidata"] = {**prev, "status": "error", "checked": stamp, "error": str(e)[:300]}
        log("wikidata FAILED", e)

    meta["last_run"] = stamp
    write("meta.json", meta)
    # Exit 0 even on partial failure, so the workflow still commits what worked.


if __name__ == "__main__":
    main()
