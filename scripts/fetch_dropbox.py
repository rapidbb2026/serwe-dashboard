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
import io, json, os, sys, zipfile, datetime as dt, urllib.request, urllib.parse, base64, argparse
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

def newest(files):
    """files: list of (sync_time_iso, device_id, blob). Returns newest version of every case by uuid."""
    best = {}
    for when, device, blob in sorted(files, key=lambda f: f[0]):
        for c in cases_from_zip(blob):
            score = sum(x.get("revision", 0) for x in c.get("clock", []))
            old = best.get(c["uuid"])
            if old is None or score >= old[0]:
                best[c["uuid"]] = (score, c)
    return [c for _, c in best.values()]

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
        "division": dict(by_div.most_common()),
        "division_group": {k: dict(v) for k, v in div_group.items()},
        "district": dict(count("DISTRICT", "FIELD_INFO", interviewed).most_common()),
        "area": dict(count("AREA_TYPE", "FIELD_INFO", interviewed)),
        "group": dict(count("D0", "MODULE_D", interviewed)),
        "enumerators": dict(sorted(enum.items(), key=lambda kv: -kv[1]["interviews"])),
        "issues": issues,
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
    t = summary["totals"]
    print(f"Read {len(good)} sync files, {len(cases)} cases.")
    print(f"Interviews: {t['interviews']}  Refused: {t['refused']}  Partial: {t['partial']}  Data issues: {len(summary['issues'])}")

if __name__ == "__main__":
    main()
