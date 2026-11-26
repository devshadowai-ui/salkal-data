"""סל קל פלוס — מחירים ממוצעים לכל מוצר פשוט, לכל רשת ולכל סניף.

לכל מוצר פשוט שמופיע ברשימות ("חלב 3%", "חיתולים", "קנלוני") מחשבים בכל סניף את המחיר הממוצע
של כל המוצרים מהסוג הזה (בלי הקצוות הקיצוניים), ומזה האפליקציה מרכיבה את מחיר הסל.

שומר:
  prices/index.json      — רשתות, ממוצע ארצי לכל רשת, ורשימת ערים
  prices/c/<n>.json      — כל הסניפים בעיר אחת עם המחירים שלהם (האפליקציה טוענת רק את העיר שנבחרה)
"""
import html, json, os, re, sys, time
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from collect import (get, parse_xml, ci_all, ci_text, file_info, CERB, CERB_CHAINS, UA, _tok, key, tidy, CATS)

MAX_STORES = int(os.environ.get("MAX_STORES", "700"))   # תקרת סניפים לרשת (ביטחון לזמן ריצה)

# ---------- שמות ערים לפי סמל יישוב (חלק מהרשתות כותבות מספר במקום שם) ----------
CITY_CODES = {
    "3000": "ירושלים", "5000": "תל אביב-יפו", "4000": "חיפה", "8300": "ראשון לציון", "7900": "פתח תקווה", "70": "אשדוד",
    "7400": "נתניה", "9000": "באר שבע", "6100": "בני ברק", "6600": "חולון", "8600": "רמת גן", "7100": "אשקלון",
    "8400": "רחובות", "6200": "בת ים", "2610": "בית שמש", "6900": "כפר סבא", "6400": "הרצליה", "6500": "חדרה",
    "1200": "מודיעין-מכבים-רעות", "7300": "נצרת", "7000": "לוד", "8500": "רמלה", "8700": "רעננה", "2530": "באר יעקב",
    "7200": "נס ציונה", "2640": "ראש העין", "9700": "הוד השרון", "2630": "קריית גת", "2600": "אילת", "7700": "עפולה",
    "2660": "יבנה", "2400": "אור יהודה", "6800": "קריית אתא", "8200": "קריית מוצקין", "9500": "קריית ביאליק",
    "9600": "קריית ים", "9100": "נהריה", "6700": "טבריה", "2200": "דימונה", "2560": "ערד", "6300": "גבעתיים",
    "2620": "קריית אונו", "681": "גבעת שמואל", "9400": "יהוד-מונוסון", "3570": "אריאל", "3616": "מעלה אדומים",
    "31": "אופקים", "1031": "שדרות", "246": "נתיבות", "2800": "קריית שמונה", "8000": "צפת", "1139": "כרמיאל",
    "874": "מגדל העמק", "2500": "נשר", "2100": "טירת כרמל", "7600": "עכו", "1309": "אלעד", "3780": "ביתר עילית",
    "3797": "מודיעין עילית", "2550": "גדרה", "1034": "קריית מלאכי", "9300": "זכרון יעקב", "7800": "פרדס חנה-כרכור",
    "168": "כפר יונה", "1304": "שוהם", "229": "גני תקווה", "1020": "אור עקיבא", "9200": "בית שאן",
}
UNKNOWN_CODES = Counter()
def city_name(raw):
    raw = (raw or "").strip()
    if raw.isdigit():
        c = CITY_CODES.get(raw.lstrip("0") or "0")
        if not c and raw.strip("0"): UNKNOWN_CODES[raw.lstrip("0")] += 1
        return c
    raw = re.sub(r"\s+", " ", raw.replace("-", " - ")).replace(" - ", "-").strip()
    return raw or None


# ---------- מוצר פשוט לכל שם מוצר ----------
GENERIC = sorted({(key(t), t) for d in CATS["departments"].values() for t in d}, key=lambda x: -len(x[0]))
_gcache = {}
def generic_of(name):
    """("חלב טרי 3% תנובה 1 ליטר") -> ("חלב 3%"?, "חלב") : מוצר פשוט מהמילון + המילה הראשונה"""
    if name in _gcache: return _gcache[name]
    k = key(tidy(name))
    g = next((t for kk, t in GENERIC if k.startswith(kk)), None)
    toks = [t for t in k.split() if not re.search(r"\d", t) and len(t) >= 2]
    w = toks[0] if toks else None
    _gcache[name] = (g, w); return g, w

