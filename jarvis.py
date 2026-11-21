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
import math
import os
import queue
import random
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
RATE = os.environ.get("JARVIS_RATE", "48")
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
ESPEAK_MSI_URL = os.environ.get(
    "JARVIS_ESPEAK_MSI",
    "https://github.com/espeak-ng/espeak-ng/releases/download/1.52.0/espeak-ng.msi")

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
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        roots = (r"C:\Program Files", r"C:\Program Files (x86)",
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"))
        for root in roots:
            if not root or not os.path.isdir(root):
                continue
            for d in os.listdir(root):
                if "espeak" not in d.lower():
                    continue
                for exe in ("espeak-ng.exe", "espeak.exe"):
                    p = os.path.join(root, d, exe)
                    if os.path.exists(p):
                        return p
    return None


ESPEAK_VOICE = None       # выбранный голос espeak (кэшируется после первой проверки)


def list_espeak_voices(engine):
    try:
        result = subprocess.run([engine, "--voices"], capture_output=True,
                                text=True, errors="ignore", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    voices = []
    for line in result.stdout.splitlines():
        for token in line.split():
            if re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]+)*", token) and token not in voices:
                voices.append(token)
    return voices


def resolve_voice(engine):
    """Проверяет, есть ли голос VOICE у espeak; если нет — берёт ближайший вместо английского."""
    global ESPEAK_VOICE
    if ESPEAK_VOICE:
        return ESPEAK_VOICE
    if VARIANT:
        ESPEAK_VOICE = f"{VOICE}+{VARIANT}"
        return ESPEAK_VOICE
    voices = list_espeak_voices(engine)
    if not voices or VOICE in voices:
        ESPEAK_VOICE = VOICE
        return VOICE
    same_lang = [v for v in voices if v == VOICE or v.startswith(VOICE + "-")]
    if same_lang:
        ESPEAK_VOICE = same_lang[0]
        print(f"[ голос '{VOICE}' нет, использую '{ESPEAK_VOICE}' ]")
        return ESPEAK_VOICE
    if VOICE.startswith("ru"):
        ESPEAK_VOICE = VOICE
        return VOICE
    ESPEAK_VOICE = VOICE
    return VOICE


def speak(engine, text):
    if not engine:
        return
    orb_notify("speak")
    voice = resolve_voice(engine)
    base = [engine, "-v", voice, "-s", RATE, "-p", PITCH, "-a", str(VOLUME)]
    tmp = None
    try:
        if PLAYER:
            wav = os.path.join(tempfile.gettempdir(), "jarvis_tts.wav")
            # на Windows argv ломает кириллицу (ANSI) — текст идёт через UTF-8 файл
            fd, tmp = tempfile.mkstemp(suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            subprocess.run(base + ["-f", tmp, "-w", wav], check=True)
            subprocess.run(shlex.split(PLAYER.format(file=wav, device=DEVICE)), check=False)
        else:
            fd, tmp = tempfile.mkstemp(suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            subprocess.run(base + ["-f", tmp], check=False)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"\n[ ошибка озвучки: {exc} ]")
    finally:
        orb_notify("idle")
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass

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
    "параметры": "ms-settings:", "настройки": "ms-settings:", "стеттингс": "ms-settings:",
    "студия": "code", "студию": "code", "студио": "code", "визуал": "code", "визвал": "code",
    "фотошоп": "photoshop", "ворд": "winword", "ворде": "winword", "верд": "winword",
    "эксель": "excel",
}

# Как называем программу в ответе (иначе espeak прочитает 'ms-settings:' вслух)
APP_DISPLAY = {
    "ms-settings:": "настройки Windows",
    "code": "визуал студио",
    "winword": "ворд",
    "excel": "эксель",
    "photoshop": "фотошоп",
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
            if " " in key or len(key) < 5:
                continue
            r = difflib.SequenceMatcher(None, w, key).ratio()
            if r > ratio:
                best, ratio = value, r
        if ratio >= 0.85:
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
            if name.startswith(("ms-settings:", "http")):
                os.startfile(name)     # noqa: S606
            else:
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
                reply = f"Открываю {APP_DISPLAY.get(target, target)}, {MGMT}."
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


def ask_yes(prompt):
    print(prompt + " [y/N] ", end="", flush=True)
    try:
        return input().strip().lower() in ("y", "yes", "да", "д", "дa")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def install_espeak():
    if os.name == "nt":
        msi = os.path.join(tempfile.gettempdir(), "espeak-ng.msi")
        if not os.path.exists(msi):
            print(f"[ скачиваю espeak-ng (~13 МБ) в {msi} ]")

            def report(blocks, block_size, total):
                if total > 0:
                    print(f"\r[ скачивание: {min(100, blocks * block_size * 100 // total)}% ]",
                          end="", flush=True)

            urllib.request.urlretrieve(ESPEAK_MSI_URL, msi, reporthook=report)
            print()
        print("[ устанавливаю espeak-ng (Windows может спросить разрешение) ]")
        result = subprocess.run(["msiexec", "/i", msi, "/qn", "/norestart"], check=False)
        if result.returncode != 0:
            print(f"[ тихая установка не удалась (код {result.returncode}). "
                  f"Запустите файл вручную: {msi} ]")
    else:
        print("[ ставлю espeak-ng через apt (понадобится sudo) ]")
        subprocess.run(["sudo", "apt-get", "install", "-y", "espeak-ng"], check=False)


def setup_voice():
    """python jarvis.py setup voice — озвучка + пакеты голосового режима."""
    engine = find_tts()
    if engine is None:
        if not ask_yes("[ озвучка espeak-ng не найдена. Установить её сейчас (~13 МБ)? ]"):
            print("[ отмена. Позже: python jarvis.py setup voice ]")
            return
        install_espeak()
        engine = find_tts()
        if engine is None:
            print("[ espeak-ng установлен, но не виден: ОТКРОЙТЕ консоль заново ]")
            return
        print(f"[ озвучка на месте: {engine} ]")
    else:
        print(f"[ озвучка уже есть: {engine} ]")

    missing = []
    for mod in ("vosk", "sounddevice"):
        try:
            __import__(mod)
        except (ImportError, OSError):
            missing.append(mod)
    if missing:
        if not ask_yes(f"[ для голосового режима нужны пакеты: {', '.join(missing)}. Через pip? ]"):
            print("[ отмена. Позже: pip install " + " ".join(missing) + " ]")
        else:
            subprocess.run([sys.executable, "-m", "pip", "install"] + missing, check=False)
    speak(find_tts(), "Голос на связи, сэр.")
    print("[ проверка голоса выполнена. Если молчал — JARVIS_VOLUME или устройство вывода ]")


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
    orb_notify("think")
    try:
        for token in stream_reply(history):
            full += token
            buffer += token
            print(token, end="", flush=True)
            sentences, buffer = pop_sentences(buffer)
            for sentence in sentences:
                speak(engine, sentence)
            orb_notify("think")
    except (RuntimeError, urllib.error.URLError, OSError) as exc:
        print(f"\n[ нет связи с моделью: {exc} ]")
        orb_notify("idle")
        return None, history[:-1]

    tail = buffer.strip()
    if tail:
        speak(engine, tail)
    print()
    orb_notify("idle")

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
              "Автоустановка: python jarvis.py setup voice ]")

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
        print("[ предупреждение: espeak-ng не найден — ответы будут только текстом. "
              "Автоустановка: python jarvis.py setup voice ]")

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
            while not VOICE_STOP:
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

