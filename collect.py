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
class _Local:
    def __init__(self, path): self.content = open(path, "rb").read(); self.text = ""

def get(url, session=None, **kw):
    if url.startswith("file:"): return _Local(url[5:])
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
_D = {n.split(" ", 1)[1]: n for n in DEPTS}   # שם מחלקה בלי האימוג'י -> שם מלא
def R(dept, words):
    """רשימת מילים מופרדת בפסיקים. מילה = תחילת מילה בשם ("לחמ" תופס "לחמניות").
    מילה שמסתיימת ברווח = מילה שלמה בלבד ("סט ", "כוס ")."""
    out = []
    for w in words.split(","):
        if not w.strip(): continue
        whole = w.endswith(" ")
        t = " " + key(w).strip() + (" " if whole else "")
        out.append(t)
    return (DEPTS.index(_D[dept]), sorted(out, key=len, reverse=True))

# כללים חזקים: קובעים מחלקה גם אם השם מתחיל במוצר אחר ("מחית עגבניות", "חטיף לבבות עוף לכלב")
STRONG = [
  R("חיות מחמד", "לחתול,לחתולים,לכלב,לכלבים,חתולים,כלבים,לחת ,מזון חתולים,מזון כלבים,פרמיו ,מגרדת,חול מתגבש"),
  R("תינוקות", "מטרנה,סימילאק,סימילק,נוטרילון,האגיס,פמפרס,טיטולים,חיתול,מוצץ,לתינוק,תינוקלין,בייביסיטר,ג'וניור,פטמה לבקבוק,החתלה"),
  R("חומרי ניקוי ותחזוקת הבית", "נ כלים,למדיח,מדיח,כריות קרצוף,קרצוף,מגבוני חיטוי,טואלט דאק,נוזל כלים,נוזל רצפות,נוזל לניקוי,נוזל ניקוי,אקונומיקה,אקנומיקה,מרכך כביסה,כביסה,לכביסה,פיניש,פרסיל,אריאל,כביסכל,אגקס,מטהר,מבשם,מפיץ ריח,מפיץ אדים,דפיוזר,מטליות,מטלית,ספוג ,ספוגי,ספוגית,מגבוני רצפה,הרפיק,פוליוקס,אסטוניש,קוטל,מלכודת,דוחה יתוש,יתושים,נמלים,רייד ,מסיר כתמים,מסיר אבן,זאוול,ז'אוול,זוואל,אקופרנד,אקופירנד,מנקה,צמר פלדה,מטאטא,שקיות אשפה,ריחני לרכב,סנו קליר,סנו מקסימה,לניקוי"),
  R("פארם וטואלטיקה", "פלמוליב,קולגייט,סנסודיין,ליסטרין,שמפו,מרכך שיער,תחליב רחצה,גל רחצה,ג'ל רחצה,סבון,דאו ,דאודורנט,רול און,משחת שיניים,מברשת שיניים,מברשות שיניים,מב' שיניים,מי פה,טמפונים,טמפקס,תחבושת,תחבושות,מגן תחתון,מ. תחתון,קוטקס,גילוח,אפטר שייב,קרם יום,קרם ידיים,קרם גוף,חמאת גוף,חמאת שיאה,מסכה לשיער,מסיכת,לק ,ספיד סטיק,רקסונה,ניוואה,קונדישינר,קיסמי שיניים,מגבונים,תוחם שפתיים,אדפ,בושם,ממחטות,סקין גארד,ערכה לבדיקת,קרם להסרת,מסלסל שיער,שעווה,פינצטה,תחליב לתינוק,מסרק,מברשת מתירת,מברשת גב,מברשת"),
  R("בריאות, טבע וחלבון", "גרעיני חמניות,גרעיני חמניה,גרעיני דלעת,גרעיני אבטיח,פיצוחים,פיצוח,פרוביוטיק,פרופוליס,גלופלקס,ליפואית,חטיף חלבון,אבקת חלבון,משקה חלבון,ויטמין,כמוסות,קפליות,טבליות של,פרוביוטיקה,ספירולינה,מגנזיום,ברזל קומפלקס,גינסנג,פסיליום,אומגה"),
  R("קפואים", "מוקפא,מוקפאת,מוקפאים,גחנון,ג'חנון,מלאווח,מלווח,לביבות,דים סאם,כיסונים,קרפלך,נאגטס,טבעול,קפוא,קפואה,קפואים,גלידה,גלידת,גלידל,גליד,שלגון,שלגוני,ארטיק,טבעפרוסט,גודיז,פלומביר,קרחון,מגנום,מאגדת"),
  R("מזון יבש ומזווה", "פולפה,במיץ עגבניות,קוביות עגבניות,איטריות,אטריות,רביולי,פודינג אינסטנט,לקט ,מחית,רסק,קצוצות,שימורי,במיץ טבעי,סודה לשתייה,סודה לשתיה,תערובת להכנת,תערובת אפיה,תערובת אפייה,שמרי"),
  R("חד פעמי ולבית", "צלחת,מרקיה,כפיות,צלחות,כוסות,מפיות,סכו\"ם,מזלגות,חד פעמי,חד\"פ,סכיני פלסטיק"),
  R("חלב, ביצים ותחליפים", "בריסטה,משקה סויה,משקה שקדים,משקה שיבולת,משקה אורז,משקה קוקוס,גאודה,משקה חלב"),
  R("מעדניה וסלטים", "סלמי,נקניק "),
  R("תבלינים, רטבים וממרחים", "מרמלדה,מיץ לימון,סירופ מייפל,קרם שום"),
  R("תבלינים, רטבים וממרחים", "ריבת,ריבה,קונפיטורת,קונפיטורה,ממרח,רוטב"),
  R("מעדניה וסלטים", "מלפפון במלח,מלפפון בחומץ,בחומץ,חמוצים ,כרוב כבוש"),
  R("חטיפים ומתוקים", "נאצוס,נאצ`וס,נאצ'וס,פסטילה,ליקריץ"),
  R("מאפייה ומאפים", "עוגת ,עוגה ,פנתוני,ברלינר"),
  R("בשר, עוף ודגים", "עוף טרי,שוק וירך,ירכיים,ירך עוף"),
  R("משקאות", "מיץ,נקטר,משקה,יין ,יינות,בירה,ויסקי,וודקה,ליקר ,ליקרים,ברנדי,קוניאק,אוזו,ג'ין ,שמפניה,שמפיין,קאווה,מוגז,תה ,חליטת,קפסולות,קפה,אספרסו,פולי קפה,נביעות,תפוזינה,פריגת,שוופס,סירופ,תירוש,קוקטייל,אפריטיב,מוחיטו,קוואס,פרימור,מוגו ,פרוט&ווג,סאפה,ספרייט,פאנטה,סבן אפ,קולה,מים מינרלים,מים מינרליים,קריסטל,רום ,טקילה,ערק,סיידר"),
]
# כללים רגילים: אחרי שבדקנו אם השם מתחיל במוצר שהאפליקציה מכירה
NORMAL = [
  R("דגני בוקר ושיבולת שועל", "גרנולה,דגני בוקר,שיבולת,שבולת,קורנפלקס,קורן פלקס,מוזלי,סובין"),
  R("חטיפים ומתוקים", "גומי ,סקיטלס,מייק&אייק,טוויזלרס,דובשניות,מנטוס,פרלינים,מוצ'י,לינדט,נשנושי,מאלטיזרס,סושקה,שוקותמר,מנצ'ס,סבלה,ממתק,בונבוני,שוקולד,שוק ,ממתק,ממתקים,סוכריות,סוכ ,לקקן,מרשמלו,וופל,ופל ,טופי,מסטיק,אורביט,טיק טק,קינדר,מילקה,טובלרון,לינדור,פררו,חטיף,ביסלי,במבה,צ'יפס,ציפס,פופקורן,בייגלה,חלווה,דרגיה,עוגיות,עוג ,פסק זמן,קליק,כיפלי,רולדת"),
  R("פארם וטואלטיקה", "א ד פ,אדט,אדפ,עפרון שפתיים,שפתון,פלסטר,פילינג,גל ניקוי לעור,אציטון,לציפורן,ציפורניים,צפרניים,ספרי לגבר,אקס ספריי,סאיוס,גליס,קולור,הגנה מהשמש,להגנה מהשמש,מסכת שיער,ליפה,קצף אמבט,ג'ילט,סנובון,צבע לשיער,סבון,מרכך ,אלסבון,elvive,קרליין"),
  R("חומרי ניקוי ותחזוקת הבית", "כלים ,מנטרל,פלדג,קרפרי,חיטוי,מרסס,גודרס,קלרו,אבר קלין,מגבות נייר,נייר טואלט,מגב ,דלי ,מתלה כביסה,לנור,בדין,סנו ,חלונות,נוזל "),
  R("תבלינים, רטבים וממרחים", "קטשופ,מיונז,חרדל,חומץ,תבלין,תערובת תיבול,מלח ,מלח הימלאיה,פפריקה,פלפל שחור,פלפל לבן,כמון,כורכום,אבקת שום,מוסקט,קימל,דבש,סילאן,טחינה"),
  R("מזון יבש ומזווה", "תערובת ,קניידלך,קנלוני,ריזוטו,טמפורה,פולי סויה,אפונת,אפונה,מקלובה,מגדרה,קמח,סוכר,אבקת אפיה,גלטין,תמצית,צבע מאכל,דייסת,נודלס,ראמן,אטריות,פתיתים,קוסקוס,אורז,פסטה,ספגטי,טונה,סרדינים,שמן ,קוקוס טחון,פירורי לחם,כוסמת,גריסי ,פול ,שעועית,עדשים,מרק,תבשיל,ארוחת"),
  R("מעדניה וסלטים", "קוסטיצה,גרודינקה,בייקון,ביקון,פסטרמי,וינר,דנובה,סיביו,סרוולט,מעושן,מעושנת,קבנוס,זיתים,זית ,חמוצים,חמוצי,בחומץ,הרינג,סלמי,פסטרמה,נקניק ,סלט ,עלי גפן,צלפים,סחוג,אנטיפסטי,חומוס ,מטבוחה,פסטו"),
  R("בשר, עוף ודגים", "צלי ,שייטל,אלמוגית,פיקניה,מולים,דיונון,קלמרי,שרימפס,אונטריב,אונטריפ,אנטריקוט,פריים ריב,שולטר,סטייק,טחון,צלעות,שיפודי,פרגית,אוסובוקו,אסאדו,סינטה,קבב,בשר,קישקע,נקניקיות,זנב ,פילה,שריר,עוף,הודו ,בקר ,כבש ,עגל,המבורגר,שניצל,כרעיים,שוקיים,כנפיים,סלמון,דניס,לברק,בורי,אמנון,מושט,דג "),
  R("חלב, ביצים ותחליפים", "בולגרית,בוראטה,פקורינו,כפיר,צפתית,פרמזנו,טום ,ג נעם,נעם ,הולנדי,טבורוג,לבנה,לאבנה,קשקבל,קאשקבל,פטה ,חלומית,פרמזן,פרמגנו,פיקורינו,צדר,עמק ,ברי ,קממבר,גאודה,מוצרלה,קצפת,ריוויון,יוגטה,דנונה,מולר,מילקי,שמנת,גבינת,גבינה,יוגורט,יוג.,מעדן,חמאה,ביצים,טופו,משקה סויה,משקה שקדים,משקה שיבולת"),
  R("מאפייה ומאפים", "פת פריכה,לחמניות,לחמניה,לחמ.,לחם,באגט,בגט,חלה ,חלות,פרנה,ג'בטה,פוקאצה,פוקצ'ה,קרואסון,קוראסון,סופגני,עוגת,עוגה,עוגות,בריוש,מאפינס,קרקר,פריכיות,פרכיות,צנימים,גריסיני,פיתה,פיתות,קדאיף,מאפה,מאפים,לאפה,טורטיה,בורקס,רוגלך,שטרודל,רולדה"),
  R("בריאות, טבע וחלבון", "צימוק,טבליות,אגוזי,אגוזים,שקדים,שקד ,פקאן,קשיו,פיסטוק,בוטנים,בוטן ,גרעין,גרעינים,צימוקים,פירות יבשים,זרעי,דוחן,קינואה,חמאת שקד,חמאת בוטנים"),
  R("פירות וירקות", "חבוש,שרי ,תות שדה,פלפל צילי,פלפלים,תפוא,תפוחי אדמה,סלק,מנדרינ,ליצי,שזיפ,אגסים,אגס ,פטרייה,אבטיח,מלון ,ענבים ,בננ,תפוחים,לימונים,אבוקדו,דלעת,קישוא,גויבה,בזיל,בזיליקום,ערמונים,ברוקלי,כרובית,פטריות,חסה,כוסברה,פטרוזיליה,שמיר ,נענע,מליסה,בטטה,צנונית,סלרי,ארטישוק,נבטים,עגבני,מלפפונ,תפוחי עץ,פומלה,קלמנטינ,אשכולית"),
  R("פארם וטואלטיקה", "קרם "),
  R("משקאות", "סוביניון,קברנה,מרלו,שרדונה,פינו ,מלבק,רוזה,שיראז,ריזלינג,סירה ,750 מ״ל,700 מ״ל,500 מ״ל,330 מ״ל,פחית,גין ,הנסי,ווקר,סמירנוף,ארטואה,שרי ,פרוסקו,ורמוט"),
  R("חד פעמי ולבית", "אוזניות,משקפי,כובע,גרבי,גרביים,בובת,בובה,מכונית,מסוק,משחק,קומקום,ספל,מאג ,כיסא,כורסא,כורסאת,שולחן,תרמוס,צידנית,כבל,תאורה,שרשרת,חנוכיה,סכך,קישוט,זר ,בלון,כרזת,כרזה,מפה ,סינר,מספריים,מועך,מקציף,קוצץ,תרווד,מרית,פטיש,פלטה,קעריות,לפתניה,סוטאז,סוטז,נרונים,מצית,פחמים,כפכפי,בוקסר,גופיות,שמיכה,כרית,ערכת,קנבס,סרט הדבקה,גליל,עט ,דפי ,ספר ,תיק ,מטריית,זוג ,כוסות,כוס ,צלחות,מפיות,סכו\"ם,מזלגות,כפות,סכין,סכינים,תבנית,תבניות,אלומיניום,אלומניום,ניילון נצמד,נייר אפייה,נייר אפיה,נרות,נר ,מפת שולחן,מפות,קופסת,קופסאת,צנצנת,מיכל,איחסונית,אחסון,קערת,קערה,מסננת,מטרפה,פומפיה,פומפייה,קרש חיתוך,מלקחיים,כף ,מחבת,סיר ,מגש ,מגבת,שמיכת,מאוורר,מזגן,טוסטר,כיריים,נורת,נורות,סוללת,סוללות,מטען,מטריה,כפפות,כפפה,צעצוע,דפדפות,עיתון,מדבקה,מדבקות,קישוטים,עציץ,מנגל,אטבים,שקיות,שקית,בקבוק,כוננית,ארגונית,אביזרים,פמוט,סט ,מלחיה,כד ,פורס,תחתית,בובות,דמות,ראנר,עץ אשוח,מארז שי,אריזת מתנה,סיגריות,מרלבורו,טאץ ,קליפסים,גומיות,פתיל,גופייה,תחתון,חם-צוואר,כורסת,אורגן,פסלון,מסלסלה,סט קידוש,נייר גילגול,טריאה"),
]

