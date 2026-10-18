#!/usr/bin/env python3
# discount_monitor.py
# Monitors Saudi (Arabic) discount listings for several stores.
#   Locally (Windows): Windows notification + results page opened in Firefox, runs forever.
#   On GitHub Actions: runs once per scheduled job, sends offers to Telegram, publishes docs/index.html.
#
#   python discount_monitor.py            -> run forever (locally) / once (GitHub Actions)
#   python discount_monitor.py --dump     -> save page 1 of every site to ./dump/ (for debugging)
#   python discount_monitor.py --test FILE -> offline parser test on a saved page

import base64
import datetime
import html as htmllib
import json
import os
import random
import re
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

import undetected_chromedriver as uc
from selenium.common.exceptions import TimeoutException

# ----------------------------- Settings -----------------------------
PAGES = 20                      # pages per site (84 items each on Landmark sites)
PAGE_TIMEOUT_SEC = 90           # the pages are ~7 MB, so allow time before giving up
READY_WAIT_SEC = 40             # extra time to wait for the product data to be complete
CHECK_EVERY_MIN = 30            # minutes between full scans
MIN_DISCOUNT_PCT = 75           # only offers with at least this discount (alerts + results page)
IMG_WIDTH = 400                 # product image width on the results page (small = fast)
IMG_QUALITY = 70
OPEN_IN_BROWSER = True          # open the results page in the browser below
BROWSER = "firefox"             # "firefox" | "default"
FIREFOX_PATHS = [
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
]
HISTORY_HOURS = 24              # offers stay on the results page this long
PAGE_REFRESH_SEC = 60           # the open results page reloads itself

CLOUD = bool(os.environ.get("GITHUB_ACTIONS"))
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
PAGES_URL = os.environ.get("PAGES_URL", "")       # link to the published results page
MAX_TELEGRAM_OFFERS = 40        # max offer messages per scan (best discounts first)
if CLOUD:
    OPEN_IN_BROWSER = False

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "discount_seen.json"
LOG_FILE = HERE / "discount_new_offers.log"
HISTORY_FILE = HERE / "discount_found.json"
HTML_FILE = HERE / ("docs/index.html" if CLOUD else "offers.html")
HTML_FILE.parent.mkdir(exist_ok=True)
DUMP_DIR = HERE / "dump"

_DISC = quote("*:index:discount", safe="")
SITES = [
    # name, base url, listing url template, parser
    {"name": "Centrepoint", "base": "https://www.centrepointstores.com", "kind": "landmark",
     "url": "https://www.centrepointstores.com/sa/ar/search?q=" + _DISC + "&p={page}"},
    {"name": "Splash", "base": "https://www.splashfashions.com", "kind": "landmark",
     "url": "https://www.splashfashions.com/sa/ar/search?q=" + _DISC + "&p={page}"},
    {"name": "Max", "base": "https://www.maxfashion.com", "kind": "landmark",
     "url": "https://www.maxfashion.com/sa/ar/search?q=" + _DISC + "&p={page}"},
    {"name": "HomeCentre", "base": "https://www.homecentre.com", "kind": "landmark",
     "url": "https://www.homecentre.com/sa/ar/search?q=" + _DISC + "&p={page}"},
    {"name": "Homebox", "base": "https://www.homeboxstores.com", "kind": "landmark",
     "url": "https://www.homeboxstores.com/sa/ar/search?q=" + _DISC + "&p={page}"},
]

# Category slugs (substring match) to exclude
EXCLUDE_CATEGORY_PARTS = [
    "watches", "makeup", "fragrance", "lunchessentials", "lunchbag", "lunchbox",
]
# Words to exclude from product titles live in excluded_words.txt (one word or phrase per line).
# The file is created on first run and re-read at the start of every scan, so you can edit it while the script runs.
EXCLUDED_WORDS_FILE = HERE / "excluded_words.txt"
DEFAULT_EXCLUDED_WORDS = [
    "ساعة", "ساعات", "مكياج", "عطر", "عطور", "أحمر شفاه",
    "حقيبة غداء", "شنطة غداء", "صندوق غداء", "علبة غداء",
    "وسادة", "شنطة", "صحن", "مرآة", "ستائر", "طاولة", "كرسي", "مصباح",
    "صندل", "بلوزة", "فستان", "حاوية", "لمبة", "شرشف", "مرتبة", "كنبة",
]
_EXCLUDED_NORM = []