def trimmed_mean(xs):
    xs = sorted(xs)
    if len(xs) >= 5: cut = max(1, len(xs) // 10); xs = xs[cut:-cut]
    return round(sum(xs) / len(xs), 2)


# ---------- רשימות קבצים ----------
def shufersal_lists():
    def pages(cat):
        links = []
        for page in range(1, 120):
            r = get(f"https://prices.shufersal.co.il/FileObject/UpdateCategory?catID={cat}&storeId=0&page={page}")
            found = re.findall(r'href="(https://[^"]+?\.(?:gz|xml|zip)[^"]*)"', r.text)
            if not found: break
            links += [html.unescape(u) for u in found]
        return links
    stores_xml = get(pages(5)[0]).content
    return stores_xml, pages(2), None

def cerberus_lists(user, pw):
    s = requests.Session(); s.headers["User-Agent"] = UA; s.verify = False
    t = _tok(s.get(CERB + "/login", timeout=60).text)
    s.post(CERB + "/login/user", data={"r": "", "username": user, "password": pw, "Submit": "Sign in", "csrftoken": t}, timeout=60)
    t = _tok(s.get(CERB + "/file", timeout=60).text)
    r = s.post(CERB + "/file/json/dir", data={"sEcho": 1, "iColumns": 5, "sColumns": ",,,,", "iDisplayStart": 0,
               "iDisplayLength": 100000, "mDataProp_0": "fname", "mDataProp_1": "type", "mDataProp_2": "size",
               "mDataProp_3": "ftime", "mDataProp_4": "", "sSearch": "", "bRegex": "false", "iSortingCols": 0,
               "cd": "/", "csrftoken": t}, timeout=120)
    names = [f["fname"] for f in r.json().get("aaData", [])]
    st = sorted([n for n in names if n.lower().startswith("stores")], reverse=True)
    stores_xml = s.get(CERB + "/file/d/" + st[0], timeout=120).content if st else b""
    return stores_xml, [CERB + "/file/d/" + n for n in names if n.lower().startswith("pricefull")], s

def stores_of(xml):
    out = {}
    if not xml: return out
    for e in ci_all(parse_xml(xml), "store"):
        sid = ci_text(e, "storeid").lstrip("0")
        if not sid: continue
        out[int(sid)] = {"name": ci_text(e, "storename"), "city": city_name(ci_text(e, "city")) or "", "addr": ci_text(e, "address")}
    return out

def latest_by_store(links):
    best = {}
    for u in links:
        inf = file_info(u.split("/")[-1].split("?")[0])
        if inf and (inf[0] not in best or inf[1] > best[inf[0]][0]): best[inf[0]] = (inf[1], u)
    return {sid: u for sid, (d, u) in best.items()}


# ---------- מחירים לסניף ----------
def store_prices(url, session):
    root = parse_xml(get(url, session=session, timeout=240).content)
    g_prices, w_prices = defaultdict(list), defaultdict(list)
    for it in ci_all(root, "item"):
        name, price = ci_text(it, "itemname"), ci_text(it, "itemprice")
        try: p = float(price)
        except ValueError: continue
        if not name or p <= 0 or p > 2000: continue
        g, w = generic_of(name)
        if g: g_prices[g].append(p)
        if w: w_prices[w].append(p)
    return ({g: trimmed_mean(v) for g, v in g_prices.items()},
            {w: trimmed_mean(v) for w, v in w_prices.items() if len(v) >= 2})


# ---------- מיקום סניפים (פעם אחת לכל כתובת; נשמר ב-geo-cache.json) ----------
GEO_FILE = "geo-cache.json"
def geocode_all(stores, budget_sec=1200):
    try: cache = json.load(open(GEO_FILE, encoding="utf-8"))
    except Exception: cache = {}
    sess = requests.Session(); sess.headers["User-Agent"] = "salkal-prices/1.0 (github.com/devshadowai-ui/salkal-data)"
    t_end = time.time() + budget_sec
    def look(q):
        if q in cache: return cache[q]
        if time.time() > t_end: return None
        try:
            r = sess.get("https://nominatim.openstreetmap.org/search", params={"q": q, "format": "json", "limit": 1, "countrycodes": "il"}, timeout=30)
            j = r.json(); cache[q] = [round(float(j[0]["lat"]), 5), round(float(j[0]["lon"]), 5)] if j else None
        except Exception: cache[q] = None
        time.sleep(1.1)                       # כללי השימוש של OpenStreetMap: בקשה אחת בשנייה
        return cache[q]
    for s in stores:
        addr = re.sub(r"\s+", " ", s["addr"] or "").strip()
        if addr and s["city"]: ll = look(f"{addr}, {s['city']}")
        elif addr: ll = look(f"{addr}, {s['name']}, ישראל") or look(f"{addr}, ישראל")
        else: ll = None
        if not ll and s["city"]: ll = look(s["city"]); s["approx"] = 1 if ll else 0
        if ll: s["lat"], s["lon"] = ll
    json.dump(cache, open(GEO_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    return sum(1 for s in stores if "lat" in s)


def main():
    t0 = time.time(); report = []
    chains, stores_all = {}, []          # chain -> {"g":..., "w":...} ; stores_all: list of store dicts
    sources = [("שופרסל", shufersal_lists)] + [(c, (lambda u=u, p=p: cerberus_lists(u, p))) for c, (u, p) in CERB_CHAINS.items()]
    try:   # רשתות שהורדו לתיקייה dumps/ (קרפור, ויקטורי, חצי חינם, סיטי מרקט)
        import extras
        if not os.path.isdir(extras.DUMPS): extras.download()
        for chain, stores_list, files in extras.local_sources():
            sources.append((chain, lambda st=stores_list, f=files: (st, f, None)))
    except Exception as e:
        report.append(f"❌ רשתות נוספות: {str(e)[:150]}")
    for chain, fn in sources:
        try:
            stores_xml, links, sess = fn()
            stores = {}
            for x in (stores_xml if isinstance(stores_xml, list) else [stores_xml]): stores.update(stores_of(x))
            files = latest_by_store(links)
            files = dict(list(files.items())[:MAX_STORES])
            got = []
            def work(item):
                sid, url = item
                try: return sid, store_prices(url, sess)
                except Exception as e: print("  failed", chain, sid, str(e)[:100]); return sid, None
            with ThreadPoolExecutor(6) as ex:
                for sid, res in ex.map(work, files.items()):
                    if not res: continue
                    info = stores.get(sid, {"name": f"סניף {sid}", "city": "", "addr": ""})
                    got.append({"chain": chain, "id": sid, "name": info["name"], "city": info["city"], "addr": info["addr"], "g": res[0], "w": res[1]})
            # ממוצע ארצי לרשת: ממוצע של הסניפים
            cg, cw = defaultdict(list), defaultdict(list)
            for s in got:
                for g, p in s["g"].items(): cg[g].append(p)
                for w, p in s["w"].items(): cw[w].append(p)
            chains[chain] = {"stores": len(got), "g": {g: trimmed_mean(v) for g, v in cg.items()},
                             "w": {w: trimmed_mean(v) for w, v in cw.items() if len(v) >= 2}}
            stores_all += got
            report.append(f"✅ {chain}: {len(got)} סניפים עם מחירים, {len(cg)} מוצרים פשוטים")
        except Exception as e:
            report.append(f"❌ {chain}: {str(e)[:150]}")
        print(report[-1], flush=True)

    if len(stores_all) < 50:
        print("מעט מדי סניפים — לא מעדכן"); sys.exit(1)

    located = geocode_all(stores_all)
    report.append(f"📍 סניפים עם מיקום: {located} מתוך {len(stores_all)}")
    # סניפים בלי עיר: לפי העיר שמופיעה בשם הסניף, או לפי העיר הקרובה ביותר על המפה
    known = defaultdict(list)
    for s in stores_all:
        if s["city"] and "lat" in s: known[s["city"]].append((s["lat"], s["lon"]))
    cent = {c: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v)) for c, v in known.items()}
    names = sorted(set(CITY_CODES.values()) | set(cent), key=len, reverse=True)
    for s in stores_all:
        if s["city"]: continue
        txt = f"{s['name']} {s['addr']}"
        c = next((n for n in names if n.split("-")[0] in txt), None)
        if not c and "lat" in s and cent:
            c, d = min(((c, (s["lat"] - p[0]) ** 2 + (s["lon"] - p[1]) ** 2) for c, p in cent.items()), key=lambda x: x[1])
            if d > 0.15 ** 2: c = None           # רחוק מכל עיר מוכרת (~15 ק"מ)
        if c: s["city"] = c
    by_city = defaultdict(list)
    for s in stores_all: by_city[s["city"] or "לא ידוע"].append(s)
    cities = sorted(by_city.items(), key=lambda kv: (kv[0] == "לא ידוע", kv[0]))
    os.makedirs("prices/c", exist_ok=True)
    for f in os.listdir("prices/c"): os.remove(os.path.join("prices/c", f))
    city_index = []
    for n, (city, sts) in enumerate(cities):
        with open(f"prices/c/{n}.json", "w", encoding="utf-8") as f:
            json.dump({"city": city, "stores": [{k: s[k] for k in ("chain", "name", "addr", "g", "w", "lat", "lon") if k in s} for s in sts]},
                      f, ensure_ascii=False, separators=(",", ":"))
        pts = [(s["lat"], s["lon"]) for s in sts if "lat" in s]
        entry = {"name": city, "file": n, "stores": len(sts)}
        if pts: entry["lat"], entry["lon"] = round(sum(p[0] for p in pts) / len(pts), 4), round(sum(p[1] for p in pts) / len(pts), 4)
        city_index.append(entry)
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open("prices/index.json", "w", encoding="utf-8") as f:
        json.dump({"updated": updated, "chains": chains, "cities": city_index}, f, ensure_ascii=False, separators=(",", ":"))
    summary = [f"מחירים עודכנו: {updated}", f"סניפים: {len(stores_all)}", f"ערים: {len(city_index)}", ""] + report + \
              ["", "ערים עם הכי הרבה סניפים:"] + [f"  {c['name']}: {c['stores']}" for c in sorted(city_index, key=lambda c: -c["stores"])[:15]] + \
              ["", "סמלי ערים לא מוכרים: " + ", ".join(f"{c}×{n}" for c, n in UNKNOWN_CODES.most_common(40))] + \
              [f"\nזמן ריצה: {time.time() - t0:.0f} שניות"]
    open("prices-report.txt", "w", encoding="utf-8").write("\n".join(summary)); print("\n".join(summary))

if __name__ == "__main__":
    main()
