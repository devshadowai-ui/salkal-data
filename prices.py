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
from collect import (get, parse_xml, ci_all, ci_text, file_info, CERB, CERB_CHAINS, UA, _tok, key, tidy, CATS, classify, DEPTS, JUNK_RE)

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
_GIDX = {}
def _reindex():
    _GIDX.clear()
    for kk, t in GENERIC: _GIDX.setdefault(kk.split()[0], []).append((kk, t))   # כבר ממוין מהארוך לקצר
def match_generic(k):
    """השם הפשוט הארוך ביותר שהשם מתחיל בו (k = key(...))"""
    w = k.split()
    if not w: return None
    return next((t for kk, t in _GIDX.get(w[0], ()) if k.startswith(kk)), None)
_reindex()

# ---------- שמות פשוטים חדשים — אוטומטית מתוך כל המוצרים בסופרים ----------
# מוצרים שלא נכנסו תחת אף שם פשוט: מחפשים תחילת-שם (מילה או שתיים) שחוזרת בהרבה מוצרים
# של אותה מחלקה ("רוטב פסטו", "חטיף אנרגיה"), ומוסיפים אותה כשם פשוט. מותגים (מילה שמופיעה
# בכמה מחלקות שונות, כמו "אסם") לא הופכים לשם.
UNIT_WORDS = {"גרם", "גר", "מל", "ליטר", "יח", "יחידות", "קג", "ק", "ג", "מ", "ל", "של", "עם", "ללא", "בטעם", "במבצע", "חדש", "חדשה"}
PACK_WORDS = {"מארז", "שלישיית", "שלישייה", "שלישית", "רביעיית", "רביעייה", "שישיית", "שישייה", "חמישיית", "זוג", "צמד", "מגש", "חבילת", "ענק", "משפחתי", "משפחתית"}
def _brands():
    try: return {key(w).strip() for line in open("brands.txt", encoding="utf-8") if not line.startswith("#") for w in line.split()}
    except Exception: return set()
BRANDS = _brands()
AUTO, COVERAGE = {}, {}
def discover():
    """שם פשוט = סוג המוצר בתחילת השם, בלי מותג/אריזה/כמות: "מילקה שוקולד חלב 100 גרם" -> "שוקולד חלב".
    שתי מילים כשיש מספיק מוצרים כאלה ("גבינת עיזים"), ואחרת מילה אחת ("חטיף")."""
    try: cat = json.load(open("catalog.json", encoding="utf-8"))["products"]
    except Exception: return
    gen = match_generic
    brand = BRANDS
    rows, shown = [], defaultdict(Counter)
    for name, d in cat:
        n = tidy(name)
        toks = []
        for w in n.split():
            kw = key(w).strip()
            if len(kw) >= 2 and not re.search(r"[\da-z]", kw) and kw not in UNIT_WORDS: toks.append((kw, w.strip("-,.()'\"")))
            elif toks and re.search(r"\d", kw): break           # אחרי מספר (כמות/משקל) בא בדרך כלל המותג — עוצרים
        while toks and (toks[0][0] in PACK_WORDS or toks[0][0] in brand): toks.pop(0)
        rows.append((key(n), toks, d))
    c2, c1 = defaultdict(Counter), defaultdict(Counter)
    matched = 0
    for k, toks, d in rows:
        if gen(k): matched += 1; continue
        if d == -1 or not toks: continue
        t1 = toks[0][0]; c1[t1][d] += 1; shown[t1][toks[0][1]] += 1
        if len(toks) > 1 and toks[1][0] not in brand:
            t2 = t1 + " " + toks[1][0]; c2[t2][d] += 1; shown[t2][toks[0][1] + " " + toks[1][1]] += 1
    def ok(c, need):
        d, n = c.most_common(1)[0]; return d if n >= need and n / sum(c.values()) >= 0.6 else None
    for t, c in c2.items():
        d = ok(c, 4)
        if d is not None: AUTO[shown[t].most_common(1)[0][0]] = DEPTS[d]
    for t, c in c1.items():
        d = ok(c, 5)
        if d is not None and len(t) >= 3: AUTO.setdefault(shown[t].most_common(1)[0][0], DEPTS[d])
    COVERAGE["before"] = sum(1 for k, _, d in rows if d != -1 and gen(k)) / max(1, sum(1 for r in rows if r[2] != -1))
    global GENERIC
    known = {kk.strip() for kk, _ in GENERIC}
    for t in list(AUTO):
        if key(t).strip() in known: del AUTO[t]
    GENERIC = sorted(set(GENERIC) | {(key(t), t) for t in AUTO}, key=lambda x: -len(x[0]))
    _reindex()
    # כיסוי: מוצר נחשב מכוסה אם השם שלו, או השם בלי המותג בהתחלה, מתחיל בשם פשוט
    def covered(k, toks): return gen(k) or (toks and gen(" " + " ".join(t for t, _ in toks) + " "))
    food = [(k, t) for k, t, d in rows if d != -1]            # מזון ומוצרי בית (בלי "לא מסווג")
    COVERAGE["after"] = sum(1 for k, t in food if covered(k, t)) / max(1, len(food))
    COVERAGE["total"] = len(food)
