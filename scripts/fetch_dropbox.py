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
            k = str(v["pairs"][0]["value"]).strip()
            # the same code used for two labels: show both so nothing is mislabelled
            out[k] = out[k] + " / " + text.strip() if k in out and out[k] != text.strip() else text.strip()
        return out
    out = {n: vs(n) for n in ("DIVISION", "DISTRICT", "AREA_TYPE", "ENUMERATOR_NAME", "AGREE", "D0")}
    out["_dcf"] = d
    return out

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
    # --- A: respondent ---
    ("A2",  "Age of respondent",                   "years",   "A · Respondent", "MODULE_A", "A2",  None),
    ("A5",  "Household size",                      "people",  "A · Respondent", "MODULE_A", "A5",  None),
    # --- B: enterprise ---
    ("BAGE","Business age",                        "years",   "B · Enterprise", "MODULE_B", "B2",  "age"),
    ("B5",  "Share of business owned",             "%",       "B · Enterprise", "MODULE_B", "B5",  None),
    # --- D and E: loan ---
    ("D4A", "Year of application",                 "year",    "D-E · Loan",     "MODULE_D", "D4A", "year"),
    ("E3",  "Loan received",                       "Tk",      "D-E · Loan",     "MODULE_E", "E3",  None),
    ("E6",  "Loan applied for",                    "Tk",      "D-E · Loan",     "MODULE_E", "E6",  None),
    ("E4A", "Year loan received",                  "year",    "D-E · Loan",     "MODULE_E", "E4A", "year"),
    ("E5A", "Year loan fully repaid",              "year",    "D-E · Loan",     "MODULE_E", "E5A", "year"),
    ("E8B", "Annual interest rate",                "%",       "D-E · Loan",     "MODULE_E", "E8B", None),
    # --- F: business, before and now ---
    ("F2",  "Monthly sales before",                "Tk",      "F · Business",   "MODULE_F", "F2",  None),
    ("F3",  "Monthly sales now",                   "Tk",      "F · Business",   "MODULE_F", "F3",  None),
    ("F4",  "Monthly profit before",               "Tk",      "F · Business",   "MODULE_F", "F4",  None),
    ("F5",  "Monthly profit now",                  "Tk",      "F · Business",   "MODULE_F", "F5",  None),
    ("F6",  "Business assets before",              "Tk",      "F · Business",   "MODULE_F", "F6",  None),
    ("F7",  "Business assets now",                 "Tk",      "F · Business",   "MODULE_F", "F7",  None),
    ("F8",  "Stock and raw materials before",      "Tk",      "F · Business",   "MODULE_F", "F8",  None),
    ("F9",  "Stock and raw materials now",         "Tk",      "F · Business",   "MODULE_F", "F9",  None),
    ("F10", "Products or services before",         "items",   "F · Business",   "MODULE_F", "F10", None),
    ("F11", "Products or services now",            "items",   "F · Business",   "MODULE_F", "F11", None),
    ("F12", "Business locations before",           "places",  "F · Business",   "MODULE_F", "F12", None),
    ("F13", "Business locations now",              "places",  "F · Business",   "MODULE_F", "F13", None),
    ("F16", "Spent on land or business space",     "Tk",      "F · Business",   "MODULE_F", "F16", None),
    # --- G: workers and household, before and now ---
    ("G1",  "Regular paid workers before",         "workers", "G · Workers and household", "MODULE_G", "G1",  None),
    ("G2",  "Regular paid workers now",            "workers", "G · Workers and household", "MODULE_G", "G2",  None),
    ("G3",  "Part-time paid workers before",       "workers", "G · Workers and household", "MODULE_G", "G3",  None),
    ("G4",  "Part-time paid workers now",          "workers", "G · Workers and household", "MODULE_G", "G4",  None),
    ("G5",  "Unpaid family workers before",        "workers", "G · Workers and household", "MODULE_G", "G5",  None),
    ("G6",  "Unpaid family workers now",           "workers", "G · Workers and household", "MODULE_G", "G6",  None),
    ("G7",  "Women among paid workers before",     "workers", "G · Workers and household", "MODULE_G", "G7",  None),
    ("G8",  "Women among paid workers now",        "workers", "G · Workers and household", "MODULE_G", "G8",  None),
    ("G9",  "Household monthly income before",     "Tk",      "G · Workers and household", "MODULE_G", "G9",  None),
    ("G10", "Household monthly income now",        "Tk",      "G · Workers and household", "MODULE_G", "G10", None),
    ("G11", "Household monthly spending before",   "Tk",      "G · Workers and household", "MODULE_G", "G11", None),
    ("G12", "Household monthly spending now",      "Tk",      "G · Workers and household", "MODULE_G", "G12", None),
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

