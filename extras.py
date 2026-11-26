"""רשתות שמפרסמות באתרים אחרים (קרפור/מגה/יינות ביתן, ויקטורי, חצי חינם, סיטי מרקט):
מורידים בעזרת הכלי הפתוח il-supermarket-scraper לתיקייה dumps/, וקוראים משם כמו כל רשת אחרת.
"""
import glob, os, subprocess, sys, time, traceback

DUMPS = "dumps"
WANTED = {  # מילת מפתח בשם הסקרייפר/התיקייה -> שם הרשת באפליקציה
    "CARREFOUR": "קרפור", "YAYNO": "קרפור", "VICTORY": "ויקטורי", "HAZI": "חצי חינם", "CITYMARKET": "סיטי מרקט",
}

def chain_of(name):
    n = str(name).upper().replace("_", "").replace("-", "").replace(" ", "")
    for k, v in WANTED.items():
        if k in n: return v
    return None

# רץ בתהליך נפרד (עם הגבלת זמן) כדי שתקלה בכלי לא תתקע את כל הריצה
RUNNER = r'''
import os, sys, inspect, traceback
from il_supermarket_scarper import ScraperFactory, ScarpingTask
keys = ["CARREFOUR", "YAYNO", "VICTORY", "HAZI", "CITYMARKET"]
def wanted(n): n = n.upper().replace("_", ""); return any(k in n for k in keys)
valid = []
for m in ScraperFactory:
    if not wanted(m.name): continue
    try:
        ScraperFactory.get(m.name) if hasattr(ScraperFactory, "get") else m.value
        valid.append(m.name)
    except Exception as e:
        print("skip", m.name, str(e)[:80])
print("VALID", ",".join(valid), flush=True)
types = None
try:
    from il_supermarket_scarper.utils import FileTypesFilters
    allt = [t.name for t in FileTypesFilters]
    print("TYPES", ",".join(allt))
    types = [t for t in allt if "STORE" in t or ("PRICE" in t and "FULL" in t)]
except Exception as e:
    print("no FileTypesFilters", e)
out = os.environ["STORAGE_PATH"]
params = inspect.signature(ScarpingTask.__init__).parameters
print("PARAMS", ",".join(params), flush=True)
kw = {}
for k, v in {"enabled_scrapers": valid, "files_types": types, "dump_folder_name": out,
             "multiprocessing": 4, "suppress_exception": True,
             "output_configuration": {"output_mode": "disk", "base_storage_path": out}}.items():
    if k in params and v is not None: kw[k] = v
print("KW", list(kw), flush=True)
task = ScarpingTask(**kw)
sp = inspect.signature(task.start).parameters
skw = {}
if "limit" in sp: skw["limit"] = None
task.start(**skw)
if hasattr(task, "join"): task.join()
print("DONE", flush=True)
'''

def download(log=print):
    """מוריד קובצי סניפים ומחירים מלאים לתיקייה dumps/. לא מפיל את הריצה אם משהו נכשל."""
    t0 = time.time()
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "il-supermarket-scraper"], check=True, timeout=600)
    except Exception as e:
        log(f"❌ התקנת il-supermarket-scraper נכשלה: {e}"); return
    os.makedirs(DUMPS, exist_ok=True)
    env = dict(os.environ)
    env.update({"OUTPUT_MODE": "disk", "STORAGE_PATH": os.path.abspath(DUMPS),
                "ENABLED_FILE_TYPES": "STORE_FILE,PRICE_FULL_FILE", "NUMBER_OF_PROCESSES": "4"})
    try:
        p = subprocess.run([sys.executable, "-c", RUNNER], env=env, capture_output=True, text=True, timeout=3600)
        lines = (p.stdout + "\n" + p.stderr).strip().splitlines()
        for ln in [l for l in lines if l.split(" ")[0] in ("VALID", "TYPES", "PARAMS", "KW", "DONE", "skip")]:
            log("  " + ln[:300])
        if p.returncode != 0:
            log("  ❌ שגיאה בכלי:"); [log("    " + l[:200]) for l in lines[-12:]]
    except subprocess.TimeoutExpired:
        log("  ⚠️ הכלי רץ יותר משעה ונעצר — משתמשים במה שהספיק לרדת")
    except Exception as e:
        log(f"  ❌ {str(e)[:200]}")
    # מה ירד בפועל
    counts = {}
    for path in glob.glob(os.path.join(DUMPS, "**", "*"), recursive=True):
        if not os.path.isfile(path): continue
        top = os.path.relpath(path, DUMPS).split(os.sep)[0]
        b = os.path.basename(path).lower()
        c = counts.setdefault(top, [0, 0, 0])
        if b.startswith("store"): c[0] += 1
        elif b.startswith("pricefull"): c[1] += 1
        else: c[2] += 1
    for top, (s, pf, other) in sorted(counts.items()):
        log(f"  📁 {top} → {chain_of(top) or '?'}: סניפים {s}, מחירים {pf}, אחר {other}")
    if not counts: log("  ⚠️ לא ירדו קבצים בכלל")
    log(f"הורדת רשתות נוספות: {time.time() - t0:.0f} שנ׳")

def local_sources():
    """[(chain, stores_file_bytes, [price_file_paths])] מתוך dumps/"""
    by_chain = {}
    for path in glob.glob(os.path.join(DUMPS, "**", "*"), recursive=True):
        if not os.path.isfile(path): continue
        parts = os.path.relpath(path, DUMPS).split(os.sep)
        chain = chain_of(parts[0]) or next((chain_of(p) for p in parts if chain_of(p)), None)
        if not chain: continue
        base = os.path.basename(path).lower()
        d = by_chain.setdefault(chain, {"stores": [], "prices": []})
        if base.startswith("store"): d["stores"].append(path)
        elif base.startswith("pricefull"): d["prices"].append(path)
    out = []
    for chain, d in by_chain.items():
        stores = [open(p, "rb").read() for p in sorted(d["stores"])[-3:]]
        out.append((chain, stores, ["file:" + p for p in d["prices"]]))
    return out
