import os
import json
import subprocess
import tempfile
import requests
import shutil
import traceback
from pathlib import Path
from datetime import datetime, date
from instagrapi import Client
import whisper

# --------------------- НАСТРОЙКИ ---------------------
DIRECT_FOLDER = "direct"
VOICE_CACHE_FOLDER = os.path.join(DIRECT_FOLDER, "voice_cache")
WHISPER_MODEL = "base"
LANGUAGE = "ru"
DEBUG = True  # Включить подробный вывод

# Создаём папки
os.makedirs(DIRECT_FOLDER, exist_ok=True)
os.makedirs(VOICE_CACHE_FOLDER, exist_ok=True)

# Проверяем наличие ffmpeg и ffprobe
FFMPEG_PATH = shutil.which('ffmpeg')
FFPROBE_PATH = shutil.which('ffprobe')
if not FFMPEG_PATH:
    print("⚠️ ffmpeg не найден в PATH. Установите ffmpeg и добавьте в переменную окружения PATH.")
if not FFPROBE_PATH:
    print("⚠️ ffprobe не найден в PATH. Установите ffmpeg (включает ffprobe).")

def log(msg):
    if DEBUG:
        print(f"[DEBUG] {msg}")

# --------------------- Вспомогательные функции ---------------------

def get_sessionid():
    print("Введите sessionid из cookie браузера (можно получить в DevTools -> Application -> Cookies -> sessionid):")
    sessionid = input().strip()
    if not sessionid:
        raise ValueError("Sessionid не может быть пустым")
    return sessionid

def login():
    print("Создание клиента...")
    client = Client()
    sessionid = get_sessionid()
    print("Выполняется вход по sessionid...")
    try:
        client.login_by_sessionid(sessionid)
        print("Вход выполнен успешно!")
        print(f"Авторизован как: @{client.username} (ID: {client.user_id})")
        return client
    except Exception as e:
        print(f"Ошибка входа: {e}")
        raise

def format_datetime(ts):
    if isinstance(ts, datetime):
        dt = ts
    else:
        dt = datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M:%S %d.%m.%Y")

def get_username_by_id(user_id, users):
    user = next((u for u in users if u.pk == user_id), None)
    return user.username if user else str(user_id)

def get_sender_name(user_id, my_id, users):
    if str(user_id) == str(my_id):
        return "me (self)"
    return get_username_by_id(user_id, users)

# --------------------- Сериализация для JSON-дампа ---------------------

def serialize_obj(obj):
    """Рекурсивно преобразует объект в JSON-сериализуемый формат."""
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: serialize_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [serialize_obj(v) for v in obj]
    # Для объектов с атрибутами пытаемся преобразовать в dict
    try:
        # если есть __dict__, используем его
        return serialize_obj(obj.__dict__)
    except AttributeError:
        # иначе просто строковое представление
        return str(obj)

# --------------------- Работа с аудио / Whisper ---------------------

def download_audio(url, output_path):
    log(f"Скачивание аудио: {url} -> {output_path}")
    try:
        response = requests.get(url, stream=True, timeout=60)
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}")
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        log(f"Скачивание завершено, размер: {os.path.getsize(output_path)} байт")
    except Exception as e:
        log(f"Ошибка скачивания: {e}")
        raise

def get_audio_duration(file_path):
    if not FFPROBE_PATH:
        raise RuntimeError("ffprobe не найден, невозможно определить длительность.")
    cmd = [
        FFPROBE_PATH,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ]
    log(f"Выполняется ffprobe: {' '.join(cmd)}")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        log(f"ffprobe stderr: {result.stderr}")
        raise RuntimeError(f"ffprobe ошибка: {result.stderr}")
    duration = float(result.stdout.strip())
    log(f"Длительность: {duration} сек")
    return duration

