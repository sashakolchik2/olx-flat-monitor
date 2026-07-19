#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OLX — монитор аренды квартир (Краков / Варшава) с уведомлениями в Telegram.

Следит за новыми объявлениями (Mieszkania > Wynajem), фильтрует по общей сумме
(аренда + czynsz), вытаскивает из описания все оплаты — czynsz, паркинг, свет/медиа,
интернет, kaucja — и дату заселения. Варианты с заселением от сентября выделяет
отдельно (⭐️). Шлёт карточку в Telegram.

Только стандартная библиотека Python (3.9+). Ничего устанавливать не нужно.

Запуск:            python3 olx_monitor.py
Одна проверка:     python3 olx_monitor.py --once
Самопроверка:      python3 olx_monitor.py --selftest
"""

import json
import os
import re
import sys
import time
import html as html_mod
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "monitor.log"

OLX_API = "https://www.olx.pl/api/v1/offers/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DEFAULT_CONFIG = {
    "telegram_bot_token": "СЮДА_ВСТАВЬ_ТОКЕН_ОТ_BOTFATHER",
    "telegram_chat_id": None,       # первый чат (для совместимости)
    "telegram_chat_ids": [],        # все подписчики — кто нажал /start
    "city": "krakow",               # krakow | warszawa
    "max_total": 4000,              # потолок: аренда + czynsz, zł/мес
    "min_total": 0,                 # нижняя граница (0 = нет)
    "max_age_days": 3,              # только объявления не старше N дней (0 = без ограничения)
    "rooms": ["two"],               # one/two/three/four; [] = любые
    "min_area": 0,                  # минимум м² (0 = не фильтровать)
    "districts": [],                # [] = весь город, иначе список районов
    "only_private": False,          # True = скрывать агентства
    "check_interval_min": 5,        # как часто проверять OLX
}

# OLX id города. region_id указываем только там, где он реально нужен (Краков);
# для Варшавы одного city_id достаточно (проверено на API).
CITIES = {
    "krakow":   {"name": "Краков",  "city_id": 8959,  "region_id": 4},
    "warszawa": {"name": "Варшава", "city_id": 17871, "region_id": None},
}
CITY_ALIASES = {
    "krakow": "krakow", "kraków": "krakow", "краков": "krakow", "cracow": "krakow", "kr": "krakow",
    "warszawa": "warszawa", "warsaw": "warszawa", "варшава": "warszawa", "wawa": "warszawa", "wa": "warszawa",
}
CITY_RU = {"Kraków": "Краков", "Warszawa": "Варшава"}


def city_key(cfg):
    return cfg.get("city") if cfg.get("city") in CITIES else "krakow"


def city_name(cfg):
    return CITIES[city_key(cfg)]["name"]

ROOM_RU = {"one": "кавалерка", "two": "2 комнаты", "three": "3 комнаты", "four": "4+ комнат"}
ROOMS_LABEL_RU = {
    "1 pokój": "1 комната", "2 pokoje": "2 комнаты",
    "3 pokoje": "3 комнаты", "4 i więcej": "4+ комнат",
    "Kawalerka lub garsoniera": "кавалерка",
}
MONTHS_RU = {1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
             7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"}
PL_MONTH_PREFIX = {"sty": 1, "lut": 2, "mar": 3, "kwi": 4, "maj": 5, "cze": 6,
                   "lip": 7, "sie": 8, "wrz": 9, "paz": 10, "lis": 11, "gru": 12}

_DIAC = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


# ---------------------------------------------------------------- utils

def log(msg):
    line = "[{}] {}".format(datetime.now().strftime("%d.%m %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2_000_000:
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.old"))
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_PATH, {}))
    if not CONFIG_PATH.exists():
        save_json(CONFIG_PATH, cfg)
    return cfg


def save_config(cfg):
    save_json(CONFIG_PATH, cfg)


def load_state():
    s = load_json(STATE_PATH, {})
    s.setdefault("seen", {})
    s.setdefault("fp", {})
    s.setdefault("tg_offset", 0)
    s.setdefault("paused", False)
    s.setdefault("recent", [])
    s.setdefault("stats", {"checks": 0, "sent": 0})
    return s


def save_state(state):
    save_json(STATE_PATH, state)


def esc(s):
    return html_mod.escape(str(s or ""), quote=False)


def to_int(x):
    if x is None:
        return None
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return int(x)
    d = re.sub(r"[^\d]", "", str(x))
    return int(d) if d else None


def norm_pl(s):
    """нижний регистр + без польских диакритик"""
    return (s or "").lower().translate(_DIAC)


def strip_html(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_mod.unescape(s)
    s = s.replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def http_json(url, payload=None, timeout=30):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- OLX

def fetch_offers(cfg):
    """Две страницы свежих объявлений (до 100 шт.)."""
    city = CITIES[city_key(cfg)]
    out, ids = [], set()
    for offset in (0, 50):
        params = [
            ("offset", str(offset)),
            ("limit", "50"),
            ("category_id", "15"),                    # Mieszkania > Wynajem
            ("city_id", str(city["city_id"])),
            ("sort_by", "created_at:desc"),
        ]
        if city.get("region_id"):
            params.append(("region_id", str(city["region_id"])))
        if cfg.get("max_total"):
            params.append(("filter_float_price:to", str(int(cfg["max_total"]))))
        for i, r in enumerate(cfg.get("rooms") or []):
            params.append(("filter_enum_rooms[{}]".format(i), r))
        url = OLX_API + "?" + urllib.parse.urlencode(params)
        data = http_json(url).get("data") or []
        for o in data:
            oid = o.get("id")
            if oid and oid not in ids:
                ids.add(oid)
                out.append(o)
        if len(data) < 50:
            break
        time.sleep(1)
    return out


def parse_offer(o):
    params = {}
    for p in o.get("params") or []:
        params[p.get("key")] = p.get("value") or {}

    pv = params.get("price", {})
    price = to_int(pv.get("value"))
    if price is None:
        price = to_int(pv.get("label"))

    rv = params.get("rent", {})
    rent = to_int(rv.get("key"))
    if rent is None:
        rent = to_int(rv.get("label"))
    if rent is not None and not (0 <= rent <= 15000):
        rent = None

    av = params.get("m", {})
    area = None
    for cand in (av.get("key"), av.get("label")):
        if cand is None:
            continue
        m = re.search(r"\d+(?:[.,]\d+)?", str(cand))
        if m:
            area = float(m.group(0).replace(",", "."))
            break

    loc = o.get("location") or {}
    created = o.get("created_time") or o.get("last_refresh_time") or ""
    return {
        "id": o.get("id"),
        "url": o.get("url") or "",
        "title": strip_html(o.get("title") or ""),
        "desc": strip_html(o.get("description") or ""),
        "price": price,
        "rent": rent,
        "area": area,
        "rooms_label": (params.get("rooms", {}) or {}).get("label") or "",
        "floor": (params.get("floor_select", {}) or params.get("floor", {}) or {}).get("label") or "",
        "pets": (params.get("pets", {}) or {}).get("label") or "",
        "furniture": (params.get("furniture", {}) or {}).get("label") or "",
        "business": o.get("business"),
        "district": ((loc.get("district") or {}).get("name")) or "",
        "city": ((loc.get("city") or {}).get("name")) or "",
        "created_iso": created,
    }


# ------------------------------------------------------- разбор описания

def _num(s):
    return to_int(s)


def _amount_after(text, head_re, lo, hi, window=40):
    """Первое число из диапазона [lo;hi] в пределах `window` символов ПОСЛЕ ключа.
    Терпит 'ok.', 'około', ':', '-', 'zł'. Возвращает (val, position) или (None, None)."""
    for m in re.finditer(head_re, text):
        seg = text[m.end(): m.end() + window]
        nm = re.search(r"(?:ok\.?|okolo|~|:|-|\s)*?(\d[\d  ]{1,6})", seg)
        if not nm:
            continue
        val = _num(nm.group(1))
        if val is not None and lo <= val <= hi:
            return val, m.start()
    return None, None


def _amount_before(text, tail_re, lo, hi, window=22):
    """Число из диапазона перед ключом (шаблон 'NNN zł (opłaty administracyjne)')."""
    for m in re.finditer(tail_re, text):
        seg = text[max(0, m.start() - window): m.start()]
        nums = re.findall(r"(\d[\d  ]{1,6})", seg)
        if nums:
            val = _num(nums[-1])
            if val is not None and lo <= val <= hi:
                return val
    return None


def _find_czynsz(t, price):
    """Административный czynsz из текста (когда поле OLX пустое)."""
    if re.search(r"czynsz\w*[^.]{0,30}?(w cenie|wliczon|zawiera|w tym czynsz|razem z czynsz)", t) or \
       re.search(r"(w cenie|wliczon\w*)[^.]{0,25}?czynsz", t):
        return 0
    # 1) после слова "administracyjn"/"eksploatacyjn"
    for head in (r"czynsz\w* administracyjn\w*", r"oplat\w* administracyjn\w*",
                 r"administracyjn\w*", r"eksploatacyjn\w*", r"czynsz\w* do (?:sm|wspolnoty|spoldzielni)"):
        val, _ = _amount_after(t, head, 120, 1800, window=30)
        if val is not None and val != price:
            return val
    # 2) число ПЕРЕД словом administracyjne: "+ 600 zł (opłaty administracyjne)"
    val = _amount_before(t, r"administracyjn\w*", 120, 1800)
    if val is not None and val != price:
        return val
    # 3) "odstępne 686" — но только если это похоже на доп.плату, а не на саму аренду
    val, _ = _amount_after(t, r"odstepne", 120, 1600, window=18)
    if val is not None and val != price and (price is None or val < price):
        return val
    # 4) простой "czynsz: NNN" (но НЕ "czynsz najmu/najemcy" — это сама аренда)
    for m in re.finditer(r"czynsz\w*", t):
        after = t[m.end(): m.end() + 20]
        if re.match(r"\s*(najm|najem|za najem|najemc)", after):
            continue
        nm = re.search(r"^(?:ok\.?|okolo|~|:|-|\s)*?(\d[\d  ]{1,5})", after)
        if nm:
            v = _num(nm.group(1))
            if v is not None and 120 <= v <= 1800 and v != price:
                return v
    return None


def _find_parking(t):
    pk = re.search(r"(miejsc\w* postojow\w*|miejsc\w* parkingow\w*|parking\w*|"
                   r"garaz\w*|hal\w* garazow\w*|\bmpp\b|\bmp\b(?= ?[-:0-9]))", t)
    if not pk:
        return None
    # явное отсутствие
    if re.search(r"(brak|bez)\s+(miejsc|parking|garaz)", t[max(0, pk.start() - 20): pk.start() + 30]):
        return "нет"
    seg = t[pk.start(): pk.start() + 90]
    free = re.search(r"w cenie|wliczon|gratis|bezplatn|za darmo|w ramach czynsz", seg)
    price_m = re.search(r"(?:za dodatkow\w*\s*)?(\d[\d  ]{1,5})\s*(?:zl|pln)", seg)
    if free and (not price_m or free.start() <= price_m.start()):
        return "в цене"
    if price_m:
        v = _num(price_m.group(1))
        if v is not None and 30 <= v <= 2500:
            return "{} zł/мес".format(v)
    return "есть (цена не указана)"


def _find_media(t):
    """Возвращает (строка_для_карточки, фикс_сумма|None)."""
    if not re.search(r"(media|prad|energi|licznik|zuzyci|wskazan|gaz\b|oplaty za)", t):
        return None, None
    included = re.search(r"(media|oplaty|prad|energi\w+)[^.]{0,25}(w cenie|wliczon|w czynsz)", t)
    metered = re.search(r"(wg|wedlug|na podstawie|wedle)\s*\.?\s*(zuzyci\w*|wskazan\w*|licznik\w*)"
                        r"|licznikow|wg licznik|media (?:platne )?dodatkowo|plus media|\+\s*media|"
                        r"media wg|prad (?:i gaz )?(?:wg|wedlug)|rozlicz\w+ wg", t)
    # фиксированная сумма за электричество/медиа
    fixed, _ = _amount_after(t, r"energi\w* elektryczn\w*", 30, 900, window=30)
    if fixed is None:
        fixed, _ = _amount_after(t, r"\bprad\b", 30, 900, window=22)
    if fixed is None:
        mm = re.search(r"\bmedia\b", t)
        if mm:
            seg = mm.string[mm.end(): mm.end() + 80]
            pm = re.search(r"(\d{2,4})\s*(?:zl|pln)", seg)
            if pm:
                v = _num(pm.group(1))
                if v is not None and 30 <= v <= 900:
                    fixed = v
    if included:
        return "включены в цену", None
    if fixed is not None and metered:
        return "≈ {} zł + свет по счётчику".format(fixed), fixed
    if fixed is not None:
        return "≈ {} zł/мес".format(fixed), fixed
    if metered:
        return "по счётчикам (сверх цены)", None
    return "упоминаются — смотри описание", None


def _find_kaucja(t, price):
    val, _ = _amount_after(t, r"kaucj\w*", 300, 40000, window=30)
    if val is None:
        # число перед словом kaucja: "5000 ... kaucja zwrotna"
        val = _amount_before(t, r"kaucj\w*", 300, 40000, window=30)
    return val


def _find_internet(t):
    if not re.search(r"internet|swiatlowod|wi-?fi", t):
        return None
    if re.search(r"(internet|swiatlowod|wi-?fi)[^.]{0,20}(w cenie|wliczon|gratis|w czynsz)", t):
        return "в цене"
    val, _ = _amount_after(t, r"internet", 20, 300, window=25)
    if val is not None:
        return "{} zł/мес".format(val)
    return None


def _find_availability(t):
    """Возвращает (строка, месяц|None). Месяц нужен для «заселение от сентября»."""
    if re.search(r"od zaraz|od reki|od reke|odreke|dostepn\w* natychmiast|"
                 r"natychmiast|do wprowadzenia\s+(?:od\s+)?zaraz|wolne od zaraz", t):
        return "сразу (od zaraz)", 0
    # дата вида "od 01.09" / "od 1.9.2026"
    for m in re.finditer(r"od\s+(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?", t):
        d, mo = int(m.group(1)), int(m.group(2))
        if 1 <= d <= 31 and 1 <= mo <= 12:
            s = "с {:02d}.{:02d}".format(d, mo)
            if m.group(3) and len(m.group(3)) == 4:
                s += "." + m.group(3)
            return s, mo
    # словом: "od (1) września", "dostępne od października"
    mw = re.search(r"(?:od|dostepn\w+ od|woln\w+ od|wprowadz\w+ od|zamieszk\w+ od)\s+"
                   r"(?:(\d{1,2})\s+)?"
                   r"(stycz\w*|luteg\w*|lut\w*|marc\w*|kwiet\w*|maj\w*|czerw\w*|lipc\w*|"
                   r"sierp\w*|wrzes\w*|wrzesn\w*|pazdzier\w*|listopad\w*|grudni\w*)", t)
    PL_FULL = {"stycz": 1, "luteg": 2, "lut": 2, "marc": 3, "kwiet": 4, "maj": 5, "czerw": 6,
               "lipc": 7, "sierp": 8, "wrzes": 9, "wrzesn": 9, "pazdzier": 10, "listopad": 11, "grudni": 12}
    if mw:
        word = mw.group(2)
        mo = None
        for pref, num in PL_FULL.items():
            if word.startswith(pref):
                mo = num
                break
        if mo:
            day = mw.group(1)
            s = "с {} {}".format(day, MONTHS_RU[mo]) if day else "с " + MONTHS_RU[mo]
            return s, mo
    # просто «wrzesień» рядом со словом о заселении
    if re.search(r"(dostepn\w+|wprowadz\w+|zamieszk\w+|wynaj\w+|woln\w+|najem)\D{0,20}wrzes", t):
        return "с сентября", 9
    return None, None


def analyze_desc(desc, price=None):
    t = re.sub(r"\s+", " ", norm_pl(desc))
    out = {}

    cz = _find_czynsz(t, price)
    if cz is not None:
        out["czynsz_desc"] = cz

    pk = _find_parking(t)
    if pk:
        out["parking"] = pk

    media, media_amount = _find_media(t)
    if media:
        out["media"] = media
    if media_amount:
        out["media_amount"] = media_amount

    intr = _find_internet(t)
    if intr:
        out["internet"] = intr

    ka = _find_kaucja(t, price)
    if ka is not None:
        out["kaucja"] = ka

    avail, month = _find_availability(t)
    if avail:
        out["available"] = avail
    if month is not None:
        out["move_in_month"] = month
    # «заселение от сентября» — сентябрь и позже (сентябрь…декабрь)
    if month is not None and month >= 9:
        out["september"] = True
        out["perfect"] = True

    return out


# ---------------------------------------------------------------- фильтры

def effective_costs(of, an):
    czynsz = of["rent"] if of["rent"] is not None else an.get("czynsz_desc")
    total = (of["price"] or 0) + (czynsz or 0)
    return czynsz, total


def passes_filters(of, an, cfg):
    if not of["price"]:
        return False
    czynsz, total = effective_costs(of, an)
    if cfg.get("max_total") and total > cfg["max_total"]:
        return False
    if cfg.get("min_total") and total < cfg["min_total"]:
        return False
    if cfg.get("min_area") and of["area"] and of["area"] < cfg["min_area"]:
        return False
    districts = cfg.get("districts") or []
    if districts:
        d = norm_pl(of["district"])
        if not d:
            return False
        if not any(norm_pl(x) in d or d in norm_pl(x) for x in districts):
            return False
    if cfg.get("only_private") and of.get("business") is True:
        return False
    return True


def fingerprint(of):
    t = re.sub(r"\W+", "", norm_pl(of["title"]))[:60]
    return "{}|{}|{}".format(t, of["price"], of["area"])


def too_old(created_iso, max_age_days):
    """True, если объявление добавлено раньше, чем N дней назад. Нет даты → не отбрасываем."""
    if not max_age_days:
        return False
    try:
        ts = datetime.fromisoformat(created_iso).timestamp()
    except (ValueError, TypeError, OverflowError, AttributeError):
        return False
    return (time.time() - ts) > max_age_days * 86400 + 3600  # +час запаса на часовые пояса


def added_label(created_iso):
    try:
        dt = datetime.fromisoformat(created_iso)
    except (ValueError, TypeError):
        return ""
    hm = dt.strftime("%H:%M")
    try:
        days = (datetime.now(dt.tzinfo).date() - dt.date()).days
    except (ValueError, TypeError, OverflowError):
        days = None
    if days == 0:
        return "сегодня в " + hm
    if days == 1:
        return "вчера в " + hm
    return dt.strftime("%d.%m %H:%M")


# ---------------------------------------------------------------- карточка

def _floor_ru(f):
    fl = norm_pl(f)
    if fl in ("parter", "0"):
        return "1-й (parter)"
    if "poddasz" in fl:
        return "мансарда"
    return f


def format_offer(of, an):
    czynsz, total = effective_costs(of, an)
    czynsz_from_desc = of["rent"] is None and "czynsz_desc" in an

    city = CITY_RU.get(of["city"], of["city"] or "")
    loc = of["district"] or city or ""
    head_bits = []
    if of["area"]:
        head_bits.append("{:g} м²".format(of["area"]))
    rl = of["rooms_label"]
    if rl:
        head_bits.append(ROOMS_LABEL_RU.get(rl, rl))
    head = "🏠 <b>{}{}</b>".format(city + ", " if city and of["district"] else "", esc(loc))
    if head_bits:
        head += " — " + ", ".join(head_bits)

    lines = [head]

    if czynsz:
        pl = "💰 {} + czynsz {} = <b>{} zł/мес</b>".format(of["price"], czynsz, total)
        if czynsz_from_desc:
            pl += " (czynsz из описания)"
    elif czynsz == 0:
        pl = "💰 <b>{} zł/мес</b> — czynsz включён".format(of["price"])
    else:
        pl = "💰 {} zł + czynsz? (не указан — уточни у владельца)".format(of["price"])
    lines.append(pl)

    # ориентир по полной сумме, если известна фикс. часть медиа
    if an.get("media_amount") and of["price"]:
        with_media = total + an["media_amount"]
        lines.append("   ≈ с медиа около {} zł/мес".format(with_media))

    if "media" in an:
        lines.append("⚡ Свет/медиа: {}".format(an["media"]))
    if "parking" in an:
        lines.append("🚗 Паркинг: {}".format(an["parking"]))
    if "internet" in an:
        lines.append("🌐 Интернет: {}".format(an["internet"]))
    if "kaucja" in an:
        lines.append("🔐 Kaucja (залог): {} zł".format(an["kaucja"]))
    if "available" in an:
        line = "📅 Заселение: {}".format(an["available"])
        if an.get("september"):
            line += " ⭐️"
        lines.append(line)
    if of["floor"]:
        lines.append("🏢 Этаж: {}".format(esc(_floor_ru(of["floor"]))))
    extras = []
    if of["pets"] == "Tak":
        extras.append("🐾 можно с животными")
    if of.get("business") is False:
        extras.append("👤 от собственника")
    elif of.get("business") is True:
        extras.append("🏢 агентство")
    if extras:
        lines.append(" · ".join(extras))

    if of["title"]:
        lines.append("«{}»".format(esc(of["title"])))
    dt = added_label(of["created_iso"])
    tail = '🔗 <a href="{}">Открыть на OLX</a>'.format(of["url"])
    if dt:
        tail += "  ·  добавлено {}".format(dt)
    lines.append(tail)
    return "\n".join(lines)


# ---------------------------------------------------------------- Telegram

def get_token(cfg):
    """Токен: сначала переменная окружения (для GitHub Actions), потом config.json."""
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip() or (cfg.get("telegram_bot_token") or "")


def tg(cfg, method, payload=None, timeout=40):
    token = get_token(cfg)
    url = "https://api.telegram.org/bot{}/{}".format(token, method)
    try:
        return http_json(url, payload, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:200]
        except OSError:
            pass
        if e.code == 409:
            log("⚠️ Telegram 409: похоже, запущена вторая копия скрипта — закрой лишнюю.")
        elif e.code == 429:
            log("⚠️ Telegram: слишком часто, жду 5 сек")
            time.sleep(5)
        else:
            log("⚠️ Telegram {} {}: {}".format(e.code, method, body))
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log("⚠️ Сеть (Telegram {}): {}".format(method, e))
        return None


def subscribers(cfg):
    """Все чаты-получатели (с миграцией со старого одиночного поля), без дублей."""
    ids = list(cfg.get("telegram_chat_ids") or [])
    single = cfg.get("telegram_chat_id")
    if single and single not in ids:
        ids.append(single)
    return ids


def add_subscriber(cfg, chat):
    ids = list(cfg.get("telegram_chat_ids") or [])
    if chat not in ids:
        ids.append(chat)
    cfg["telegram_chat_ids"] = ids
    if not cfg.get("telegram_chat_id"):     # первый — заполним и singular
        cfg["telegram_chat_id"] = chat
    save_config(cfg)


def send_one(cfg, chat_id, text, silent=False):
    if not chat_id:
        return False
    payload = {"chat_id": chat_id, "text": text,
               "parse_mode": "HTML", "disable_notification": silent}
    r = tg(cfg, "sendMessage", payload)
    if r is None:
        time.sleep(3)
        r = tg(cfg, "sendMessage", payload)
    return bool(r and r.get("ok"))


def send(cfg, text, silent=False):
    """Разослать всем подписчикам."""
    ok = False
    for cid in subscribers(cfg):
        if send_one(cfg, cid, text, silent):
            ok = True
    return ok


def rooms_ru(cfg):
    rooms = cfg.get("rooms") or []
    if not rooms:
        return "любые комнаты"
    return ", ".join(ROOM_RU.get(r, r) for r in rooms)


def recent_filtered(state, cfg, only_perfect=False):
    """История подходящих под ТЕКУЩИЕ фильтры и текущий город, свежие сверху."""
    ck = city_key(cfg)
    out = []
    for item in reversed(state.get("recent") or []):
        if "of" not in item:                       # старый формат — пропускаем
            continue
        if item.get("city", "krakow") != ck:
            continue
        if only_perfect and not item.get("perfect"):
            continue
        if too_old(item["of"].get("created_iso"), cfg.get("max_age_days")):
            continue
        if not passes_filters(item["of"], item.get("an") or {}, cfg):
            continue
        out.append(item)
    return out


def status_text(cfg, state):
    districts = cfg.get("districts") or []
    last = state.get("last_check")
    if last:
        try:
            last = datetime.fromisoformat(last).strftime("%d.%m %H:%M")
        except ValueError:
            pass
    return (
        "📊 <b>Статус монитора</b>\n"
        "Город: <b>{}</b>\n"
        "Бюджет: {}до {} zł/мес (аренда + czynsz)\n"
        "Комнаты: {}\n"
        "Районы: {}\n"
        "Мин. площадь: {}\n"
        "Свежесть: {}\n"
        "Получателей: {}\n"
        "Проверка каждые {} мин · пауза: {}\n"
        "Проверок: {} · прислано квартир: {}\n"
        "Последняя проверка: {}"
    ).format(
        city_name(cfg),
        "от {} ".format(cfg["min_total"]) if cfg.get("min_total") else "",
        cfg.get("max_total"),
        rooms_ru(cfg),
        esc(", ".join(districts)) if districts else "весь город",
        "{} м²".format(cfg["min_area"]) if cfg.get("min_area") else "без ограничения",
        "не старше {} дн.".format(cfg["max_age_days"]) if cfg.get("max_age_days") else "любой возраст",
        len(subscribers(cfg)),
        cfg.get("check_interval_min", 5),
        "да ⏸" if state.get("paused") else "нет",
        state["stats"].get("checks", 0),
        state["stats"].get("sent", 0),
        last or "ещё не было",
    )


HELP_TEXT = (
    "🤖 <b>Команды</b>\n"
    "/city warszawa — сменить город (krakow / warszawa)\n"
    "/max 4000 — потолок: аренда + czynsz вместе\n"
    "/min 2500 — нижняя граница суммы\n"
    "/rooms 2 — комнаты: 1, 2, 3, 4, можно «1,2», any = любые\n"
    "/area 35 — минимум площади, м²\n"
    "/age 3 — только объявления не старше N дней (/age off — без ограничения)\n"
    "/districts Zabłocie, Podgórze — только эти районы (/districts all — весь город)\n"
    "/september — только варианты с заселением ⭐️ от сентября\n"
    "/last 5 — последние подходящие под текущие фильтры\n"
    "/check — проверить OLX прямо сейчас\n"
    "/status — фильтры и статистика\n"
    "/pause и /resume — приостановить / продолжить\n"
    "/stop — отписаться от уведомлений\n"
    "\nВсё меняется на лету, перезапускать не надо.\n"
    "Второй человек? Пусть просто нажмёт /start у этого бота."
)


def handle_command(cfg, state, text, chat=None):
    """Возвращает True, если надо сделать внеплановую проверку OLX."""
    parts = text.split()
    cmd = parts[0].lower().split("@")[0] if parts else ""
    arg = " ".join(parts[1:]).strip()

    if cmd in ("/stop", "/unsubscribe"):
        ids = [c for c in (cfg.get("telegram_chat_ids") or []) if c != chat]
        cfg["telegram_chat_ids"] = ids
        if cfg.get("telegram_chat_id") == chat:
            cfg["telegram_chat_id"] = ids[0] if ids else None
        save_config(cfg)
        send_one(cfg, chat, "🔕 Отписал. Захочешь вернуться — снова /start.")
        return False

    if cmd in ("/start", "/help"):
        send(cfg, HELP_TEXT)
        send(cfg, status_text(cfg, state))
        return False

    if cmd == "/status":
        send(cfg, status_text(cfg, state))
        return False

    if cmd in ("/max", "/min", "/area", "/age"):
        if cmd == "/age" and norm_pl(arg) in ("off", "выкл", "выключить", "any", "все", "любые"):
            v = 0
        else:
            v = to_int(arg)
        limits = {"/max": (500, 50000), "/min": (0, 50000), "/area": (0, 300), "/age": (0, 90)}
        lo, hi = limits[cmd]
        if v is None or not (lo <= v <= hi):
            hint = {"/max": "4000", "/min": "2500", "/area": "35", "/age": "3  (или /age off)"}[cmd]
            send(cfg, "Напиши число, например: {} {}".format(cmd, hint))
            return False
        key = {"/max": "max_total", "/min": "min_total", "/area": "min_area", "/age": "max_age_days"}[cmd]
        cfg[key] = v
        save_config(cfg)
        if key == "max_age_days":
            send(cfg, "✅ Только объявления не старше {} дн.".format(v) if v
                 else "✅ Фильтр по дате выключен — показываю любые по возрасту.")
            return True   # пересобрать выдачу с новым окном свежести
        names = {"max_total": "Потолок (аренда+czynsz)", "min_total": "Нижняя граница", "min_area": "Мин. площадь"}
        unit = " м²" if key == "min_area" else " zł"
        send(cfg, "✅ {}: {}{}".format(names[key], v, unit))
        return False

    if cmd == "/rooms":
        a = norm_pl(arg)
        if a in ("any", "all", "vse", "все", "любые", "0"):
            cfg["rooms"] = []
        else:
            nums = re.findall(r"[1-4]", a)
            mapping = {"1": "one", "2": "two", "3": "three", "4": "four"}
            if not nums:
                send(cfg, "Например: /rooms 2  или  /rooms 1,2  или  /rooms any")
                return False
            cfg["rooms"] = [mapping[n] for n in dict.fromkeys(nums)]
        save_config(cfg)
        send(cfg, "✅ Комнаты: {}".format(rooms_ru(cfg)))
        return False

    if cmd == "/districts":
        a = arg.strip()
        if norm_pl(a) in ("all", "vse", "все", "весь", "любые", ""):
            cfg["districts"] = []
        else:
            cfg["districts"] = [x.strip() for x in a.split(",") if x.strip()]
        save_config(cfg)
        d = cfg["districts"]
        send(cfg, "✅ Районы: {}".format(esc(", ".join(d)) if d else "весь Краков"))
        return False

    if cmd == "/pause":
        state["paused"] = True
        send(cfg, "⏸ Пауза. /resume — продолжить.")
        return False

    if cmd == "/resume":
        state["paused"] = False
        send(cfg, "▶️ Поехали дальше.")
        return True

    if cmd == "/city":
        a = norm_pl(arg)
        target = CITY_ALIASES.get(a)
        if not target:
            send(cfg, "Сейчас: <b>{}</b>. Сменить: /city krakow или /city warszawa".format(city_name(cfg)))
            return False
        if target == city_key(cfg):
            send(cfg, "Уже слежу за городом: <b>{}</b>.".format(city_name(cfg)))
            return False
        cfg["city"] = target
        save_config(cfg)
        state["initialized"] = False          # свежая стартовая подборка для нового города
        state["recent"] = []
        send(cfg, "✅ Город: <b>{}</b>. Сейчас соберу стартовую подборку…".format(city_name(cfg)))
        return True

    if cmd == "/check":
        send(cfg, "🔍 Проверяю OLX…", silent=True)
        return True

    if cmd in ("/last", "/september", "/sep", "/perfect"):
        only_perfect = cmd in ("/september", "/sep", "/perfect")
        n = to_int(arg) or (10 if only_perfect else 5)
        n = max(1, min(n, 12))
        items = recent_filtered(state, cfg, only_perfect=only_perfect)
        if not items:
            if only_perfect:
                send(cfg, "Пока не попадались варианты с заселением от сентября под текущие фильтры. "
                          "Как появятся — пришлю с пометкой ⭐️.")
            else:
                send(cfg, "Под текущие фильтры в истории пусто. Попробуй /check или ослабь /max.")
            return False
        head = "⭐️ Заселение от сентября ({}):".format(len(items)) if only_perfect \
            else "🔎 Последние под фильтры ({}):".format(len(items))
        send(cfg, head, silent=True)
        for item in items[:n]:
            send(cfg, item["text"], silent=True)
            time.sleep(1)
        return False

    if cmd.startswith("/"):
        send(cfg, "Не знаю такую команду. /help — список.")
    return False


def process_updates(cfg, state, poll_timeout=20):
    """Long-poll обновлений Telegram. Возвращает True, если просили /check."""
    r = tg(cfg, "getUpdates", {"offset": state.get("tg_offset", 0) + 1, "timeout": poll_timeout},
           timeout=poll_timeout + 15)
    if not r or not r.get("ok"):
        time.sleep(5)
        return False
    force = False
    for u in r.get("result", []):
        state["tg_offset"] = u.get("update_id", state["tg_offset"])
        msg = u.get("message") or u.get("edited_message") or {}
        chat = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat or not text:
            continue
        subs = subscribers(cfg)
        if chat not in subs:
            first_ever = len(subs) == 0
            add_subscriber(cfg, chat)
            log("Новый подписчик: {} (всего {})".format(chat, len(subscribers(cfg))))
            send_one(cfg, chat,
                     "Привет! 👋 Ты подключён к монитору OLX.\n"
                     "Слежу за арендой квартир, город <b>{}</b>, {}, до {} zł/мес "
                     "(аренда + czynsz вместе). Считаю все оплаты из описания — паркинг, свет, "
                     "медиа, kaucja — и отдельно ⭐️ выделяю заселение от сентября.\n"
                     "/help — команды, /status — фильтры, /city — сменить город."
                     .format(city_name(cfg), rooms_ru(cfg), cfg.get("max_total")))
            if subs:   # кто-то уже был — предупредим, что фильтры теперь общие
                for cid in subs:
                    send_one(cfg, cid, "➕ К боту подключился ещё один человек. "
                                       "Уведомления идут вам обоим, фильтры общие.", silent=True)
            items = recent_filtered(state, cfg)
            if items:
                send_one(cfg, chat, "📦 Что сейчас подходит под фильтры:", silent=True)
                for it in items[:8]:
                    send_one(cfg, chat, it["text"], silent=True)
                    time.sleep(0.5)
            elif first_ever:
                force = True     # самый первый — соберём стартовую подборку
            continue
        force = handle_command(cfg, state, text, chat) or force
    return force


# ---------------------------------------------------------------- проверка

def check_olx(cfg, state):
    now = time.time()
    if now < state.get("backoff_until", 0):
        log("Пропускаю проверку (пауза после ошибки OLX)")
        return
    try:
        offers = fetch_offers(cfg)
    except urllib.error.HTTPError as e:
        log("⚠️ OLX ответил {} — жду 15 минут".format(e.code))
        state["backoff_until"] = now + 900
        return
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log("⚠️ Сеть/OLX: {}".format(e))
        state["backoff_until"] = now + 300
        return

    first_run = not state.get("initialized")
    matches = []
    for o in offers:
        of = parse_offer(o)
        if not of["id"]:
            continue
        # старьё отсеиваем ДО seen — если потом ослабишь /age, оно снова сможет прийти
        if too_old(of["created_iso"], cfg.get("max_age_days")):
            continue
        sid = str(of["id"])
        if sid in state["seen"]:
            continue
        state["seen"][sid] = int(now)
        an = analyze_desc(of["desc"], of["price"])
        if not passes_filters(of, an, cfg):
            continue
        fp = fingerprint(of)
        if fp in state["fp"] and now - state["fp"][fp] < 7 * 86400:
            continue
        state["fp"][fp] = int(now)
        matches.append((of, an))

    matches.sort(key=lambda x: x[0]["created_iso"])

    def remember(of, an, text):
        state["recent"].append({
            "text": text, "ts": int(now), "city": city_key(cfg),
            "perfect": bool(an.get("perfect")),
            "of": {"price": of["price"], "rent": of["rent"], "area": of["area"],
                   "district": of["district"], "business": of.get("business"),
                   "created_iso": of["created_iso"]},
            "an": {"czynsz_desc": an.get("czynsz_desc")},
        })

    PERFECT_BANNER = "⭐️⭐️ <b>ЗАСЕЛЕНИЕ С СЕНТЯБРЯ — то, что нужно!</b>\n"
    sent = 0
    if first_run:
        state["initialized"] = True
        perfect = [x for x in matches if x[1].get("perfect")]
        regular = [x for x in matches if not x[1].get("perfect")]
        batch = (perfect[-6:] + regular[-8:])
        if batch:
            send(cfg, "📦 <b>Стартовая подборка</b> ({}) — уже висит на OLX по твоим фильтрам. "
                      "⭐️ отмечены варианты с заселением от сентября. "
                      "Дальше буду слать только новые:".format(city_name(cfg)))
        for of, an in batch:
            text = format_offer(of, an)
            banner = PERFECT_BANNER if an.get("perfect") else ""
            if send(cfg, banner + text):
                sent += 1
                remember(of, an, text)
            time.sleep(1.2)
    else:
        for of, an in matches:
            text = format_offer(of, an)
            if an.get("perfect"):
                head = PERFECT_BANNER
            else:
                head = "🆕 <b>Новая квартира</b>\n"
            if send(cfg, head + text):
                sent += 1
                remember(of, an, text)
            time.sleep(1.2)

    # уборка
    state["recent"] = state["recent"][-40:]
    cutoff_seen = int(now) - 45 * 86400
    state["seen"] = {k: v for k, v in state["seen"].items() if v > cutoff_seen}
    cutoff_fp = int(now) - 14 * 86400
    state["fp"] = {k: v for k, v in state["fp"].items() if v > cutoff_fp}
    state["stats"]["checks"] = state["stats"].get("checks", 0) + 1
    state["stats"]["sent"] = state["stats"].get("sent", 0) + sent
    state["last_check"] = datetime.now().isoformat(timespec="seconds")
    log("Проверка: {} объявл., подходят новых: {}, отправлено: {}".format(len(offers), len(matches), sent))


# ---------------------------------------------------------------- selftest

def _mk(oid, business, title, desc, price, rent, area, city="Kraków",
        district="", rooms="two", floor=None, pets=False, created="2026-07-18T09:00:00+02:00"):
    params = [{"key": "price", "value": {"value": price, "label": "{} zł".format(price)}},
              {"key": "m", "value": {"key": str(area), "label": "{} m²".format(area)}},
              {"key": "rooms", "value": {"key": rooms, "label": "2 pokoje"}}]
    if rent is not None:
        params.append({"key": "rent", "value": {"key": str(rent), "label": "{} zł".format(rent)}})
    if floor is not None:
        params.append({"key": "floor_select", "value": {"label": floor}})
    if pets:
        params.append({"key": "pets", "value": {"label": "Tak"}})
    return {"id": oid, "url": "https://www.olx.pl/d/oferta/test{}.html".format(oid), "business": business,
            "title": title, "description": desc, "created_time": created,
            "location": {"city": {"name": city}, "district": {"name": district}}, "params": params}


def selftest():
    fixtures = [
        # 1. Идеальный: заселение с сентября, паркинг с ценой, свет по счётчику, kaucja.
        _mk(1, False, "Przytulne 2 pokoje Zabłocie 38m2",
            "Do wynajęcia 38 m2, salon + sypialnia. Cena 2500 zł + czynsz administracyjny 500 zł. "
            "Prąd według zużycia. Miejsce postojowe w hali garażowej 200 zł/mc. Kaucja 3000 zł. "
            "Mieszkanie dostępne od 1 września 2026.",
            2500, 500, 38, district="Zabłocie", floor="3", pets=True),
        # 2. Дорогой (3600+700=4300) — должен отсеяться при max 4000.
        _mk(2, True, "Apartament premium Stare Miasto",
            "Luksusowy apartament. Czynsz najmu 3600 zł, czynsz administracyjny 700 zł.",
            3600, 700, 45, district="Stare Miasto"),
        # 3. czynsz только в тексте, паркинг в цене, od zaraz.
        _mk(3, False, "2 pokoje Podgórze od zaraz",
            "Wynajmę mieszkanie 2-pokojowe. Czynsz: 450 zł miesięcznie. Parking podziemny w cenie. "
            "Media według zużycia. Dostępne od zaraz.",
            2900, None, 40.5, district="Podgórze"),
        # 4. Реальный стиль: odstępne + administracyjny ok., energia ok. 150, parking za dodatkowe 300, od 01.08.
        _mk(4, False, "Komfortowe mieszkanie Prądnik",
            "Oferuję do wynajęcia komfortowe mieszkanie 39 m2. Istnieje możliwość wynajęcia miejsca "
            "parkingowego w garażu podziemnym za dodatkowe 300 zł miesięcznie. Koszty: odstępne 2800 "
            "zł/miesiąc, czynsz administracyjny: ok. 550 zł/miesiąc, energia elektryczna: ok. 150 "
            "zł/miesiąc, kaucja: 3500 zł. Mieszkanie dostępne od 01.08.",
            2800, None, 39, district="Prądnik Czerwony"),
        # 5. Реальный стиль: media (список) 130 PLN + prąd wg licznika, odstępne 686, od 01.08.2026.
        _mk(5, False, "Przytulne 2 pokoje bezpośrednio",
            "Do wynajęcia przytulne 2-pokojowe mieszkanie bezpośrednio od właściciela. Całkowite koszty: "
            "2699 PLN - odstępne 686 PLN - media (ogrzewanie, ciepła/zimna woda, śmieci) 130 PLN - "
            "opłata za prąd wg licznika. Dostępne od 01.08.2026.",
            2699, None, 42, district="Bronowice"),
        # 6. Варшава, заселение с сентября (perfect), агентство.
        _mk(6, True, "2 pokoje Mokotów",
            "Nowoczesne mieszkanie. Czynsz najmu 3000 zł + czynsz administracyjny 200 zł. Media wg "
            "zużycia. Dostępne od 1 września.",
            3000, 200, 44, city="Warszawa", district="Mokotów"),
    ]
    cfg = dict(DEFAULT_CONFIG)
    cfg["max_total"] = 4000

    R = []
    for raw in fixtures:
        of = parse_offer(raw)
        an = analyze_desc(of["desc"], of["price"])
        ok = passes_filters(of, an, cfg)
        R.append((of, an, ok))
        print("=" * 62)
        print("id{} OK:{}  czynsz/total:{}  perfect:{}".format(
            of["id"], ok, effective_costs(of, an), an.get("perfect", False)))
        print(format_offer(of, an))

    (of1, an1, ok1), (of2, an2, ok2), (of3, an3, ok3) = R[0], R[1], R[2]
    (of4, an4, ok4), (of5, an5, ok5), (of6, an6, ok6) = R[3], R[4], R[5]

    # 1 — идеальный сентябрьский
    assert ok1 and of1["rent"] == 500 and effective_costs(of1, an1)[1] == 3000
    assert an1.get("parking") == "200 zł/мес", an1.get("parking")
    assert an1.get("media") == "по счётчикам (сверх цены)", an1.get("media")
    assert an1.get("kaucja") == 3000
    assert an1.get("perfect") is True and an1.get("september") is True
    assert an1.get("move_in_month") == 9

    # 2 — отсекается по бюджету
    assert not ok2, "4300 должно отсеяться при max 4000"

    # 3 — czynsz из текста, паркинг в цене, сразу
    czynsz3, total3 = effective_costs(of3, an3)
    assert ok3 and czynsz3 == 450 and total3 == 3350, (czynsz3, total3)
    assert an3.get("parking") == "в цене"
    assert an3.get("move_in_month") == 0 and not an3.get("perfect")

    # 4 — odstępne+administracyjny: czynsz 550, паркинг 300, свет ≈150, kaucja 3500, август (не perfect)
    assert an4.get("czynsz_desc") == 550, an4.get("czynsz_desc")
    assert effective_costs(of4, an4)[1] == 3350
    assert an4.get("parking") == "300 zł/мес", an4.get("parking")
    assert an4.get("media_amount") == 150, an4.get("media_amount")
    assert an4.get("kaucja") == 3500, an4.get("kaucja")
    assert an4.get("move_in_month") == 8 and not an4.get("perfect")
    assert "с 01.08" in an4.get("available", "")

    # 5 — czynsz из "odstępne" 686, media фикс 130 + счётчик
    assert an5.get("czynsz_desc") == 686, an5.get("czynsz_desc")
    assert effective_costs(of5, an5)[1] == 3385, effective_costs(of5, an5)
    assert an5.get("media_amount") == 130, an5.get("media_amount")
    assert "счёт" in an5.get("media", ""), an5.get("media")
    assert not an5.get("perfect")

    # 6 — Варшава, сентябрь → perfect, шапка «Варшава»
    assert ok6 and an6.get("perfect") is True
    m6 = format_offer(of6, an6)
    assert "Варшава" in m6 and "⭐️" in m6, m6

    # карточка №1
    m1 = format_offer(of1, an1)
    for frag in ("Краков, Zabłocie", "38", "2500", "3000 zł/мес", "Паркинг", "200 zł/мес",
                 "Kaucja (залог): 3000", "Заселение", "⭐️", "1-й (parter)" if False else "Этаж: 3"):
        assert frag in m1, frag

    # /last и /september теперь учитывают ТЕКУЩИЕ фильтры (баг из скринов)
    st = {"recent": [
        {"text": "дорогая", "ts": 1, "city": "krakow", "perfect": False,
         "of": {"price": 3000, "rent": 250, "area": 50, "district": "X", "business": True}, "an": {}},
        {"text": "в бюджете-сентябрь", "ts": 2, "city": "krakow", "perfect": True,
         "of": {"price": 2500, "rent": 500, "area": 40, "district": "Y", "business": False}, "an": {}},
        {"text": "варшава", "ts": 3, "city": "warszawa", "perfect": True,
         "of": {"price": 2000, "rent": 200, "area": 40, "district": "Z", "business": False}, "an": {}},
    ]}
    cfg2 = dict(DEFAULT_CONFIG); cfg2["max_total"] = 3200; cfg2["city"] = "krakow"
    last = recent_filtered(st, cfg2)
    assert [i["text"] for i in last] == ["в бюджете-сентябрь"], [i["text"] for i in last]
    sep = recent_filtered(st, cfg2, only_perfect=True)
    assert [i["text"] for i in sep] == ["в бюджете-сентябрь"], sep
    # переключение города переключает и выдачу истории
    cfg2["city"] = "warszawa"
    assert [i["text"] for i in recent_filtered(st, cfg2)] == ["варшава"]

    # фильтр по дате добавления
    assert too_old("2026-05-01T10:00:00+02:00", 3) is True
    assert too_old("2026-05-01T10:00:00+02:00", 0) is False      # 0 = выключено
    fresh_iso = datetime.now().astimezone().isoformat()
    assert too_old(fresh_iso, 3) is False
    assert added_label(fresh_iso).startswith("сегодня")
    st_age = {"recent": [
        {"text": "старая", "ts": 8, "city": "krakow", "perfect": False,
         "of": {"price": 2500, "rent": 400, "area": 40, "district": "A", "business": False,
                "created_iso": "2026-05-01T10:00:00+02:00"}, "an": {}},
        {"text": "свежая", "ts": 9, "city": "krakow", "perfect": False,
         "of": {"price": 2500, "rent": 400, "area": 40, "district": "A", "business": False,
                "created_iso": fresh_iso}, "an": {}},
    ]}
    cfg3 = dict(DEFAULT_CONFIG); cfg3["max_age_days"] = 3
    assert [i["text"] for i in recent_filtered(st_age, cfg3)] == ["свежая"], "старое должно отсеяться"
    cfg3["max_age_days"] = 0
    assert [i["text"] for i in recent_filtered(st_age, cfg3)] == ["свежая", "старая"], "с /age off — обе"

    # несколько подписчиков (без записи в файл)
    assert subscribers({}) == []
    assert subscribers({"telegram_chat_id": 111}) == [111]
    assert subscribers({"telegram_chat_ids": [111, 222]}) == [111, 222]
    assert set(subscribers({"telegram_chat_id": 111, "telegram_chat_ids": [222]})) == {111, 222}
    assert subscribers({"telegram_chat_id": 111, "telegram_chat_ids": [111]}) == [111]  # без дублей

    assert CITY_ALIASES.get("варшава") == "warszawa" and CITY_ALIASES.get("krakow") == "krakow"
    assert norm_pl("Zabłocie") == "zablocie" and to_int("2 500 zł") == 2500

    print("=" * 62)
    print("SELFTEST OK ✅")


# ---------------------------------------------------------------- main

SETUP_HINT = """
──────────────────────────────────────────────────────
Токен Telegram-бота ещё не настроен. Делается один раз:

