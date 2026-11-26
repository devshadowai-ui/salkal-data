"""סל קל — בניית קטלוג מוצרים מקובצי שקיפות המחירים של רשתות השיווק.

רץ אוטומטית ב-GitHub Actions (ראו .github/workflows/collect.yml) ושומר:
  catalog.json  — כל המוצרים, ממוינים למחלקות של סל קל
  report.txt    — סיכום ההרצה האחרונה (רשתות, מוצרים, אחוז סיווג)
"""
import gzip, html, io, json, re, sys, time, zipfile
from collections import Counter
from datetime import datetime, timezone

import requests, urllib3
from lxml import etree

urllib3.disable_warnings()
UA = "Mozilla/5.0 (salkal catalog bot)"
STORES_PER_CHAIN = 8          # כמה סניפים לדגום מכל רשת לבניית הקטלוג
MAX_NAME = 60

S = requests.Session(); S.headers["User-Agent"] = UA


# ---------- כלים ----------
def get(url, session=None, **kw):
    s = session or S; kw.setdefault("timeout", 90)
    try: return s.get(url, **kw)
    except requests.exceptions.SSLError: return s.get(url, verify=False, **kw)

def unpack(b):
    if b[:2] == b"\x1f\x8b": return gzip.decompress(b)
    if b[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(b)); return z.read(z.namelist()[0])
    return b

def parse_xml(b):
    b = unpack(b); p = etree.XMLParser(recover=True, huge_tree=True)
    try: return etree.fromstring(b, p)
    except Exception: return etree.fromstring(b.decode("utf-16", "ignore").encode(), p)

def ci_all(root, tag):
    t = tag.lower(); return [e for e in root.iter() if isinstance(e.tag, str) and e.tag.lower() == t]

def ci_text(el, tag):
    t = tag.lower()
    for c in el:
        if isinstance(c.tag, str) and c.tag.lower() == t: return (c.text or "").strip()
    return ""

def items_from(root):
    out = []
    for it in ci_all(root, "item"):
        code, name = ci_text(it, "itemcode"), ci_text(it, "itemname")
        if code and name: out.append((code, html.unescape(name)))
    return out

FILE_RE = re.compile(r"PriceFull(\d+)((?:-\d+)+)", re.I)
def file_info(name):
    """PriceFull<chain>-<sub>-<store>-<yyyymmdd>-<hhmmss> (או וריאציות) -> (store, date)"""
    m = FILE_RE.search(name)
    if not m: return None
    parts = m.group(2).strip("-").split("-")
    if len(parts) >= 3 and len(parts[-2]) == 8:       # ...-store-YYYYMMDD-HHMMSS
        return int(parts[-3]), parts[-2] + parts[-1]
    if len(parts) >= 2 and len(parts[-1]) >= 12:       # ...-store-YYYYMMDDHHMM
        return int(parts[-2]), parts[-1]
    return None

def latest_per_store(links):
    best = {}
    cutoff = (datetime.now(timezone.utc).date().toordinal() - 10)
    for u in links:
        inf = file_info(u.split("/")[-1].split("?")[0])
        if not inf: continue
        store, date = inf
        try:
            if datetime.strptime(date[:8], "%Y%m%d").date().toordinal() < cutoff: continue   # קבצים ישנים
        except ValueError: continue
        if store not in best or date > best[store][0]: best[store] = (date, u)
    return [u for _, (d, u) in sorted(best.items())]

def spread(seq, n):
    if len(seq) <= n: return seq
    step = len(seq) / n; return [seq[int(i * step)] for i in range(n)]


# ---------- מקורות ----------
def shufersal():
    links = []
    for page in range(1, 60):
        r = get(f"https://prices.shufersal.co.il/FileObject/UpdateCategory?catID=2&storeId=0&page={page}")
        found = re.findall(r'href="(https://[^"]+?\.(?:gz|xml|zip)[^"]*)"', r.text)
        if not found: break
        links += [html.unescape(u) for u in found]
    return [(u, None) for u in spread(latest_per_store(links), STORES_PER_CHAIN)]