def extract_audio_to_wav(input_path, output_wav_path):
    if not FFMPEG_PATH:
        raise RuntimeError("ffmpeg не найден, невозможно конвертировать.")
    cmd = [
        FFMPEG_PATH,
        "-i", str(input_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        "-y",
        str(output_wav_path)
    ]
    log(f"Выполняется ffmpeg: {' '.join(cmd)}")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        log(f"ffmpeg stderr: {result.stderr}")
        raise RuntimeError(f"FFmpeg ошибка:\n{result.stderr}")
    log(f"Конвертация успешна: {output_wav_path}")

def transcribe_audio(wav_path, model):
    log(f"Распознавание речи: {wav_path}")
    result = model.transcribe(str(wav_path), language=LANGUAGE)
    text = result["text"].strip()
    log(f"Распознано: {text if text else '(пусто)'}")
    return text

def get_voice_text(msg_id, audio_url, model):
    """
    Возвращает (duration_seconds, recognized_text) для голосового сообщения.
    Использует кэширование по msg_id.
    """
    cache_file = os.path.join(VOICE_CACHE_FOLDER, f"{msg_id}.json")
    if os.path.exists(cache_file):
        log(f"Кэш найден: {cache_file}")
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f"[VOICE] Использован кэш для {msg_id}: длительность {data['duration']:.1f} сек, текст: {data['text']}")
        return data['duration'], data['text']

    print(f"[VOICE] Обработка голосового {msg_id} (без кэша)")
    # Скачиваем аудио во временный файл
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp_audio:
            audio_path = Path(tmp_audio.name)
            log(f"Временный аудиофайл: {audio_path}")
        print("[VOICE] Скачивание аудио...")
        download_audio(audio_url, audio_path)

        print("[VOICE] Определение длительности...")
        duration = get_audio_duration(audio_path)
        print(f"[VOICE] Длительность: {duration:.1f} сек")

        # Конвертируем в WAV
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            wav_path = Path(tmp_wav.name)
        try:
            print("[VOICE] Конвертация в WAV...")
            extract_audio_to_wav(audio_path, wav_path)
            print("[VOICE] Распознавание речи...")
            text = transcribe_audio(wav_path, model)
            if not text:
                text = "..."
            print(f"[VOICE] Распознано: {text}")
        finally:
            if wav_path.exists():
                wav_path.unlink()
                log(f"Удалён временный WAV: {wav_path}")
    finally:
        if audio_path.exists():
            audio_path.unlink()
            log(f"Удалён временный аудиофайл: {audio_path}")

    # Сохраняем в кэш
    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump({'duration': duration, 'text': text}, f, ensure_ascii=False)
    log(f"Кэш сохранён: {cache_file}")

    return duration, text

