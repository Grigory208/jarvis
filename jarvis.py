#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JARVIS Lite — сверхлёгкий голосовой ассистент в стиле J.A.R.V.I.S.

Текстовый режим:   python jarvis.py
Голосовой режим:   python jarvis.py voice   (нужны pip-пакеты vosk и sounddevice)

Ядро — только стандартная библиотека Python, ни одного pip-пакета.
Озвучка — espeak-ng. Ответы — Mistral API, либо локальная Ollama как запасной вариант.
"""

import difflib
import ctypes
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile

# ------------- Настройки (переопределяются переменными окружения) -------------

API_KEY = os.environ.get("MISTRAL_API_KEY", "")
API_URL = os.environ.get("JARVIS_API_URL", "https://api.mistral.ai/v1/chat/completions")
MODEL = os.environ.get("JARVIS_MODEL", "mistral-small-latest")

OLLAMA_URL = os.environ.get("JARVIS_OLLAMA", "http://127.0.0.1:11434/api/chat")
LOCAL_MODEL = os.environ.get("JARVIS_LOCAL_MODEL", "qwen2.5:0.5b-instruct")

POLLINATIONS_URL = os.environ.get("JARVIS_POLLINATIONS", "https://text.pollinations.ai/openai")
POLLINATIONS_MODEL = os.environ.get("JARVIS_POLLINATIONS_MODEL", "openai-fast")

VOICE = os.environ.get("JARVIS_VOICE", "ru")
VARIANT = os.environ.get("JARVIS_VARIANT", "")   # например mb-ru2, если установлен mbrola
RATE = os.environ.get("JARVIS_RATE", "155")
PITCH = os.environ.get("JARVIS_PITCH", "45")
MAX_TOKENS = int(os.environ.get("JARVIS_MAX_TOKENS", "300"))

VOLUME = max(0, min(200, int(os.environ.get("JARVIS_VOLUME", "100"))))
PLAYER = os.environ.get("JARVIS_PLAYER", "")     # шаблон проигрывателя: "aplay {file}" или "aplay -D {device} {file}"
DEVICE = os.environ.get("JARVIS_DEVICE", "")     # аудиоустройство для {device} в JARVIS_PLAYER

WAKE_WORDS = tuple(w.strip().lower() for w in os.environ.get(
    "JARVIS_WAKE_WORDS",
    "джарвис,джервис,жарвис,жар верс,ярвис,джерри,джери,джарис").split(",") if w.strip())

VOSK_MODEL_NAME = "vosk-model-small-ru-0.22"
VOSK_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL_NAME}.zip"

EXIT_WORDS = ("выход", "exit", "quit", "спокойной ночи", "отключись")

REPLY_CACHE = {}          # повторный вопрос — мгновенный ответ без запроса в сеть
REPLY_CACHE_MAX = 20

MEMORY_FILE = os.environ.get(
    "JARVIS_MEMORY",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.json"))
MEMORY_MAX = 100          # максимум фактов в постоянной памяти

CITY = os.environ.get("JARVIS_CITY", "москва")   # город погоды по умолчанию
REMINDERS = []            # активные напоминания сессии: {"text", "at"}

WMO_CODE = {
    0: "ясно", 1: "почти ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозь", 51: "лёгкая морось", 53: "морось", 55: "сильная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "ледяной дождь", 71: "небольшой снег", 73: "снег",
    75: "сильный снег", 77: "снежные зёрна", 80: "ливень", 81: "ливень",
    82: "сильный ливень", 85: "снегопад", 86: "снегопад",
    95: "гроза", 96: "гроза с градом", 99: "гроза с градом",
}

MGMT = os.environ.get("JARVIS_ADDRESS", "сэр")

SYSTEM_PROMPT = (
    "Ты JARVIS — голосовой ИИ-ассистент, как в фильмах Marvel. "
    f"Обращайся к пользователю: {MGMT}. "
    "Отвечай всегда по-русски, кратко (1-3 предложения), по существу, "
    "спокойно и с лёгкой британской иронией. "
    "Никогда не используй списки, разметку и эмодзи — только гладкий текст, "
    "который удобно озвучивать вслух."
)

# ----------------------------- Озвучка -----------------------------

def find_tts():
    for name in ("espeak-ng", "espeak"):
        path = shutil.which(name)
        if path:
            return name
    return None


def speak(engine, text):
    if not engine:
        return
    voice = VOICE + (f"+{VARIANT}" if VARIANT else "")
    base = [engine, "-v", voice, "-s", RATE, "-p", PITCH, "-a", str(VOLUME)]
    try:
        if PLAYER:
            wav = os.path.join(tempfile.gettempdir(), "jarvis_tts.wav")
            subprocess.run(base + ["-w", wav, text], check=True)
            subprocess.run(shlex.split(PLAYER.format(file=wav, device=DEVICE)), check=False)
        else:
            subprocess.run(base + [text], check=False)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"\n[ ошибка озвучки: {exc} ]")

# ----------------------------- Вызов модели -----------------------------

def encode_json(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _stream_openai_like(url, data, headers=None):
    req = urllib.request.Request(
        url,
        data=encode_json(data),
        headers=headers or {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = event.get("choices") or [{}]
            delta = choices[0].get("delta", {}) if choices else {}
            token = delta.get("content") or ""
            if token:
                yield token


def stream_mistral(messages):
    data = {"model": MODEL, "messages": messages, "stream": True, "max_tokens": MAX_TOKENS}
    yield from _stream_openai_like(API_URL, data, {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    })


def stream_pollinations(messages):
    """Бесплатное облако без ключей и регистрации: text.pollinations.ai"""
    data = {"model": POLLINATIONS_MODEL, "messages": messages, "stream": True}
    yield from _stream_openai_like(POLLINATIONS_URL, data, {
        "Content-Type": "application/json",
        "Authorization": "Bearer guest",
    })


def stream_ollama(messages):
    data = {"model": LOCAL_MODEL, "messages": messages, "stream": True}
    req = urllib.request.Request(OLLAMA_URL, data=encode_json(data), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            token = (event.get("message") or {}).get("content") or ""
            if token:
                yield token
            if event.get("done"):
                break


def stream_reply(history):
    """Генератор токенов ответа: ключ -> Ollama -> бесплатный Pollinations."""
    system = SYSTEM_PROMPT
    memory = load_memory()
    if memory:
        system += ("\n\nЧто ты помнишь о хозяине (используй, когда уместно):\n"
                   + "\n".join("- " + f for f in memory[-10:]))
    messages = [{"role": "system", "content": system}] + history
    if API_KEY:
        try:
            yield from stream_mistral(messages)
            return
        except (urllib.error.URLError, OSError) as exc:
            print(f"\n[ Mistral API недоступен: {exc}, пробую Ollama ]")
    if not API_KEY:
        try:
            yield from stream_ollama(messages)
            return
        except (urllib.error.URLError, OSError) as exc:
            print(f"\n[ локальная Ollama не отвечает: {exc} ]")
            print(f"[ переключаюсь на бесплатное облако Pollinations ({POLLINATIONS_MODEL}, без ключа) ]")
            yield from stream_pollinations(messages)
            return
    try:
        yield from stream_ollama(messages)
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(
            f"Mistral API недоступен, а Ollama не отвечает ({exc}). "
            f"Установите Ollama (ollama.com) и выполните: ollama pull {LOCAL_MODEL}"
        ) from exc

# ------------- Мгновенные команды, которые не тратят запросы API -------------

NUMBER_WORDS = {
    "ноль": 0, "десять": 10, "двадцать": 20, "тридцать": 30, "сорок": 40,
    "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80,
    "девяносто": 90, "сто": 100, "полтораста": 150, "двести": 200,
}


def set_volume(value):
    global VOLUME
    VOLUME = max(0, min(200, int(value)))


# ------------- Постоянная память (memory.json, переживает перезапуск) -------------

def load_memory():
    try:
        with open(MEMORY_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return [f.get("text", "").strip() for f in data.get("facts", [])
                if f.get("text", "").strip()]
    except (OSError, ValueError):
        return []


def save_memory(facts):
    payload = {"facts": [{"text": t, "when": time.strftime("%Y-%m-%d %H:%M")}
                         for t in facts]}
    tmp = MEMORY_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, MEMORY_FILE)
    except OSError as exc:
        print(f"[ не удалось сохранить память: {exc} ]")


def handle_memory(query, engine):
    """Запомни / что помнишь / забудь. Возвращает ответ или None."""
    q = query.strip().lower()
    m = re.match(r"(?:!)?(?:запомни(?:\s+что)?|запомнить|помни)\s*[,:!]?\s+(.+)", q)
    if m:
        facts = load_memory()
        text = re.sub(r"^что\s+", "", m.group(1).strip(" .!"))
        if text and text not in facts:
            facts.append(text)
            facts[:] = facts[-MEMORY_MAX:]
            save_memory(facts)
            reply = f"Запомнил: {text}, {MGMT}."
        else:
            reply = f"Это я уже помню, {MGMT}."
        print("JARVIS > " + reply)
        speak(engine, reply)
        return reply
    if re.search(r"что ты (?:обо мне\s+)?помнишь|что помнишь|что ты знаешь (?:обо )?мне", q):
        facts = load_memory()
        if facts:
            shown = "; ".join(facts[-5:])
            reply = f"Я помню {len(facts)} факт(ов): {shown}, {MGMT}."
        else:
            reply = f"Память пока пуста, {MGMT}. Скажите: запомни: ..."
        print("JARVIS > " + reply)
        speak(engine, reply)
        return reply
    if re.search(r"забудь\s+(?:вс[её]|все)|очисти(?!\s+окн)\s+память", q) or q in ("забудь всё",):
        save_memory([])
        reply = f"Все факты забыты, {MGMT}."
        print("JARVIS > " + reply)
        speak(engine, reply)
        return reply
    m = re.match(r"(?:!)?забудь(?:те)?\s+(?:про\s+|обо\s+мне\s+|об\s+)?(.+)", q)
    if m:
        word = m.group(1).strip(" .!")
        def fact_hit(f):
            tokens = re.findall(r"\w+", f)
            return word in f or bool(difflib.get_close_matches(word, tokens, n=1, cutoff=0.72))
        facts = [f for f in load_memory() if not fact_hit(f)]
        save_memory(facts)
        reply = f"Все упоминания «{word}» забыты, {MGMT}."
        print("JARVIS > " + reply)
        speak(engine, reply)
        return reply
    return None


# ------------- Погода (open-meteo.com, без ключей) и напоминания -------------

def fetch_weather(city=None):
    city = (city or CITY).strip()
    hit = None
    for name in (city, city[:-1], city[:-2], city[:-3]):   # падежи: "лондоне" -> "лондон"
        if len(name) < 3:
            break
        geo_url = ("https://geocoding-api.open-meteo.com/v1/search?name="
                   + urllib.parse.quote(name) + "&count=1&language=ru&format=json")
        with urllib.request.urlopen(geo_url, timeout=10) as resp:
            hit = (json.load(resp).get("results") or [None])[0]
        if hit:
            break
    if not hit:
        return None
    fc_url = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
              "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
              "&timezone=auto").format(lat=hit["latitude"], lon=hit["longitude"])
    with urllib.request.urlopen(fc_url, timeout=10) as resp:
        cur = json.load(resp).get("current", {})
    return {"city": hit.get("name", city), "temp": cur.get("temperature_2m"),
            "feels": cur.get("apparent_temperature"), "code": cur.get("weather_code"),
            "wind": cur.get("wind_speed_10m")}


def weather_reply(query):
    q = query.strip().lower()
    if not re.search(r"погод\w*|температур\w*(?!\s*(ператур)?\s*\d)|на улице\s+(тепло|холодно|жарко)", q):
        return None
    city = None
    m = re.search(r"погод\w*\s+(?:сейчас\s+)?(?:в|во|на)\s+([а-яёa-z\- ]{3,30})", q)
    if m:
        city = m.group(1).strip()
    try:
        w = fetch_weather(city)
    except (urllib.error.URLError, OSError, ValueError):
        return f"Не достучался до погодного сервиса, {MGMT}."
    if not w or w.get("temp") is None:
        return f"Не нашёл город, {MGMT}."
    desc = WMO_CODE.get(w.get("code"), "неизвестно")
    def temp_word(value):
        txt = str(round(value)) + "°"
        return txt.replace("-", " минус ")
    parts = [f"{w['city']}: {temp_word(w['temp'])}", desc]
    if w.get("feels") is not None:
        parts.append("ощущается как " + temp_word(w['feels']))
    if w.get("wind") is not None:
        parts.append(f"ветер {round(w['wind'])} м/с")
    return ", ".join(parts) + f", {MGMT}."


def schedule_reminder(text, delay_s, engine):
    at = time.time() + delay_s
    entry = {"text": text, "at": at}
    REMINDERS.append(entry)
    def fire():
        if entry in REMINDERS:
            REMINDERS.remove(entry)
        print(f"\n[ напоминание: {text} ]")
        speak(engine, f"Напоминаю: {text}, {MGMT}.")
    timer = threading.Timer(delay_s, fire)
    timer.daemon = True
    timer.start()


def reminder_reply(query, engine):
    q = query.strip().lower()
    m = re.match(r"(?:!)?напомни(?:ть)?(?:\s+мне)?\s+(.+)", q)
    if not m:
        if re.search(r"каки[еёйх]\s+напоминан|что\s+напоминан|напоминан\w*$", q):
            if REMINDERS:
                left = "; ".join(f"{r['text']} (через {int((r['at'] - time.time()) / 60) + 1} мин)"
                                 for r in REMINDERS[:5])
                return f"Активных напоминаний: {len(REMINDERS)}: {left}, {MGMT}."
            return f"Активных напоминаний нет, {MGMT}."
        return None
    rest = m.group(1).strip()
    delay, text = None, ""
    m_half = re.match(r"через\s+(?:полчаса|пол\s+часа|полчасика)\s*(.*)", rest)
    m_at = re.match(r"(?:в|во)\s+(\d{1,2})[:.](\d{2})\s*(.*)", rest)
    m_num = re.match(r"через\s+([\dа-яё]+)\s*(секунд\w*|сек\b|минут\w*|мин\b|час\w*|ч\b)\s*(.*)", rest)
    m_word = re.match(r"через\s+(минуту|секунду|час|часа|пять\s+минут|десять\s+минут|"
                      r"пятнадцать\s+минут|двадцать\s+минут|тридцать\s+минут)\s*(.*)", rest)
    if m_half:
        delay, text = 1800, m_half.group(1).strip()
    elif m_at:
        hh, mm = int(m_at.group(1)), int(m_at.group(2))
        if hh < 24 and mm < 60:
            target = time.localtime()
            at = time.mktime((target.tm_year, target.tm_mon, target.tm_mday, hh, mm, 0, 0, 0, -1))
            while at <= time.time():
                at += 86400
            delay, text = int(at - time.time()), m_at.group(3).strip()
    elif m_word:
        mult = {"секунду": 1, "минуту": 60, "час": 3600, "часа": 3600,
                "пять минут": 300, "десять минут": 600, "пятнадцать минут": 900,
                "двадцать минут": 1200, "тридцать минут": 1800}[m_word.group(1)]
        delay, text = mult, m_word.group(2).strip()
    elif m_num:
        raw, unit, text = m_num.group(1), m_num.group(2), m_num.group(3)
        try:
            n = int(raw)
        except ValueError:
            n = NUMBER_WORDS.get(raw)
        if n is not None:
            mult = 3600 if unit.startswith(("час", "ч")) else (60 if unit.startswith("мин") else 1)
            delay, text = max(1, n) * mult, m_num.group(3).strip()
    elif m_word:
        mult = {"секунду": 1, "минуту": 60, "час": 3600, "часа": 3600,
                "пять минут": 300, "десять минут": 600, "пятнадцать минут": 900,
                "двадцать минут": 1200, "тридцать минут": 1800}[m_word.group(1)]
        delay, text = mult, m_word.group(2).strip()
    if delay is None:
        delay, text = 3600, rest          # время не указано — напомню через час
    text = text.strip(" .!") or "без описания"
    human = (f"через {delay // 60} минут" if delay >= 120 and delay % 3600 else
             f"через {delay / 60:.0f} мин" if delay > 90 else f"через {delay} сек")
    if delay >= 3600 and delay % 3600 == 0:
        human = f"через {delay // 3600} ч"
    schedule_reminder(text, delay, engine)
    return f"Хорошо: {text} — {human}, {MGMT}."


def briefing_reply():
    lines = [f"Сейчас {time.strftime(('%d.%m.%Y, %H:%M'))}"]
    try:
        w = fetch_weather()
        if w and w.get("temp") is not None:
            desc = WMO_CODE.get(w.get("code"), "")
            lines.append(f"{w['city']}: {round(w['temp'])}°, {desc}")
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        lines.append(f"нагрузка ЦП {os.getloadavg()[0]:.2f}")
    except (AttributeError, NotImplementedError, OSError):
        pass
    mem = load_memory()
    if mem:
        lines.append(f"в памяти {len(mem)} факт(ов)")
    if REMINDERS:
        lines.append(f"активных напоминаний: {len(REMINDERS)}")
    return ", ".join(lines) + f", {MGMT}."


def instant_command(query):
    q = query.strip().lower()
    if q in ("громче", "!громче", "louder", "громкость"):
        set_volume(VOLUME + 20)
        return f"Увеличиваю громкость до {VOLUME} процентов, {MGMT}."
    if q in ("тише", "!тише", "quieter", "громкость -"):
        set_volume(VOLUME - 20)
        return f"Уменьшаю громкость до {VOLUME} процентов, {MGMT}."
    m = re.fullmatch(r"(?:!)?(?:громкость|volume)\s*(\d{1,3})", q)
    if m:
        set_volume(m.group(1))
        return f"Громкость установлена на {VOLUME} процентов, {MGMT}."
    for word, value in NUMBER_WORDS.items():
        if value <= 200 and word in q and ("громкость" in q or "volume" in q):
            set_volume(value)
            return f"Громкость установлена на {VOLUME} процентов, {MGMT}."
    if q in ("громкость", "!громкость", "volume"):
        return f"Текущая громкость — {VOLUME} процентов, {MGMT}."
    if q in ("доклад", "дай доклад", "утренний доклад", "!доклад") or "дай доклад" in q:
        return briefing_reply()
    if q in ("время", "который час", "сколько времени", "!время") or "который час" in q:
        return f"Сейчас {time.strftime('%H:%M')}, {MGMT}."
    if (q in ("дата", "!дата") or "какое сегодня число" in q
            or "какой день" in q or "какое число" in q or "какая дата" in q):
        return f"Сегодня {time.strftime('%d.%m.%Y')}, {MGMT}."
    if q in ("статус", "система", "!система"):
        try:
            avg = os.getloadavg()[0]
            load = f"Нагрузка процессора — {avg:.2f}."
        except (AttributeError, NotImplementedError, OSError):
            load = "Работаю в облегчённом режиме без нагрузки на процессор."
        return (f"Системы в норме, {MGMT}. {load} "
                "Рекомендую не перегружать этот почтенный процессор.")
    if q in ("ты кто", "кто ты", "!кто"):
        return f"Я JARVIS, ваш персональный ассистент, {MGMT}. Работаю в облегчённом режиме."
    return None

# --------- Системные команды из JARVIS v1 (комбинации клавиш, Windows) ---------

if os.name == "nt":
    user32 = ctypes.windll.user32
else:
    user32 = None

VK_TAB, VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN = 0x09, 0x10, 0x11, 0x12, 0x5B
VK_D, VK_H, VK_J, VK_M, VK_N, VK_T, VK_W = 0x44, 0x48, 0x4A, 0x4D, 0x4E, 0x54, 0x57
VK_F4 = 0x73
VK_SNAPSHOT, VK_F5, VK_F11 = 0x2C, 0x74, 0x7A
VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
VK_MEDIA_NEXT_TRACK, VK_MEDIA_PREV_TRACK, VK_MEDIA_PLAY_PAUSE = 0xB0, 0xB1, 0xB3
KEYEVENTF_KEYUP = 0x0002


def press_vk(vk, times=1):
    for _ in range(times):
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.02)


def press_combo(*codes):
    for vk in codes:
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
    for vk in reversed(codes):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.02)


def list_windows():
    if os.name == "nt":
        ps = "Get-Process | Where-Object {$_.MainWindowTitle -ne ''} | Select-Object -ExpandProperty MainWindowTitle"
        result = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                capture_output=True, text=True, errors="ignore")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    try:
        result = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, errors="ignore")
        return [" ".join(line.split()[4:]) for line in result.stdout.splitlines() if line.strip()]
    except (OSError, IndexError):
        return []


def open_url(url):
    try:
        webbrowser.open(url)
        return True
    except OSError:
        return False


def match_hotkey(t):
    """Возвращает (вид, данные, фраза) или (None, None, None). Клавиши не жмёт."""
    if "ютуб" in t and any(p in t for p in ("найди", "найти", "включи")):
        q = re.sub(r".*?(?:найди|найти|включи)\s+(?:в|на)\s+ютуб[еь]?\s*", "", t).strip()
        return ("web", "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q),
                f"Ищу на YouTube {q}, {{}}.")
    if "ютуб муз" in t or "youtube music" in t:
        return "web", "https://youtube.com", "Открываю YouTube Music, {}."
    if "ютуб" in t or "youtube" in t:
        return "web", "https://youtube.com", "Открываю YouTube, {}."
    if " вк " in t or "вконтакт" in t:
        return "web", "https://vk.com", "Открываю ВКонтакте, {}."
    if ("нов" in t and "вкладк" in t) or "открой вкладк" in t:
        return "combo", (VK_CONTROL, VK_T), "Открываю новую вкладку, {}."
    if "закрой вкладк" in t or "закрыть вкладк" in t:
        return "combo", (VK_CONTROL, VK_W), "Вкладка закрыта, {}."
    if ("верн" in t or "восстанов" in t) and "вкладк" in t:
        return "combo", (VK_CONTROL, VK_SHIFT, VK_T), "Восстанавливаю вкладку, {}."
    if "следующ" in t and "вкладк" in t:
        return "combo", (VK_CONTROL, VK_TAB), "Переключаю вкладку, {}."
    if "предыдущ" in t and "вкладк" in t:
        return "combo", (VK_CONTROL, VK_SHIFT, VK_TAB), "Предыдущая вкладка, {}."
    if ("обнов" in t or "перезагруз" in t) and "страниц" in t:
        return "keys", (VK_F5, 1), "Обновляю страницу, {}."
    if "истори" in t and ("открой" in t or "браузер" in t):
        return "combo", (VK_CONTROL, VK_H), "Открываю историю браузера, {}."
    if "загрузк" in t and ("открой" in t or "браузер" in t):
        return "combo", (VK_CONTROL, VK_J), "Открываю загрузки, {}."
    if "инкогнито" in t:
        return "combo", (VK_CONTROL, VK_SHIFT, VK_N), "Запускаю режим инкогнито, {}."
    if "звук" in t and any(w in t for w in ("громче", "выше", "прибав")):
        return "keys", (VK_VOLUME_UP, 5), "Увеличиваю громкость системы, {}."
    if "звук" in t and any(w in t for w in ("тише", "ниже", "убав")):
        return "keys", (VK_VOLUME_DOWN, 5), "Уменьшаю громкость системы, {}."
    if ("выключи звук" in t or "включи звук" in t or "без звука" in t or "мьют" in t):
        return "keys", (VK_VOLUME_MUTE, 1), "Переключаю режим звука, {}."
    if any(w in t for w in ("паузу", "пауза", "продолжи", "играй", "включи музык")):
        return "keys", (VK_MEDIA_PLAY_PAUSE, 1), "Выполнено, {}."
    if "следующ" in t and any(w in t for w in ("трек", "песн", "видео")):
        return "keys", (VK_MEDIA_NEXT_TRACK, 1), "Переключаю трек, {}."
    if "предыдущ" in t and any(w in t for w in ("трек", "песн", "видео")):
        return "keys", (VK_MEDIA_PREV_TRACK, 1), "Возвращаю предыдущий трек, {}."
    if "сверн" in t and "все" in t:
        return "combo", (VK_LWIN, VK_D), "Сворачиваю все окна, {}."
    if "разверни окн" in t or "верни окн" in t:
        return "combo", (VK_LWIN, VK_SHIFT, VK_M), "Восстанавливаю окна, {}."
    if "закрой окно" in t or "закрой текущее" in t:
        return "combo", (VK_MENU, VK_F4), "Окно закрыто, {}."
    if "переключи окн" in t or "переключ окн" in t:
        return "combo", (VK_MENU, VK_TAB), "Переключаю окно, {}."
    if "весь экран" in t or "полный экран" in t or "фулскрин" in t:
        return "keys", (VK_F11, 1), "Полноэкранный режим, {}."
    if "скриншот" in t or "снимок экрана" in t:
        return "combo", (VK_LWIN, VK_SNAPSHOT), "Скриншот экрана сохранён, {}."
    if "заблокируй" in t:
        return "lock", None, "Блокирую систему, {}."
    return None, None, None


def handle_hotkeys(query, engine):
    """Системные команды из v1 и сайты. Возвращает ответ или None."""
    t = " " + query.strip().lower() + " "

    windows_rx = re.compile(
        r"список\s+окон|что\s+открыт\w*"
        r"|каки[еёйх]\w*\s+(?:сейчас\s+)?(?:программ\w*|окн\w*)"
        r"|(?:программ\w*|окн\w*)\s+(?:сейчас\s+)?откр\w*")
    if windows_rx.search(t):
        windows = list_windows()
        if windows:
            reply = f"Сейчас открыты: {', '.join(windows[:5])}, {MGMT}."
        else:
            reply = f"Активных окон не обнаружено, {MGMT}."
        print("JARVIS > " + reply)
        speak(engine, reply)
        return reply

    kind, data, phrase = match_hotkey(t)
    if phrase is None:
        return None

    if kind == "web":
        reply = phrase.format(MGMT)
        open_url(data)
    elif user32 is None:
        reply = f"Эта системная команда работает только в Windows, {MGMT}."
    elif kind == "lock":
        reply = phrase.format(MGMT)
        speak(engine, reply)
        print("JARVIS > " + reply)
        time.sleep(1)
        user32.LockWorkStation()
        return reply
    elif kind == "combo":
        press_combo(*data)
        reply = phrase.format(MGMT)
    else:
        press_vk(*data)
        reply = phrase.format(MGMT)

    print("JARVIS > " + reply)
    speak(engine, reply)
    return reply

# ------------- Действия: открыть/закрыть программу, поиск -------------

# Русская транслитерация английских названий (vosk-ru не знает латиницу)
TRANSLIT = {
    "стим": "steam", "стиим": "steam", "стимп": "steam",
    "хром": "chrome", "хрум": "chrome", "кхром": "chrome", "гуглхром": "chrome",
    "телеграм": "telegram", "телиграм": "telegram",
    "дискорд": "discord", "дискорт": "discord",
    "блокнот": "notepad",
    "калькулятор": "calc", "калкулятор": "calc",
    "диспетчер": "taskmgr",
    "майнкрафт": "minecraft", "майнкраф": "minecraft", "майнкрафта": "minecraft",
    "роблокс": "roblox", "роблок": "roblox",
    "гитхаб": "github", "ютуб": "youtube", "ютюб": "youtube",
    "браузер": "chrome", "брузер": "chrome",
    "проводник": "explorer", "провадник": "explorer",
    "консоль": "cmd", "командная": "cmd",
}

OPEN_PREFIXES = ("открой", "открыть", "запусти", "запустить", "open", "launch", "старт")
CLOSE_PREFIXES = ("закрой", "закрыть", "заверши", "close", "kill")
SEARCH_PREFIXES = ("найди", "найдети", "загугли", "поищи", "поиск", "search")


def fix_recognition(text):
    """Чинит русский транслит английских слов: 'открой стим' -> 'открой steam'."""
    words = []
    for raw in text.split():
        w = raw.strip(",.!?")
        if not w:
            continue
        if w.isascii():
            words.append(w)
            continue
        if w in TRANSLIT:
            words.append(TRANSLIT[w])
            continue
        best, ratio = w, 0.0
        for key, value in TRANSLIT.items():
            if " " in key:
                continue
            r = difflib.SequenceMatcher(None, w, key).ratio()
            if r > ratio:
                best, ratio = value, r
        if ratio >= 0.75:
            words.append(best)
        else:
            words.append(raw)
    return " ".join(words)


def open_app(name):
    name = name.strip()
    if not name:
        return False
    try:
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "start", "", name], check=False)
        elif shutil.which(name):
            subprocess.Popen([name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def close_app(name):
    name = name.strip()
    if not name:
        return False
    if os.name == "nt":
        for suffix in ("", ".exe"):
            result = subprocess.run(["taskkill", "/IM", name + suffix, "/F"], capture_output=True)
            if result.returncode == 0:
                return True
        return False
    return subprocess.run(["pkill", "-f", name], capture_output=True).returncode == 0


def handle_action(query, engine):
    """Оффлайн-действия. Возвращает ответ или None, если это не действие."""
    t = fix_recognition(query.strip().lower())
    for prefix in OPEN_PREFIXES:
        if t.startswith(prefix + " "):
            target = t[len(prefix) + 1:].strip()
            if open_app(target):
                reply = f"Открываю {target}, {MGMT}."
            else:
                reply = f"Не нашёл программу {target}, {MGMT}."
            print("JARVIS > " + reply)
            speak(engine, reply)
            return reply
    for prefix in CLOSE_PREFIXES:
        if t.startswith(prefix + " "):
            target = t[len(prefix) + 1:].strip()
            if close_app(target):
                reply = f"Закрываю {target}, {MGMT}."
            else:
                reply = f"Не вижу открытого окна {target}, {MGMT}."
            print("JARVIS > " + reply)
            speak(engine, reply)
            return reply
    for prefix in SEARCH_PREFIXES:
        if t.startswith(prefix + " "):
            target = query.strip()[len(prefix) + 1:].strip()
            webbrowser.open("https://google.com/search?q=" + urllib.parse.quote(target))
            reply = f"Ищу в сети {target}, {MGMT}."
            print("JARVIS > " + reply)
            speak(engine, reply)
            return reply
    return None

# ------------- Встроенный движок: установка и запуск Ollama -------------

OLLAMA_SETUP_URL = "https://ollama.com/download/OllamaSetup.exe"
OLLAMA_INSTALL_CMD = "curl -fsSL https://ollama.com/install.sh | sh"


def find_ollama():
    exe = shutil.which("ollama")
    if exe:
        return exe
    if os.name == "nt":
        local = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
        if os.path.exists(local):
            return local
        local = os.path.join(os.environ.get("ProgramFiles", ""), "Ollama", "ollama.exe")
        if os.path.exists(local):
            return local
    return None


def ollama_server_up():
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2).close()
        return True
    except OSError:
        return False


def start_ollama_server(exe):
    print("[ запускаю сервер ollama serve... ]")
    try:
        subprocess.Popen([exe, "serve"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        print(f"[ не удалось запустить сервер: {exc} ]")
        return False
    for _ in range(45):
        if ollama_server_up():
            return True
        time.sleep(1)
    return False


def ollama_has_model(exe):
    try:
        result = subprocess.run([exe, "list"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    base = LOCAL_MODEL.split(":")[0]
    return LOCAL_MODEL in result.stdout or base in result.stdout


def install_ollama():
    if os.name == "nt":
        exe_path = os.path.join(tempfile.gettempdir(), "OllamaSetup.exe")
        if not os.path.exists(exe_path):
            print(f"[ скачиваю установщик Ollama для Windows (~700 МБ) в {exe_path} ]")

            def report(blocks, block_size, total):
                if total > 0:
                    print(f"\r[ скачивание: {min(100, blocks * block_size * 100 // total)}% ]",
                          end="", flush=True)

            urllib.request.urlretrieve(OLLAMA_SETUP_URL, exe_path, reporthook=report)
            print()
        print("[ устанавливаю Ollama (тихая установка)... ]")
        subprocess.run([exe_path, "/VERYSILENT"], check=False)
    else:
        print("[ устанавливаю Ollama через install.sh (понадобится sudo) ]")
        subprocess.run(["sh", "-c", OLLAMA_INSTALL_CMD], check=False)


def setup_engine():
    """python jarvis.py setup — ставит движок, сервер и модель одной командой."""
    exe = find_ollama()
    if exe is None:
        print("[ движок Ollama не найден. Установлю его локально: ]")
        print("  Windows: установщик ~700 МБ, затем модель ~400 МБ.")
        print("  Ollama остаётся вашей программой, работает без интернета и ключей.")
        print("Продолжить? [y/N] ", end="", flush=True)
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes", "да", "д", "дa"):
            print("[ отмена. Движок можно поставить позже: python jarvis.py setup ]")
            return
        install_ollama()
        exe = find_ollama()
        if exe is None:
            print("[ Ollama установлена, но не видна в PATH. Перезапустите терминал/скрипт ]")
            return

    if not ollama_server_up() and not start_ollama_server(exe):
        print("[ сервер Ollama не поднялся — использовать её не получится ]")
        return
    print("[ движок Ollama работает ]")

    if not ollama_has_model(exe):
        print(f"[ скачиваю модель {LOCAL_MODEL} (~400 МБ, один раз) ]")
        subprocess.run([exe, "pull", LOCAL_MODEL], check=False)
    if ollama_has_model(exe):
        print(f"[ готово: {LOCAL_MODEL} на месте. Запускайте jarvis.py обычно ]")
        subprocess.run([exe, "run", LOCAL_MODEL, "Скажи привет одним словом"], check=False,
                       capture_output=True)
    else:
        print(f"[ модель {LOCAL_MODEL} не загрузилась — проверьте интернет и повторите setup ]")


def maybe_prepare_local_brain():
    """Фоновый подхват: если Ollama установлена, но сервер не запущен — поднять."""
    if API_KEY or ollama_server_up():
        return
    exe = find_ollama()
    if exe is None:
        print("[ движка нет — отвечаю через бесплатное облако Pollinations. "
              "Подсказка: python jarvis.py setup поставит локальный движок Ollama ]")
        return
    if start_ollama_server(exe):
        print("[ локальный движок Ollama на связи ]")
    else:
        print("[ Ollama есть, но сервер не поднялся — остаюсь на Pollinations ]")

# ----------------------------- Единый ответ -----------------------------

SENTENCE_SPLIT = re.compile(r"(?<=[.!?…\n])\s+")


def pop_sentences(buffer):
    """Отдаёт завершённые предложения из буфера, возвращает незавершённый хвост."""
    parts = SENTENCE_SPLIT.split(buffer)
    if len(parts) < 2:
        return [], buffer
    done, tail = parts[:-1], parts[-1]
    return [s.strip() for s in done if s.strip()], tail


def respond(query, history, engine):
    """Один ответ на запрос: хоткеи/сайты -> действия -> мгновенные команды -> LLM.
    Возвращает (текст ответа | None, обновлённая история)."""
    hotkey_reply = handle_hotkeys(query, engine)
    if hotkey_reply:
        return hotkey_reply, history

    action_reply = handle_action(query, engine)
    if action_reply:
        return action_reply, history

    memory_reply = handle_memory(query, engine)
    if memory_reply:
        return memory_reply, history

    remind_reply = reminder_reply(query, engine)
    if remind_reply:
        print("JARVIS > " + remind_reply)
        speak(engine, remind_reply)
        return remind_reply, history

    w_reply = weather_reply(query)
    if w_reply:
        print("JARVIS > " + w_reply)
        speak(engine, w_reply)
        return w_reply, history

    answer = instant_command(query)
    if answer:
        print("JARVIS > " + answer)
        speak(engine, answer)
        return answer, history

    key = re.sub(r"[!?.…,\s]+", " ", query.strip().lower()).strip()
    if not history and key in REPLY_CACHE:
        cached = REPLY_CACHE[key]
        print("JARVIS > " + cached + "  [ из кэша ]")
        speak(engine, cached)
        return cached, history

    history = history + [{"role": "user", "content": query}]
    history = history[-12:]

    full = ""
    buffer = ""
    try:
        for token in stream_reply(history):
            full += token
            buffer += token
            print(token, end="", flush=True)
            sentences, buffer = pop_sentences(buffer)
            for sentence in sentences:
                speak(engine, sentence)
    except (RuntimeError, urllib.error.URLError, OSError) as exc:
        print(f"\n[ нет связи с моделью: {exc} ]")
        return None, history[:-1]

    tail = buffer.strip()
    if tail:
        speak(engine, tail)
    print()

    if not full.strip():
        print("[ модель вернула пустой ответ ]")
        return None, history[:-1]
    full = full.strip()
    if not history[:-1]:
        if len(REPLY_CACHE) >= REPLY_CACHE_MAX:
            REPLY_CACHE.pop(next(iter(REPLY_CACHE)))
        REPLY_CACHE[key] = full
    history = history + [{"role": "assistant", "content": full}]
    return full, history

# ----------------------------- Текстовый режим -----------------------------

def main_text():
    engine = find_tts()
    if not engine:
        print("[ предупреждение: espeak-ng не найден — ответы будут только текстом. "
              "Установите: sudo apt install espeak-ng ]")

    if not API_KEY:
        maybe_prepare_local_brain()
    mode = ("Mistral API" if API_KEY else
            f"Ollama/{LOCAL_MODEL} -> Pollinations/{POLLINATIONS_MODEL}")
    print(f"=== JARVIS Lite | режим: {mode} | озвучка: {engine or 'нет'} ===")
    print(f"Готов к работе, {MGMT}. ('выход' — завершить, 'voice' — голосовой режим)\n")

    history = []

    while True:
        try:
            query = input("Вы > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query:
            continue
        if query.lower() in EXIT_WORDS:
            speak(engine, f"Всего наилучшего, {MGMT}. Отключаюсь.")
            break
        if query.lower() == "voice":
            main_voice()
            break

        _, history = respond(query, history, engine)

# ----------------------------- Голосовой режим -----------------------------

def ensure_vosk_model():
    path = os.environ.get("JARVIS_VOSK_MODEL", "")
    if path and os.path.isdir(path):
        return path
    models_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
    candidate = os.path.join(models_dir, VOSK_MODEL_NAME)
    if os.path.isdir(candidate):
        return candidate
    os.makedirs(models_dir, exist_ok=True)
    zip_path = os.path.join(models_dir, VOSK_MODEL_NAME + ".zip")
    if not os.path.exists(zip_path):
        print(f"[ скачиваю русскую модель vosk: {VOSK_MODEL_NAME} (~45 МБ), один раз ]")
        urllib.request.urlretrieve(VOSK_MODEL_URL, zip_path)
    print("[ распаковываю модель... ]")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(models_dir)
    if not os.path.isdir(candidate):
        raise RuntimeError(f"модель не распаковалась: {candidate}")
    return candidate


def main_voice():
    try:
        from vosk import Model, KaldiRecognizer
    except ImportError:
        print("[ нет пакета vosk. Установите: pip install vosk sounddevice ]")
        return
    try:
        import sounddevice as sd
    except ImportError:
        print("[ нет пакета sounddevice. Установите: pip install sounddevice ]")
        return

    engine = find_tts()
    if not engine:
        print("[ предупреждение: espeak-ng не найден — ответы будут только текстом ]")

    if not API_KEY:
        maybe_prepare_local_brain()

    model_path = ensure_vosk_model()
    print("[ загружаю vosk... ]")
    model = Model(model_path)
    recognizer = KaldiRecognizer(model, 16000)

    audio_q = queue.Queue()

    def callback(indata, frames, time_info, status):
        audio_q.put(bytes(indata))

    mode = ("Mistral API" if API_KEY else
            f"Ollama/{LOCAL_MODEL} -> Pollinations/{POLLINATIONS_MODEL}")
    print(f"=== JARVIS Lite | голосовой режим | {mode} ===")
    print(f"Скажите: {' / '.join(WAKE_WORDS)} + команда. (Ctrl+C — выход)\n")
    speak(engine, f"Слушаю, {MGMT}.")

    history = []

    try:
        with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype="int16",
                               channels=1, callback=callback):
            while True:
                data = audio_q.get()
                if not recognizer.AcceptWaveform(data):
                    continue
                text = json.loads(recognizer.Result()).get("text", "").strip().lower()
                if not text:
                    continue
                print(f"[ услышал: {text} ]")
                if not any(wake in text for wake in WAKE_WORDS):
                    continue
                command = ""
                for wake in WAKE_WORDS:
                    if wake in text:
                        command = text.split(wake, 1)[-1].strip()
                        break
                if not command:
                    speak(engine, f"Слушаю внимательно, {MGMT}.")
                    continue
                if command in EXIT_WORDS:
                    speak(engine, f"Всего наилучшего, {MGMT}. Отключаюсь.")
                    break
                reply, history = respond(command, history, engine)
                if reply is None:
                    speak(engine, f"Не смог выполнить запрос, {MGMT}.")
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\nJARVIS отключён, {MGMT}.")

# ----------------------------- Точка входа -----------------------------

if __name__ == "__main__":
    if os.name == "nt" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    try:
        if len(sys.argv) > 1 and sys.argv[1].lower() in ("setup", "установить", "движок"):
            setup_engine()
        elif len(sys.argv) > 1 and sys.argv[1].lower() in ("voice", "-v", "голос", "голосовой"):
            main_voice()
        else:
            main_text()
    except KeyboardInterrupt:
        print(f"\nJARVIS отключён, {MGMT}.")