CERB = "https://url.publishedprices.co.il"
CERB_CHAINS = {"רמי לוי": ("RamiLevi", ""), "יוחננוף": ("yohananof", ""), "אושר עד": ("osherad", ""),
               "טיב טעם": ("TivTaam", ""), "קשת טעמים": ("Keshet", ""), "סטופ מרקט": ("Stop_Market", ""),
               "פרשמרקט": ("freshmarket", ""), "פוליצר": ("politzer", "")}

def _tok(t):
    m = re.search(r'name="csrftoken"\s+content="([^"]+)"', t); return m.group(1) if m else ""

def cerberus(user, pw):
    s = requests.Session(); s.headers["User-Agent"] = UA; s.verify = False
    t = _tok(s.get(CERB + "/login", timeout=60).text)
    s.post(CERB + "/login/user", data={"r": "", "username": user, "password": pw, "Submit": "Sign in", "csrftoken": t}, timeout=60)
    t = _tok(s.get(CERB + "/file", timeout=60).text)
    r = s.post(CERB + "/file/json/dir", data={"sEcho": 1, "iColumns": 5, "sColumns": ",,,,", "iDisplayStart": 0,
               "iDisplayLength": 100000, "mDataProp_0": "fname", "mDataProp_1": "type", "mDataProp_2": "size",
               "mDataProp_3": "ftime", "mDataProp_4": "", "sSearch": "", "bRegex": "false", "iSortingCols": 0,
               "cd": "/", "csrftoken": t}, timeout=120)
    names = [f["fname"] for f in r.json().get("aaData", []) if f["fname"].lower().startswith("pricefull")]
    if not names: raise RuntimeError("רשימת קבצים ריקה")
    return [(CERB + "/file/d/" + n, s) for n in spread(latest_per_store(names), STORES_PER_CHAIN)]


# ---------- סיווג למחלקות (לפי המילון של סל קל) ----------
CATS = json.load(open("categories.json", encoding="utf-8"))
OTHER = CATS["other"]
DEPTS = list(CATS["departments"].keys())
def key(s):
    s = re.sub(r"[׳״'\"`’”*.,()\-_/+]", " ", s.lower())
    return " " + re.sub(r"\s+", " ", s).strip() + " "
TERMS = sorted(((key(t), i) for i, d in enumerate(DEPTS) for t in CATS["departments"][d]), key=lambda x: -len(x[0]))
EXTRA = {  # מילים שכיחות בשמות מוצרים ברשתות שלא מופיעות במילון האפליקציה
    "🍎 פירות וירקות": ["עגבני", "מלפפונ", "תפוחי עץ", "תפו\"א", "פטריות", "נבטים", "עלי ", "ירוק", "תמר", "פומלה", "קלמנטינ", "אשכולית"],
    "🧀 חלב, ביצים ותחליפים": ["יוגורט", "מעדן", "גבינת", "גבינה", "שמנת", "חמאה", "ביצים", "קוטג", "לבנה", "משקה חלב", "אשל", "יופלה", "דנונה", "מילקי"],
    "🥩 בשר, עוף ודגים": ["עוף", "בקר", "הודו", "סלמון", "דג ", "פילה", "קבב", "המבורגר", "אנטריקוט", "צלעות"],
    "🍞 מאפייה ומאפים": ["לחם", "לחמני", "פיתות", "חלה", "עוגת", "עוגיות", "מאפה", "קרואסון", "בורקס", "רוגלך", "לאפה", "טורטיה"],
    "🧊 קפואים": ["קפוא", "קפואה", "קפואים", "גלידה", "ארטיק", "שלגון"],
    "🥫 מזון יבש ומזווה": ["אורז", "פסטה", "ספגטי", "קמח", "סוכר", "שימורי", "רסק", "תירס", "חומוס", "פתיתים", "קוסקוס", "מרק", "אבקת", "שמן", "טונה"],
    "🧂 תבלינים, רטבים וממרחים": ["תבלין", "רוטב", "קטשופ", "מיונז", "חרדל", "ממרח", "ריבה", "דבש", "טחינה", "חומץ", "פלפל שחור"],
    "🥣 חטיפים ומתוקים": ["שוקולד", "חטיף", "במבה", "ביסלי", "צ'יפס", "וופל", "סוכריות", "מסטיק", "עוגיות", "קרקר", "פריכיות", "בייגלה"],
    "☕ משקאות": ["משקה", "מיץ", "נקטר", "קולה", "סודה", "מים ", "בירה", "יין ", "וודקה", "ויסקי", "קפה", "תה ", "נס קפה", "שוופס", "ספרינג"],
    "🧹 חומרי ניקוי ותחזוקת הבית": ["נוזל", "אקונומיקה", "מרכך כביסה", "כביסה", "מנקה", "מסיר", "מטהר", "ספוג", "שקיות אשפה", "נייר טואלט", "מגבות נייר"],
    "🧴 פארם וטואלטיקה": ["שמפו", "מרכך", "סבון", "דאודורנט", "משחת שיניים", "קרם", "תחבושות", "גילוח", "מברשת"],
    "👶 תינוקות": ["חיתול", "מטרנה", "סימילאק", "האגיס", "פמפרס", "תינוק", "מגבוני תינוק"],
    "🐾 חיות מחמד": ["לכלב", "לחתול", "כלבים", "חתולים"],
    "🏠 חד פעמי ולבית": ["חד פעמי", "חד\"פ", "כוסות", "צלחות", "מפיות", "נייר כסף", "ניילון נצמד", "אלומיניום"],
}
EXTRA_TERMS = sorted(((key(t).strip(), DEPTS.index(d)) for d, ts in EXTRA.items() if d in DEPTS for t in ts), key=lambda x: -len(x[0]))