def auto_brands():
    """מותג = מילה שבאה לפני סוג מוצר: "טוסו חטיף", "טוסו עוגיות", "הוואי שמפו" -> טוסו, הוואי.
    סוג מוצר = מילה ראשונה של שם במילון המקורי, או של כמה שמות חדשים ("חטיף ...", "גבינת ...")"""
    orig_first = {key(t).split()[0] for d in CATS["departments"].values() for t in d}
    heads = Counter(key(t).split()[0] for t in AUTO if len(key(t).split()) == 2)
    types = orig_first | {w for w, n in heads.items() if n >= 3}
    dict_words = {w for d in CATS["departments"].values() for t in d for w in key(t).split()}
    pairs = defaultdict(lambda: [0, 0])          # מילה -> [לפני סוג מוצר, סה"כ]
    for t in AUTO:
        w = key(t).split()
        if len(w) != 2 or w[0] in dict_words: continue
        pairs[w[0]][1] += 1
        if w[1] in types and len(w[1]) >= 3: pairs[w[0]][0] += 1
    return {w for w, (a, n) in pairs.items() if a >= 2 and a / n >= 0.6 or (a == 1 and n == 1 and w + " " not in heads)}

discover()
_gcache = {}
def generic_of(name):
    """("חלב טרי 3% תנובה 1 ליטר") -> ("חלב 3%"?, "חלב") : מוצר פשוט מהמילון + המילה הראשונה"""
    if name in _gcache: return _gcache[name]
    k = key(tidy(name))
    g = match_generic(k)
    if not g:                                   # "מילקה שוקולד חלב" -> "שוקולד חלב"
        ws = k.split()
        while ws and (ws[0] in BRANDS or ws[0] in PACK_WORDS): ws.pop(0)
        if ws: g = match_generic(" " + " ".join(ws) + " ")
    toks = [t for t in k.split() if not re.search(r"\d", t) and len(t) >= 2]
    w = toks[0] if toks else None
    _gcache[name] = (g, w); return g, w

def median(xs):
    xs = sorted(xs); n = len(xs)
    if not n: return None
    return round(xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2, 2)

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
# מחיר מוצר פשוט = ממוצע כל המוצרים שתחת אותו שם (כל החברות), למשל "קוטג׳ 3%".
# מוצרים שנמכרים במשקל (מחיר לק״ג) לא מתערבבים עם מוצרים ארוזים: אם רוב המוצרים
# תחת השם במשקל (ירקות, בשר) — ממוצע לק״ג; אחרת — ממוצע הארוזים בלבד.
WEIGHT_RE = re.compile(r"במשקל|לק[\"״']?ג|לקילו")
import threading
ALIAS, ALIAS_LOCK, KG_VOTES = Counter(), threading.Lock(), Counter()   # (מילה/שתי מילים ראשונות, מוצר פשוט) -> כמה מוצרים
def build_alias():
    """שם שמשתמש כותב ("במבה", "טונה סטארקיסט") -> המוצר הפשוט שרוב המוצרים שמתחילים כך שייכים אליו"""
    by_tok = defaultdict(Counter)
    for (t, g), n in ALIAS.items(): by_tok[t][g] += n
    gkeys = {key(t).strip() for _, t in GENERIC}
    out = {}
    for t, c in by_tok.items():
        if t in gkeys: continue
        g, n = c.most_common(1)[0]; tot = sum(c.values())
        if n >= 5 and n / tot >= 0.6: out[t] = g
    return out

def is_weighted(it, name):
    if (ci_text(it, "bisweighted") or "").strip().lower() in ("1", "true"): return True
    return bool(WEIGHT_RE.search(name))

# שם כללי ("חלב") כולל גם את הסוגים הרגילים שלו ("חלב 3%", "חלב טרי", "חלב בשקית"),
# אבל לא סוגים שהם בעצם מוצר אחר ("חלב שוקו", "חלב שקדים", "טונה טרייה")
DIFF_WORDS = {"שוקו", "שקדים", "סויה", "שיבולת", "אורז", "קוקוס", "עיזים", "עזים", "כבשים", "לקטוז", "טבעוני", "טבעונית",
              "צמחי", "צמחית", "הצומח", "טרייה", "טריה", "קלויה", "מעושן", "מעושנת", "דיאט", "זירו", "לייט"}