1. В Telegram открой @BotFather → команда /newbot →
   придумай имя (например, Alex Flat Hunter).
2. BotFather пришлёт токен вида 1234567890:AAxxxxx…
3. Открой файл config.json (лежит рядом со скриптом)
   и вставь токен в поле "telegram_bot_token".
4. Запусти меня снова: python3 olx_monitor.py
5. Открой своего бота в Telegram и напиши ему /start.
──────────────────────────────────────────────────────
"""


def main():
    if "--selftest" in sys.argv:
        selftest()
        return

    cfg = load_config()
    token = get_token(cfg)
    if "ВСТАВЬ" in token or not re.match(r"^\d+:[\w-]{30,}$", token):
        print(SETUP_HINT)
        sys.exit(1)

    state = load_state()
    log("🚀 Монитор OLX Kraków запущен (интервал {} мин, потолок {} zł)".format(
        cfg.get("check_interval_min", 5), cfg.get("max_total")))

    # Режим для GitHub Actions: один прогон и выход (память — в state.json).
    if "--cron" in sys.argv:
        process_updates(cfg, state, poll_timeout=6)
        if cfg.get("telegram_chat_id") and not state.get("paused"):
            check_olx(cfg, state)
        else:
            log("Чат не привязан или пауза — проверку пропустил.")
        save_config(cfg)
        save_state(state)
        return

    if "--once" in sys.argv:
        if not cfg.get("telegram_chat_id"):
            log("Чат не привязан: запусти без --once и напиши боту /start")
            sys.exit(1)
        check_olx(cfg, state)
        save_state(state)
        return

    if cfg.get("telegram_chat_id"):
        send(cfg, "🚀 Монитор запущен. /status — фильтры, /help — команды.", silent=True)
    else:
        log("Жду привязки: открой своего бота в Telegram и напиши ему /start")

    next_check = 0.0
    while True:
        try:
            cfg = load_config()
            force = process_updates(cfg, state)
            due = time.time() >= next_check
            if cfg.get("telegram_chat_id") and not state.get("paused") and (force or due):
                check_olx(cfg, state)
                next_check = time.time() + max(2, int(cfg.get("check_interval_min", 5))) * 60
            save_state(state)
        except KeyboardInterrupt:
            log("Остановлен вручную. Пока!")
            save_state(state)
            break
        except Exception as e:  # noqa: BLE001 — монитор не должен падать
            log("⚠️ Ошибка цикла: {!r}".format(e))
            time.sleep(20)


if __name__ == "__main__":
    main()