def norm(text: str) -> str:
    """Make Arabic spelling variants match: أ/إ/آ->ا, ة->ه, ى->ي, drop diacritics and tatweel."""
    text = re.sub("[\u064B-\u0652\u0640]", "", text or "")
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ٱ", "ا"), ("ة", "ه"), ("ى", "ي")):
        text = text.replace(a, b)
    return text.lower().strip()


def reload_excluded_words():
    global _EXCLUDED_NORM
    if not EXCLUDED_WORDS_FILE.exists():
        EXCLUDED_WORDS_FILE.write_text(
            "# One word or phrase per line. Lines starting with # are ignored.\n"
            + "\n".join(DEFAULT_EXCLUDED_WORDS) + "\n", encoding="utf-8")
    lines = EXCLUDED_WORDS_FILE.read_text(encoding="utf-8-sig").splitlines()
    _EXCLUDED_NORM = [norm(l) for l in lines if l.strip() and not l.strip().startswith("#")]


reload_excluded_words()


# ----------------------------- Parsing ------------------------------
def is_excluded(title: str, cats: str = "") -> bool:
    cats = cats.lower()
    if any(p in cats for p in EXCLUDE_CATEGORY_PARTS):
        return True
    t = norm(title)
    return any(w in t for w in _EXCLUDED_NORM)


def make_offer(site, pid, title, price, was, url, brand="", img=""):
    try:
        price, was = float(price), float(was)
    except (TypeError, ValueError):
        return None
    if not price or was <= price:
        return None
    return {"id": f"{site['name']}:{pid}", "site": site["name"], "title": title, "brand": brand,
            "price": price, "was": was, "pct": round((was - price) / was * 100), "url": url, "img": img}


def page_has_data(src):
    """True once the __NEXT_DATA__ script is complete (a cut-off page has no closing tag)."""
    return bool(re.search(r'<script id="__NEXT_DATA__"[^>]*>.*?</script>', src or "", re.S))


def parse_landmark(site, page_source):
    try:
        return _parse_landmark(site, page_source)
    except (ValueError, KeyError, TypeError):    # truncated / changed page -> treated as a failed page
        return None


def _parse_landmark(site, page_source):
    """Landmark Group sites (Centrepoint, Splash, Max, Home Centre, Homebox):
    products are embedded as base64 JSON inside __NEXT_DATA__."""
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page_source, re.S)
    if not m:
        return None
    data = json.loads(m.group(1))
    state = json.loads(base64.b64decode(data["props"]["initialState"]))
    hits = state["algoliaProductReducer"]["data"]["hits"]
    out = []
    for h in hits:
        if not h.get("inStock", 1):
            continue
        if is_excluded(h.get("title", ""), " ".join(h.get("allCategories") or [])):
            continue
        u = h.get("url", "")
        url = site["base"] + "/sa/ar" + u if u.startswith("/") else u
        o = make_offer(site, h.get("pid") or h.get("sku"), h.get("title", ""),
                       h.get("price"), h.get("wasPrice"), url, h.get("brand", ""),
                       h.get("thumb_image") or h.get("thumbnailImg") or "")
        if o:
            out.append(o)
    return out, len(hits)


def parse(site, page_source):
    return parse_landmark(site, page_source)


# ----------------------------- Results page -------------------------
def _num(x):
    return f"{x:g}"


def small_img(url, fmt="auto"):
    """Ask the image CDN (Amplience) for a small, compressed version instead of the 2100px original."""
    if not url:
        return ""
    return url.split("?")[0] + f"?w={IMG_WIDTH}&qlt={IMG_QUALITY}&fmt={fmt}"


def dedupe_key(o):
    """Colour/size variants of one product are separate ids but look identical on the page."""
    return (o["site"], o["title"].strip(), o["price"], o["was"])


def unique(offers):
    seen, out = set(), []
    for o in offers:
        k = dedupe_key(o)
        if k not in seen:
            seen.add(k)
            out.append(o)
    return out


def load_history():
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
    except Exception:
        return []