def cat_rows(dcf, cases, day_of):
    """The option codes each interview picked, one row per interview (same order as micro rows),
    so Response Breakdown can be filtered by date in the browser."""
    items = []
    for rec in dcf["levels"][0]["records"]:
        if rec["name"] in CAT_SKIP_RECORDS:
            continue
        for it in rec["items"]:
            vals = (it.get("valueSets") or [{}])[0].get("values", [])
            name = it["name"]
            if not vals or name in CAT_SKIP_ITEMS or name.endswith("_OTHER"):
                continue
            codes = {str(v["pairs"][0]["value"]).strip() for v in vals}
            items.append((rec["name"], name, it.get("contentType") == "alpha", codes))
    rows = []
    for c in cases:
        if not day_of(c):
            continue
        row = []
        for rec, name, multi, codes in items:
            raw = get(c, rec, name)
            txt = None
            if raw is not None:
                txt = str(int(raw)) if isinstance(raw, float) and raw.is_integer() else str(raw).strip()
                if multi:
                    txt = txt.upper()
                    if not txt or not all(ch in codes for ch in txt):
                        txt = None
                elif not txt:
                    txt = None
            row.append(txt)
        rows.append(row)
    return [i[1] for i in items], rows

def micro_block(cases, grp_of, day_of, year):
    """One row per interview with only the numeric answers, so the dashboard can filter the
    statistics by date. Saved as data/micro.json for the website only (not kept in the repo history)."""
    days = sorted({day_of(c).isoformat() for c in cases if day_of(c)})
    idx = {d: i for i, d in enumerate(days)}
    rows = []
    for c in cases:
        dd = day_of(c)
        if not dd:
            continue
        g = grp_of(c)
        row = [str(c.get("key", "")).strip(), idx[dd.isoformat()], 1 if g == "Beneficiary" else 2 if g == "Non Beneficiary" else 0]
        for key, label, unit, group, rec, item, tf in STAT_VARS:
            v = get(c, rec, item)
            if v is None or isinstance(v, str):
                v = None
            elif tf == "age":
                v = year - v if 1900 < v <= year else None
            elif tf == "year" and not (1900 < v <= year + 1):
                v = None
            row.append(v)
        rows.append(row)
    return {"days": days, "vars": [k[0] for k in STAT_VARS], "rows": rows}

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
            if tf == "year" and not (1900 < v <= year + 1):
                continue                      # skip impossible years
            rid = str(c.get("key", "")).strip()
            pairs.append((rid, v)); by_g[grp_of(c)].append(v)
        d = describe(pairs)
        d.update({"label": label, "unit": unit, "group": group, "question": item})
        d["by_group"] = {g: {"n": len(v), "mean": _r(sum(v) / len(v)), "median": _r(_q(sorted(v), .5))} for g, v in by_g.items() if v}
        res[key] = d
    return res


# ---------------- questions answered by choosing options ----------------
CAT_SKIP_RECORDS = {"FIELD_INFO"}                 # already shown elsewhere on the dashboard
CAT_SKIP_ITEMS = {"D4B", "E4B", "E5B"}            # month pickers

def _en(labels):
    return next((l["text"] for l in labels if l.get("language", "EN") == "EN"), labels[0]["text"] if labels else "").strip()