def classify(name):
    k = key(name)
    for i, words in STRONG:            # 1) מילים חזקות (מזון לחיות, מוצר תינוק, ניקוי, שתייה, קפוא...)
        if any(w in k for w in words): return i
    for t, i in TERMS:                 # 2) השם מתחיל במוצר שהאפליקציה מכירה ("חלב טרי 3% תנובה")
        if k.startswith(t): return i
    for i, words in NORMAL:            # 3) מילות מפתח לפי מחלקה
        if any(w in k for w in words): return i
    for t, i in TERMS:                 # 4) מוצר מוכר באמצע השם
        if len(t) >= 6 and t in k: return i
    return -1


# ---------- שמות ברורים ----------
def best_name(names):
    """מבין כל הכתיבים של אותו מוצר ברשתות השונות: הכי מלא, הכי מעט קיצורים, הכי נפוץ"""
    def score(n):
        c = names[n]
        return (len(n) - 6 * n.count(".") - 4 * n.count("*") - 3 * len(re.findall(r"[A-Za-z]{2,}", n))
                + 4 * min(c, 5) - (8 if re.search(r"\s\S{1,2}$", n) and not re.search(r"\d", n[-3:]) else 0))
    return max(names, key=score)

ABBR = [  # קיצורים נפוצים בשמות של הרשתות -> מילה מלאה
    (r"(?<!\S)שוק\.", "שוקולד "), (r"(?<!\S)ת\.רחצה", "תחליב רחצה"), (r"(?<!\S)ת\.חלב", "תחליף חלב"),
    (r"(?<!\S)גב\.", "גבינת "), (r"(?<!\S)יוג\.", "יוגורט "), (r"(?<!\S)עוג\.", "עוגיות "), (r"(?<!\S)גליד\.", "גלידה "),
    (r"(?<!\S)לחמ\.", "לחמניות "), (r"(?<!\S)ש\.עוף", "שניצל עוף"), (r"(?<!\S)ס\.גילוח", "סכיני גילוח"),
    (r"(?<!\S)מ\.מוגז", "משקה מוגז"), (r"(?<!\S)מ\.\s?תחתון", "מגן תחתון"), (r"(?<!\S)מב'\s?שיניים", "מברשת שיניים"),
    (r"(?<!\S)סוכ(?=\s)", "סוכריות"), (r"(?<!\S)מש\.\s?שיניים", "משחת שיניים"), (r"(?<!\S)נ\.\s?כלים", "נוזל כלים"), (r"(?<!\S)מ\.\s?בדים", "מרכך בדים"), (r"(?<!\S)ג\.\s?נעם", "גבינת נעם"), (r"(?<!\S)ש\.\s?שועל", "שיבולת שועל"), (r"(?<!\S)ט\.", "טעם "),
    (r"לל\"ג|לל״ג|ללג(?=\s|\d|$)", "ללא גלוטן"), (r"(?<!\S)ללת(?=\s|$)", "ללא תוספת סוכר"),
    (r"תפו\"א|תפו״א", "תפוחי אדמה"), (r"תפו\"ע|תפו״ע", "תפוחי עץ"), (r"(?<!\S)בד\"צ|(?<!\S)בדץ", "בד״ץ"),
    (r"(\d)\s?גר?'?(?=\s|$)", r"\1 גרם"), (r"(\d)\s?ג(?=\s|$)", r"\1 גרם"), (r"(?<!\S)גר'?(?=\s|$)", "גרם"),
    (r"(\d)\s?מ\"?ל(?=\s|$)", r"\1 מ״ל"), (r"(\d)\s?מ'ל(?=\s|$)", r"\1 מ״ל"), (r"(\d)\s?ליט?(?=\s|$)", r"\1 ליטר"),
    (r"(\d)\s?ל(?=\s|$)", r"\1 ליטר"), (r"(\d)\s?ק\"?ג(?=\s|$)", r"\1 ק״ג"), (r"(\d)\s?יח'?(?=\s|$)", r"\1 יחידות"),
    (r"(?<!\S)יח'(?=\s|$)", "יחידות"), (r"\*מבצע\*", ""), (r"\s*[-–]\s*$", ""),
]
ABBR = [(re.compile(a), b) for a, b in ABBR]
def tidy(n):
    n = html.unescape(n)
    n = re.sub(r"ל\.גלוטן", "ללא גלוטן", n)
    n = re.sub(r"(?<=[א-ת])(?=\d)|(?<=\d)(?=[א-ת])", " ", n)        # "מריר60%" -> "מריר 60%"
    for rx, rep in ABBR: n = rx.sub(rep, n)
    n = re.sub(r"\s[א-ת]$", "", n.strip())                            # אות בודדת שנחתכה בסוף ("בננה ו")
    n = re.sub(r"\s+", " ", n).strip(" -*,.`")
    return n[:MAX_NAME]