def build_page(items):
    items = sorted(unique(items), key=lambda o: (-o["pct"], o["site"]))
    e = htmllib.escape
    cards = []
    for o in items:
        img = (f'<img loading="lazy" src="{e(small_img(o["img"]))}" data-o="{e(o["img"])}" alt="" '
               f'onerror="this.onerror=null;this.src=this.dataset.o">') if o.get("img") else ""
        cards.append(
            f'<a class="card" href="{e(o["url"])}" target="_blank" rel="noopener">'
            f'<div class="im">{img}<span class="site">{e(o["site"])}</span></div>'
            f'<div class="bd"><div class="pr"><b>{_num(o["price"])}</b>'
            f'<s>{_num(o["was"])}</s><span class="off">%{o["pct"]} \u062e\u0635\u0645</span></div>'
            f'<div class="br">{e(o.get("brand", ""))}</div>'
            f'<div class="tt">{e(o["title"])}</div>'
            f'<div class="tm">{e(o.get("found", "")[11:16])}</div></div></a>')
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{PAGE_REFRESH_SEC}">
<title>\u0627\u0644\u0639\u0631\u0648\u0636 \u0627\u0644\u062c\u062f\u064a\u062f\u0629</title>
<style>
body{{margin:0;font-family:Tahoma,Arial,sans-serif;background:#f4f4f4;color:#222}}
header{{padding:14px 20px;background:#fff;border-bottom:1px solid #ddd;position:sticky;top:0}}
header small{{color:#777;margin-inline-start:10px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px;padding:16px}}
.card{{background:#fff;border-radius:6px;overflow:hidden;text-decoration:none;color:inherit;box-shadow:0 1px 3px #0002}}
.im{{position:relative;aspect-ratio:3/4;background:#eee}}
.im img{{width:100%;height:100%;object-fit:cover;display:block}}
.site{{position:absolute;top:8px;inset-inline-start:8px;background:#000b;color:#fff;font-size:12px;padding:2px 8px;border-radius:10px}}
.bd{{padding:10px 12px}}
.pr{{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}}
.pr b{{font-size:20px;color:#c00}}.pr s{{color:#888;font-size:14px}}
.off{{background:#c00;color:#fff;font-size:12px;padding:1px 6px;border-radius:3px}}
.br{{color:#666;font-size:13px;margin-top:6px}}.tt{{font-size:14px;margin-top:2px;line-height:1.4}}
.tm{{color:#aaa;font-size:11px;margin-top:6px}}
</style></head><body>
<header><b>\u0627\u0644\u0639\u0631\u0648\u0636 \u0627\u0644\u062c\u062f\u064a\u062f\u0629 \u2014 {len(items)}</b>
<small>\u0622\u062e\u0631 \u062a\u062d\u062f\u064a\u062b {now}</small></header>
<div class="grid">{"".join(cards)}</div></body></html>"""


def update_results(new_offers, now_dt):
    """Keep offers found in the last HISTORY_HOURS and rewrite the results page."""
    hist = load_history()
    cutoff = (now_dt - datetime.timedelta(hours=HISTORY_HOURS)).isoformat(timespec="minutes")
    stamp = now_dt.isoformat(timespec="minutes")
    for o in new_offers:
        o["found"] = stamp
    ids = {o["id"] for o in new_offers}
    hist = [h for h in hist if h["id"] not in ids and h.get("found", "") >= cutoff
            and h["pct"] >= MIN_DISCOUNT_PCT and not is_excluded(h["title"])] + new_offers
    hist = unique(sorted(hist, key=lambda o: o.get("found", ""), reverse=True))
    HISTORY_FILE.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
    HTML_FILE.write_text(build_page(hist), encoding="utf-8")
    return hist


# ----------------------------- Notifications / browser --------------
def tg_call(method, **payload):
    """Call the Telegram Bot API. Returns True on success."""
    import requests
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                          json={"chat_id": TG_CHAT, **payload}, timeout=30)
        if r.status_code == 429:                       # flood limit: wait as told and retry once
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                              json={"chat_id": TG_CHAT, **payload}, timeout=30)
        if not r.ok:
            print("  telegram error:", r.status_code, r.text[:200])
        return r.ok
    except Exception as e:
        print("  telegram error:", e)
        return False


def tg_text(text):
    return tg_call("sendMessage", text=text, parse_mode="HTML", disable_web_page_preview=True)


def tg_offer(o):
    e = htmllib.escape
    caption = (f"\U0001F525 <b>%{o['pct']} \u062e\u0635\u0645</b>  [{e(o['site'])}]\n"
               f"<b>{o['price']:g}</b> \u0631.\u0633 (\u0643\u0627\u0646 {o['was']:g})\n"
               f"{e(o.get('brand', ''))} {e(o['title'])}")[:1000]
    link = quote(o["url"], safe=":/?&=%#")             # Telegram wants a percent-encoded URL
    markup = {"inline_keyboard": [[{"text": "\u0641\u062a\u062d \u0627\u0644\u0645\u0646\u062a\u062c", "url": link}]]}
    if o.get("img") and tg_call("sendPhoto", photo=small_img(o["img"], "jpeg"), caption=caption,
                                parse_mode="HTML", reply_markup=markup):
        return True
    return tg_call("sendMessage", text=caption, parse_mode="HTML", reply_markup=markup,
                   disable_web_page_preview=True)


def send_offers_telegram(offers):
    for o in offers[:MAX_TELEGRAM_OFFERS]:
        tg_offer(o)
        time.sleep(1.2)                                # stay under Telegram's per-chat rate limit
    extra = len(offers) - MAX_TELEGRAM_OFFERS
    tail = []
    if extra > 0:
        tail.append(f"\u0648{extra} \u0639\u0631\u0636 \u0622\u062e\u0631")
    if PAGES_URL:
        tail.append(f'<a href="{htmllib.escape(PAGES_URL)}">\u0627\u0644\u0635\u0641\u062d\u0629 \u0627\u0644\u0643\u0627\u0645\u0644\u0629</a>')
    if tail:
        tg_text(" \u2022 ".join(tail))


def notify(title: str, message: str):
    if TG_TOKEN and TG_CHAT:
        tg_text(f"<b>{htmllib.escape(title)}</b>\n{htmllib.escape(message)}")
    if CLOUD:
        print(f"[NOTIFY] {title}: {message}")
        return
    try:
        from winotify import Notification
        Notification(app_id="Discount Monitor", title=title, msg=message[:250]).show()
        return
    except Exception:
        pass
    try:
        from plyer import notification
        notification.notify(title=title, message=message[:250], timeout=10)
        return
    except Exception:
        pass
    print(f"[NOTIFY] {title}: {message}")


def open_urls(urls):
    if not urls:
        return
    if BROWSER == "firefox":
        exe = next((p for p in FIREFOX_PATHS if Path(p).exists()), None)
        if exe:
            subprocess.Popen([exe, "-new-tab", *urls])   # all in one Firefox window
            return
        print("Firefox not found, using the default browser")
    for u in urls:
        webbrowser.open_new_tab(u)


# ----------------------------- State --------------------------------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


# ----------------------------- Scan ---------------------------------
def make_driver():
    opts = uc.ChromeOptions()
    opts.add_argument("--lang=ar-SA")
    if CLOUD:
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
    # Don't load images while scanning (Firefox, used for viewing offers, loads them normally)
    opts.add_argument("--blink-settings=imagesEnabled=false")
    opts.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
    # Return as soon as the HTML is parsed (the product data is inside it); don't wait for trackers/ads
    opts.page_load_strategy = "eager"
    d = uc.Chrome(options=opts)
    d.set_page_load_timeout(PAGE_TIMEOUT_SEC)
    try:    # shorter wait on a stuck chromedriver connection (default is 120s)
        d.command_executor._client_config.timeout = PAGE_TIMEOUT_SEC + 30
    except Exception:
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                d.command_executor.set_timeout(PAGE_TIMEOUT_SEC + 30)
        except Exception:
            pass
    return d


class Browser:
    """Chrome wrapper that survives timeouts: stops slow pages, restarts a hung browser."""

    def __init__(self):
        self.d = None

    def start(self):
        self.stop()
        self.d = make_driver()

    def stop(self):
        if self.d is not None:
            try:
                self.d.quit()
            except Exception:
                pass
            self.d = None

    def source(self, url, ready=None):
        """Load url and return the page HTML. If `ready(html)` is given, wait until it is true
        (up to READY_WAIT_SEC) so a half-loaded page is never parsed."""
        for attempt in range(2):
            try:
                if self.d is None:
                    self.start()
                try:
                    self.d.get(url)
                except TimeoutException:
                    pass                      # keep waiting below for the data to finish arriving
                deadline = time.time() + READY_WAIT_SEC
                while True:
                    src = self.d.page_source
                    if ready is None or ready(src) or time.time() > deadline:
                        break
                    time.sleep(1)
                try:
                    self.d.execute_script("window.stop()")
                except Exception:
                    pass
                time.sleep(random.uniform(1, 2))
                return src
            except Exception as e:
                print(f"    fetch failed ({type(e).__name__}); restarting Chrome")
                try:
                    self.start()
                except Exception as e2:
                    print(f"    could not restart Chrome: {e2}")
                    self.d = None
        return None


def scan_site(browser, site):
    offers = {}
    failures = 0
    for page in range(1, PAGES + 1):
        res = None
        for _try in range(2):             # one retry if the page came back incomplete
            src = browser.source(site["url"].format(page=page), ready=page_has_data)
            res = parse(site, src) if src else None
            if res is not None and res[1] > 0:
                break
        if res is None or res[1] == 0:
            failures += 1
            print(f"  {site['name']} page {page}: no products found (timeout, blocked, last page, or layout differs)")
            if page == 1 or failures >= 3:
                break
            continue
        failures = 0
        for o in res[0]:
            offers[o["id"]] = o
    print(f"  {site['name']}: {len(offers)} offers after filters")
    return offers or None


PAGE_OPENED = False   # the results page is opened once per run; it refreshes itself afterwards


def run_once(browser):
    global PAGE_OPENED
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[{now}] scanning...")
    reload_excluded_words()        # pick up edits to excluded_words.txt
    state = load_state()           # {site_name: {offer_id: last_price}}
    all_new, all_drops, failed = [], [], []
    for site in SITES:
        try:
            offers = scan_site(browser, site)
        except Exception as e:
            print(f"  {site['name']} error: {e}")
            offers = None
        if not offers:
            failed.append(site["name"])
            continue
        seen = state.get(site["name"])
        if seen is None:           # first time for this site = silent baseline
            state[site["name"]] = {k: v["price"] for k, v in offers.items()}
            notify(f"{site['name']} monitoring started", f"Baseline saved: {len(offers)} offers.")
            continue
        all_new += [o for k, o in offers.items() if k not in seen and o["pct"] >= MIN_DISCOUNT_PCT]
        all_drops += [o for k, o in offers.items() if k in seen and o["price"] < seen[k]
                      and o["pct"] >= MIN_DISCOUNT_PCT]
        seen.update({k: v["price"] for k, v in offers.items()})
        save_state(state)
    # Tell the user when a site can't be read (e.g. blocked), but only when the list of failing sites changes
    if sorted(failed) != state.get("_failed", []):
        state["_failed"] = sorted(failed)
        if failed:
            notify("Could not read some sites", ", ".join(failed))
        elif state.get("_failed") is not None:
            notify("All sites readable again", "")
    save_state(state)

    all_new.sort(key=lambda o: -o["pct"])
    known = {dedupe_key(h) for h in load_history()}
    all_new = [o for o in unique(all_new) if dedupe_key(o) not in known]   # drop duplicates/variants
    with LOG_FILE.open("a", encoding="utf-8") as f:
        for o in all_new:
            line = f"{now} NEW [{o['site']}] {o['pct']}% {o['was']:g}->{o['price']:g} SAR {o['title']} {o['url']}"
            print(line)
            f.write(line + "\n")
    if all_new:
        if TG_TOKEN and TG_CHAT:
            send_offers_telegram(all_new)
        else:
            notify(f"{len(all_new)} new offers",
                   "\n".join(f"[{o['site']}] {o['pct']}% - {o['title'][:45]}" for o in all_new[:3]))
    if all_new:
        update_results(all_new, datetime.datetime.now())
        if OPEN_IN_BROWSER and not PAGE_OPENED:
            open_urls([HTML_FILE.as_uri()])
            PAGE_OPENED = True
    if all_drops:
        notify(f"{len(all_drops)} price drops",
               "\n".join(f"[{o['site']}] {o['title'][:45]} -> {o['price']:g}" for o in all_drops[:3]))


def dump_pages():
    DUMP_DIR.mkdir(exist_ok=True)
    browser = Browser()
    try:
        for site in SITES:
            src = browser.source(site["url"].format(page=1), ready=page_has_data)
            if not src:
                print(site["name"], "-> NO DATA (page did not load)")
                continue
            (DUMP_DIR / f"{site['name']}.html").write_text(src, encoding="utf-8")
            res = parse(site, src)
            print(site["name"], "->", "parsed %d offers of %d items" % (len(res[0]), res[1]) if res else "NO DATA")
    finally:
        browser.stop()


def main():
    browser = Browser()
    try:
        while True:
            try:
                run_once(browser)
            except Exception as e:
                print("scan error:", e)
                if CLOUD:
                    sys.exit(1)
            if CLOUD:                  # GitHub Actions starts a new job on schedule
                break
            time.sleep(CHECK_EVERY_MIN * 60)
    finally:
        browser.stop()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--test":
        html = Path(sys.argv[2]).read_text(encoding="utf-8")
        res = parse(SITES[0], html)
        print(f"{res[1]} items, {len(res[0])} kept")
        for o in res[0][:8]:
            print(o["pct"], o["was"], o["price"], o["title"][:50])
    elif len(sys.argv) > 1 and sys.argv[1] == "--dump":
        dump_pages()
    else:
        main()