def categorical_block(dcf, cases, grp_of):
    """Counts per answer option for every question that has a value set.
    Numeric items are single choice; text items hold one character per ticked option."""
    res = {}
    for rec in dcf["levels"][0]["records"]:
        if rec["name"] in CAT_SKIP_RECORDS:
            continue
        module = _en(rec.get("labels", [])).replace("Module ", "").replace(":", " ·", 1)
        if rec["name"] == "CONSENT":
            module = "Consent"
        module = module.split(",")[0]
        if len(module) > 46:
            module = module[:44].rstrip() + "…"
        for it in rec["items"]:
            vals = (it.get("valueSets") or [{}])[0].get("values", [])
            name = it["name"]
            if not vals or name in CAT_SKIP_ITEMS or name.endswith("_OTHER"):
                continue
            multi = it.get("contentType") == "alpha"
            order, lab = [], {}
            for v in vals:
                k = str(v["pairs"][0]["value"]).strip()
                t = _en(v["labels"])
                if k in lab:
                    if t not in lab[k]: lab[k] += " / " + t
                else:
                    lab[k] = t; order.append(k)
            cnt = {k: Counter() for k in order}
            n = 0; by_n = Counter()
            for c in cases:
                raw = get(c, rec["name"], name)
                if raw is None:
                    continue
                txt = str(raw).strip()
                if isinstance(raw, float) and raw.is_integer():
                    txt = str(int(raw))
                if not txt:
                    continue
                picked = [ch for ch in txt.upper()] if multi else [txt]
                if multi and not all(ch in lab for ch in picked):
                    continue                           # not a real answer (for example a placeholder)
                g = grp_of(c); n += 1; by_n[g] += 1
                for k in dict.fromkeys(picked):
                    if k not in cnt:
                        cnt[k] = Counter(); lab[k] = f"Unknown code {k}"; order.append(k)
                    cnt[k][g] += 1
            q = _en(it.get("labels", []))
            if q.upper().startswith(name.upper() + "."):
                q = q[len(name) + 1:].strip()
            res[name] = {"question": name, "label": q, "group": module, "multi": multi, "n": n,
                         "n_ben": by_n.get("Beneficiary", 0), "n_non": by_n.get("Non Beneficiary", 0),
                         "options": [{"code": k, "label": lab[k], "count": sum(cnt[k].values()),
                                      "ben": cnt[k].get("Beneficiary", 0), "non": cnt[k].get("Non Beneficiary", 0)} for k in order]}
    return res

# District codes from the first version of the app (1-64, alphabetical).
# Used only for codes the current dictionary does not have.
OLD_DISTRICT = {"1": "Bagerhat", "2": "Bandarban", "3": "Barguna", "4": "Barishal", "5": "Bhola", "6": "Bogura", "7": "Brahmanbaria", "8": "Chandpur", "9": "Chapai Nawabganj", "10": "Chattogram", "11": "Chuadanga", "12": "Cox's Bazar", "13": "Cumilla", "14": "Dhaka", "15": "Dinajpur", "16": "Faridpur", "17": "Feni", "18": "Gaibandha", "19": "Gazipur", "20": "Gopalganj", "21": "Habiganj", "22": "Jamalpur", "23": "Jashore", "24": "Jhalokathi", "25": "Jhenaidah", "26": "Joypurhat", "27": "Khagrachhari", "28": "Khulna", "29": "Kishoreganj", "30": "Kurigram", "31": "Kushtia", "32": "Lakshmipur", "33": "Lalmonirhat", "34": "Madaripur", "35": "Magura", "36": "Manikganj", "37": "Meherpur", "38": "Moulvibazar", "39": "Munshiganj", "40": "Mymensingh", "41": "Naogaon", "42": "Narail", "43": "Narayanganj", "44": "Narsingdi", "45": "Natore", "46": "Netrokona", "47": "Nilphamari", "48": "Noakhali", "49": "Pabna", "50": "Panchagarh", "51": "Patuakhali", "52": "Pirojpur", "53": "Rajbari", "54": "Rajshahi", "55": "Rangamati", "56": "Rangpur", "57": "Satkhira", "58": "Shariatpur", "59": "Sherpur", "60": "Sirajganj", "61": "Sunamganj", "62": "Sylhet", "63": "Tangail", "64": "Thakurgaon"}

# ---------------- summary ----------------
def summarize(cases, labels, syncs):
    def _dist_fix(code):
        k = str(code).strip()
        if code is not None and k not in labels["DISTRICT"] and k in OLD_DISTRICT:
            return OLD_DISTRICT[k]
        return None
    L0 = lambda f, code: labels[f].get(str(code).strip(), f"Unknown code {code}") if code is not None else "Missing"
    L = lambda f, code: (_dist_fix(code) or L0(f, code)) if f == "DISTRICT" else L0(f, code)
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
        if _dist_fix(get(c, "FIELD_INFO", "DISTRICT")):
            flag("District entered with the old code list (app not updated)", c)
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
        "answers": categorical_block(labels["_dcf"], interviewed, grp),
        "_micro": dict(micro_block(interviewed, grp, day, today.year),
                       **dict(zip(("cvars", "crows"), cat_rows(labels["_dcf"], interviewed, day)))),
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
    micro = summary.pop("_micro")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(os.path.join(os.path.dirname(OUT), "micro.json"), "w", encoding="utf-8") as f:
        json.dump(micro, f, separators=(",", ":"))
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