GDEPT = {t: i for i, d in enumerate(DEPTS) for t in CATS["departments"][d]}
for _t, _d in AUTO.items(): GDEPT.setdefault(_t, DEPTS.index(_d))
# שם כללי שלא קיים במילון אבל יש לו כמה סוגים באותה מחלקה ("טונה" ← "טונה בשמן", "טונה במים")
_fw = defaultdict(Counter)
for t, i in list(GDEPT.items()):
    w = t.split()
    if len(w) > 1 and len(w[0]) >= 3: _fw[w[0]][i] += 1
for w0, ds in _fw.items():
    d, n = ds.most_common(1)[0]
    if w0 not in GDEPT and n >= 2 and n / sum(ds.values()) >= 0.5: GDEPT[w0] = d
_terms = sorted(GDEPT, key=len)
ANCESTORS = {}
for t in _terms:
    kt = key(t)
    ANCESTORS[t] = [a for a in _terms if a != t and kt.startswith(key(a)) and GDEPT[a] == GDEPT[t]
                    and not (set(kt[len(key(a)) - 1:].split()) & DIFF_WORDS)]

# ---------- רמה 3: מוצר ספציפי / מותג ("נוטלה", "סוויטנגו", "קפסולות נספרסו") ----------
# כל מילה בשם המוצר שאינה סוג מוצר/כמות (מותג, סדרה, דגם) ומופיעה בלפחות 2 מוצרים.
# המחיר נשמר לכל רשת (prices/tokens.json), לפי הסוג הכללי של המוצרים: "נוטלה" -> רוב המוצרים ממרח.
def token_set():
    try: cat = json.load(open("catalog.json", encoding="utf-8"))["products"]
    except Exception: return set(BRANDS)
    dict_words = {w for d in CATS["departments"].values() for t in d for w in key(t).split()}
    type_first = {kk.split()[0] for kk, _ in GENERIC}
    c = Counter()
    for name, d in cat:
        for w in set(key(tidy(name)).split()):
            if len(w) >= 3 and not re.search(r"\d", w) and w not in dict_words and w not in type_first and w not in UNIT_WORDS and w not in PACK_WORDS: c[w] += 1
    return {w for w, n in c.items() if n >= 2} | set(BRANDS)
TOKENS = token_set()
L1 = {g: (sorted(a, key=len)[0] if a else g) for g, a in ANCESTORS.items()}     # הסוג הכללי ביותר: "חלב 3%" -> "חלב"
TOK_ACC = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))      # רשת -> (מילה, סוג כללי) -> [סכום, מספר סניפים]
TOK_CNT = Counter()                                              # (מילה, סוג כללי) -> מוצרים
TOK_LOCK = threading.Lock()

