"""רשתות שמפרסמות באתרים אחרים (קרפור/מגה/יינות ביתן, ויקטורי, חצי חינם, סיטי מרקט):
מורידים בעזרת הכלי הפתוח il-supermarket-scraper לתיקייה dumps/, וקוראים משם כמו כל רשת אחרת.
"""
import glob, os, subprocess, sys, time, traceback

DUMPS = "dumps"
WANTED = {  # מילת מפתח בשם הסקרייפר -> שם הרשת באפליקציה
    "CARREFOUR": "קרפור", "YAYNO": "קרפור", "VICTORY": "ויקטורי", "HAZI": "חצי חינם", "CITY_MARKET": "סיטי מרקט",
}

def chain_of(scraper_name):
    for k, v in WANTED.items():
        if k in scraper_name.upper(): return v
    return None

def download(log=print):
    """מוריד קובצי סניפים ומחירים מלאים. לא מפיל את הריצה אם משהו נכשל."""
    t0 = time.time()
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "il-supermarket-scraper"], check=True, timeout=600)
    except Exception as e:
        log(f"❌ התקנת il-supermarket-scraper נכשלה: {e}"); return
    try:
        import il_supermarket_scarper as ils
        from il_supermarket_scarper import ScraperFactory
    except Exception as e:
        log(f"❌ ייבוא נכשל: {e}"); return
    names = [m.name for m in ScraperFactory if chain_of(m.name)]
    log("סקרייפרים: " + ", ".join(names))
    try:
        from il_supermarket_scarper.utils import FileTypesFilters
        types = [FileTypesFilters.PRICE_FULL_FILE.name, FileTypesFilters.STORE_FILE.name]
    except Exception:
        types = None
    os.makedirs(DUMPS, exist_ok=True)
    for name in names:
        t1 = time.time()
        try:
            cls = ScraperFactory.get(name) if hasattr(ScraperFactory, "get") else ScraperFactory[name].value
            folder = os.path.join(DUMPS, name)
            try: scraper = cls(folder_name=folder)
            except TypeError: scraper = cls()
            kw = {}
            if types:
                import inspect
                params = inspect.signature(scraper.scrape).parameters
                if "files_types" in params: kw["files_types"] = types
                if "suppress_exception" in params: kw["suppress_exception"] = True
            scraper.scrape(**kw)
            n = len(glob.glob(os.path.join(DUMPS, "**", "*"), recursive=True))
            log(f"  {name}: {time.time() - t1:.0f} שנ׳, קבצים בתיקייה: {n}")
        except Exception as e:
            log(f"  ❌ {name}: {str(e)[:200]}"); traceback.print_exc()
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
