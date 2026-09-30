"""
Reads the SERWE survey data that CSEntry syncs to Dropbox and writes
data/summary.json for the dashboard.

Only counts go into summary.json. Names, business names and other answers
are never written out, because the dashboard site is public.

Runs in GitHub Actions with three secrets:
  DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN

For a local test without Dropbox:
  python scripts/fetch_dropbox.py --local path/to/folder_with_sync_files --dcf path/to/dict.dcf
"""
import io, json, math, os, sys, zipfile, datetime as dt, urllib.request, urllib.parse, base64, argparse
from collections import Counter, defaultdict

DICT_NAME = "BD_BANK_SURVEY_2026_DICT"
DATA_DIR = f"/CSPro/DataSync/{DICT_NAME}/data"
DICT_DIR = f"/CSPro/DataSync/{DICT_NAME}/dict"
LEVEL = "BD_BANK_SURVEY_2026_LEVEL"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "summary.json")
BD = dt.timezone(dt.timedelta(hours=6))

# ---------------- Dropbox ----------------
def dbx_token():
    key, secret, refresh = (os.environ[k] for k in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"))
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh}).encode()
    req = urllib.request.Request("https://api.dropbox.com/oauth2/token", data=body)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode())
    return json.load(urllib.request.urlopen(req, timeout=60))["access_token"]

def dbx_rpc(token, endpoint, payload):
    req = urllib.request.Request("https://api.dropboxapi.com/2/" + endpoint, data=json.dumps(payload).encode())
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    return json.load(urllib.request.urlopen(req, timeout=60))

def dbx_list(token, path):
    out, res = [], dbx_rpc(token, "files/list_folder", {"path": path, "recursive": False})
    out += res["entries"]
    while res.get("has_more"):
        res = dbx_rpc(token, "files/list_folder/continue", {"cursor": res["cursor"]})
        out += res["entries"]
    return [e for e in out if e[".tag"] == "file"]

def dbx_download(token, path):
    req = urllib.request.Request("https://content.dropboxapi.com/2/files/download", data=b"")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Dropbox-API-Arg", json.dumps({"path": path}))
    req.add_header("Content-Type", "text/plain")  # Dropbox wants an empty body here
    return urllib.request.urlopen(req, timeout=120).read()

# ---------------- dictionary labels ----------------
def load_labels(dcf):
    d = json.loads(dcf)
    items = {i["name"]: i for r in d["levels"][0]["records"] for i in r["items"]}
    def vs(name):
        out = {}
        for v in (items[name].get("valueSets") or [{}])[0].get("values", []):
            labs = v["labels"]
            text = next((l["text"] for l in labs if l.get("language", "EN") == "EN"), labs[0]["text"])
            out[str(v["pairs"][0]["value"]).strip()] = text.strip()
        return out
    return {n: vs(n) for n in ("DIVISION", "DISTRICT", "AREA_TYPE", "ENUMERATOR_NAME", "AGREE", "D0")}