def store_prices(url, session, chain=None):
    root = parse_xml(get(url, session=session, timeout=240).content)
    kg, unit = defaultdict(list), defaultdict(list)
    bkg, bunit = defaultdict(list), defaultdict(list)
    for it in ci_all(root, "item"):
        name, price = ci_text(it, "itemname"), ci_text(it, "itemprice")
        try: p = float(price)
        except (TypeError, ValueError): continue
        if not name or p <= 0 or p > 2000: continue
        name = html.unescape(name)
        if JUNK_RE.search(name): continue          # טבק/סיגריות/וייפים ומנויי תחבורה
        g, _ = generic_of(name)
        weighted = is_weighted(it, name)
        kn = key(tidy(name)).split()
        bb = bkg if weighted else bunit             # רמה 3: לפי מילה ספציפית + הסוג הכללי ("נוטלה|ממרח")
        for w in [w for w in kn if w in TOKENS][:3]:
            bb[(w, L1.get(g, g) if g else "")].append(p)
        if not g: continue
        d = classify(tidy(name))
        if d != -1 and d != GDEPT.get(g, d): continue          # "חלב גוף" (פארם) לא נכנס ל"חלב"
        bucket = kg if weighted else unit
        for t in [g] + ANCESTORS.get(g, []): bucket[t].append(p)
        toks = [t for t in key(tidy(name)).split() if len(t) >= 2 and not re.search(r"\d", t)]
        if toks:
            with ALIAS_LOCK:
                ALIAS[(toks[0], g)] += 1
                if len(toks) > 1: ALIAS[(toks[0] + " " + toks[1], g)] += 1
    out = {}
    for g in set(kg) | set(unit):
        by_kg = len(kg[g]) > len(unit[g])
        out[g] = trimmed_mean(kg[g] if by_kg else unit[g])
        with ALIAS_LOCK: KG_VOTES[g] += 1 if by_kg else -1
    with TOK_LOCK:
        acc = TOK_ACC[chain]
        for k in set(bkg) | set(bunit):
            v = bkg[k] if len(bkg[k]) > len(bunit[k]) else bunit[k]
            e = acc[k]; e[0] += median(v); e[1] += 1
            TOK_CNT[k] += len(v)
    return out, {}


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
                try: return sid, store_prices(url, sess, chain)
                except Exception as e: print("  failed", chain, sid, str(e)[:100]); return sid, None
            with ThreadPoolExecutor(6) as ex:
                for sid, res in ex.map(work, files.items()):
                    if not res: continue
                    info = stores.get(sid, {"name": f"סניף {sid}", "city": "", "addr": ""})
                    got.append({"chain": chain, "id": sid, "name": info["name"], "city": info["city"], "addr": info["addr"], "g": res[0]})
            # ממוצע ארצי לרשת: ממוצע של הסניפים
            cg = defaultdict(list)
            for s in got:
                for g, p in s["g"].items(): cg[g].append(p)
            chains[chain] = {"stores": len(got), "g": {g: trimmed_mean(v) for g, v in cg.items()}}
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
            json.dump({"city": city, "stores": [{k: s[k] for k in ("chain", "name", "addr", "g", "lat", "lon") if k in s} for s in sts]},
                      f, ensure_ascii=False, separators=(",", ":"))
        pts = [(s["lat"], s["lon"]) for s in sts if "lat" in s]
        entry = {"name": city, "file": n, "stores": len(sts)}
        if pts: entry["lat"], entry["lon"] = round(sum(p[0] for p in pts) / len(pts), 4), round(sum(p[1] for p in pts) / len(pts), 4)
        city_index.append(entry)
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open("prices/index.json", "w", encoding="utf-8") as f:
        alias = build_alias()
        per_kg = sorted(g for g, v in KG_VOTES.items() if v > 0)      # שמות שהמחיר שלהם לק״ג (ירקות, בשר...)
        json.dump({"updated": updated, "chains": chains, "cities": city_index, "alias": alias, "kg": per_kg,
                   "l1": {g: t for g, t in L1.items() if t != g}}, f, ensure_ascii=False, separators=(",", ":"))
    report.append(f"⚖️ מוצרים במחיר לק״ג: {len(per_kg)}")
    # רמה 3: prices/tokens.json  { מילה: {"t": סוג כללי, "c": {רשת: מחיר}, "x": {סוג אחר: {רשת: מחיר}}} }
    by_tok = defaultdict(Counter)
    for (w, t), n in TOK_CNT.items(): by_tok[w][t] += n
    tokens = {}
    for w, types in by_tok.items():
        if sum(types.values()) < 2: continue
        main = next((t for t, _ in types.most_common() if t), "")       # עדיף סוג ידוע על "בלי סוג"
        def prices_for(t): return {ch: round(acc[(w, t)][0] / acc[(w, t)][1], 2) for ch, acc in TOK_ACC.items() if acc.get((w, t)) and acc[(w, t)][1]}
        e = {"t": main, "c": prices_for(main)}
        x = {t: prices_for(t) for t, n in types.items() if t != main and t and n >= 2}
        if x: e["x"] = x
        if e["c"]: tokens[w] = e
    with open("prices/tokens.json", "w", encoding="utf-8") as f:
        json.dump({"updated": updated, "tokens": tokens}, f, ensure_ascii=False, separators=(",", ":"))
    report.append(f"🏷️ מוצרים ספציפיים/מותגים (רמה 3): {len(tokens):,}")
    report.append(f"🔤 שמות חופשיים שמשויכים למוצר פשוט: {len(alias)}")
    if COVERAGE:
        report.append(f"🆕 שמות פשוטים חדשים שנמצאו אוטומטית: {len(AUTO)} (רשימה ב-prices/auto-names.txt)")
        report.append(f"📦 מוצרים שנכנסו תחת שם פשוט: {COVERAGE['before']:.0%} → {COVERAGE['after']:.0%} מתוך {COVERAGE['total']:,}")
        with open("prices/auto-names.txt", "w", encoding="utf-8") as f:
            for d in DEPTS:
                ts = sorted(t for t, dd in AUTO.items() if dd == d)
                if ts: f.write(f"\n{d} ({len(ts)}):\n" + "\n".join("  " + t for t in ts) + "\n")
    summary = [f"מחירים עודכנו: {updated}", f"סניפים: {len(stores_all)}", f"ערים: {len(city_index)}", ""] + report + \
              ["", "ערים עם הכי הרבה סניפים:"] + [f"  {c['name']}: {c['stores']}" for c in sorted(city_index, key=lambda c: -c["stores"])[:15]] + \
              ["", "סמלי ערים לא מוכרים: " + ", ".join(f"{c}×{n}" for c, n in UNKNOWN_CODES.most_common(40))] + \
              [f"\nזמן ריצה: {time.time() - t0:.0f} שניות"]
    open("prices-report.txt", "w", encoding="utf-8").write("\n".join(summary)); print("\n".join(summary))

if __name__ == "__main__":
    main()