def doctor():
    """Диагностика: что видит JARVIS (озвучка, голос, движок, память)."""
    print("=== JARVIS doctor ===")
    tts = find_tts()
    print(f"озвучка (espeak-ng): {tts or 'НЕ НАЙДЕНА'}")
    if not tts:
        print("  -> установите с https://github.com/espeak-ng/espeak-ng/releases")
        print("     (msi для Windows), затем ОТКРОЙТЕ консоль заново")
    else:
        try:
            res = subprocess.run([tts, "--version"], capture_output=True,
                                 text=True, errors="ignore", timeout=10)
            ver = (res.stdout or res.stderr).strip().split("\n")[0]
            if ver:
                print(f"версия озвучки: {ver}")
        except (OSError, subprocess.SubprocessError):
            pass
        ru = [v for v in list_espeak_voices(tts) if v.startswith("ru")]
        print(f"русский голос espeak: {', '.join(ru) if ru else 'НЕТ — будет английский акцент'}")
    for mod in ("vosk", "sounddevice"):
        try:
            __import__(mod)
            print(f"голосовой режим, {mod}: ок")
        except (ImportError, OSError) as exc:
            print(f"голосовой режим, {mod}: НЕ НАЙДЕН ({exc}); поставь: pip install {mod}")
    exe = find_ollama()
    print(f"движок (ollama.exe): {exe or 'не найден'}")
    print(f"сервер Ollama (порт 11434): {'поднят' if ollama_server_up() else 'не запущен'}")
    if exe and ollama_server_up():
        try:
            print(f"модель {LOCAL_MODEL}: {'скачана' if ollama_has_model(exe) else 'нет — python jarvis.py setup'}")
        except (urllib.error.URLError, OSError):
            pass
    print(f"ключ MISTRAL_API_KEY: {'задан' if API_KEY else 'нет (будет бесплатный Pollinations)'}")
    mem = load_memory()
    print(f"память ({os.path.basename(MEMORY_FILE)}): {len(mem)} факт(ов)")
    print(f"город погоды: {CITY}; голос: {VOICE}; громкость: {VOLUME}")


# ----------------------------- GUI: шар-ядро -----------------------------

GUI_BG = "#04070d"
GUI_BG_RGB = (4, 7, 13)
GUI_FG = "#8fd0ff"
GUI_FG_DIM = "#2e5a78"
GUI_ERR = "#ff8a80"
ORB_CORE = (110, 190, 255)
ORB_DEEP = (20, 60, 100)
ORB_LIT = (185, 232, 255)
ORB_WARM = (150, 105, 70)
ORB_STATE = "idle"
VOICE_STOP = False
GUI_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_settings.json")
GUI_SUGGESTIONS = ("Открой YouTube", "Открой калькулятор", "Как дела?", "Открой блокнот", "Сколько времени?")


def orb_notify(state):
    global ORB_STATE
    ORB_STATE = state