def format_voice(duration, text):
    minutes = int(duration // 60)
    seconds = int(duration % 60)
    time_str = f"{minutes:01d}:{seconds:02d}"
    return f"{time_str} voice({text})"

# --------------------- Основная логика форматирования сообщения ---------------------

def get_message_content(msg, users, my_id, msg_dict, whisper_model):
    item_type = getattr(msg, 'item_type', 'unknown')
    log(f"Обработка сообщения {msg.id}, тип: {item_type}")

    if item_type in ('text', 'link'):
        if hasattr(msg, 'link') and msg.link and hasattr(msg.link, 'text') and msg.link.text:
            return msg.link.text
        if hasattr(msg, 'text') and msg.text:
            return msg.text
        return ""

    if item_type == 'voice_media':
        audio_url = None
        if hasattr(msg, 'media') and msg.media:
            if hasattr(msg.media, 'audio_url'):
                audio_url_obj = msg.media.audio_url
                if hasattr(audio_url_obj, '_url'):
                    audio_url = audio_url_obj._url
                else:
                    audio_url = audio_url_obj
        if audio_url:
            try:
                duration, text = get_voice_text(msg.id, audio_url, whisper_model)
                return format_voice(duration, text)
            except Exception as e:
                log(f"Ошибка обработки голосового: {e}\n{traceback.format_exc()}")
                return "[voice error]"
        else:
            return "[voice]"

    if item_type in ('xma_story_share', 'story_share'):
        username = None
        if hasattr(msg, 'raw_xma') and msg.raw_xma:
            raw = msg.raw_xma
            if isinstance(raw, dict) and 'xma_story_share' in raw and raw['xma_story_share']:
                share_data = raw['xma_story_share'][0] if isinstance(raw['xma_story_share'], list) else raw['xma_story_share']
                if isinstance(share_data, dict):
                    username = share_data.get('header_title_text')
        if not username and hasattr(msg, 'story_share') and msg.story_share:
            if hasattr(msg.story_share, 'user') and hasattr(msg.story_share.user, 'username'):
                username = msg.story_share.user.username
        if not username:
            username = "unknown"
        return f"(share story @{username})"

    if item_type in ('media', 'animated_media', 'visual_media', 'clip'):
        media_type = getattr(msg, 'media_type', None)
        if media_type == 1:
            return "[photo]"
        elif media_type == 2:
            return "[video]"
        elif media_type == 11:
            return "[voice]"
        else:
            return "[media]"

    return f"[{item_type}]"

def format_message(msg, users, my_id, msg_dict, whisper_model, is_reply=False):
    sender = get_sender_name(msg.user_id, my_id, users)
    time_str = format_datetime(msg.timestamp)
    content = get_message_content(msg, users, my_id, msg_dict, whisper_model)

    reply_msg = None
    if hasattr(msg, 'reply') and msg.reply:
        reply_msg = msg.reply
    elif hasattr(msg, 'reply_to_message') and msg.reply_to_message:
        reply_msg = msg.reply_to_message
    elif hasattr(msg, 'reply_to_message_id') and msg.reply_to_message_id:
        reply_msg = msg_dict.get(msg.reply_to_message_id)

    if reply_msg:
        reply_sender = get_sender_name(reply_msg.user_id, my_id, users)
        reply_time = format_datetime(reply_msg.timestamp)
        reply_content = get_message_content(reply_msg, users, my_id, msg_dict, whisper_model)
        reply_part = f"(reply @{reply_sender} {reply_time}: {reply_content})"
    else:
        reply_part = ""

    if is_reply:
        return content
    else:
        if reply_part:
            return f"{sender} {time_str} {reply_part}: {content}"
        else:
            return f"{sender} {time_str}: {content}"

# --------------------- Основная функция сохранения диалога ---------------------

def save_thread_messages(client, thread, whisper_model):
    print("Загрузка всех сообщений диалога (это может занять время)...")
    messages = client.direct_messages(thread.id, amount=10000)
    if not messages:
        print("В этом диалоге нет сообщений.")
        return
    print(f"Загружено {len(messages)} сообщений. Сортировка...")
    messages_sorted = sorted(messages, key=lambda m: m.timestamp)
    msg_dict = {m.id: m for m in messages_sorted}
    users = thread.users
    my_id = client.user_id

    other_users = [u for u in users if u.pk != my_id]
    if len(other_users) == 1:
        base_name = other_users[0].username
    else:
        if thread.title:
            base_name = thread.title.replace(" ", "_")
        else:
            names = [u.username for u in other_users[:3]]
            if len(other_users) > 3:
                names.append("...")
            base_name = "_".join(names)

    filename_txt = base_name + ".txt"
    filename_json = base_name + "_dump.json"
    filepath_txt = os.path.join(DIRECT_FOLDER, filename_txt)
    filepath_json = os.path.join(DIRECT_FOLDER, filename_json)

    print(f"Сохранение текстового лога в: {filepath_txt}")
    print(f"Сохранение JSON-дампа в: {filepath_json}")

    # ------------------- Текстовый лог -------------------
    with open(filepath_txt, "w", encoding="utf-8") as f:
        for idx, msg in enumerate(messages_sorted, 1):
            try:
                line = format_message(msg, users, my_id, msg_dict, whisper_model)
                f.write(line + "\n")
                if idx % 100 == 0:
                    print(f"   Обработано {idx} сообщений...")
            except Exception as e:
                print(f"Ошибка при обработке сообщения {msg.id}: {e}")
                f.write(f"[Ошибка: {e}]\n")

    # ------------------- JSON-дамп всех сообщений -------------------
    dump_data = []
    for msg in messages_sorted:
        try:
            serialized = serialize_obj(msg)
        except Exception as e:
            log(f"Ошибка сериализации {msg.id}: {e}")
            serialized = str(msg)
        dump_data.append(serialized)

    with open(filepath_json, "w", encoding="utf-8") as f:
        json.dump(dump_data, f, ensure_ascii=False, indent=2, default=str)

    # Вывод информации о структуре первого сообщения (для отладки)
    if messages_sorted and DEBUG:
        first_msg = messages_sorted[0]
        print("\n=== Пример структуры первого сообщения (ключи и типы) ===")
        for key, value in first_msg.__dict__.items():
            print(f"{key}: {type(value).__name__} = {repr(value)[:100]}")
        print("=================================================\n")
        print(f"Чтобы увидеть все атрибуты, откройте файл: {filepath_json}")

    print(f"Готово. Текстовый лог: {filepath_txt}")
    print(f"JSON-дамп: {filepath_json}")

# --------------------- Главная функция ---------------------

def main():
    try:
        client = login()
    except Exception as e:
        print(f"Не удалось войти: {e}")
        return

    print(f"\nЗагрузка модели Whisper ('{WHISPER_MODEL}')...")
    whisper_model = whisper.load_model(WHISPER_MODEL)
    print("Модель загружена.\n")

    print("Загрузка списка последних 20 диалогов...")
    try:
        threads = client.direct_threads(amount=20, thread_message_limit=0)
        if not threads:
            print("Диалогов не найдено.")
            return
        print(f"Получено {len(threads)} диалогов:\n")
        for idx, thread in enumerate(threads, 1):
            users = thread.users
            usernames = [u.username for u in users]
            print(f"{idx}. {', '.join(usernames)}")

        print()
        choice = input("Введите номер диалога для сохранения (или q для выхода): ").strip()
        if choice.lower() == 'q':
            print("Выход.")
            return
        try:
            idx = int(choice)
            if idx < 1 or idx > len(threads):
                print("Неверный номер.")
                return
            selected_thread = threads[idx - 1]
            save_thread_messages(client, selected_thread, whisper_model)
        except ValueError:
            print("Введите число.")
    except Exception as e:
        print(f"Ошибка: {e}")

if __name__ == "__main__":
    main()