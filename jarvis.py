#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JARVIS Lite — сверхлёгкий голосовой ассистент в стиле J.A.R.V.I.S.

Только стандартная библиотека Python: ни одного pip-пакета.
Озвучка — espeak-ng (поддерживает русский даже на старом железе).
Ответы — Mistral API, либо локальная Ollama в роли запасного варианта.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# ------------- Настройки (переопределяются переменными окружения) -------------

API_KEY = os.environ.get("MISTRAL_API_KEY", "")
API_URL = os.environ.get("JARVIS_API_URL", "https://api.mistral.ai/v1/chat/completions")
MODEL = os.environ.get("JARVIS_MODEL", "mistral-small-latest")

OLLAMA_URL = os.environ.get("JARVIS_OLLAMA", "http://127.0.0.1:11434/api/chat")
LOCAL_MODEL = os.environ.get("JARVIS_LOCAL_MODEL", "qwen2.5:0.5b-instruct")

VOICE = os.environ.get("JARVIS_VOICE", "ru")
VARIANT = os.environ.get("JARVIS_VARIANT", "")   # например mb-ru2, если установлен mbrola
RATE = os.environ.get("JARVIS_RATE", "155")
PITCH = os.environ.get("JARVIS_PITCH", "45")
MAX_TOKENS = int(os.environ.get("JARVIS_MAX_TOKENS", "300"))

VOLUME = max(0, min(200, int(os.environ.get("JARVIS_VOLUME", "100"))))
PLAYER = os.environ.get("JARVIS_PLAYER", "")     # шаблон проигрывателя: "aplay {file}" или "aplay -D {device} {file}"
DEVICE = os.environ.get("JARVIS_DEVICE", "")     # аудиоустройство для {device} в JARVIS_PLAYER

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
    except subprocess.CalledProcessError as exc:
        print(f"\n[ ошибка озвучки: {exc} ]")
    except OSError as exc:
        print(f"\n[ ошибка озвучки: {exc} ]")

# ----------------------------- Вызов модели -----------------------------

def encode_json(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def stream_mistral(messages):
    data = {"model": MODEL, "messages": messages, "stream": True, "max_tokens": MAX_TOKENS}
    req = urllib.request.Request(
        API_URL,
        data=encode_json(data),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
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
            delta = event.get("choices", [{}])[0].get("delta", {})
            token = delta.get("content") or ""
            if token:
                yield token


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
    """Генератор токенов ответа: облако, а при отказе — локальная Ollama."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    if API_KEY:
        try:
            yield from stream_mistral(messages)
            return
        except (urllib.error.URLError, OSError) as exc:
            print(f"\n[ Mistral API недоступен: {exc}, пробую Ollama ]")
    if not API_KEY:
        raise RuntimeError("Не задан MISTRAL_API_KEY и локальная Ollama не отвечает")
    yield from stream_ollama(messages)

# ------------- Мгновенные команды, которые не тратят запросы API -------------

def set_volume(value):
    global VOLUME
    VOLUME = max(0, min(200, int(value)))


def instant_command(query):
    q = query.strip().lower()
    if q in ("громче", "!громче", "louder"):
        set_volume(VOLUME + 20)
        return f"Увеличиваю громкость до {VOLUME} процентов, {MGMT}."
    if q in ("тише", "!тише", "quieter"):
        set_volume(VOLUME - 20)
        return f"Уменьшаю громкость до {VOLUME} процентов, {MGMT}."
    m = re.fullmatch(r"(?:!)?(?:громкость|volume)\s*(\d{1,3})", q)
    if m:
        set_volume(m.group(1))
        return f"Громкость установлена на {VOLUME} процентов, {MGMT}."
    if q in ("громкость", "!громкость"):
        return f"Текущая громкость — {VOLUME} процентов, {MGMT}."
    if q in ("время", "который час", "!время"):
        return f"Сейчас {time.strftime('%H:%M')}, {MGMT}."
    if q in ("дата", "какое сегодня число", "!дата"):
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

# ----------------------------- Главный цикл -----------------------------

SENTENCE_SPLIT = re.compile(r"(?<=[.!?…\n])\s+")


def pop_sentences(buffer):
    """Отдаёт завершённые предложения из буфера, возвращает незавершённый хвост."""
    parts = SENTENCE_SPLIT.split(buffer)
    if len(parts) < 2:
        return [], buffer
    done, tail = parts[:-1], parts[-1]
    return [s.strip() for s in done if s.strip()], tail


def main():
    engine = find_tts()
    if not engine:
        print("[ предупреждение: espeak-ng не найден — ответы будут только текстом. "
              "Установите: sudo apt install espeak-ng ]")

    mode = "Mistral API" if API_KEY else f"Ollama/{LOCAL_MODEL}"
    print(f"=== JARVIS Lite | режим: {mode} | озвучка: {engine or 'нет'} ===")
    print(f"Готов к работе, {MGMT}. ('выход' — завершить)\n")

    history = []

    while True:
        try:
            query = input("Вы > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query:
            continue
        if query.lower() in ("выход", "exit", "quit"):
            speak(engine, f"Всего наилучшего, {MGMT}. Отключаюсь.")
            break

        answer = instant_command(query)
        if answer:
            print("JARVIS > " + answer)
            speak(engine, answer)
            history.extend([
                {"role": "user", "content": query},
                {"role": "assistant", "content": answer},
            ])
            continue

        history.append({"role": "user", "content": query})
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
            history.pop()
            continue

        tail = buffer.strip()
        if tail:
            speak(engine, tail)
        print()

        if not full.strip():
            print("[ модель вернула пустой ответ ]")
            history.pop()
            continue
        history.append({"role": "assistant", "content": full.strip()})


if __name__ == "__main__":
    if os.name == "nt" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    try:
        main()
    except KeyboardInterrupt:
        print(f"\nJARVIS отключён, {MGMT}.")
