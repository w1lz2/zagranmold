"""
Мониторинг свободных окон записи в Посольство Молдовы в РФ (Reservio) → Telegram.

Берёт свободные слоты напрямую из API сайта записи (тем же запросом, что делает сам сайт),
сравнивает с прошлой проверкой и присылает в Telegram только НОВЫЕ окна:
  • освободившиеся после чьей-то отмены;
  • новые дни, которые открываются для записи.

Запуск:
  python monitor.py          — одна проверка (так запускает GitHub Actions по расписанию)
  python monitor.py --loop   — проверять бесконечно раз в INTERVAL_MIN минут (для своего сервера/VPS)
"""

import json
import os
import random
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ===================== НАСТРОЙКИ =====================

SITE = "https://ambasada-r-moldova-in-f-rusa.reservio.com"
BUSINESS_ID = "09250556-2450-437f-aede-82e78712f114"
# Услуга. По умолчанию «Изготовление паспорта (загран)».
# ID других услуг программа печатает в лог при каждом запуске.
SERVICE_ID = os.getenv("SERVICE_ID") or "59c845fa-48c9-4f79-b7b2-1d07f895f2e6"
# Следить только за определёнными сотрудниками (имена через запятую, как на сайте).
# Пусто — за всеми, кто ведёт эту услугу.
STAFF_FILTER = [s.strip().lower() for s in (os.getenv("STAFF") or "").split(",") if s.strip()]
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD") or "90")
INTERVAL_MIN = float(os.getenv("INTERVAL_MIN") or "2")

TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

STATE_FILE = Path(os.getenv("STATE_FILE") or "state.json")
TZ = ZoneInfo("Europe/Moscow")

# =====================================================

API = f"{SITE}/api/v2/businesses/{BUSINESS_ID}"
BOOKING_URL = f"{SITE}/order?what=resource"
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/vnd.api+json, application/json",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": f"{SITE}/order?what=time",
}
session = requests.Session()
session.headers.update(HEADERS)


def log(msg):
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------- Telegram ----------------------

def notify(text):
    log("УВЕДОМЛЕНИЕ:\n" + text)
    if not (TG_TOKEN and TG_CHAT_ID):
        log("Telegram не настроен (нет TG_TOKEN / TG_CHAT_ID) — только лог.")
        return
    # Telegram ограничивает сообщение 4096 символами — режем по строкам
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for chunk in chunks:
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              data={"chat_id": TG_CHAT_ID, "text": chunk,
                                    "disable_web_page_preview": "true"}, timeout=30)
            if not r.ok:
                log(f"Telegram ответил ошибкой: {r.status_code} {r.text[:300]}")
        except Exception as e:
            log(f"Не удалось отправить в Telegram: {e}")


# ---------------------- Состояние ----------------------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------- API ----------------------

def api_get(path, params=None):
    for attempt in range(3):
        try:
            r = session.get(API + path, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            err = str(e)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Сайт не ответил ({path}): {err}")


def get_names():
    """id сотрудника -> имя; id услуги -> название."""
    staff = {d["id"]: d["attributes"]["name"].strip() for d in api_get("/resources")["data"]}
    services = {d["id"]: d["attributes"]["name"].strip() for d in api_get("/services")["data"]}
    return staff, services


def get_free_slots(staff_names):
    """Множество строк 'Имя|YYYY-MM-DDTHH:MM' со всеми свободными окнами."""
    today = datetime.now(TZ).date()
    data = api_get("/availability/booking-slots", {
        "filter[from]": today.isoformat(),
        "filter[to]": (today + timedelta(days=DAYS_AHEAD)).isoformat(),
        "filter[serviceId]": SERVICE_ID,
        "ignoreBookingBoundaries": "0",
    })
    slots = set()
    for d in data.get("data", []):
        start = d.get("attributes", {}).get("start")
        if not start:
            continue
        rid = (((d.get("relationships") or {}).get("resource") or {}).get("data") or {}).get("id")
        name = staff_names.get(rid, "Сотрудник")
        if STAFF_FILTER and name.lower() not in STAFF_FILTER:
            continue
        dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(TZ)
        slots.add(f"{name}|{dt:%Y-%m-%dT%H:%M}")
    return slots


# ---------------------- Форматирование ----------------------

def group(keys):
    by = {}
    for k in sorted(keys):
        name, ts = k.split("|", 1)
        d, t = ts.split("T")
        by.setdefault(name, {}).setdefault(d, []).append(t)
    return by


def day_label(d):
    dd = date.fromisoformat(d)
    return f"{WEEKDAYS[dd.weekday()]} {dd:%d.%m}"


def plural_okno(n):
    if n % 10 == 1 and n % 100 != 11:
        return "окно"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "окна"
    return "окон"


def fmt_summary(keys):
    lines = []
    for name, days in group(keys).items():
        lines.append(f"👤 {name}")
        for d, times in days.items():
            lines.append(f"  {day_label(d)}: {len(times)} {plural_okno(len(times))} ({times[0]}–{times[-1]})")
    return "\n".join(lines)


def fmt_new(new, prev):
    prev_days = {(k.split("|")[0], k.split("|")[1][:10]) for k in prev}
    lines = []
    for name, days in group(new).items():
        lines.append(f"👤 {name}")
        for d, times in days.items():
            mark = " 🆕 новый день" if (name, d) not in prev_days else ""
            if len(times) <= 12:
                lines.append(f"  {day_label(d)}{mark}: {', '.join(times)}")
            else:
                lines.append(f"  {day_label(d)}{mark}: {len(times)} {plural_okno(len(times))} "
                             f"({times[0]}–{times[-1]})")
    return "\n".join(lines)


# ---------------------- Проверка ----------------------

def run_once():
    state = load_state()
    first_run = "slots" not in state
    prev = set(state.get("slots", []))
    try:
        staff_names, services = get_names()
        service_name = services.get(SERVICE_ID, SERVICE_ID)
        if first_run:
            log("Услуги на сайте (для настройки SERVICE_ID):\n" +
                "\n".join(f"  {sid}  {n}" for sid, n in services.items()))
        current = get_free_slots(staff_names)
    except Exception as e:
        traceback.print_exc()
        fails = state.get("fails", 0) + 1
        state["fails"] = fails
        save_state(state)
        if fails == 3 or fails % 100 == 0:
            notify(f"⚠️ Мониторинг записи: {fails} ошибок подряд.\n{e}")
        return

    new = current - prev
    log(f"{service_name}: свободно {len(current)}, новых {len(new)}")
    if first_run:
        msg = f"✅ Мониторинг запущен: {service_name}\nСейчас свободно: {len(current)}\n"
        if current:
            msg += "\n" + fmt_summary(current) + "\n"
        msg += f"\nДальше буду присылать только новые окна.\n{BOOKING_URL}"
        notify(msg)
    elif new:
        notify(f"🟢 Новые свободные окна ({len(new)}) — {service_name}\n\n"
               f"{fmt_new(new, prev)}\n\nЗаписаться: {BOOKING_URL}")

    save_state({"slots": sorted(current), "fails": 0,
                "last_ok": datetime.now(TZ).isoformat(timespec="seconds")})


def main():
    if "--loop" not in sys.argv:
        run_once()
        return
    while True:
        try:
            run_once()
        except Exception:
            traceback.print_exc()
        time.sleep(INTERVAL_MIN * 60 + random.randint(0, 20))


if __name__ == "__main__":
    main()