def classify(name):
    k = key(name)
    for t, i in TERMS:                 # 1) שם המוצר מתחיל במוצר מוכר ("חלב טרי 3% תנובה")
        if k.startswith(t): return i
    for t, i in TERMS:                 # 2) מוצר מוכר באמצע השם (מילים שלמות, 4+ אותיות)
        if len(t) >= 6 and t in k: return i
    for t, i in EXTRA_TERMS:           # 3) מילות מפתח נפוצות
        if t in k: return i
    return -1


# ---------- הרצה ----------
def main():
    t0 = time.time(); report, products = [], {}
    sources = [("שופרסל", shufersal)] + [(c, (lambda u=u, p=p: cerberus(u, p))) for c, (u, p) in CERB_CHAINS.items()]
    for chain, fn in sources:
        try:
            files = fn(); n_before = len(products); n_items = 0
            for url, sess in files:
                try:
                    for code, name in items_from(parse_xml(get(url, session=sess, timeout=180).content)):
                        name = re.sub(r"\s+", " ", name).strip()[:MAX_NAME]
                        k = key(name)
                        if len(name) < 2 or k in products: continue
                        products[k] = name; n_items += 1
                except Exception as e:
                    print("  file failed:", chain, str(e)[:120])
            report.append(f"✅ {chain}: {len(files)} סניפים, {len(products) - n_before:,} מוצרים חדשים")
        except Exception as e:
            report.append(f"❌ {chain}: {str(e)[:150]}")
        print(report[-1], flush=True)

    if len(products) < 5000:
        print("פחות מדי מוצרים — לא מעדכן את הקטלוג"); open("report.txt", "w", encoding="utf-8").write("\n".join(report)); sys.exit(1)

    items = sorted(products.values(), key=lambda s: (len(s), s))
    rows = [[n, classify(n)] for n in items]
    counts = Counter(i for _, i in rows)
    classified = 1 - counts.get(-1, 0) / len(rows)
    catalog = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "departments": DEPTS, "other": OTHER, "products": rows}
    with open("catalog.json", "w", encoding="utf-8") as f: json.dump(catalog, f, ensure_ascii=False, separators=(",", ":"))
    summary = [f"עודכן: {catalog['updated']}", f"מוצרים: {len(rows):,}", f"סווגו למחלקה: {classified:.0%}", ""] + report + [""] + \
              [f"{(DEPTS[i] if i >= 0 else OTHER)}: {n:,}" for i, n in counts.most_common()] + [f"\nזמן ריצה: {time.time() - t0:.0f} שניות"]
    open("report.txt", "w", encoding="utf-8").write("\n".join(summary)); print("\n".join(summary))

if __name__ == "__main__":
    main()