# ---------- הרצה ----------
def main():
    t0 = time.time(); report, products = [], {}
    open(".gitignore", "w").write("__pycache__/\ndumps/\n")
    sources = [("שופרסל", shufersal)] + [(c, (lambda u=u, p=p: cerberus(u, p))) for c, (u, p) in CERB_CHAINS.items()]
    try:   # קרפור, ויקטורי, חצי חינם, סיטי מרקט — דרך הכלי הפתוח
        import extras
        extras.download(log=lambda m: (print(m, flush=True), report.append(m)))
        for chain, _stores, files in extras.local_sources():
            sources.append((chain, lambda files=files: [(u, None) for u in spread(latest_per_store(files), STORES_PER_CHAIN)]))
    except Exception as e:
        report.append(f"❌ רשתות נוספות: {str(e)[:150]}")
    for chain, fn in sources:
        try:
            files = fn(); n_before = len(products); n_items = 0
            for url, sess in files:
                try:
                    for code, name in items_from(parse_xml(get(url, session=sess, timeout=180).content)):
                        name = re.sub(r"\s+", " ", name).strip()
                        if len(name) < 2: continue
                        # ברקוד אמיתי (8+ ספרות) מאחד את אותו מוצר מכל הרשתות; קודים פנימיים — לפי שם
                        pk = "b" + code.lstrip("0") if code.isdigit() and len(code.lstrip("0")) >= 8 else "n" + key(name)
                        products.setdefault(pk, Counter())[name] += 1; n_items += 1
                except Exception as e:
                    print("  file failed:", chain, str(e)[:120])
            report.append(f"✅ {chain}: {len(files)} סניפים, {len(products) - n_before:,} מוצרים חדשים")
        except Exception as e:
            report.append(f"❌ {chain}: {str(e)[:150]}")
        print(report[-1], flush=True)

    if len(products) < 5000:
        print("פחות מדי מוצרים — לא מעדכן את הקטלוג"); open("report.txt", "w", encoding="utf-8").write("\n".join(report)); sys.exit(1)

    # שם אחד ברור לכל מוצר: הגרסה הכי מלאה מבין כל הרשתות, עם קיצורים פתוחים
    seen, items = set(), []
    for names in products.values():
        n = tidy(best_name(names))
        k = key(n)
        if len(n) >= 2 and k not in seen: seen.add(k); items.append(n)
    items.sort(key=lambda s: (len(s), s))
    rows = [[n, classify(n)] for n in items]
    counts = Counter(i for _, i in rows)
    classified = 1 - counts.get(-1, 0) / len(rows)
    catalog = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "departments": DEPTS, "other": OTHER, "products": rows}
    with open("catalog.json", "w", encoding="utf-8") as f: json.dump(catalog, f, ensure_ascii=False, separators=(",", ":"))

    # מילון מילים -> מחלקה, בשביל האפליקציה: כל מילה שמקלידים ("קנלוני", "פרופוליס", "האגיס")
    # נכנסת לבד למחלקה הנכונה, לפי המחלקה של רוב המוצרים שמתחילים בה
    votes = {}
    for n, i in rows:
        if i < 0: continue
        toks = [t for t in key(n).split() if not re.search(r"\d", t) and len(t) >= 2]
        for w in {" ".join(toks[:1]), " ".join(toks[:2])}:
            if w: votes.setdefault(w, Counter())[i] += 1
    words = []
    for w, c in votes.items():
        (i, top), total = c.most_common(1)[0], sum(c.values())
        if total >= 2 and top / total >= 0.6: words.append([w, i])
    words.sort()
    with open("words.json", "w", encoding="utf-8") as f:
        json.dump({"updated": catalog["updated"], "departments": DEPTS, "words": words}, f, ensure_ascii=False, separators=(",", ":"))
    print("words.json:", len(words), "מילים")
    summary = [f"עודכן: {catalog['updated']}", f"מוצרים: {len(rows):,}", f"סווגו למחלקה: {classified:.0%}", ""] + report + [""] + \
              [f"{(DEPTS[i] if i >= 0 else OTHER)}: {n:,}" for i, n in counts.most_common()] + [f"\nזמן ריצה: {time.time() - t0:.0f} שניות"]
    open("report.txt", "w", encoding="utf-8").write("\n".join(summary)); print("\n".join(summary))
    # דוגמאות לכיוונון הסיווג: מוצרים שלא סווגו, ודגימה מכל מחלקה
    import random; random.seed(1)
    other = [n for n, i in rows if i < 0]
    lines = ["=== לא סווגו (דגימה) ==="] + random.sample(other, min(400, len(other)))
    for d in range(len(DEPTS)):
        names = [n for n, i in rows if i == d]
        lines += ["", f"=== {DEPTS[d]} (דגימה) ==="] + random.sample(names, min(40, len(names)))
    with open("report.txt", "a", encoding="utf-8") as f: f.write("\n\n\n" + "\n".join(lines))

if __name__ == "__main__":
    main()