def load_gui_settings():
    try:
        with open(GUI_SETTINGS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_gui_settings(data):
    try:
        with open(GUI_SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass


def _rgb(col):
    return "#%02x%02x%02x" % col


def _mix(c1, c2, k):
    return tuple(int(a + (b - a) * k) for a, b in zip(c1, c2))


# ----------------------------- GUI: шар-ядро (pygame) -----------------------------

def gui():
    """Ядро Джарвиса в стиле референса (pygame): облако токенов со свечением,
    подсказки-фразы, строка статуса, панель настроек по кнопке ⚙."""
    global RATE, VOLUME, CITY, VOICE_STOP
    try:
        import pygame
    except ImportError:
        print("[ для красивого GUI нужен пакет pygame ]")
        if ask_yes("установить pygame сейчас (pip install pygame)?"):
            subprocess.run([sys.executable, "-m", "pip", "install", "pygame"], check=False)
            try:
                import pygame
            except ImportError:
                print("[ pygame не установился — открываю простое tkinter-окно ]")
                gui_tk()
                return
        else:
            gui_tk()
            return

    balls_n = max(120, min(700, int(os.environ.get("JARVIS_ORB_BALLS", "400"))))
    W = max(340, int(os.environ.get("JARVIS_ORB_SIZE", "460")))
    ORB_H = int(W * 0.72)
    H = ORB_H + 108

    pygame.init()
    pygame.display.set_caption("JARVIS")
    screen = pygame.display.set_mode((W, H))
    clock = pygame.time.Clock()

    hwnd = None
    try:
        hwnd = pygame.display.get_wm_info().get("window")
    except Exception:
        pass

    def apply_topmost(on):
        if hwnd and os.name == "nt":
            try:
                ctypes.windll.user32.SetWindowPos(hwnd, -1 if on else -2, 0, 0, 0, 0, 0x0001 | 0x0002)
            except OSError:
                pass

    engine = find_tts()
    if not engine:
        print("[ предупреждение: espeak-ng не найден — озвучки не будет. "
              "Автоустановка: python jarvis.py setup voice ]")

    BG = (3, 7, 16)
    CORE = (120, 198, 255)
    DEEP = (26, 62, 104)
    LITC = (205, 240, 255)
    FG = (150, 210, 255)
    DIM = (70, 115, 150)
    ERR = (255, 130, 120)

    def font(size, bold=False):
        for name in ("consolas", "couriernew", "arial"):
            try:
                f = pygame.font.SysFont(name, size, bold=bold)
                if f:
                    return f
            except Exception:
                pass
        return pygame.font.Font(None, size)

    f_small = font(13)
    f_mid = font(15, True)
    f_btn = font(13, True)

    def dot(core_col, glow_col, r):
        size = r * 6
        c = r * 3
        surf = pygame.Surface((size, size), pygame.SRCALPHA)
        for i in range(c, r, -1):
            k = 1.0 - (i - r) / (r * 2)
            k = k ** 3
            col = tuple(int(v * k) for v in glow_col)
            pygame.draw.circle(surf, col + (255,), (c, c), i)
        pygame.draw.circle(surf, core_col + (255,), (c, c), r)
        hi = (min(255, core_col[0] + 90), min(255, core_col[1] + 80), min(255, core_col[2] + 70))
        pygame.draw.circle(surf, hi + (255,), (c, c), max(1, r - 1))
        return surf

    NB = 12
    orb_sprites = [dot(_mix(DEEP, CORE, b / (NB - 1)), _mix(DEEP, CORE, b / (NB - 1)), 1) for b in range(NB)]
    lit_sprites = [dot(_mix(CORE, LITC, b / (NB - 1)), LITC, 1) for b in range(NB)]
    spark_sprites = [dot((242, 251, 255), (170, 225, 255), 1) for b in range(NB)]
    scan_sprites = [dot((255, 255, 255), (150, 220, 255), 2), dot((210, 240, 255), LITC, 1)]
    inner_sprites = [dot(_mix(DEEP, CORE, b / 5), DEEP, 1) for b in range(6)]
    rim_soft = [dot((70, 130, 195), (52, 108, 168), 2), dot((55, 115, 180), (40, 95, 155), 1)]
    core_halo = dot((238, 250, 255), CORE, 14)
    core_haze = dot((150, 210, 255), (55, 115, 185), 34)
    halo_layer = pygame.Surface((W, W), pygame.SRCALPHA)
    c0 = W // 2
    for i in range(c0, 0, -1):
        k = 1.0 - i / c0
        pygame.draw.circle(halo_layer, (int(26 * k ** 2.2), int(62 * k ** 2.2), int(106 * k ** 2.2), 255),
                           (c0, c0), i)
    big_glow = pygame.Surface((W, W), pygame.SRCALPHA)
    for i in range(c0, 0, -1):
        k = 1.0 - i / c0
        pygame.draw.circle(big_glow, (int(30 * k ** 1.7), int(72 * k ** 1.7), int(120 * k ** 1.7), 255),
                           (c0, c0), i)

    cx = W / 2
    cy = ORB_H / 2 + 10
    radius = W * 0.33

    def form_fib(n):
        g = math.pi * (3 - 5 ** 0.5)
        out = []
        for i in range(n):
            y = 1 - 2 * (i + 0.5) / n
            r = math.sqrt(max(0.0, 1 - y * y))
            a = i * g
            out.append((r * math.cos(a), y, r * math.sin(a)))
        return out

    def form_rings(n):
        raw = []
        rows = max(6, n // 14)
        for j in range(rows):
            y = 1 - 2 * (j + 0.5) / rows
            r = math.sqrt(max(0.0, 1 - y * y))
            per = max(6, int(15 * r) + 3)
            for k in range(per):
                a = 2 * math.pi * k / per + j * 0.25
                raw.append((r * math.cos(a), y, r * math.sin(a)))
        while len(raw) < n:
            raw.append(raw[len(raw) % max(1, len(raw))])
        return raw[:n]

    def form_spiral(n):
        out = []
        turns = 5
        for i in range(n):
            s = i / max(1, n - 1)
            y = 1 - 2 * s
            r = math.sqrt(max(0.0, 1 - y * y))
            a = s * turns * 2 * math.pi
            out.append((r * math.cos(a), y, r * math.sin(a)))
        return out

    FORMS = [form_fib(balls_n), form_rings(balls_n), form_spiral(balls_n)]
    form_a = [0]
    form_b = [1]
    form_m = [1.0]
    _tnow = time.perf_counter()
    next_form = [_tnow + 10.0]
    next_scan = [_tnow + random.uniform(3, 6)]
    scan_t0 = [-10.0]
    scan_dur = [2.0]
    pts = []
    for i in range(balls_n):
        pts.append({"unit": FORMS[0][i],
                    "spark": (i % 9 == 0),
                    "ph": random.uniform(0, 6.28),
                    "amp": random.uniform(0.006, 0.02),
                    "base": random.uniform(0.72, 1.0)})
    inner_n = max(6, balls_n // 20)
    inner = [{"r": 0.35 + 0.42 * j / max(1, inner_n - 1),
              "speed": 1.3 + (j % 4) * 0.9,
              "phase": j * 1.7,
              "ax": 0.5 + 0.22 * ((j * 7) % 3)} for j in range(inner_n)]

    def project(v, cos_yaw, sin_yaw, cos_t, sin_t):
        x, y, z = v
        y2 = y * cos_t - z * sin_t
        z2 = y * sin_t + z * cos_t
        x3 = x * cos_yaw + z2 * sin_yaw
        z3 = -x * sin_yaw + z2 * cos_yaw
        p = 1.0 / (1.0 + 0.16 * z3)
        return (x3 * p, -y2 * p, z3, x3, y2, z3)

    def persist():
        save_gui_settings({"rate": RATE, "volume": VOLUME, "city": CITY})

    def set_rate(v):
        global RATE
        RATE = str(max(20, min(200, int(v))))
        os.environ["JARVIS_RATE"] = RATE
        persist()

    def set_vol(v):
        global VOLUME
        VOLUME = max(0, min(200, int(v)))
        os.environ["JARVIS_VOLUME"] = str(VOLUME)
        persist()

    def set_city(s):
        global CITY
        CITY = s.strip() or "москва"
        os.environ["JARVIS_CITY"] = CITY
        persist()

    voice_active = [False]
    voice_btn_lbl = ["🎤 голос"]

    def run_voice():
        global VOICE_STOP
        if voice_active[0]:
            VOICE_STOP = True
            voice_btn_lbl[0] = "🎤 останавливаю…"
            return
        VOICE_STOP = False
        voice_active[0] = True
        orb_notify("listen")
        voice_btn_lbl[0] = "🎤 голос: вкл — выкл"

        def worker():
            try:
                main_voice()
            finally:
                orb_notify("idle")
                voice_active[0] = False
                voice_btn_lbl[0] = "🎤 голос"

        threading.Thread(target=worker, daemon=True).start()

    history = [[]]
    last_action = [""]
    info_override = [""]

    def send(text):
        info_override[0] = "обрабатываю: " + text
        orb_notify("think")

        def worker():
            try:
                reply, new_hist = respond(text, history[0], engine)
                history[0] = new_hist
                if reply:
                    last_action[0] = "Готово: " + reply
                else:
                    last_action[0] = "Не смог выполнить, сэр"
            except Exception as exc:
                last_action[0] = f"Ошибка: {exc}"

        threading.Thread(target=worker, daemon=True).start()

    def default_hint():
        if voice_active[0]:
            return ("СЛУШАЮ", DIM)
        return ("ОЖИДАНИЕ", DIM)

    chip_labels = list(GUI_SUGGESTIONS[:3])
    chip_surfs = [f_small.render("« " + t + " »", True, DIM) for t in chip_labels]
    panel_visible = [False]
    help_visible = [False]
    top_on = [os.environ.get("JARVIS_TOPMOST", "0") == "1"]
    if top_on[0]:
        apply_topmost(True)
    city_val = [CITY]
    entry_focus = [False]
    drag = [None]
    ui = {}
    t0 = time.perf_counter()
    next_twitch = [t0 + random.uniform(2, 6)]
    twitch_t0 = [-10.0]
    last_now = [-10.0]
    scan_active = [False]

    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                if entry_focus[0]:
                    if ev.key == pygame.K_BACKSPACE:
                        city_val[0] = city_val[0][:-1]
                        set_city(city_val[0])
                    elif ev.unicode and ev.unicode not in "\r\n\x1b":
                        city_val[0] += ev.unicode
                        set_city(city_val[0])
                    elif ev.key == pygame.K_ESCAPE:
                        entry_focus[0] = False
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                mx, my = ev.pos
                if ui.get("gear") and ui["gear"].collidepoint(mx, my):
                    panel_visible[0] = not panel_visible[0]
                elif ui.get("chips") and any(r.collidepoint(mx, my) for r in ui["chips"]):
                    for r, t in zip(ui["chips"], chip_labels):
                        if r.collidepoint(mx, my):
                            entry_focus[0] = False
                            send(t)
                elif ui.get("help_sheet") and ui["help_sheet"].collidepoint(mx, my):
                    help_visible[0] = False
                    entry_focus[0] = False
                elif ui.get("combo") and ui["combo"].collidepoint(mx, my):
                    help_visible[0] = not help_visible[0]
                elif ui.get("howb") and ui["howb"].collidepoint(mx, my):
                    help_visible[0] = not help_visible[0]
                elif panel_visible[0]:
                    hit = False
                    for i, kr in enumerate(ui.get("knobs", [])):
                        if kr.collidepoint(mx, my):
                            drag[0] = i
                            hit = True
                    for i, (x0, x1, y) in enumerate(ui.get("tracks", [])):
                        if x0 - 6 <= mx <= x1 + 6 and y - 10 <= my <= y + 10 and not hit:
                            drag[0] = i
                            hit = True
                    if drag[0] is not None:
                        x0, x1, _y = ui["tracks"][drag[0]]
                        k = min(1.0, max(0.0, (mx - x0) / max(1, x1 - x0)))
                        if drag[0] == 0:
                            set_rate(20 + k * 180)
                        else:
                            set_vol(k * 200)
                    elif ui.get("entry") and ui["entry"].collidepoint(mx, my):
                        entry_focus[0] = True
                        hit = True
                    elif ui.get("check") and ui["check"].collidepoint(mx, my):
                        top_on[0] = not top_on[0]
                        apply_topmost(top_on[0])
                    elif ui.get("voice_btn") and ui["voice_btn"].collidepoint(mx, my):
                        run_voice()
                    elif ui.get("doctor_btn") and ui["doctor_btn"].collidepoint(mx, my):
                        threading.Thread(target=doctor, daemon=True).start()
                    if not hit and ui.get("sheet") and not ui["sheet"].collidepoint(mx, my):
                        entry_focus[0] = False
        if drag[0] is not None and ui.get("tracks"):
            mx, my = pygame.mouse.get_pos()
            x0, x1, _y = ui["tracks"][drag[0]]
            k = min(1.0, max(0.0, (mx - x0) / max(1, x1 - x0)))
            if drag[0] == 0:
                set_rate(20 + k * 180)
            else:
                set_vol(k * 200)

        now = time.perf_counter()
        t = now - t0
        frame_dt = min(0.1, max(0.001, now - last_now[0]))
        last_now[0] = now
        if now > next_form[0]:
            form_a[0] = form_b[0]
            form_b[0] = (form_b[0] + random.randint(1, len(FORMS) - 1)) % len(FORMS)
            form_m[0] = 0.0
            next_form[0] = now + random.uniform(9, 15)
        if form_m[0] < 1.0:
            form_m[0] = min(1.0, form_m[0] + frame_dt / 1.6)
        if scan_active[0] and (now - scan_t0[0]) / scan_dur[0] >= 1.0:
            scan_active[0] = False
        elif not scan_active[0] and now > next_scan[0] and ORB_STATE != "think":
            scan_active[0] = True
            scan_t0[0] = now
            scan_dur[0] = random.uniform(1.4, 2.2)
            next_scan[0] = now + scan_dur[0] + random.uniform(5, 10)
        if now > next_twitch[0]:
            next_twitch[0] = now + random.uniform(4, 9)
            twitch_t0[0] = now
        dt = now - twitch_t0[0]
        spring = 1.0 + 0.075 * math.exp(-2.5 * dt) * math.cos(15 * dt)
        if ORB_STATE in ("speak", "think"):
            spring *= 1.0 + 0.03 * math.sin(t * 7)
        tilt = 0.42 + 0.07 * math.sin(t * 0.25)
        ct, st = math.cos(tilt), math.sin(tilt)
        yaw = t * 0.4
        cyw, syw = math.cos(yaw), math.sin(yaw)

        thinking = ORB_STATE == "think"
        lit_half = max(1.0, balls_n / 7.0)
        lit_center = (t * balls_n / 2.0) if thinking else 0.0

        screen.fill(BG)
        gw = big_glow.get_width()
        screen.blit(big_glow, (int(cx - gw / 2), int(cy - gw / 2)), special_flags=pygame.BLEND_ADD)
        hw = halo_layer.get_width()
        screen.blit(halo_layer, (int(cx - hw / 2), int(cy - hw / 2)), special_flags=pygame.BLEND_ADD)
        hc = core_haze.get_width()
        screen.blit(core_haze, (int(cx - hc / 2), int(cy - hc / 2)), special_flags=pygame.BLEND_ADD)

        LX, LY, LZ = -0.62, 0.50, 0.60
        ln = math.sqrt(LX * LX + LY * LY + LZ * LZ)
        LX, LY, LZ = LX / ln, LY / ln, LZ / ln

        fa, fb, fm = form_a[0], form_b[0], form_m[0]
        mk = fm * fm * (3.0 - 2.0 * fm)
        ua, ub = FORMS[fa], FORMS[fb]
        scan_pos = ((now - scan_t0[0]) / scan_dur[0] * balls_n) if scan_active[0] else -99.0

        for i, p in enumerate(pts):
            sc = 1.0 + p["amp"] * math.sin(t * 0.8 + p["ph"])
            va, vb = ua[i], ub[i]
            vx = va[0] + (vb[0] - va[0]) * mk
            vy = va[1] + (vb[1] - va[1]) * mk
            vz = va[2] + (vb[2] - va[2]) * mk
            ln = math.hypot(vx, math.hypot(vy, vz)) or 1.0
            vx, vy, vz = vx / ln, vy / ln, vz / ln
            ox, oy, z, nx, ny, nz = project((vx * sc, vy * sc, vz * sc), cyw, syw, ct, st)
            lam = max(0.0, nx * LX + ny * LY + nz * LZ)
            shade = p["base"] * (0.22 + 0.42 * (z + 1) / 2 + 0.36 * lam)
            b = min(NB - 1, int(shade * NB))
            sx = int(cx + ox * radius * spring)
            sy = int(cy + oy * radius * spring)
            d_scan = abs(i - scan_pos)
            d_scan = min(d_scan, balls_n - d_scan)
            if d_scan < 1.0:
                spr = scan_sprites[0]
            elif d_scan < 4.0:
                spr = scan_sprites[1]
            elif p["spark"]:
                spr = spark_sprites[b]
            elif thinking and min(abs(i - lit_center), balls_n - abs(i - lit_center)) < lit_half:
                spr = lit_sprites[b]
            else:
                spr = orb_sprites[b]
            screen.blit(spr, (sx - spr.get_width() // 2, sy - spr.get_height() // 2),
                        special_flags=pygame.BLEND_ADD)
            rho = math.hypot(ox, oy) / radius
            if 0.94 < rho <= 1.10 and not p["spark"] and d_scan >= 4.0:
                rs = rim_soft[0] if rho <= 1.02 else rim_soft[1]
                screen.blit(rs, (sx - rs.get_width() // 2, sy - rs.get_height() // 2),
                            special_flags=pygame.BLEND_ADD)

        for j, part in enumerate(inner):
            ang = t * part["speed"] + part["phase"]
            v = (part["r"] * math.cos(ang),
                 part["r"] * math.sin(ang) * part["ax"],
                 part["r"] * math.sin(ang * 0.63))
            ox, oy, z, *_r = project(v, cyw, syw, ct, st)
            depth = (z + 1) / 2
            b = min(5, int(depth * 6))
            spr = inner_sprites[b]
            sx = int(cx + ox * radius * spring)
            sy = int(cy + oy * radius * spring)
            screen.blit(spr, (sx - spr.get_width() // 2, sy - spr.get_height() // 2),
                        special_flags=pygame.BLEND_ADD)

        cw = core_halo.get_width()
        screen.blit(core_halo, (int(cx - cw / 2), int(cy - cw / 2)), special_flags=pygame.BLEND_ADD)

        chips_x = cx - sum(s.get_width() for s in chip_surfs) / 2 - 10 * (len(chip_surfs) - 1) / 2
        chips_y = H - 26
        ui["chips"] = []
        for s in chip_surfs:
            rct = s.get_rect(topleft=(int(chips_x), int(chips_y)))
            screen.blit(s, rct)
            ui["chips"].append(rct)
            chips_x += s.get_width() + 20

        if ORB_STATE == "think":
            info_txt, info_col = "ОБРАБАТЫВАЮ…", FG
        elif ORB_STATE == "speak":
            info_txt, info_col = "ГОВОРЮ", FG
        elif ORB_STATE == "listen":
            info_txt, info_col = "СЛУШАЮ", DIM
        elif info_override[0] and not last_action[0]:
            info_txt, info_col = info_override[0][:62], DIM
        elif last_action[0]:
            info_txt, info_col = last_action[0][:62], DIM
        else:
            info_txt, info_col = default_hint()
        surf = f_mid.render(info_txt, True, info_col)
        screen.blit(surf, surf.get_rect(center=(int(cx), H - 70)))
        hdr = f_small.render("Попробуй сказать", True, DIM)
        screen.blit(hdr, hdr.get_rect(center=(int(cx), H - 48)))

        ui["gear"] = pygame.Rect(W - 30, 8, 22, 22)
        gear_s = f_btn.render("⚙", True, FG if panel_visible[0] else DIM)
        screen.blit(gear_s, (W - 26, 8))

        combo_s = f_small.render("Джарвис  ▾", True, FG)
        cr = combo_s.get_rect(topleft=(14, 12))
        box = cr.inflate(18, 10)
        pygame.draw.rect(screen, (10, 28, 46), box, border_radius=5)
        pygame.draw.rect(screen, (30, 70, 105), box, 1, border_radius=5)
        screen.blit(combo_s, cr)
        ui["combo"] = box
        how_s = f_small.render("Как начать", True, FG)
        hr = how_s.get_rect(topleft=(box.right + 10, 17))
        hbox = hr.inflate(16, 10)
        pygame.draw.rect(screen, (10, 28, 46), hbox, border_radius=5)
        pygame.draw.rect(screen, (30, 70, 105), hbox, 1, border_radius=5)
        screen.blit(how_s, hr)
        ui["howb"] = hbox

        if help_visible[0]:
            hs = pygame.Surface((W - 60, 86), pygame.SRCALPHA)
            hs.fill((8, 20, 34, 224))
            screen.blit(hs, (30, 40))
            ui["help_sheet"] = pygame.Rect(30, 40, W - 60, 86)
            for li, ltxt in enumerate((
                    "1. жми 🎤 (в ⚙) и говори: «джарвис, + команда»",
                    "2. или кликай фразы внизу — выполняется сразу",
                    "3. ⚙ — скорость, громкость, город, поверх всех")):
                lf = f_small.render(ltxt, True, FG)
                screen.blit(lf, (42, 52 + li * 22))

        if panel_visible[0]:
            sheet = pygame.Surface((W - 16, 148), pygame.SRCALPHA)
            sheet.fill((8, 20, 34, 216))
            screen.blit(sheet, (8, 8))
            ui["sheet"] = pygame.Rect(8, 8, W - 16, 148)

            x0, x1 = 24, W - 104
            ui["tracks"] = [(x0, x1, 46), (x0, x1, 82)]
            ui["knobs"] = []
            lab = f_small.render(f"скорость речи  {int(RATE)}", True, FG)
            screen.blit(lab, (x0, 24))
            pygame.draw.rect(screen, (26, 60, 92), (x0, 44, x1 - x0, 4), border_radius=2)
            k = (int(RATE) - 20) / 180.0
            kx = x0 + (x1 - x0) * k
            pygame.draw.circle(screen, FG, (int(kx), 46), 7)
            ui["knobs"].append(pygame.Rect(int(kx) - 9, 37, 18, 18))
            lab = f_small.render(f"громкость  {int(VOLUME)}", True, FG)
            screen.blit(lab, (x0, 62))
            pygame.draw.rect(screen, (26, 60, 92), (x0, 80, x1 - x0, 4), border_radius=2)
            k = VOLUME / 200.0
            kx = x0 + (x1 - x0) * k
            pygame.draw.circle(screen, FG, (int(kx), 82), 7)
            ui["knobs"].append(pygame.Rect(int(kx) - 9, 73, 18, 18))

            ui["entry"] = pygame.Rect(x0, 98, 150, 22)
            pygame.draw.rect(screen, (10, 30, 50), ui["entry"], border_radius=4)
            pygame.draw.rect(screen, FG if entry_focus[0] else (26, 60, 92), ui["entry"], 1, border_radius=4)
            caret = "|" if (entry_focus[0] and int(t * 2) % 2 == 0) else ""
            city_s = f_small.render((city_val[0] or "город погоды...") + caret, True, FG if city_val[0] else DIM)
            screen.blit(city_s, (ui["entry"].x + 6, ui["entry"].y + 4))

            ui["check"] = pygame.Rect(W - 150, 98, 22, 22)
            pygame.draw.rect(screen, FG if top_on[0] else (26, 60, 92), ui["check"], 1, border_radius=4)
            if top_on[0]:
                pygame.draw.rect(screen, FG, (ui["check"].x + 4, ui["check"].y + 4, 14, 14), border_radius=3)
            chk = f_small.render("поверх всех", True, FG)
            screen.blit(chk, (W - 122, 102))

            ui["voice_btn"] = pygame.Rect(24, 126, 120, 22)
            pygame.draw.rect(screen, (14, 42, 68), ui["voice_btn"], border_radius=6)
            vb = f_btn.render(voice_btn_lbl[0], True, FG)
            screen.blit(vb, vb.get_rect(center=ui["voice_btn"].center))
            ui["doctor_btn"] = pygame.Rect(152, 126, 84, 22)
            pygame.draw.rect(screen, (14, 42, 68), ui["doctor_btn"], border_radius=6)
            db = f_btn.render("doctor", True, FG)
            screen.blit(db, db.get_rect(center=ui["doctor_btn"].center))

        pygame.display.flip()
        clock.tick(40)

    VOICE_STOP = True
    orb_notify("idle")
    pygame.quit()

def gui_tk():
    """Окно-оверлей: шар из «токенов» + статус + подсказки фраз (tkinter, без зависимостей)."""
    global RATE, VOLUME, CITY, VOICE_STOP
    try:
        import tkinter as tk
    except (ImportError, OSError) as exc:
        print(f"[ GUI недоступно: {exc} — работает текстовый режим ]")
        main_text()
        return

    balls_n = max(60, min(260, int(os.environ.get("JARVIS_ORB_BALLS", "150"))))
    win_size = max(260, int(os.environ.get("JARVIS_ORB_SIZE", "420")))

    saved = load_gui_settings()
    if "rate" in saved:
        RATE = str(saved["rate"])
        os.environ["JARVIS_RATE"] = RATE
    if "volume" in saved:
        VOLUME = max(0, min(200, int(saved["volume"])))
        os.environ["JARVIS_VOLUME"] = str(VOLUME)
    if saved.get("city"):
        CITY = str(saved["city"])
        os.environ["JARVIS_CITY"] = CITY

    root = tk.Tk()
    root.title("JARVIS")
    root.configure(bg=GUI_BG)
    root.resizable(False, False)

    engine = find_tts()
    if not engine:
        print("[ предупреждение: espeak-ng не найден — слова будут только текстом. "
              "Автоустановка: python jarvis.py setup voice ]")

    canvas = tk.Canvas(root, width=win_size, height=win_size, bg=GUI_BG, highlightthickness=0)
    canvas.pack(fill="x")

    cx = cy = win_size / 2.0
    radius = win_size * 0.37
    for grow, shade in ((0.98, "#071523"), (0.80, "#05121d")):
        canvas.create_oval(cx - radius * grow, cy - radius * grow,
                           cx + radius * grow, cy + radius * grow, fill=shade, outline="")

    info = tk.Label(root, text="ОЖИДАНИЕ", bg=GUI_BG, fg=GUI_FG_DIM,
                    font=("Consolas", 10, "bold"))
    info.pack(pady=(0, 2))
    last_action = [""]

    chips = tk.Frame(root, bg=GUI_BG)
    chips.pack(pady=(0, 8))

    panel = tk.Frame(root, bg=GUI_BG)
    rate_var = tk.IntVar(value=int(RATE))
    vol_var = tk.IntVar(value=VOLUME)
    city_var = tk.StringVar(value=CITY)
    top_var = tk.BooleanVar(value=False)

    def changed(*_):
        global RATE, VOLUME, CITY
        RATE = str(rate_var.get())
        os.environ["JARVIS_RATE"] = RATE
        VOLUME = max(0, min(200, vol_var.get()))
        os.environ["JARVIS_VOLUME"] = str(VOLUME)
        CITY = city_var.get().strip() or "москва"
        os.environ["JARVIS_CITY"] = CITY
        save_gui_settings({"rate": RATE, "volume": VOLUME, "city": CITY})

    def on_top(*_):
        try:
            root.attributes("-topmost", bool(top_var.get()))
        except OSError:
            pass

    def _line(text):
        tk.Label(panel, text=text, bg=GUI_BG, fg=GUI_FG, font=("Consolas", 9)).pack(anchor="w", padx=6)

    _line("скорость речи (words/min)")
    tk.Scale(panel, from_=20, to=200, orient="horizontal", variable=rate_var, command=changed,
             bg=GUI_BG, fg=GUI_FG, highlightthickness=0, troughcolor="#0a1a2b", length=280).pack(padx=8)
    _line("громкость озвучки (0-200)")
    tk.Scale(panel, from_=0, to=200, orient="horizontal", variable=vol_var, command=changed,
             bg=GUI_BG, fg=GUI_FG, highlightthickness=0, troughcolor="#0a1a2b", length=280).pack(padx=8)
    _line("город погоды")
    tk.Entry(panel, textvariable=city_var, bg="#0a1a2b", fg=GUI_FG, insertbackground=GUI_FG,
             relief="flat", width=22).pack(anchor="w", padx=8, pady=(0, 2))
    tk.Checkbutton(panel, text="окно поверх всех", variable=top_var, command=on_top,
                   bg=GUI_BG, fg=GUI_FG, activebackground=GUI_BG, activeforeground=GUI_FG,
                   selectcolor="#0a1a2b", relief="flat", highlightthickness=0,
                   font=("Consolas", 9)).pack(anchor="w", padx=6)

    voice_active = [False]
    voice_btn = None

    def run_voice():
        global VOICE_STOP
        if voice_active[0]:
            VOICE_STOP = True
            voice_btn.config(text="🎤 останавливаю…")
            return
        VOICE_STOP = False
        voice_active[0] = True
        orb_notify("listen")
        voice_btn.config(text="🎤 голос: вкл — выключить")

        def worker():
            try:
                main_voice()
            finally:
                orb_notify("idle")
                voice_active[0] = False
                root.after(0, lambda: voice_btn.config(text="🎤 голос"))

        threading.Thread(target=worker, daemon=True).start()

    voice_btn = tk.Button(panel, text="🎤 голос", command=run_voice, bg="#0a1a2b", fg=GUI_FG,
                          activebackground="#123a5c", activeforeground=GUI_FG, relief="flat",
                          font=("Consolas", 10), width=14)
    voice_btn.pack(side="left", padx=(6, 3), pady=6)
    tk.Button(panel, text="doctor", command=lambda: threading.Thread(target=doctor, daemon=True).start(),
              bg="#0a1a2b", fg=GUI_FG, activebackground="#123a5c", activeforeground=GUI_FG,
              relief="flat", font=("Consolas", 10), width=8).pack(side="left", padx=3, pady=6)

    def toggle_panel():
        if panel.winfo_ismapped():
            panel.pack_forget()
        else:
            panel.pack(fill="x", padx=10, pady=(0, 8))

    tk.Button(root, text="⚙", command=toggle_panel, bg=GUI_BG, fg=GUI_FG_DIM,
              activebackground=GUI_BG, activeforeground=GUI_FG, relief="flat",
              font=("Consolas", 12)).place(relx=1.0, x=-6, y=4, anchor="ne")

    history = [[]]

    def send(text):
        info.config(text=f"обрабатываю: {text}", fg=GUI_FG)
        orb_notify("think")

        def worker():
            try:
                reply, new_hist = respond(text, history[0], engine)
                history[0] = new_hist
                if reply:
                    last_action[0] = "Готово: " + reply
                    root.after(0, lambda: info.config(
                        text="Готово: " + reply[:90], fg=GUI_FG_DIM))
                else:
                    root.after(0, lambda: info.config(
                        text="не смог выполнить, сэр", fg=GUI_ERR))
            except Exception as exc:
                root.after(0, lambda: info.config(text=f"ошибка: {exc}", fg=GUI_ERR))

        threading.Thread(target=worker, daemon=True).start()

    def default_hint():
        return "СЛУШАЮ" if voice_active[0] else "ОЖИДАНИЕ"

    for text in GUI_SUGGESTIONS:
        tk.Button(chips, text="«" + text + "»", command=lambda t=text: send(t),
                  bg="#0a1a2b", fg=GUI_FG_DIM, activebackground="#123a5c",
                  activeforeground=GUI_FG, relief="flat", font=("Consolas", 9),
                  bd=0, padx=6).pack(side="left", padx=3)

    golden = math.pi * (3 - 5 ** 0.5)
    pts = []
    for i in range(balls_n):
        y = 1 - 2 * (i + 0.5) / balls_n
        r = math.sqrt(max(0.0, 1 - y * y))
        a = i * golden
        warm = (i % 13 == 0)
        pts.append({"v": (r * math.cos(a), y, r * math.sin(a)),
                    "warm": warm,
                    "ph": random.uniform(0, 6.28),
                    "amp": random.uniform(2.0, 5.5)})
    inner_n = max(5, balls_n // 12)
    inner = [{"r": 0.35 + 0.42 * j / max(1, inner_n - 1),
              "speed": 1.3 + (j % 4) * 0.9,
              "phase": j * 1.7,
              "ax": 0.5 + 0.22 * ((j * 7) % 3)}
             for j in range(inner_n)]

    ids = [canvas.create_oval(-1, -1, -1, -1, fill="", outline="") for _ in pts]
    inner_ids = [canvas.create_oval(-1, -1, -1, -1, fill="", outline="") for _ in inner]
    last_col = [None] * (balls_n + inner_n)

    def project(v, cos_yaw, sin_yaw, cos_t, sin_t):
        x, y, z = v
        y2 = y * cos_t - z * sin_t
        z2 = y * sin_t + z * cos_t
        return (x * cos_yaw + z2 * sin_yaw, -y2, -x * sin_yaw + z2 * cos_yaw)

    t0 = time.perf_counter()
    next_twitch = [time.perf_counter() + random.uniform(2, 6)]
    twitch_t0 = [-10.0]

    def draw():
        now = time.perf_counter()
        t = now - t0
        if now > next_twitch[0]:
            next_twitch[0] = now + random.uniform(4, 9)
            twitch_t0[0] = now
        dt = now - twitch_t0[0]
        spring = 1.0 + 0.085 * math.exp(-2.5 * dt) * math.cos(15 * dt)
        if ORB_STATE in ("speak", "think"):
            spring *= 1.0 + 0.03 * math.sin(t * 7)
        tilt = 0.42 + 0.07 * math.sin(t * 0.25)
        ct, st = math.cos(tilt), math.sin(tilt)
        yaw = t * 0.4
        cyw, syw = math.cos(yaw), math.sin(yaw)

        thinking = ORB_STATE == "think"
        lit_half = max(1.0, balls_n / 7.0)
        lit_center = (t * balls_n / 2.0) if thinking else 0.0

        for i, p in enumerate(pts):
            jitter = p["amp"] * math.sin(t * 0.9 + p["ph"])
            vx, vy, vz = p["v"]
            k = 1.0 + jitter / max(0.01, math.sqrt(vx * vx + vy * vy + vz * vz) or 1.0)
            ox, oy, z = project((vx * k, vy * k, vz * k), cyw, syw, ct, st)
            depth = (z + 1) / 2
            size = (1.6 + 4.4 * depth) * spring
            sx = cx + ox * radius * spring
            sy = cy + oy * radius * spring
            col = _mix(ORB_WARM, (60, 42, 30), depth) if p["warm"] else _mix(ORB_DEEP, ORB_CORE, depth)
            if thinking and not p["warm"]:
                d = min(abs(i - lit_center), balls_n - abs(i - lit_center))
                kk = 0.75 * max(0.0, 1.0 - d / lit_half)
                if kk > 0:
                    col = _mix(col, ORB_LIT, kk)
            canvas.coords(ids[i], sx - size, sy - size, sx + size, sy + size)
            if last_col[i] != col:
                canvas.itemconfig(ids[i], fill=_rgb(col), outline="")
                last_col[i] = col

        for j, part in enumerate(inner):
            ang = t * part["speed"] + part["phase"]
            v = (part["r"] * math.cos(ang),
                 part["r"] * math.sin(ang) * part["ax"],
                 part["r"] * math.sin(ang * 0.63))
            ox, oy, z = project(v, cyw, syw, ct, st)
            depth = (z + 1) / 2
            size = (1.2 + 2.2 * depth) * spring
            sx = cx + ox * radius * spring
            sy = cy + oy * radius * spring
            col = _mix(_mix(ORB_DEEP, ORB_CORE, depth), GUI_BG_RGB, 0.35)
            n2 = balls_n + j
            canvas.coords(inner_ids[j], sx - size, sy - size, sx + size, sy + size)
            if last_col[n2] != col:
                canvas.itemconfig(inner_ids[j], fill=_rgb(col), outline="")
                last_col[n2] = col

        if ORB_STATE == "think":
            want, fg = "ОБРАБАТЫВАЮ…", GUI_FG
        elif ORB_STATE == "speak":
            want, fg = "ГОВОРЮ", GUI_FG
        elif ORB_STATE == "listen":
            want, fg = "СЛУШАЮ", GUI_FG_DIM
        elif last_action[0]:
            want, fg = last_action[0][:90], GUI_FG_DIM
        else:
            want, fg = default_hint(), GUI_FG_DIM
        if info.cget("text") != want:
            info.config(text=want, fg=fg)
        root.after(33, draw)

    def safe_draw():
        try:
            draw()
        except Exception:
            root.after(200, safe_draw)

    safe_draw()
    root.mainloop()

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
            if len(sys.argv) > 2 and sys.argv[2].lower() in ("voice", "tts", "голос", "озвучка"):
                setup_voice()
            else:
                setup_engine()
        elif len(sys.argv) > 1 and sys.argv[1].lower() in ("doctor", "diag", "доктор"):
            doctor()
        elif len(sys.argv) > 1 and sys.argv[1].lower() in ("gui", "шар", "окно"):
            gui()
        elif len(sys.argv) > 1 and sys.argv[1].lower() in ("voice", "-v", "голос", "голосовой"):
            main_voice()
        else:
            main_text()
    except KeyboardInterrupt:
        print(f"\nJARVIS отключён, {MGMT}.")