# ---------------- reading cases ----------------
def cases_from_zip(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for n in z.namelist():
            if n.endswith(".json"):
                yield json.loads(z.read(n).decode("utf-8"))

def get(case, record, item):
    rec = case.get(LEVEL, {}).get(record) or [{}]
    return (rec[0].get(item) or {}).get("code")

def _clock(c):
    return {x.get("deviceId"): x.get("revision", 0) for x in c.get("clock", [])}

def _covers(a, b):
    """True if clock a has seen everything clock b has (a is the same or newer)."""
    return all(a.get(d, 0) >= r for d, r in b.items())

CONFLICTS = []

def newest(files):
    """files: list of (sync_time, device_id, blob).
    Returns the latest version of every case, resolved like CSPro sync does:
    a version replaces another only if its clock covers the other's clock."""
    versions = defaultdict(list)            # uuid -> [(when, case)]
    for when, device, blob in sorted(files, key=lambda f: f[0]):
        for c in cases_from_zip(blob):
            versions[c["uuid"]].append((when, c))
    out = []
    for uid, vs in versions.items():
        tops = []
        for when, c in vs:
            ck = _clock(c)
            if any(_covers(_clock(t), ck) for _, t in tops):
                continue                    # already have this or newer
            tops = [(w, t) for w, t in tops if not _covers(ck, _clock(t))]
            tops.append((when, c))
        if len(tops) > 1:                   # edited on two tablets separately
            tops.sort(key=lambda x: x[0])
            CONFLICTS.append((str(tops[-1][1].get("key", "")).strip(),
                              [("deleted" if t.get("deleted") else "kept") for _, t in tops]))
        out.append(tops[-1][1])
    return out


# ---------------- descriptive statistics ----------------
# (key, label, unit, group, record, item, transform)
STAT_VARS = [
    ("A2",  "Age of respondent",               "years",  "Respondent", "MODULE_A", "A2",  None),
    ("A5",  "Household size",                  "people", "Respondent", "MODULE_A", "A5",  None),
    ("BAGE","Business age",                    "years",  "Business",   "MODULE_B", "B2",  "age"),
    ("F3",  "Monthly sales now",               "Tk",     "Business",   "MODULE_F", "F3",  None),
    ("F5",  "Monthly profit now",              "Tk",     "Business",   "MODULE_F", "F5",  None),
    ("F7",  "Business assets now",             "Tk",     "Business",   "MODULE_F", "F7",  None),
    ("F9",  "Stock and raw materials now",     "Tk",     "Business",   "MODULE_F", "F9",  None),
    ("E3",  "Loan received",                   "Tk",     "Loan",       "MODULE_E", "E3",  None),
    ("E8B", "Annual interest rate",            "%",      "Loan",       "MODULE_E", "E8B", None),
    ("G2",  "Regular paid workers now",        "workers","Workers",    "MODULE_G", "G2",  None),
    ("G4",  "Part-time paid workers now",      "workers","Workers",    "MODULE_G", "G4",  None),
    ("G8",  "Women among paid workers now",    "workers","Workers",    "MODULE_G", "G8",  None),
    ("G10", "Household monthly income now",    "Tk",     "Household",  "MODULE_G", "G10", None),
    ("G12", "Household monthly spending now",  "Tk",     "Household",  "MODULE_G", "G12", None),
]

def _q(xs, p):
    """Quantile with linear interpolation (same as Excel QUARTILE.INC / numpy default)."""
    if not xs: return None
    h = (len(xs) - 1) * p
    lo = int(math.floor(h)); hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (h - lo)

def _r(v, nd=2):
    return None if v is None else round(v, nd)

def describe(pairs):
    """pairs: list of (respondent_id, value). Returns summary, distribution and outlier info."""
    xs = sorted(v for _, v in pairs)
    n = len(xs)
    if n == 0:
        return {"n": 0}
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    med = _q(xs, .5); q1 = _q(xs, .25); q3 = _q(xs, .75); iqr = q3 - q1
    cnt = Counter(xs); top = max(cnt.values())
    modes = sorted(v for v, c in cnt.items() if c == top) if top > 1 else []
    out = {"n": n, "mean": _r(mean), "median": _r(med), "sd": _r(sd), "min": xs[0], "max": xs[-1],
           "q1": _r(q1), "q3": _r(q3), "iqr": _r(iqr), "mode": modes[:3], "mode_count": top if modes else 0}
    # shape: sample skewness (G1) and excess kurtosis (G2)
    m2 = sum((x - mean) ** 2 for x in xs) / n
    m3 = sum((x - mean) ** 3 for x in xs) / n
    m4 = sum((x - mean) ** 4 for x in xs) / n
    if m2 > 0 and n > 3:
        g1 = m3 / m2 ** 1.5; g2 = m4 / m2 ** 2 - 3
        skew = math.sqrt(n * (n - 1)) / (n - 2) * g1
        kurt = (n - 1) / ((n - 2) * (n - 3)) * ((n + 1) * g2 + 6)
        jb = n / 6 * (g1 ** 2 + g2 ** 2 / 4)
        p = math.exp(-jb / 2)               # chi-square with 2 df
        out.update({"skew": _r(skew), "kurtosis": _r(kurt), "jb": _r(jb), "jb_p": round(p, 4)})
        shape = "symmetric" if abs(skew) < 0.5 else ("right-skewed" if skew > 0 else "left-skewed")
        if n < 8:
            verdict = "Too few answers to judge"
        elif p >= 0.05:
            verdict = "Close to normal"
        else:
            verdict = "Not normal"
        out.update({"shape": shape, "normal": verdict})
    else:
        out.update({"shape": "all the same value" if n > 1 else "one answer", "normal": "Too few answers to judge"})
    # outliers: Tukey fences (1.5 x IQR), extreme at 3 x IQR
    lf, uf = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    elf, euf = q1 - 3 * iqr, q3 + 3 * iqr
    outl = sorted(((rid, v) for rid, v in pairs if v < lf or v > uf), key=lambda t: t[1])
    inside = [x for x in xs if lf <= x <= uf] or xs
    out.update({"fence_low": _r(lf), "fence_high": _r(uf),
                "whisker_low": inside[0], "whisker_high": inside[-1],
                "outliers": [{"id": rid, "value": v, "extreme": v < elf or v > euf} for rid, v in outl]})
    # histogram with expected normal counts
    lo, hi = xs[0], xs[-1]
    ints = all(float(x).is_integer() for x in xs)
    if hi == lo:
        edges = [lo - .5, hi + .5]
    elif ints and hi - lo <= 15:
        edges = [lo - .5 + i for i in range(int(hi - lo) + 2)]
    else:
        k = max(5, min(12, math.ceil(math.log2(n)) + 1))
        w = (hi - lo) / k
        edges = [lo + i * w for i in range(k + 1)]
    counts = [0] * (len(edges) - 1)
    for x in xs:
        i = min(len(counts) - 1, max(0, int((x - edges[0]) / ((edges[-1] - edges[0]) / len(counts)))))
        counts[i] += 1
    exp = []
    for i in range(len(counts)):
        a, b = edges[i], edges[i + 1]
        if sd > 0:
            cdf = lambda z: 0.5 * (1 + math.erf((z - mean) / (sd * math.sqrt(2))))
            exp.append(round(n * (cdf(b) - cdf(a)), 2))
        else:
            exp.append(counts[i])
    out["hist"] = {"edges": [_r(e) for e in edges], "counts": counts, "normal": exp}
    return out

def stats_block(cases, grp_of, year):
    res = {}
    for key, label, unit, group, rec, item, tf in STAT_VARS:
        pairs, by_g = [], defaultdict(list)
        for c in cases:
            v = get(c, rec, item)
            if v is None or isinstance(v, str):
                continue
            if tf == "age":
                if not (1900 < v <= year): continue
                v = year - v
            rid = str(c.get("key", "")).strip()
            pairs.append((rid, v)); by_g[grp_of(c)].append(v)
        d = describe(pairs)
        d.update({"label": label, "unit": unit, "group": group, "question": item})
        d["by_group"] = {g: {"n": len(v), "mean": _r(sum(v) / len(v)), "median": _r(_q(sorted(v), .5))} for g, v in by_g.items() if v}
        res[key] = d
    return res

# ---------------- summary ----------------
def summarize(cases, labels, syncs):
    L = lambda f, code: labels[f].get(str(code).strip(), f"Unknown code {code}") if code is not None else "Missing"
    live = [c for c in cases if not c.get("deleted")]
    partial = [c for c in live if c.get("partialSave")]
    done = [c for c in live if not c.get("partialSave")]
    refused = [c for c in done if get(c, "CONSENT", "AGREE") == 2]
    interviewed = [c for c in done if get(c, "CONSENT", "AGREE") != 2]

    def day(c):
        v = get(c, "FIELD_INFO", "DATE_OF_INTERVIEW")
        try:
            return dt.datetime.strptime(str(int(v)), "%Y%m%d").date()
        except Exception:
            return None

    today = dt.datetime.now(BD).date()
    days = [day(c) for c in interviewed if day(c)]
    timeline = {}
    if days:
        d0, d1 = min(days), max(max(days), today)
        cnt = Counter(days)
        d = d0
        while d <= d1:
            timeline[d.isoformat()] = cnt.get(d, 0)
            d += dt.timedelta(days=1)

    grp = lambda c: L("D0", get(c, "MODULE_D", "D0"))
    timeline_group = defaultdict(Counter)
    for c in interviewed:
        dd = day(c)
        if dd:
            timeline_group[dd.isoformat()][grp(c)] += 1
    area_group = defaultdict(Counter)
    for c in interviewed:
        area_group[L("AREA_TYPE", get(c, "FIELD_INFO", "AREA_TYPE"))][grp(c)] += 1

    count = lambda field, rec, cs: Counter(L(field, get(c, rec, field)) for c in cs)
    by_div = count("DIVISION", "FIELD_INFO", interviewed)
    div_group = defaultdict(Counter)
    for c in interviewed:
        div_group[L("DIVISION", get(c, "FIELD_INFO", "DIVISION"))][L("D0", get(c, "MODULE_D", "D0"))] += 1

    enum = {}
    for c in interviewed:
        name = L("ENUMERATOR_NAME", get(c, "FIELD_INFO", "ENUMERATOR_NAME"))
        e = enum.setdefault(name, {"interviews": 0, "last": None, "today": 0})
        e["interviews"] += 1
        dd = day(c)
        if dd and (e["last"] is None or dd.isoformat() > e["last"]):
            e["last"] = dd.isoformat()
        if dd == today:
            e["today"] += 1

    # data checks (counts only, with respondent IDs so the team can fix them)
    issues = []
    def flag(kind, c):
        issues.append({"issue": kind, "respondent_id": str(c.get("key", "")).strip(),
                       "enumerator": L("ENUMERATOR_NAME", get(c, "FIELD_INFO", "ENUMERATOR_NAME"))})
    keys = Counter(str(c.get("key", "")).strip() for c in live)
    for c in live:
        if keys[str(c.get("key", "")).strip()] > 1:
            flag("Same respondent ID used twice", c)
    for c in interviewed:
        if str(get(c, "FIELD_INFO", "ENUMERATOR_NAME")) not in labels["ENUMERATOR_NAME"]:
            flag("Enumerator code not in list", c)
        if get(c, "MODULE_D", "D0") is None:
            flag("Survey group (D0) missing", c)
        dd = day(c)
        if dd is None:
            flag("Interview date missing or invalid", c)
        elif dd > today:
            flag("Interview date is in the future", c)

    return {
        "updated": dt.datetime.now(BD).strftime("%Y-%m-%d %H:%M"),
        "totals": {
            "interviews": len(interviewed),
            "refused": len(refused),
            "partial": len(partial),
            "today": sum(1 for d in days if d == today),
            "last7": sum(1 for d in days if (today - d).days < 7),
            "tablets": len({s["device"] for s in syncs}),
        },
        "timeline": timeline,
        "timeline_group": {k: dict(v) for k, v in sorted(timeline_group.items())},
        "division": dict(by_div.most_common()),
        "division_group": {k: dict(v) for k, v in div_group.items()},
        "district": dict(count("DISTRICT", "FIELD_INFO", interviewed).most_common()),
        "area": dict(count("AREA_TYPE", "FIELD_INFO", interviewed)),
        "area_group": {k: dict(v) for k, v in area_group.items()},
        "group": dict(count("D0", "MODULE_D", interviewed)),
        "enumerators": dict(sorted(enum.items(), key=lambda kv: -kv[1]["interviews"])),
        "issues": issues,
        "stats": stats_block(interviewed, grp, today.year),
        "tablet_syncs": sorted(syncs_latest(syncs), key=lambda s: s["last_sync"], reverse=True),
    }

def syncs_latest(syncs):
    last = {}
    for s in syncs:
        if s["device"] not in last or s["when"] > last[s["device"]]:
            last[s["device"]] = s["when"]
    return [{"tablet": d[-6:], "last_sync": w} for d, w in last.items()]

# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local"); ap.add_argument("--dcf")
    a = ap.parse_args()
    files, syncs = [], []
    if a.local:
        dcf = open(a.dcf, encoding="utf-8").read()
        for n in os.listdir(a.local):
            p = os.path.join(a.local, n)
            when = dt.datetime.fromtimestamp(os.path.getmtime(p), BD).strftime("%Y-%m-%d %H:%M")
            files.append((when, n.split("$")[0], open(p, "rb").read()))
    else:
        tok = dbx_token()
        dict_files = dbx_list(tok, DICT_DIR)
        dcf = dbx_download(tok, next(f for f in dict_files if f["name"].lower().endswith(".dcf"))["path_lower"]).decode("utf-8")
        listing = dbx_list(tok, DATA_DIR)
        print(f"Found {len(listing)} sync files in Dropbox. Downloading...", flush=True)
        from concurrent.futures import ThreadPoolExecutor
        def fetch(f):
            when = dt.datetime.fromisoformat(f["server_modified"].replace("Z", "+00:00")).astimezone(BD).strftime("%Y-%m-%d %H:%M")
            for attempt in range(3):
                try:
                    return (when, f["name"].split("$")[0], dbx_download(tok, f["path_lower"]))
                except Exception as e:
                    if attempt == 2:
                        raise
        with ThreadPoolExecutor(max_workers=8) as pool:
            for i, res in enumerate(pool.map(fetch, listing), 1):
                files.append(res)
                if i % 25 == 0 or i == len(listing):
                    print(f"  downloaded {i}/{len(listing)}", flush=True)
    good = []
    for when, dev, blob in files:
        try:
            zipfile.ZipFile(io.BytesIO(blob)).testzip()
            good.append((when, dev, blob))
            syncs.append({"device": dev, "when": when})
        except zipfile.BadZipFile:
            print("Skipped a file that is not a CSPro sync file")
    cases = newest(good)
    summary = summarize(cases, load_labels(dcf), syncs)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    try:
        old = json.load(open(OUT, encoding="utf-8"))
        old.pop("updated", None)
    except Exception:
        old = None
    new = dict(summary); new.pop("updated")
    if old == new:
        print("No new data since last run.")
    else:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
    with open(os.path.join(os.path.dirname(OUT), "status.json"), "w", encoding="utf-8") as f:
        json.dump({"checked": dt.datetime.now(BD).strftime("%Y-%m-%d %H:%M"), "sync_files": len(good)}, f)
    # ---- check list for comparing with CSPro (IDs only) ----
    live = [c for c in cases if not c.get("deleted")]
    keys = Counter(str(c.get("key", "")).strip() for c in live)
    print("")
    print("=== CHECK LIST ===")
    print(f"Cases in sync files (all versions merged): {len(cases)}")
    print(f"  deleted: {sum(1 for c in cases if c.get('deleted'))}")
    print(f"  not deleted: {len(live)}  (partly saved: {sum(1 for c in live if c.get('partialSave'))})")
    dup = sorted(k for k, n in keys.items() if n > 1)
    print(f"Respondent IDs used by more than one case: {', '.join(dup) if dup else 'none'}")
    print(f"Cases edited on two tablets separately: {len(CONFLICTS)}")
    for k, states in CONFLICTS:
        print(f"  {k}: versions {', '.join(states)} (using the last synced one)")
    print("Respondent IDs counted on the dashboard:")
    ids = sorted(k for k in keys)
    for i in range(0, len(ids), 10):
        print("  " + "  ".join(ids[i:i + 10]))
    print("==================")
    t = summary["totals"]
    print(f"Read {len(good)} sync files, {len(cases)} cases.")
    print(f"Interviews: {t['interviews']}  Refused: {t['refused']}  Partial: {t['partial']}  Data issues: {len(summary['issues'])}")

if __name__ == "__main__":
    main()
