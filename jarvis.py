#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JARVIS Lite — сверхлёгкий голосовой ассистент в стиле J.A.R.V.I.S.

Текстовый режим:   python jarvis.py
Голосовой режим:   python jarvis.py voice   (нужны pip-пакеты vosk и sounddevice)

Ядро — только стандартная библиотека Python, ни одного pip-пакета.
Озвучка — espeak-ng. Ответы — Mistral API, либо локальная Ollama как запасной вариант.
"""

import atexit
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


# ------------- Прогноз следующего действия (usage.json) -------------
USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.json")
PENDING_PREDICT = {}


def load_usage():
    try:
        with open(USAGE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        ev = data.get("events", [])
        if isinstance(ev, list):
            return [e for e in ev if isinstance(e, list) and len(e) == 3][-400:]
    except (OSError, ValueError):
        pass
    return []


def save_usage(events):
    tmp = USAGE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"events": events[-400:]}, fh, ensure_ascii=False)
        os.replace(tmp, USAGE_FILE)
    except OSError:
        pass


def norm_action_key(q):
    return re.sub(r"[!?.…,\s]+", " ", fix_recognition(q.strip().lower())).strip()


def log_action(query):
    key = norm_action_key(query)
    if not key:
        return
    ev = load_usage()
    ev.append([round(time.time()), key, query.strip()[:80]])
    save_usage(ev)


def predict_next():
    """-> (запрос, обоснование) предугаданного следующего действия."""
    ev = load_usage()
    if len(ev) < 4:
        return "", ""
    now = time.time()
    seq = {}
    last_key = ev[-1][1]
    for i in range(len(ev) - 1):
        if ev[i][1] == last_key and 0 < ev[i + 1][0] - ev[i][0] <= 2700:
            q2 = ev[i + 1][2]
            seq[q2] = seq.get(q2, 0) + 1
    if seq:
        cand, cnt = max(seq.items(), key=lambda kv: kv[1])
        if cnt >= 2:
            return cand, "часто идёт после «" + last_key[:40] + "»"
    h = time.localtime().tm_hour
    per = {}
    for ts, _k, q in ev:
        if abs((time.localtime(ts).tm_hour - h + 12) % 24 - 12) <= 1:
            per[q] = per.get(q, 0) + 1
    for q, cnt in sorted(per.items(), key=lambda kv: -kv[1]):
        if cnt >= 3 and not any(now - ts < 14400 and qq == q for ts, _k, qq in ev):
            return q, "обычно в это время"
    return "", ""


def maybe_predict():
    q, why = predict_next()
    if q:
        PENDING_PREDICT["q"] = q
        return q, why
    PENDING_PREDICT.clear()
    return "", ""


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
    conf = query.strip().lower()
    if re.fullmatch(r"(?:!)?(да|давай|ага|угу|го|ок|окей|ну да)", conf) and PENDING_PREDICT.get("q"):
        query = PENDING_PREDICT.pop("q")
        PENDING_PREDICT.clear()

    custom = match_custom_command(query)
    if custom:
        custom_reply = run_custom_command(custom)
        if custom_reply:
            log_action(query)
            print("JARVIS > " + custom_reply)
            speak(engine, custom_reply)
            return custom_reply, history

    hotkey_reply = handle_hotkeys(query, engine)
    if hotkey_reply:
        log_action(query)
        return hotkey_reply, history

    action_reply = handle_action(query, engine)
    if action_reply:
        log_action(query)
        return action_reply, history

    memory_reply = handle_memory(query, engine)
    if memory_reply:
        log_action(query)
        return memory_reply, history

    remind_reply = reminder_reply(query, engine)
    if remind_reply:
        log_action(query)
        print("JARVIS > " + remind_reply)
        speak(engine, remind_reply)
        return remind_reply, history

    w_reply = weather_reply(query)
    if w_reply:
        log_action(query)
        print("JARVIS > " + w_reply)
        speak(engine, w_reply)
        return w_reply, history

    answer = instant_command(query)
    if answer:
        log_action(query)
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
        pq, why = maybe_predict()
        if pq:
            print(f"[ прогноз: «{pq}» ({why}) — «да», чтобы выполнить ]")

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
    print(f"=== JARVIS doctor ({APP_VERSION}) ===")
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
OVERLAY_STOP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hud.stop")
HUD_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hud.log")
APP_VERSION = "v19-tk"


def _hud_log(msg):
    try:
        with open(HUD_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass
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


CUSTOM_COMMANDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_commands.json")
CUSTOM_TYPES = ("site", "app", "say", "cmd")
CUSTOM_TYPE_LABELS = {"site": "сайт", "app": "программа", "say": "фраза", "cmd": "команда"}


def load_custom_commands():
    try:
        with open(CUSTOM_COMMANDS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for c in (data if isinstance(data, list) else []):
        if isinstance(c, dict) and c.get("phrase") and c.get("param") is not None:
            out.append(c)
    return out


def save_custom_commands(cmds):
    try:
        with open(CUSTOM_COMMANDS_FILE, "w", encoding="utf-8") as fh:
            json.dump(cmds, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass


def match_custom_command(query):
    q = re.sub(r"[!?.…,;\s]+", " ", str(query or "").strip().lower()).strip()
    if not q:
        return None
    best, best_score = None, 0.0
    for c in load_custom_commands():
        for raw in str(c.get("phrase", "")).lower().split(","):
            phrase = raw.strip()
            if not phrase:
                continue
            if q == phrase:
                score = 1.0
            elif phrase in q:
                score = max(best_score, 0.95)
            elif q in phrase:
                score = max(best_score, 0.88)
            else:
                score = difflib.SequenceMatcher(None, q, phrase).ratio()
            if score > best_score:
                best, best_score = c, score
    return best if best_score >= 0.82 else None


def run_custom_command(c):
    typ = c.get("type", "site")
    param = str(c.get("param", "")).strip()
    if typ == "site":
        open_url(param)
        return f"Открываю {param}, {MGMT}."
    if typ == "app":
        if open_app(param):
            return f"Открываю {param}, {MGMT}."
        return f"Не нашёл «{param}», {MGMT}."
    if typ == "say":
        return param or None
    if typ == "cmd":
        reply = instant_command(param)
        return reply or f"Команда «{param}» мне неизвестна, {MGMT}."
    return None


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

    _gs = load_gui_settings()
    balls_n = max(120, min(700, int(os.environ.get("JARVIS_ORB_BALLS", str(int(_gs.get("balls", 400)))))))
    W = max(760, int(os.environ.get("JARVIS_ORB_SIZE", "1120")))
    H = max(560, int(os.environ.get("JARVIS_ORB_HEIGHT", str(int(W * 0.6)))))
    SW = 210
    cx = SW + (W - SW) * 0.47
    cy = int(H * 0.44)
    radius = min(W - SW, H - 130) * 0.36
    ball_r = max(1, int(round(radius / 150.0)))

    pygame.init()
    pygame.display.set_caption("JARVIS")
    screen = pygame.display.set_mode((W, H))
    clock = pygame.time.Clock()

    def build_bg(w, h, gcx, gcy):
        layer = pygame.Surface((w, h))
        top, bot = (5, 14, 16), (3, 8, 11)
        for y in range(h):
            k = y / max(1, h - 1)
            col = (int(top[0] + (bot[0] - top[0]) * k),
                   int(top[1] + (bot[1] - top[1]) * k),
                   int(top[2] + (bot[2] - top[2]) * k))
            pygame.draw.line(layer, col, (0, y), (w, y))
        glow = pygame.Surface((w, h), pygame.SRCALPHA)
        gr = int(max(w, h) * 0.52)
        for i in range(gr, 0, -1):
            k = (1.0 - i / gr) ** 2
            pygame.draw.circle(glow, (int(14 * k), int(50 * k), int(48 * k), 255), (gcx, gcy), i)
        layer.blit(glow, (0, 0), special_flags=pygame.BLEND_ADD)
        sheen = pygame.Surface((w * 2, h * 2), pygame.SRCALPHA)
        pygame.draw.line(sheen, (26, 56, 58, 255), (0, h * 2 - 40), (w * 2 - 60, 0), 26)
        pygame.draw.line(sheen, (16, 36, 38, 255), (40, h * 2 - 20), (w * 2 - 20, 40), 12)
        band = pygame.transform.rotozoom(sheen, 0, 0.5)
        band = pygame.transform.smoothscale(band, (w, h))
        layer.blit(band, (0, 0), special_flags=pygame.BLEND_ADD)
        return layer

    bg_layer = build_bg(W, H, int(cx), int(cy))

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
    f_logo = font(26, True)

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
    orb_sprites = [dot(_mix(DEEP, CORE, b / (NB - 1)), _mix(DEEP, CORE, b / (NB - 1)), ball_r) for b in range(NB)]
    lit_sprites = [dot(_mix(CORE, LITC, b / (NB - 1)), LITC, ball_r) for b in range(NB)]
    spark_sprites = [dot((242, 251, 255), (170, 225, 255), ball_r) for b in range(NB)]
    scan_sprites = [dot((255, 255, 255), (150, 220, 255), ball_r * 2), dot((210, 240, 255), LITC, ball_r)]
    inner_sprites = [dot(_mix(DEEP, CORE, b / 5), DEEP, ball_r) for b in range(6)]
    rim_soft = [dot((70, 130, 195), (52, 108, 168), ball_r * 2), dot((55, 115, 180), (40, 95, 155), ball_r)]
    core_halo = dot((238, 250, 255), CORE, 14)
    core_haze = dot((150, 210, 255), (55, 115, 185), 34)
    HG = int(radius * 3.2)
    halo_layer = pygame.Surface((HG, HG), pygame.SRCALPHA)
    c0 = HG // 2
    for i in range(c0, 0, -1):
        k = 1.0 - i / c0
        pygame.draw.circle(halo_layer, (int(26 * k ** 2.2), int(62 * k ** 2.2), int(106 * k ** 2.2), 255),
                           (c0, c0), i)
    big_glow = pygame.Surface((HG, HG), pygame.SRCALPHA)
    for i in range(c0, 0, -1):
        k = 1.0 - i / c0
        pygame.draw.circle(big_glow, (int(30 * k ** 1.7), int(72 * k ** 1.7), int(120 * k ** 1.7), 255),
                           (c0, c0), i)

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

    _COAST_DATA = "-586 -642,-606 -643,-625 -651,-626 -659,-637 -665,-655 -676,-648 -687,-628 -696,-623 -704,-614 -720,-607 -732,-620 -744,-644 -753,-684 -760,-706 -766,-756 -767,-754 -773,-748 -782,-780 -788,-768 -795,-732 -804,-682 -813,-498 -817,-422 -817,-382 -813,-323 -808,-293 -800,-316 -793,-359 -791,-353 -781,-310 -774,-275 -765,-239 -762,-200 -757,-166 -748,-154 -741,-154 -731,-123 -724,-103 -713,-74 -717,-69 -709,-43 -715,-7 -712,19 -711,52 -706,71 -702,95 -700,120 -706,147 -700,170 -699,193 -699,219 -704,248 -705,281 -703,310 -698,328 -694,349 -687,372 -692,397 -695,420 -686,449 -681,465 -676,490 -671,509 -665,536 -659,564 -660,573 -667,599 -674,624 -680,650 -676,679 -679,697 -690,686 -699,691 -707,679 -719,710 -721,719 -713,733 -704,756 -697,781 -691,801 -681,821 -674,838 -673,868 -672,884 -665,906 -672,935 -672,950 -672,978 -672,1004 -669,1028 -656,1049 -663,1072 -670,1102 -667,1129 -661,1149 -664,1174 -669,1198 -673,1223 -666,1252 -667,1279 -667,1308 -664,1339 -663,1350 -657,1359 -660,1375 -670,1408 -668,1444 -668,1462 -672,1477 -681,1515 -687,1543 -686,1568 -694,1592 -696,1616 -706,1649 -708,1684 -710,1712 -717,1706 -724,1693 -737,1661 -744,1642 -755,1635 -767,1643 -778,1666 -783,1637 -791,1607 -802,1611 -813,-1553 -814,-1544 -812,-1489 -810,-1468 -799,-1516 -793,-1560 -787,-1581 -780,-1570 -773,-1529 -775,-1487 -769,-1461 -761,-1462 -754,-1428 -753,-1389 -750,-1352 -743,-1323 -743,-1296 -745,-1254 -745,-1211 -745,-1175 -740,-1139 -737,-1123 -747,-1101 -748,-1061 -751,-1020 -751,-1008 -745,-1031 -737,-1037 -726,-1003 -728,-977 -736,-937 -733,-901 -733,-884 -730,-852 -735,-815 -739,-793 -735,-762 -740,-739 -737,-702 -731,-674 -725,-676 -712,-685 -701,-680 -690,-674 -681,-673 -669,-654 -659,-636 -649,-614 -643,-599 -640,-578 -633,-586 -642;501 -136,499 -165,475 -238,437 -236,444 -201,449 -162,479 -137,501 -136;1436 -138,1459 -169,1487 -206,1521 -245,1531 -304,1507 -352,1469 -386,1422 -384,1384 -351,1378 -329,1343 -326,1251 -327,1206 -339,1156 -344,1152 -306,1135 -265,1136 -247,1146 -218,1188 -203,1223 -178,1244 -156,1266 -140,1303 -134,1324 -111,1365 -119,1354 -147,1393 -174,1416 -143,1421 -110,1436 -138;1341 -12,1392 -21,1453 -44,1470 -67,1500 -97,1489 -103,1447 -76,1421 -92,1380 -76,1337 -35,1320 -28,1318 -16,1341 -12;1252 14,1200 -5,1228 -9,1232 -47,1215 -46,1204 -55,1191 -35,1200 6,1252 14;1058 -59,1014 -28,986 18,953 55,991 36,1031 6,1045 -18,1058 -59;1179 18,1165 -25,1138 -34,1101 -16,1097 20,1137 39,1171 69,1191 50,1179 18;1410 371,1390 347,1351 346,1320 331,1304 323,1309 342,1357 355,1394 382,1414 414,1410 371;-30 586,-31 560,5 529,14 513,-36 502,-34 514,-46 535,-48 548,-56 563,-30 586;-905 695,-863 679,-812 687,-858 666,-907 636,-932 588,-873 560,-821 533,-798 547,-785 588,-747 622,-696 602,-646 603,-596 552,-564 518,-654 503,-703 470,-648 470,-598 459,-662 445,-691 440,-701 418,-719 413,-740 406,-750 392,-754 380,-765 387,-759 366,-791 335,-813 300,-801 258,-822 267,-851 296,-892 303,-898 293,-938 297,-974 267,-979 224,-959 188,-914 189,-885 215,-874 195,-881 183,-884 165,-882 157,-869 158,-852 159,-834 153,-835 136,-837 116,-822 92,-805 91,-781 92,-757 98,-726 117,-714 115,-713 91,-699 122,-662 106,-627 104,-598 84,-571 60,-529 54,-500 17,-478 -6,-415 -29,-352 -55,-377 -122,-396 -183,-431 -230,-486 -266,-516 -318,-557 -348,-572 -353,-612 -389,-638 -412,-635 -426,-676 -463,-687 -503,-699 -525,-749 -523,-741 -469,-734 -421,-735 -383,-717 -309,-702 -198,-760 -146,-794 -79,-811 -40,-808 -20,-795 10,-779 27,-773 58,-784 84,-805 81,-812 76,-829 81,-839 93,-851 96,-859 109,-877 129,-879 131,-906 139,-953 161,-997 167,-1039 187,-1053 211,-1079 245,-1104 272,-1128 300,-1149 314,-1133 288,-1113 257,-1094 234,-1117 245,-1136 266,-1142 281,-1167 316,-1191 341,-1225 376,-1242 411,-1247 482,-1228 490,-1279 523,-1320 555,-1378 585,-1471 609,-1517 592,-1526 601,-1566 570,-1631 547,-1606 560,-1570 589,-1604 591,-1638 598,-1649 626,-1608 638,-1628 643,-1667 661,-1644 676,-1629 699,-1551 711,-1497 705,-1410 697,-1329 695,-1274 704,-1215 698,-1153 679,-1082 687,-1015 676,-961 673,-964 712,-905 695;-1142 731,-1111 725,-1077 721,-1065 731,-1028 705,-1027 695,-1042 689,-1120 686,-1152 693,-1167 701,-1144 706,-1184 709,-1186 723,-1142 731;-866 732,-823 738,-778 727,-741 713,-679 701,-664 681,-622 662,-667 664,-671 651,-650 627,-674 629,-710 629,-734 642,-786 646,-740 655,-727 673,-769 689,-782 698,-849 700,-895 708,-894 731,-866 732;-1082 762,-1059 760,-1097 748,-1139 747,-1177 752,-1126 761,-1105 764,-1082 762;575 707,537 708,515 720,544 736,559 746,579 756,662 768,682 762,616 753,570 733,575 707;-947 771,-907 764,-878 756,-828 758,-805 747,-861 744,-924 748,-960 764,-947 771;1070 770,1139 753,1130 740,1232 730,1285 720,1356 717,1495 722,1597 697,1696 687,1786 694,1789 633,1737 617,1658 602,1621 578,1604 543,1560 532,1602 593,1601 605,1513 588,1390 571,1399 542,1401 484,1335 428,1300 419,1280 400,1292 374,1265 344,1257 379,1250 385,1247 397,1214 398,1180 392,1208 379,1197 356,1219 309,1211 281,1148 227,1108 214,1081 216,1083 161,1064 95,1031 112,1000 123,1005 74,1034 49,1039 25,1013 33,997 68,983 90,981 136,948 158,931 199,905 228,894 220,865 202,817 163,799 121,779 83,749 127,728 204,693 228,629 252,570 270,515 279,480 300,495 271,507 250,516 252,534 242,565 263,574 239,598 223,578 202,565 181,542 170,512 152,467 134,445 127,431 141,428 163,409 195,385 237,366 258,348 290,339 276,333 277,355 238,371 198,398 154,433 124,441 104,484 114,511 120,501 81,456 20,409 -21,392 -47,393 -80,404 -118,395 -167,348 -198,355 -231,342 -248,326 -275,306 -304,259 -337,226 -339,189 -344,182 -324,152 -271,143 -221,117 -173,127 -131,131 -98,122 -63,94 -21,93 12,85 45,54 49,19 61,-33 50,-77 44,-117 69,-141 99,-157 115,-168 132,-165 161,-164 196,-166 222,-148 251,-117 281,-93 326,-52 358,-1 359,63 371,102 372,109 357,115 331,166 312,201 322,239 320,289 309,322 313,345 316,359 354,340 362,287 367,273 404,352 420,417 420,375 447,391 470,350 463,339 444,333 461,296 450,280 420,264 402,244 401,228 397,231 379,213 376,200 399,192 420,152 442,137 451,123 446,162 417,185 402,171 389,161 390,136 412,97 440,46 434,7 407,-14 374,-54 359,-84 370,-94 394,-90 419,-43 434,-30 476,-10 493,47 531,81 535,83 568,103 569,99 550,136 541,187 544,216 574,241 583,269 594,229 598,224 638,212 650,188 601,141 554,84 583,86 635,192 698,313 705,403 679,332 666,365 648,430 664,463 683,481 675,547 681,600 695,682 691,685 719,725 711,724 662,749 690,747 728,759 719,823 739,903 756,1008 764,1070 770;-685 831,-618 826,-668 817,-678 809,-732 796,-755 792,-754 785,-784 775,-779 770,-832 765,-895 765,-883 779,-863 782,-854 790,-869 803,-818 805,-894 809,-914 816,-889 821,-843 826,-811 830,-757 831,-685 831;-271 835,-265 823,-314 820,-229 821,-232 812,-128 817,-168 804,-177 801,-197 776,-217 766,-196 752,-216 742,-208 735,-223 726,-248 723,-221 715,-243 709,-264 702,-223 701,-307 681,-328 677,-370 659,-407 648,-412 635,-429 611,-463 609,-492 614,-521 643,-537 661,-530 684,-509 699,-526 694,-548 703,-534 708,-540 715,-547 726,-561 736,-586 755,-661 761,-697 764,-668 774,-733 780,-657 794,-672 805,-622 813,-572 822,-504 824,-466 820,-468 826,-399 832,-271 835"

    def _coast_rings():
        out = []
        for grp in _COAST_DATA.split(";"):
            ring = []
            for pair in grp.split(","):
                x, y = pair.split(" ")
                ring.append((int(y) / 10.0, int(x) / 10.0))
            out.append(ring)
        return out

    def _pip(lat, lon, poly):
        inside = False
        j = len(poly) - 1
        for i in range(len(poly)):
            yi, xi = poly[i]
            yj, xj = poly[j]
            if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside

    def _ll2v(lat, lon):
        la = math.radians(max(-88, min(88, lat)))
        lo = math.radians(lon)
        y = math.sin(la)
        r = math.cos(la)
        return (r * math.cos(lo), y, r * math.sin(lo))

    COAST_RINGS = _coast_rings()
    _coast_per = []
    for _ring in COAST_RINGS:
        _p = 0.0
        for _i in range(len(_ring) - 1):
            _a, _b = _ring[_i], _ring[_i + 1]
            _p += math.hypot(_b[0] - _a[0], (_b[1] - _a[1]) * math.cos(math.radians((_a[0] + _b[0]) / 2)))
        _coast_per.append(max(_p, 1e-6))

    def form_earth(n):
        out = []
        n_coast = int(n * 0.80)
        tot = sum(_coast_per) or 1.0
        for _ring, _per in zip(COAST_RINGS, _coast_per):
            m = max(3, int(n_coast * _per / tot))
            segs = []
            for _i in range(len(_ring) - 1):
                _a, _b = _ring[_i], _ring[_i + 1]
                segs.append(math.hypot(_b[0] - _a[0],
                                       (_b[1] - _a[1]) * math.cos(math.radians((_a[0] + _b[0]) / 2))))
            stot = sum(segs) or 1.0
            for j in range(m):
                pick = (j + 0.5) * stot / m
                acc = 0.0
                for i, sl in enumerate(segs):
                    if acc + sl >= pick:
                        a, b = _ring[i], _ring[i + 1]
                        k = (pick - acc) / sl if sl else 0.0
                        out.append(_ll2v(a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k))
                        break
                    acc += sl
        form_earth.coast_n = len(out)
        while len(out) < int(n * 0.93):
            ring = COAST_RINGS[random.randrange(len(COAST_RINGS))]
            lat = random.uniform(min(p[0] for p in ring), max(p[0] for p in ring))
            lon = random.uniform(min(p[1] for p in ring), max(p[1] for p in ring))
            if _pip(lat, lon, ring):
                out.append(_ll2v(lat, lon))
        form_earth.land_n = len(out)
        while len(out) < n:
            y = random.uniform(-1, 1)
            r = math.sqrt(max(0.0, 1 - y * y))
            a = random.uniform(0, 2 * math.pi)
            out.append((r * math.cos(a), y, r * math.sin(a)))
        return out[:n]

    FORMS = [form_fib(balls_n), form_rings(balls_n), form_spiral(balls_n), form_earth(balls_n)]
    form_a = [0]
    form_b = [1]
    form_m = [1.0]
    _tnow = time.perf_counter()
    next_form = [_tnow + 10.0]
    next_scan = [_tnow + random.uniform(3, 6)]
    scan_t0 = [-10.0]
    scan_dur = [2.0]
    latwave = [-10.0]
    prev_state = [None]
    next_commute = [_tnow + 16.0]
    commute_t0 = [-10.0]
    commute_dur = [2.4]
    riders = [frozenset()]
    next_commute_ok = [True]
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
        save_gui_settings({"rate": RATE, "volume": VOLUME, "city": CITY,
                           "balls": balls_set[0], "topmost": top_on[0], "hud": hud_on[0]})

    def spawn_hud_overlay():
        if hud_proc[0] is not None and hud_proc[0].poll() is None:
            hud_proc[0].terminate()
            hud_proc[0] = None
            info_override[0] = "голограмма (HUD) скрыта"
            return
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} \
            if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW") else {}
        hud_proc[0] = subprocess.Popen([sys.executable, os.path.abspath(__file__), "hud"], **kwargs)
        info_override[0] = "голограмма (HUD) поверх экрана: убрать — Ctrl+Alt+J или кнопка ещё раз"

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
                pq, why = maybe_predict()
                if pq:
                    last_action[0] = "Прогноз: «" + pq + "» (" + why + ") — «да» или клик"
                    chip_labels[:] = [pq] + list(chip_labels[1:])
                    chip_surfs[:] = [f_small.render("« Да — " + pq + " »", True, LITC)] + chip_surfs[1:]
                elif chip_labels[0] != GUI_SUGGESTIONS[0]:
                    chip_labels[0] = GUI_SUGGESTIONS[0]
                    chip_surfs[0] = f_small.render("« " + GUI_SUGGESTIONS[0] + " »", True, DIM)
            except Exception as exc:
                last_action[0] = f"Ошибка: {exc}"

        threading.Thread(target=worker, daemon=True).start()

    def draw_entry(rect, value, placeholder, focus):
        pygame.draw.rect(screen, (10, 30, 50), rect, border_radius=4)
        pygame.draw.rect(screen, FG if focus else (26, 60, 92), rect, 1, border_radius=4)
        caret = "|" if (focus and int(t * 2) % 2 == 0) else ""
        ef = f_small.render((value or placeholder) + caret, True, FG if value else DIM)
        screen.blit(ef, (rect.x + 6, rect.y + 4))

    def default_hint():
        if voice_active[0]:
            return ("СЛУШАЮ", DIM)
        return ("ОЖИДАНИЕ", DIM)

    chip_labels = list(GUI_SUGGESTIONS[:3])
    chip_surfs = [f_small.render("« " + t + " »", True, DIM) for t in chip_labels]
    panel_visible = [False]
    help_visible = [False]
    tab_i = [0]
    balls_set = [balls_n]
    top_on = [bool(_gs.get("topmost", os.environ.get("JARVIS_TOPMOST", "0") == "1"))]
    hud_on = [bool(_gs.get("hud", True))]
    hud_mem = [0, 0.0]
    hud_proc = [None]
    atexit.register(_cleanup_hud_on_exit)
    if top_on[0]:
        apply_topmost(True)
    city_val = [CITY]
    entry_focus = [""]
    drag = [None]
    ui = {}
    cmds = load_custom_commands()
    new_phrase = [""]
    new_param = [""]
    new_type = [0]
    form_msg = [""]
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
                tgt = {"city": city_val, "phrase": new_phrase, "param": new_param}.get(entry_focus[0])
                if tgt is not None:
                    if ev.key == pygame.K_BACKSPACE:
                        tgt[0] = tgt[0][:-1]
                        if entry_focus[0] == "city":
                            set_city(city_val[0])
                    elif ev.unicode and ev.unicode not in "\r\n\x1b":
                        tgt[0] += ev.unicode
                        if entry_focus[0] == "city":
                            set_city(city_val[0])
                    elif ev.key == pygame.K_ESCAPE:
                        entry_focus[0] = ""
                elif ev.key == pygame.K_ESCAPE:
                    if panel_visible[0]:
                        panel_visible[0] = False
                        entry_focus[0] = ""
                        drag[0] = None
                    if help_visible[0]:
                        help_visible[0] = False
            elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                drag[0] = None
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                mx, my = ev.pos
                if ui.get("voice_btn") and ui["voice_btn"].collidepoint(mx, my):
                    run_voice()
                elif ui.get("doctor_btn") and ui["doctor_btn"].collidepoint(mx, my):
                    threading.Thread(target=doctor, daemon=True).start()
                elif ui.get("holob") and ui["holob"].collidepoint(mx, my):
                    spawn_hud_overlay()
                elif ui.get("sbn") and any(sr.collidepoint(mx, my) for sr in ui["sbn"]):
                    for i, sr in enumerate(ui["sbn"]):
                        if sr.collidepoint(mx, my):
                            panel_visible[0] = True
                            tab_i[0] = i
                            entry_focus[0] = ""
                            form_msg[0] = ""
                            drag[0] = None
                elif ui.get("combo") and ui["combo"].collidepoint(mx, my):
                    help_visible[0] = not help_visible[0]
                    panel_visible[0] = False
                elif ui.get("howb") and ui["howb"].collidepoint(mx, my):
                    help_visible[0] = not help_visible[0]
                    panel_visible[0] = False
                elif panel_visible[0]:
                    hit = False
                    if ui.get("close") and ui["close"].collidepoint(mx, my):
                        panel_visible[0] = False
                        entry_focus[0] = ""
                        hit = True
                    if not hit:
                        for i, kr in enumerate(ui.get("knobs", [])):
                            if kr.collidepoint(mx, my):
                                drag[0] = i
                                hit = True
                        for i, (tx0, tx1, ty, lo, hi, tgt) in enumerate(ui.get("tracks", [])):
                            if tx0 - 6 <= mx <= tx1 + 6 and ty - 10 <= my <= ty + 10 and not hit:
                                drag[0] = i
                                hit = True
                        if drag[0] is not None:
                            tx0, tx1, _ty, lo, hi, tgt = ui["tracks"][drag[0]]
                            k = min(1.0, max(0.0, (mx - tx0) / max(1, tx1 - tx0)))
                            v = lo + k * (hi - lo)
                            if tgt == 0:
                                set_rate(v)
                            elif tgt == 1:
                                set_vol(v)
                            else:
                                balls_set[0] = int(max(120, min(700, round(v / 10) * 10)))
                                persist()
                    if not hit:
                        for fid, r in ui.get("entries", []):
                            if r.collidepoint(mx, my):
                                entry_focus[0] = fid
                                hit = True
                                break
                    if not hit and ui.get("check") and ui["check"].collidepoint(mx, my):
                        top_on[0] = not top_on[0]
                        apply_topmost(top_on[0])
                        persist()
                        hit = True
                    if not hit and ui.get("hud_check") and ui["hud_check"].collidepoint(mx, my):
                        hud_on[0] = not hud_on[0]
                        persist()
                        hit = True
                    if not hit and ui.get("typebtn") and ui["typebtn"].collidepoint(mx, my):
                        new_type[0] = (new_type[0] + 1) % len(CUSTOM_TYPES)
                        entry_focus[0] = ""
                        hit = True
                    if not hit and ui.get("addbtn") and ui["addbtn"].collidepoint(mx, my):
                        ph_txt = new_phrase[0].strip()
                        pr_txt = new_param[0].strip()
                        if ph_txt and pr_txt:
                            cmds.append({"phrase": ph_txt, "type": CUSTOM_TYPES[new_type[0]], "param": pr_txt})
                            save_custom_commands(cmds)
                            new_phrase[0] = ""
                            new_param[0] = ""
                            form_msg[0] = "команда добавлена, сэр"
                        else:
                            form_msg[0] = "заполни фразу и параметр"
                        hit = True
                    if not hit and ui.get("delbtns"):
                        for di, dr in ui["delbtns"]:
                            if dr.collidepoint(mx, my):
                                if 0 <= di < len(cmds):
                                    cmds.pop(di)
                                    save_custom_commands(cmds)
                                form_msg[0] = ""
                                hit = True
                    if not hit and ui.get("sheet"):
                        if ui["sheet"].collidepoint(mx, my):
                            entry_focus[0] = ""
                        else:
                            panel_visible[0] = False
                            entry_focus[0] = ""
                            drag[0] = None
                elif ((mx - cx) ** 2 + (my - cy) ** 2) <= (radius * 1.06) ** 2:
                    panel_visible[0] = True
                    entry_focus[0] = ""
                elif ui.get("chips") and any(r.collidepoint(mx, my) for r in ui["chips"]):
                    for r, lbl in zip(ui["chips"], chip_labels):
                        if r.collidepoint(mx, my):
                            entry_focus[0] = ""
                            send(lbl)
                elif ui.get("help_sheet") and ui["help_sheet"].collidepoint(mx, my):
                    help_visible[0] = False
                    entry_focus[0] = ""
        if drag[0] is not None and ui.get("tracks"):
            try:
                tx0, tx1, _ty, lo, hi, tgt = ui["tracks"][drag[0]]
            except (IndexError, ValueError):
                drag[0] = None
            else:
                mx, my = pygame.mouse.get_pos()
                k = min(1.0, max(0.0, (mx - tx0) / max(1, tx1 - tx0)))
                v = lo + k * (hi - lo)
                if tgt == 0:
                    set_rate(v)
                elif tgt == 1:
                    set_vol(v)
                else:
                    balls_set[0] = int(max(120, min(700, round(v / 10) * 10)))
                    persist()

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
        if ORB_STATE == "listen" and prev_state[0] != "listen":
            latwave[0] = now
        prev_state[0] = ORB_STATE
        if scan_active[0] and (now - scan_t0[0]) / scan_dur[0] >= 1.0:
            scan_active[0] = False
        elif not scan_active[0] and now > next_scan[0] and ORB_STATE != "think":
            scan_active[0] = True
            scan_t0[0] = now
            scan_dur[0] = random.uniform(1.4, 2.2)
            next_scan[0] = now + scan_dur[0] + random.uniform(5, 10)
        ck = (now - commute_t0[0]) / commute_dur[0]
        commuting = ck < 1.0
        if not commuting and next_commute_ok[0] and now > next_commute[0] \
                and ORB_STATE not in ("think", "speak"):
            commute_t0[0] = now
            commute_dur[0] = 2.4
            riders[0] = frozenset(i for i in range(balls_n) if i % 11 == 3)
            next_commute[0] = now + random.uniform(28, 34)
            next_commute_ok[0] = False
        if not commuting:
            next_commute_ok[0] = True
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

        screen.blit(bg_layer, (0, 0))
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
        lw = (now - latwave[0]) / 0.9
        if lw < 2.0:
            wave_y = 1.0 - 2.0 * lw if lw < 1.0 else -1.0 + 2.0 * (lw - 1.0)
        else:
            wave_y = None
        rset = riders[0] if commuting else frozenset()

        proj = []
        for i, p in enumerate(pts):
            sc = 1.0 + p["amp"] * math.sin(t * 0.8 + p["ph"])
            va, vb = ua[i], ub[i]
            hx = va[0] + (vb[0] - va[0]) * mk
            hy = va[1] + (vb[1] - va[1]) * mk
            hz = va[2] + (vb[2] - va[2]) * mk
            ln = math.hypot(hx, math.hypot(hy, hz)) or 1.0
            hx, hy, hz = hx / ln, hy / ln, hz / ln
            vx, vy, vz = hx, hy, hz
            if i in rset:
                scl = 1.0 - 0.85 * abs(math.sin(math.pi * ck * 2.5))
                vx, vy, vz = hx * scl, hy * scl, hz * scl
            ox, oy, z, nx, ny, nz = project((vx * sc, vy * sc, vz * sc), cyw, syw, ct, st)
            lam = max(0.0, nx * LX + ny * LY + nz * LZ)
            shade = p["base"] * (0.22 + 0.42 * (z + 1) / 2 + 0.36 * lam)
            if fa == 3 or fb == 3:
                if i < getattr(form_earth, "coast_n", 0):
                    shade = max(shade, 0.95)
                elif i < getattr(form_earth, "land_n", int(balls_n * 0.93)):
                    shade = max(shade, 0.6)
                else:
                    shade *= 0.35
            b = min(NB - 1, int(shade * NB))
            sx = int(cx + ox * radius * spring)
            sy = int(cy + oy * radius * spring)
            d_scan = abs(i - scan_pos)
            d_scan = min(d_scan, balls_n - d_scan)
            if d_scan < 1.0:
                spr = scan_sprites[0]
            elif d_scan < 4.0:
                spr = scan_sprites[1]
            elif i in rset:
                spr = lit_sprites[NB - 1]
            elif wave_y is not None and abs(hy - wave_y) < 0.16:
                spr = lit_sprites[NB - 1]
            elif p["spark"]:
                spr = spark_sprites[b]
            elif thinking and min(abs(i - lit_center), balls_n - abs(i - lit_center)) < lit_half:
                spr = lit_sprites[b]
            else:
                spr = orb_sprites[b]
            rho = math.hypot(ox, oy) / radius
            proj.append((sx, sy, spr, rho, p["spark"], d_scan))

        for sx, sy, spr, rho, spark, d_scan in proj:
            screen.blit(spr, (sx - spr.get_width() // 2, sy - spr.get_height() // 2),
                        special_flags=pygame.BLEND_ADD)
            if 0.94 < rho <= 1.10 and not spark and d_scan >= 4.0:
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

        if hud_on[0]:
            GOLDC = (255, 176, 64)
            act = 1.0 if ORB_STATE in ("think", "speak") else 0.55
            for _ri, (rr, sp, segs, hcol) in enumerate((
                    (radius * 1.14, 0.55, 3, FG),
                    (radius * 1.24, -0.38, 4, GOLDC),
                    (radius * 1.34, 0.26, 2, FG))):
                a0 = t * sp * 57.29
                seg = 360 / segs
                hb = pygame.Rect(int(cx - rr), int(cy - rr), int(rr * 2), int(rr * 2))
                for s_i in range(segs):
                    s0 = a0 + s_i * seg
                    pygame.draw.arc(screen, hcol, hb, math.radians(s0), math.radians(s0 + seg * 0.62), 2)
            for ti in range(48):
                ang = math.radians(ti * 7.5 + t * 18)
                twid = 5 + int(4 * math.sin(t * 2.4 + ti * 0.52)) * act
                r1 = radius * 1.06
                r2 = r1 + twid
                tic = GOLDC if (ti % 6 == 0 and act > 0.8) else FG
                pygame.draw.line(screen, tic,
                                 (int(cx + math.cos(ang) * r1), int(cy + math.sin(ang) * r1)),
                                 (int(cx + math.cos(ang) * r2), int(cy + math.sin(ang) * r2)))
            if t - hud_mem[1] > 2.0:
                hud_mem[1] = t
                try:
                    hud_mem[0] = len(load_memory())
                except Exception:
                    hud_mem[0] = 0
            rx = int(cx + radius * 1.5)
            ry0 = int(cy - 96)
            for di, dtxt in enumerate((
                    f"ВРЕМЯ   {time.strftime('%H:%M:%S')}",
                    f"ДАТА    {time.strftime('%d.%m.%Y')}",
                    f"UPTIME  {int(t)} c",
                    f"ТОКЕНЫ  {balls_n}",
                    f"ПАМЯТЬ  {hud_mem[0]}",
                    f"КОМАНД  {len(cmds)}")):
                surf2 = f_small.render(dtxt, True, (170, 215, 245))
                screen.blit(surf2, (rx, ry0 + di * 20))
            bx = rx
            by = int(cy + 44)
            for bi in range(24):
                hh = 3 + int((14 + 20 * act) * abs(math.sin(t * (2.2 + 0.31 * bi) + bi * 0.8)))
                pygame.draw.rect(screen, GOLDC if bi % 7 == 0 else FG,
                                 (bx + bi * 8, by - hh, 5, hh), border_radius=2)

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

        sb = pygame.Surface((SW, H), pygame.SRCALPHA)
        sb.fill((8, 20, 34, 235))
        screen.blit(sb, (0, 0))
        pygame.draw.line(screen, (52, 110, 160), (SW - 1, 0), (SW - 1, H))
        screen.blit(f_logo.render("JARVIS", True, LITC), (20, 14))
        combo_s = f_small.render("Джарвис  ▾", True, FG)
        cr = combo_s.get_rect(topleft=(20, 66))
        box = cr.inflate(18, 10)
        pygame.draw.rect(screen, (10, 28, 46), box, border_radius=5)
        pygame.draw.rect(screen, (30, 70, 105), box, 1, border_radius=5)
        screen.blit(combo_s, cr)
        ui["combo"] = box
        hbox = pygame.Rect(16, 100, SW - 32, 28)
        pygame.draw.rect(screen, (10, 28, 46), hbox, border_radius=7)
        pygame.draw.rect(screen, (30, 70, 105), hbox, 1, border_radius=7)
        how_s = f_small.render("Как начать", True, FG)
        screen.blit(how_s, how_s.get_rect(center=hbox.center))
        ui["howb"] = hbox
        sb_t = f_small.render("НАСТРОЙКИ", True, DIM)
        screen.blit(sb_t, (20, 146))
        ui["sbn"] = []
        for si, sname in enumerate(("Голос", "Окно", "Команды", "Инфо")):
            sr = pygame.Rect(16, 172 + si * 34, SW - 32, 28)
            ui["sbn"].append(sr)
            sb_on = panel_visible[0] and tab_i[0] == si
            pygame.draw.rect(screen, (26, 64, 100) if sb_on else (13, 32, 52), sr, border_radius=7)
            sf2 = f_small.render(sname, True, FG if sb_on else DIM)
            screen.blit(sf2, (sr.x + 12, sr.y + 7))
        ui["holob"] = pygame.Rect(16, H - 132, SW - 32, 28)
        pygame.draw.rect(screen, (46, 38, 14), ui["holob"], border_radius=7)
        pygame.draw.rect(screen, (150, 110, 40), ui["holob"], 1, border_radius=7)
        hb_s = f_btn.render("голограмма (hud)", True, (255, 205, 120))
        screen.blit(hb_s, hb_s.get_rect(center=ui["holob"].center))
        ui["voice_btn"] = pygame.Rect(16, H - 96, SW - 32, 28)
        pygame.draw.rect(screen, (16, 46, 74), ui["voice_btn"], border_radius=7)
        vb = f_btn.render(voice_btn_lbl[0], True, FG)
        screen.blit(vb, vb.get_rect(center=ui["voice_btn"].center))
        ui["doctor_btn"] = pygame.Rect(16, H - 60, SW - 32, 28)
        pygame.draw.rect(screen, (16, 46, 74), ui["doctor_btn"], border_radius=7)
        db = f_btn.render("doctor", True, FG)
        screen.blit(db, db.get_rect(center=ui["doctor_btn"].center))

        if help_visible[0]:
            hs = pygame.Surface((W - SW - 60, 86), pygame.SRCALPHA)
            hs.fill((8, 20, 34, 224))
            screen.blit(hs, (SW + 30, 40))
            ui["help_sheet"] = pygame.Rect(SW + 30, 40, W - SW - 60, 86)
            for li, ltxt in enumerate((
                    "1. нажми на шар — откроется панель настроек поверх него",
                    "2. 🎤 внизу слева — голосовой режим, doctor — проверка системы",
                    "3. свои команды — раздел «Команды»: работают сразу, без перезапуска")):
                lf = f_small.render(ltxt, True, FG)
                screen.blit(lf, (42, 52 + li * 22))

        if panel_visible[0]:
            pw = int(radius * 2.6)
            ph = int(radius * 2.6)
            px, py = int(cx - pw / 2), max(6, int(cy - ph / 2))
            sheet = pygame.Surface((pw, ph), pygame.SRCALPHA)
            sheet.fill((9, 22, 36, 246))
            screen.blit(sheet, (px, py))
            ui["sheet"] = pygame.Rect(px, py, pw, ph)
            pygame.draw.rect(screen, (52, 110, 160), ui["sheet"], 1, border_radius=12)
            ui["knobs"] = []
            ui["tracks"] = []
            ui["entries"] = []
            ui["delbtns"] = []
            ui.pop("check", None)
            ui.pop("hud_check", None)
            ui.pop("typebtn", None)
            ui.pop("addbtn", None)
            x0, x1 = px + 24, px + pw - 24
            top_m = f_mid.render(("Голос", "Окно", "Команды", "Инфо")[tab_i[0]], True, (225, 245, 255))
            screen.blit(top_m, (x0, py + 16))
            ui["close"] = pygame.Rect(px + pw - 34, py + 12, 22, 22)
            hov = ui["close"].collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(screen, (170, 70, 70) if hov else (30, 66, 100), ui["close"], border_radius=6)
            cxs = f_small.render("✕", True, (255, 220, 220) if hov else FG)
            screen.blit(cxs, cxs.get_rect(center=ui["close"].center))
            if tab_i[0] == 0:
                for label, val, lo, hi, y, tgt in (
                        (f"скорость речи  {int(RATE)}", int(RATE), 20, 200, py + 82, 0),
                        (f"громкость  {int(VOLUME)}", int(VOLUME), 0, 200, py + 128, 1)):
                    lab = f_small.render(label, True, FG)
                    screen.blit(lab, (x0, y - 20))
                    pygame.draw.rect(screen, (26, 60, 92), (x0, y - 2, x1 - x0, 4), border_radius=2)
                    k = min(1.0, max(0.0, (val - lo) / max(1, hi - lo)))
                    kx = x0 + (x1 - x0) * k
                    pygame.draw.circle(screen, FG, (int(kx), y), 7)
                    ui["knobs"].append(pygame.Rect(int(kx) - 9, y - 9, 18, 18))
                    ui["tracks"].append((x0, x1, y, lo, hi, tgt))
                er = pygame.Rect(x0, py + 170, 200, 24)
                ui["entries"].append(("city", er))
                draw_entry(er, city_val[0], "город погоды...", entry_focus[0] == "city")
                hint = f_small.render("город для команды «погода»", True, DIM)
                screen.blit(hint, (x0 + 212, py + 176))
            elif tab_i[0] == 1:
                lab = f_small.render(f"токенов в шаре: {balls_set[0]} (вступит после перезапуска)", True, FG)
                screen.blit(lab, (x0, py + 62))
                pygame.draw.rect(screen, (26, 60, 92), (x0, py + 82, x1 - x0, 4), border_radius=2)
                k = min(1.0, max(0.0, (balls_set[0] - 120) / 580.0))
                kx = x0 + (x1 - x0) * k
                pygame.draw.circle(screen, FG, (int(kx), py + 84), 7)
                ui["knobs"].append(pygame.Rect(int(kx) - 9, py + 75, 18, 18))
                ui["tracks"].append((x0, x1, py + 84, 120, 700, 2))
                ui["check"] = pygame.Rect(x0, py + 128, 22, 22)
                pygame.draw.rect(screen, FG if top_on[0] else (26, 60, 92), ui["check"], 1, border_radius=4)
                if top_on[0]:
                    pygame.draw.rect(screen, FG, (ui["check"].x + 4, ui["check"].y + 4, 14, 14), border_radius=3)
                chk = f_small.render("окно поверх всех", True, FG)
                screen.blit(chk, (ui["check"].x + 30, ui["check"].y + 3))
                hint = f_small.render("окно всегда видно поверх других программ", True, DIM)
                screen.blit(hint, (x0, py + 166))
                ui["hud_check"] = pygame.Rect(x0, py + 196, 22, 22)
                pygame.draw.rect(screen, FG if hud_on[0] else (26, 60, 92), ui["hud_check"], 1, border_radius=4)
                if hud_on[0]:
                    pygame.draw.rect(screen, FG, (ui["hud_check"].x + 4, ui["hud_check"].y + 4, 14, 14), border_radius=3)
                chk2 = f_small.render("HUD Iron Man", True, FG)
                screen.blit(chk2, (ui["hud_check"].x + 30, ui["hud_check"].y + 3))
                hint2 = f_small.render("кольца-дуги, тики, данные и волна вокруг шара", True, DIM)
                screen.blit(hint2, (x0, py + 234))
            elif tab_i[0] == 2:
                y_form = py + ph - 148
                max_rows = max(0, int((y_form - (py + 48)) // 22))
                visible = cmds[-max_rows:] if max_rows else []
                base_idx = len(cmds) - len(visible)
                for ri, c in enumerate(visible):
                    ry = py + 50 + ri * 22
                    typ_lbl = CUSTOM_TYPE_LABELS.get(c.get("type"), "?")
                    txt = f"«{c.get('phrase', '')}» → {typ_lbl}: {c.get('param', '')}"
                    while txt and f_small.size(txt)[0] > (x1 - x0) - 30:
                        txt = txt[:-2] + "…"
                    rf = f_small.render(txt, True, FG)
                    screen.blit(rf, (x0, ry))
                    dr = pygame.Rect(x1 - 18, ry - 2, 16, 16)
                    ui["delbtns"].append((base_idx + ri, dr))
                    pygame.draw.rect(screen, (30, 66, 100), dr, border_radius=4)
                    dxr = f_small.render("✕", True, DIM)
                    screen.blit(dxr, dxr.get_rect(center=dr.center))
                if not cmds:
                    nf = f_small.render("пока нет своих команд — создай первую ниже", True, DIM)
                    screen.blit(nf, (x0, py + 52))
                lab = f_small.render("новая команда — фразы через запятую:", True, DIM)
                screen.blit(lab, (x0, y_form))
                pr_ = pygame.Rect(x0, y_form + 16, x1 - x0, 24)
                ui["entries"].append(("phrase", pr_))
                draw_entry(pr_, new_phrase[0], "фраза, например: включи музыку", entry_focus[0] == "phrase")
                prm = pygame.Rect(x0, y_form + 48, x1 - x0 - 140, 24)
                ui["entries"].append(("param", prm))
                placeholders = {"site": "https://youtube.com", "app": "chrome",
                                "say": "что озвучить", "cmd": "время"}
                draw_entry(prm, new_param[0],
                           placeholders[CUSTOM_TYPES[new_type[0]]], entry_focus[0] == "param")
                ty_ = pygame.Rect(x1 - 132, y_form + 48, 132, 24)
                ui["typebtn"] = ty_
                pygame.draw.rect(screen, (16, 46, 74), ty_, border_radius=7)
                tb = f_btn.render("тип: " + CUSTOM_TYPE_LABELS[CUSTOM_TYPES[new_type[0]]], True, FG)
                screen.blit(tb, tb.get_rect(center=ty_.center))
                ab = pygame.Rect(x1 - 132, y_form + 80, 132, 24)
                ui["addbtn"] = ab
                pygame.draw.rect(screen, (20, 62, 96), ab, border_radius=7)
                abf = f_btn.render("добавить", True, LITC)
                screen.blit(abf, abf.get_rect(center=ab.center))
                if form_msg[0]:
                    mf = f_small.render(form_msg[0], True, FG)
                    screen.blit(mf, (x0, y_form + 85))
                hf = f_small.render("создаёшь здесь — Джарвис слышит сразу, без перезапуска", True, DIM)
                screen.blit(hf, (x0 + 136, y_form + 85))
            else:
                tts_name = os.path.basename(engine) if engine else "нет (setup voice)"
                for li, ltxt in enumerate((
                        f"озвучка: {tts_name}",
                        f"мозг: Ollama/{LOCAL_MODEL} → облако {POLLINATIONS_MODEL}",
                        f"память: {len(load_memory())} факт(ов) · город: {CITY}",
                        f"своих команд: {len(cmds)} · орб: {balls_n} токенов",
                        f"окно: {W}x{H} · шар: ~{int(radius * 2)} px")):
                    lf = f_small.render(ltxt, True, FG if li == 0 else DIM)
                    screen.blit(lf, (x0, py + 70 + li * 28))

        pygame.display.flip()
        clock.tick(40)

    VOICE_STOP = True
    orb_notify("idle")
    pygame.quit()

def hud_overlay():
    import pygame
    pygame.init()
    try:
        if os.path.exists(OVERLAY_STOP_FILE):
            os.remove(OVERLAY_STOP_FILE)
    except OSError:
        pass
    desktop = pygame.display.Info()
    DW, DH = desktop.current_w, desktop.current_h
    transparent = sys.platform.startswith("win")
    PW, PH = 380, 500
    screen = pygame.display.set_mode((PW, PH), pygame.NOFRAME if transparent else 0)
    pygame.display.set_caption("JARVIS")
    ok_trans = False
    hwnd = u32 = None
    hotkey_on = False
    if transparent:
        _hud_log(f"hud {APP_VERSION} start: DW={DW} DH={DH}")
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            u32 = ctypes.windll.user32
            WS_EX_LAYERED, WS_EX_TOPMOST, WS_EX_TOOLWINDOW = 0x00080000, 0x00000008, 0x00000080
            u32.SetWindowLongW(hwnd, -20, u32.GetWindowLongW(hwnd, -20)
                               | WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW)
            u32.SetLayeredWindowAttributes(hwnd, 0x00030201, 0, 0x00000001)
            _hud_log(f"extstyle+colorkey set, hwnd={hwnd}")
            u32.MoveWindow(hwnd, DW - PW - 18, DH - PH - 18, PW, PH, 0)
            u32.ShowWindow(hwnd, 4)
            u32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            ok_trans = True
            _hud_log(f"move+topmost+colorkey ok, expect x={DW - PW - 18} y={DH - PH - 18}")
        except Exception:
            _hud_log("overlay setup FAILED:\n" + traceback.format_exc())
            hwnd = u32 = None
        try:
            u32.RegisterHotKey(None, 1, 0x0001 | 0x0002, 0x4A)
            hotkey_on = True
        except Exception:
            hotkey_on = False

    def hf(sz, bold=False):
        return pygame.font.SysFont("arial", sz, bold=bold)

    f_17, f_14 = hf(18), hf(15)
    FG2, GOLDC, DIM2 = (170, 220, 255), (255, 176, 64), (110, 160, 195)
    panel = pygame.Rect(0, 0, PW, PH)
    close_r = pygame.Rect(PW - 30, 8, 22, 18)
    chat = [("jarvis", "К вашим услугам, сэр. Напишите ниже команду.")]
    hist = [[]]
    busy = [False]
    box_txt = [""]
    focus = [False]
    state = ["idle"]
    try:
        engine = find_tts()
    except Exception:
        engine = None

    def wrap(txt, maxw):
        words, lines, cur = txt.split(), [], ""
        for w in words:
            tr = (cur + " " + w).strip()
            if f_14.size(tr)[0] <= maxw:
                cur = tr
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines

    def worker(q):
        busy[0] = True
        state[0] = "think"
        try:
            reply, new_hist = respond(q, hist[0], engine)
            hist[0] = new_hist
            if reply:
                chat[:] = chat[-60:] + [("jarvis", reply)]
                state[0] = "speak"
            else:
                chat[:] = chat[-60:] + [("jarvis", "не смог выполнить, сэр")]
        except Exception as exc:
            chat[:] = chat[-60:] + [("jarvis", f"ошибка: {exc}")]
        busy[0] = False
        state[0] = "idle"

    def send(q):
        if busy[0]:
            chat[:] = chat[-60:] + [("jarvis", "секунду, сэр…")]
            return
        chat[:] = chat[-60:] + [("you", q)]
        threading.Thread(target=worker, args=(q,), daemon=True).start()

    clock = pygame.time.Clock()
    t0 = time.perf_counter()
    WM_HOTKEY = 0x0312
    last_top = 0.0
    last_state_t = 0.0
    import ctypes.wintypes as wt
    while True:
        if hotkey_on and u32 is not None:
            try:
                msg = wt.MSG()
                while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0x0001):
                    if msg.message == WM_HOTKEY:
                        return
            except Exception:
                pass
        if os.path.exists(OVERLAY_STOP_FILE):
            try:
                os.remove(OVERLAY_STOP_FILE)
            except OSError:
                pass
            return
        t = time.perf_counter() - t0
        if u32 is not None and t - last_top > 2.0:
            try:
                u32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
                if t - last_state_t > 10.0:
                    last_state_t = t
                    rect = (ctypes.c_long * 4)()
                    u32.GetWindowRect(hwnd, rect)
                    _hud_log(f"alive, rect={tuple(rect)}")
            except Exception:
                pass
            last_top = t
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return
            if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                if close_r.collidepoint(ev.pos):
                    return
                elif ev.pos[1] > PH - 44:
                    focus[0] = True
            if ev.type == pygame.KEYDOWN and focus[0]:
                if ev.key == pygame.K_ESCAPE:
                    focus[0] = False
                elif ev.key == pygame.K_BACKSPACE:
                    box_txt[0] = box_txt[0][:-1]
                elif ev.key == pygame.K_RETURN:
                    q = box_txt[0].strip()
                    box_txt[0] = ""
                    if q:
                        send(q)
                elif ev.unicode and ev.unicode.isprintable():
                    box_txt[0] += ev.unicode
        screen.fill((1, 2, 3) if ok_trans else (12, 27, 44))
        pygame.draw.rect(screen, (10, 22, 36), panel, border_radius=12)
        pygame.draw.rect(screen, (52, 110, 160), panel, 1, border_radius=12)
        ocx, ocy, orr = panel.centerx, 76, 44
        pygame.draw.circle(screen, (18, 48, 78), (ocx, ocy), orr + 6, 1)
        np = 56
        for i in range(np):
            yy = 1.0 - 2.0 * i / (np - 1)
            rr2 = math.sqrt(max(0.0, 1 - yy * yy))
            phi = math.pi * (3 - math.sqrt(5)) * i + t * (5.0 if state[0] == "think" else 2.0)
            xo = math.cos(phi) * rr2 * orr
            yo = yy * orr * 0.92
            shade = 0.5 + 0.5 * (1 - math.cos(phi)) / 2
            if i % 6 == 0 and state[0] == "speak":
                col = GOLDC
            else:
                col = (min(255, int(150 * shade) + 40), min(255, int(210 * shade) + 40),
                       min(255, int(255 * shade) + 30))
            pygame.draw.rect(screen, col, (int(ocx + xo) - 1, int(ocy + yo) - 1, 3, 3), border_radius=1)
        pygame.draw.circle(screen, GOLDC if state[0] == "think" else (225, 245, 255),
                           (ocx, ocy), 7 + (2 if state[0] == "speak" else 0))
        screen.blit(f_17.render("JARVIS", True, GOLDC), (panel.x + 12, panel.y + 10))
        screen.blit(f_14.render("голограмма · чат · " + APP_VERSION, True, DIM2),
                    (panel.x + 96, panel.y + 13))
        pygame.draw.rect(screen, (150, 70, 70), close_r, border_radius=4)
        cf = f_14.render("✕", True, (255, 220, 220))
        screen.blit(cf, cf.get_rect(center=close_r.center))
        ca_t = panel.y + 138
        ca_b = panel.y + PH - 46
        rows = []
        for who, msg_txt in chat[-12:]:
            pfx = "ВЫ: " if who == "you" else "J: "
            col3 = GOLDC if who == "you" else FG2
            for ln in wrap(pfx + msg_txt, PW - 32):
                rows.append((ln, col3))
        for row_txt, colr in rows[-14:]:
            if ca_t + 17 > ca_b:
                break
            screen.blit(f_14.render(row_txt, True, colr), (panel.x + 16, ca_t))
            ca_t += 17
        if busy[0]:
            screen.blit(f_14.render("обрабатываю, сэр…", True, GOLDC), (panel.x + PW - 150, ca_b - 17))
        ib = pygame.Rect(panel.x + 12, panel.y + PH - 36, PW - 24, 26)
        pygame.draw.rect(screen, (6, 16, 28), ib, border_radius=6)
        pygame.draw.rect(screen, GOLDC if focus[0] else (34, 70, 104), ib, 1, border_radius=6)
        cur = "" if not focus[0] or int(t * 2) % 2 else "_"
        if box_txt[0] or focus[0]:
            screen.blit(f_14.render("› " + box_txt[0] + cur, True, FG2), (ib.x + 8, ib.y + 6))
        else:
            screen.blit(f_14.render("кликните и введите команду…", True, DIM2), (ib.x + 8, ib.y + 6))
        pygame.display.flip()
        clock.tick(30)



def hud_overlay_tk():
    """
    Голограмма на tkinter: родная виндовая отрисовка (SDL на некоторых машинах
    отдаёт чёрное окно). Панель без рамки, поверх всего: мини-орб + чат + ввод.
    """
    try:
        import tkinter as tk
    except (ImportError, OSError) as exc:
        _hud_log(f"tk недоступен ({exc}) — fallback на pygame-оверлей")
        hud_overlay()
        return

    PW, PH = 380, 500
    TC = "#010203"
    PANEL, EDGE = "#0a1624", "#346ea0"
    FG, DIM, GOLD = "#aadcf7", "#6ea0c3", "#ffb040"
    root = tk.Tk()
    root.title("JARVIS")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.attributes("-transparentcolor", TC)
    except tk.TclError:
        pass
    root.configure(bg=TC)
    dw, dh = root.winfo_screenwidth(), root.winfo_screenheight()
    wx, wy = 0, 0
    wr, wb = dw, dh
    if sys.platform.startswith("win"):
        try:
            import ctypes.wintypes as wt
            _wa = wt.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(_wa), 0):
                wx, wy, wr, wb = _wa.left, _wa.top, _wa.right, _wa.bottom
                _hud_log(f"work area: x={wx} y={wy} w={wr - wx} h={wb - wy}")
        except Exception:
            pass
    def spot_std(x, y):
        try:
            from PIL import ImageGrab, ImageStat
            img = ImageGrab.grab(bbox=(x, y, x + PW, y + PH))
            img = img.convert("L").resize((48, 48))
            return ImageStat.Stat(img).stddev[0]
        except Exception:
            return 0.0

    def pick_origin(skip=None):
        cands = [(wr - PW - 18, wb - PH - 18), (wx + 18, wb - PH - 18),
                 (wr - PW - 18, wy + 18), (wx + 18, wy + 18)]
        best, best_v = None, None
        for cx, cy in cands:
            if skip and abs(cx - skip[0]) < 40 and abs(cy - skip[1]) < 40:
                continue
            v = spot_std(cx, cy)
            _hud_log(f"spot ({cx},{cy}) busy={v:.1f}")
            if best_v is None or v < best_v:
                best, best_v = (cx, cy), v
        return best or (wr - PW - 18, wb - PH - 18)

    pos = list(pick_origin())
    root.geometry(f"{PW}x{PH}+{pos[0]}+{pos[1]}")

    canvas = tk.Canvas(root, width=PW, height=PH, bg=TC, highlightthickness=0)
    canvas.place(x=0, y=0)

    def rounded(x0, y0, x1, y1, r, color):
        ids = [canvas.create_rectangle(x0 + r, y0, x1 - r, y1, fill=color, outline=""),
               canvas.create_rectangle(x0, y0 + r, x1, y1 - r, fill=color, outline="")]
        for cx, cy in ((x0 + r, y0 + r), (x1 - r, y0 + r), (x0 + r, y1 - r), (x1 - r, y1 - r)):
            ids.append(canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline=""))
        return ids

    r1 = rounded(0, 0, PW, PH, 14, EDGE)
    r2 = rounded(1, 1, PW - 1, PH - 1, 13, PANEL)
    title_id = canvas.create_text(14, 20, text="JARVIS", fill=GOLD, anchor="w",
                                  font=("Segoe UI", 11, "bold"))
    sub_id = canvas.create_text(104, 15, text=f"голограмма · чат · {APP_VERSION}",
                                fill=DIM, anchor="w", font=("Segoe UI", 9))
    hint_id = canvas.create_text(104, 30,
                                 text="клик по шапке — свернуть · Ctrl+Alt+H — фоновое место",
                                 fill=DIM, anchor="w", font=("Segoe UI", 8))
    close_id = canvas.create_text(PW - 26, 24, text="✕", fill="#ff9c9c", font=("Segoe UI", 10))

    CPW, CPH = 110, 104
    cr1 = rounded(0, 0, CPW, CPH, 12, EDGE)
    cr2 = rounded(1, 1, CPW - 1, CPH - 1, 11, PANEL)
    ctitle_id = canvas.create_text(CPW // 2, 11, text="JARVIS", fill=DIM,
                                   font=("Segoe UI", 8))
    cclose_id = canvas.create_text(CPW - 9, 10, text="✕", fill="#ff9c9c",
                                   font=("Segoe UI", 8))
    for iid in (cr1 + cr2 + [ctitle_id, cclose_id]):
        canvas.itemconfigure(iid, state="hidden")

    cur = [PW // 2, 84, 42]
    ocx, ocy, orr = cur
    ring_id = canvas.create_oval(ocx - orr - 5, ocy - orr - 5, ocx + orr + 5, ocy + orr + 5,
                                 outline="#12304f", fill="")

    NP = 40
    dots = []
    for i in range(NP):
        yy = 1.0 - 2.0 * i / (NP - 1)
        rr2 = math.sqrt(max(0.0, 1 - yy * yy))
        dots.append((yy, rr2, math.pi * (3 - math.sqrt(5)) * i, i))
    dot_ids = [canvas.create_oval(-5, -5, -4, -4, fill=FG, outline="") for _ in range(NP)]
    core_id = canvas.create_oval(ocx - 7, ocy - 7, ocx + 7, ocy + 7,
                                 fill="#e1f5ff", outline="")

    chat_box = tk.Text(root, bg=PANEL, fg=FG, relief="flat", wrap="word",
                       font=("Segoe UI", 10), state="disabled",
                       selectbackground="#12304f")
    chat_box.place(x=12, y=150, width=PW - 24, height=PH - 224)
    status = tk.Label(root, text="", bg=PANEL, fg=GOLD, font=("Segoe UI", 9))
    status.place(x=14, y=PH - 70)
    entry = tk.Entry(root, bg="#06101c", fg=FG, insertbackground=FG,
                     relief="flat", font=("Segoe UI", 10))
    entry.place(x=12, y=PH - 44, width=PW - 24, height=26)
    entry.focus_set()

    state = ["idle"]
    busy = [False]

    def chat_add(who, txt):
        def _do():
            chat_box.configure(state="normal")
            tag = "you" if who == "you" else "j"
            chat_box.tag_configure("you", foreground=GOLD)
            chat_box.tag_configure("j", foreground=FG)
            chat_box.insert("end", ("ВЫ: " if who == "you" else "J: ") + txt + "\n", tag)
            chat_box.see("end")
            chat_box.configure(state="disabled")
        root.after(0, _do)

    def worker(q):
        busy[0] = True
        state[0] = "think"
        root.after(0, lambda: status.configure(text="обрабатываю, сэр…"))
        try:
            engine = find_tts()
            reply, _hist = respond(q, [], engine)
            if reply:
                chat_add("jarvis", reply)
                state[0] = "speak"
                root.after(0, lambda: status.configure(text="говорю…"))
            else:
                chat_add("jarvis", "не смог выполнить, сэр")
        except Exception as exc:
            chat_add("jarvis", f"ошибка: {exc}")
        busy[0] = False
        state[0] = "idle"
        root.after(0, lambda: status.configure(text=""))

    def send(event=None):
        q = entry.get().strip()
        entry.delete(0, "end")
        if not q:
            return "break"
        if busy[0]:
            chat_add("jarvis", "секунду, сэр…")
            return "break"
        chat_add("you", q)
        threading.Thread(target=worker, args=(q,), daemon=True).start()
        return "break"

    entry.bind("<Return>", send)

    compact = [False]

    def apply_place():
        w, h = (CPW, CPH) if compact[0] else (PW, PH)
        root.geometry(f"{w}x{h}+{pos[0]}+{pos[1]}")

    def show_compact(v):
        compact[0] = v
        for iid in (r1 + r2 + [title_id, sub_id, hint_id, close_id]):
            canvas.itemconfigure(iid, state="hidden" if v else "normal")
        for iid in (cr1 + cr2 + [ctitle_id, cclose_id]):
            canvas.itemconfigure(iid, state="normal" if v else "hidden")
        if v:
            cur[0], cur[1], cur[2] = CPW // 2, 60, 34
            chat_box.place_forget()
            status.place_forget()
            entry.place_forget()
        else:
            cur[0], cur[1], cur[2] = PW // 2, 84, 42
            chat_box.place(x=12, y=150, width=PW - 24, height=PH - 224)
            status.place(x=14, y=PH - 70)
            entry.place(x=12, y=PH - 44, width=PW - 24, height=26)
            entry.focus_set()
        apply_place()

    def on_click(ev):
        if compact[0]:
            if ev.x > CPW - 22 and ev.y < 22:
                root.destroy()
            else:
                show_compact(False)
        elif ev.x > PW - 40 and ev.y < 36:
            root.destroy()
        elif ev.y < 36:
            show_compact(True)
        else:
            entry.focus_set()
    canvas.bind("<Button-1>", on_click)

    t0 = time.perf_counter()

    def frame():
        ocx, ocy, orr = cur
        t = time.perf_counter() - t0
        sp = 5.0 if state[0] == "think" else 2.0
        for yy, rr2, base, i in dots:
            phi = base + t * sp
            xo = math.cos(phi) * rr2 * orr
            yo = yy * orr * 0.92
            shade = 0.5 + 0.5 * (1 - math.cos(phi)) / 2
            gold = (i % 6 == 0 and state[0] == "speak")
            if gold:
                col = GOLD
            else:
                col = f"#{min(255, int(150 * shade) + 40):02x}" \
                      f"{min(255, int(210 * shade) + 40):02x}" \
                      f"{min(255, int(255 * shade) + 30):02x}"
            canvas.coords(dot_ids[i], ocx + xo - 2, ocy + yo - 2, ocx + xo + 2, ocy + yo + 2)
            canvas.itemconfigure(dot_ids[i], fill=col)
        r_core = 9 if state[0] == "speak" else 7
        canvas.coords(core_id, ocx - r_core, ocy - r_core, ocx + r_core, ocy + r_core)
        canvas.itemconfigure(core_id,
                             fill=GOLD if state[0] == "think" else "#e1f5ff")
        canvas.coords(ring_id, ocx - orr - 5, ocy - orr - 5, ocx + orr + 5, ocy + orr + 5)
        root.after(50, frame)

    stop_flag = [False]
    move_flag = [False]
    thread_id = [0]

    def hotkey_loop():
        import ctypes
        import ctypes.wintypes as wt
        u = ctypes.windll.user32
        thread_id[0] = ctypes.windll.kernel32.GetCurrentThreadId()
        if u.RegisterHotKey(None, 1, 0x0001 | 0x0002, 0x4A):
            u.RegisterHotKey(None, 2, 0x0001 | 0x0002, 0x48)
            msg = wt.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:
                    if msg.wParam == 2:
                        move_flag[0] = True
                    else:
                        stop_flag[0] = True
                        break
            u.UnregisterHotKey(None, 1)
            u.UnregisterHotKey(None, 2)

    threading.Thread(target=hotkey_loop, daemon=True).start()

    def poll():
        if move_flag[0]:
            move_flag[0] = False
            try:
                nx, ny = pick_origin(skip=(pos[0], pos[1]))
                pos[0], pos[1] = nx, ny
                apply_place()
                _hud_log(f"repositioned to +{pos[0]}+{pos[1]}")
            except Exception:
                pass
        if stop_flag[0] or os.path.exists(OVERLAY_STOP_FILE):
            try:
                os.remove(OVERLAY_STOP_FILE)
            except OSError:
                pass
            if thread_id[0]:
                ctypes.windll.user32.PostThreadMessageW(thread_id[0], 0x0012, 0, 0)
            root.destroy()
            return
        root.after(400, poll)

    poll()
    frame()
    _hud_log(f"tk overlay v19 shown at +{pos[0]}+{pos[1]}")
    root.mainloop()


def _cleanup_hud_on_exit():
    try:
        with open(OVERLAY_STOP_FILE, "w", encoding="utf-8") as fh:
            fh.write("stop")
    except OSError:
        pass


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
        elif len(sys.argv) > 1 and sys.argv[1].lower() in ("hud", "голограмма", "holo"):
            use_tk = (sys.platform.startswith("win")
                      and os.environ.get("JARVIS_HUD_BACKEND", "tk").lower() == "tk")
            try:
                hud_overlay_tk() if use_tk else hud_overlay()
            except SystemExit:
                raise
            except Exception:
                _hud_log("overlay crashed:\n" + traceback.format_exc())
                raise
        elif len(sys.argv) > 1 and sys.argv[1].lower() in ("gui", "шар", "окно"):
            gui()
        elif len(sys.argv) > 1 and sys.argv[1].lower() in ("voice", "-v", "голос", "голосовой"):
            main_voice()
        else:
            main_text()
    except KeyboardInterrupt:
        print(f"\nJARVIS отключён, {MGMT}.")
