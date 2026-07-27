import os
import json
import subprocess
import tempfile
import requests
import shutil
import traceback
import urllib.parse
import re
from pathlib import Path
from datetime import datetime, date
from instagrapi import Client
import whisper

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("[WARNING] Pillow не установлен. Установите: pip install Pillow")

# --------------------- НАСТРОЙКИ ---------------------
DIRECT_FOLDER = "direct"
VOICE_CACHE_FOLDER = "voice_cache"
MEDIA_FOLDER = "media"
WHISPER_MODEL = "base"
LANGUAGE = "ru"
DEBUG = True
DIRECT_ONLY = True          # True - только личные диалоги
SAVE_MEDIA = True           # True - скачивать и конвертировать медиа
BUILD_USER_VOICE = True     # True - конкатенировать голосовые отдельно для меня и собеседника
PARSE_MEDIA = True          # True - распознавать аудио из видео и аудиофайлов (через Whisper)

os.makedirs(DIRECT_FOLDER, exist_ok=True)

FFMPEG_PATH = shutil.which('ffmpeg')
FFPROBE_PATH = shutil.which('ffprobe')
if not FFMPEG_PATH:
    print("[WARNING] ffmpeg не найден в PATH. Установите ffmpeg.")
if not FFPROBE_PATH:
    print("[WARNING] ffprobe не найден в PATH. Установите ffmpeg.")

def log(msg):
    if DEBUG:
        print(f"[DEBUG] {msg}")

# --------------------- Работа с sessionid ---------------------
def read_sessionid_from_file():
    filename = "sessionid.txt"
    if not os.path.exists(filename):
        return None
    for encoding in ['utf-8', 'cp1251', 'ascii']:
        try:
            with open(filename, 'r', encoding=encoding) as f:
                content = f.read().strip()
            if content:
                return content.strip()
        except:
            continue
    return None

def save_sessionid_to_file(sessionid):
    with open("sessionid.txt", "w", encoding="cp1251") as f:
        f.write(sessionid)

def get_sessionid():
    sid = read_sessionid_from_file()
    if sid:
        print("[OK] sessionid загружен из файла sessionid.txt")
        return sid
    print("Введите sessionid из cookie браузера (можно получить в DevTools -> Application -> Cookies -> sessionid):")
    sessionid = input().strip()
    if not sessionid:
        raise ValueError("Sessionid не может быть пустым")
    save_sessionid_to_file(sessionid)
    print("[OK] sessionid сохранён в sessionid.txt")
    return sessionid

# --------------------- Вспомогательные функции ---------------------
def login():
    print("Создание клиента...")
    client = Client()
    sessionid = get_sessionid()
    print("Выполняется вход по sessionid...")
    try:
        client.login_by_sessionid(sessionid)
        print("[OK] Вход выполнен успешно!")
        print(f"Авторизован как: @{client.username} (ID: {client.user_id})")
        return client
    except Exception as e:
        print(f"[ERROR] Ошибка входа: {e}")
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

# --------------------- Сериализация ---------------------
def serialize_obj(obj):
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: serialize_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [serialize_obj(v) for v in obj]
    try:
        return serialize_obj(obj.__dict__)
    except AttributeError:
        return str(obj)

# --------------------- Скачивание и конвертация ---------------------
def download_file_with_original_name(url, save_dir):
    url = str(url)
    log(f"Скачивание: {url}")
    try:
        response = requests.get(url, stream=True, timeout=60)
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}")

        filename = None
        if 'content-disposition' in response.headers:
            content_disposition = response.headers['content-disposition']
            matches = re.findall(r'filename\*?=([^;]+)', content_disposition)
            if matches:
                filename = matches[0].strip().strip('"')
                if filename.startswith("UTF-8''"):
                    filename = filename[7:]
                filename = urllib.parse.unquote(filename)

        if not filename:
            parsed = urllib.parse.urlparse(url)
            path = parsed.path
            filename = os.path.basename(path)
            if not filename:
                filename = f"file_{datetime.now().timestamp()}"
            if '?' in filename:
                filename = filename.split('?')[0]
            filename = urllib.parse.unquote(filename)

        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)

        if not filename or '.' not in filename:
            if not filename:
                filename = f"file_{datetime.now().timestamp()}.bin"
            else:
                filename += ".bin"

        save_path = os.path.join(save_dir, filename)
        if os.path.exists(save_path):
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(os.path.join(save_dir, f"{base}_{counter}{ext}")):
                counter += 1
            filename = f"{base}_{counter}{ext}"
            save_path = os.path.join(save_dir, filename)

        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        log(f"Скачивание завершено: {save_path}, размер: {os.path.getsize(save_path)} байт")
        return save_path
    except Exception as e:
        log(f"Ошибка скачивания: {e}")
        raise

def get_file_type(file_path):
    if not FFPROBE_PATH or not os.path.exists(file_path):
        return 'unknown'
    cmd = [
        FFPROBE_PATH,
        "-v", "error",
        "-show_entries", "format=format_name",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        if result.returncode != 0:
            return 'unknown'
        format_name = result.stdout.strip().lower()
    except:
        return 'unknown'

    ext = os.path.splitext(file_path)[1].lower()
    if ext in ['.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tiff']:
        pass
    if 'png' in format_name or 'jpeg' in format_name or 'jpg' in format_name or 'webp' in format_name or 'gif' in format_name:
        return 'image'
    elif 'mp4' in format_name or 'mov' in format_name or 'avi' in format_name or 'mkv' in format_name or 'webm' in format_name:
        return 'video'
    elif 'mp3' in format_name or 'ogg' in format_name or 'wav' in format_name or 'aac' in format_name:
        return 'audio'
    return 'unknown'

def get_image_format_pil(file_path):
    if not HAS_PIL:
        return None
    try:
        with Image.open(file_path) as img:
            fmt = img.format
            if fmt:
                return fmt.lower()
    except Exception:
        pass
    return None

def get_real_format(file_path):
    if not os.path.exists(file_path):
        return None

    if FFPROBE_PATH:
        cmd = [
            FFPROBE_PATH,
            "-v", "error",
            "-show_entries", "format=format_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path)
        ]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if result.returncode == 0:
                fmt = result.stdout.strip().lower()
                fmt = fmt.split(',')[0].strip()
                if fmt.startswith('png'):
                    return 'png'
                elif fmt.startswith('jpeg') or fmt.startswith('jpg'):
                    return 'jpeg'
                elif fmt.startswith('webp'):
                    return 'webp'
                elif fmt.startswith('gif'):
                    return 'gif'
                elif fmt.startswith('mp4'):
                    return 'mp4'
                elif fmt.startswith('mov'):
                    return 'mov'
                elif fmt.startswith('avi'):
                    return 'avi'
                elif fmt.startswith('mkv'):
                    return 'mkv'
                elif fmt.startswith('webm'):
                    return 'webm'
                elif fmt.startswith('mp3'):
                    return 'mp3'
                elif fmt.startswith('ogg'):
                    return 'ogg'
                elif fmt.startswith('wav'):
                    return 'wav'
                elif fmt.startswith('aac'):
                    return 'aac'
                else:
                    return fmt
        except:
            pass

    if HAS_PIL:
        try:
            with Image.open(file_path) as img:
                fmt = img.format
                if fmt:
                    fmt = fmt.lower()
                    if fmt in ('png', 'jpeg', 'jpg', 'webp', 'gif', 'bmp', 'tiff'):
                        return fmt
        except:
            pass

    try:
        with open(file_path, 'rb') as f:
            header = f.read(12)
        if header[:8] == b'\x89PNG\r\n\x1a\n':
            return 'png'
        if header[:3] == b'\xff\xd8\xff':
            return 'jpeg'
        if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
            return 'webp'
        if header[:6] in (b'GIF87a', b'GIF89a'):
            return 'gif'
        if header[4:8] == b'ftyp':
            return 'mp4'
        if header[:4] == b'OggS':
            return 'ogg'
    except:
        pass
    return None

def convert_to_png(input_path, output_path):
    if not FFMPEG_PATH:
        raise RuntimeError("ffmpeg не найден")
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Файл не найден: {input_path}")
    cmd = [
        FFMPEG_PATH,
        "-i", str(input_path),
        "-frames:v", "1",
        "-c:v", "png",
        "-y",
        str(output_path)
    ]
    log(f"Конвертация в PNG: {' '.join(cmd)}")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Ошибка конвертации: {result.stderr}")
    return output_path

def convert_to_mp4(input_path, output_path):
    if not FFMPEG_PATH:
        raise RuntimeError("ffmpeg не найден")
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Файл не найден: {input_path}")
    cmd = [
        FFMPEG_PATH,
        "-i", str(input_path),
        "-c:v", "libx264",
        "-c:a", "aac",
        "-movflags", "+faststart",
        "-y",
        str(output_path)
    ]
    log(f"Конвертация в MP4: {' '.join(cmd)}")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Ошибка конвертации: {result.stderr}")
    return output_path

def convert_to_ogg(input_path, output_path):
    if not FFMPEG_PATH:
        raise RuntimeError("ffmpeg не найден")
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Файл не найден: {input_path}")
    cmd = [
        FFMPEG_PATH,
        "-i", str(input_path),
        "-c:a", "libvorbis",
        "-q:a", "4",
        "-y",
        str(output_path)
    ]
    log(f"Конвертация в OGG: {' '.join(cmd)}")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Ошибка конвертации: {result.stderr}")
    return output_path

def process_media_file(msg_id, file_path, whisper_model, voice_cache_dir):
    if not os.path.exists(file_path):
        return f"media_audio(file not found)"

    file_type = get_file_type(file_path)
    if file_type == 'image':
        return None

    media_type = 'video' if file_type == 'video' else 'audio'
    cache_file = os.path.join(voice_cache_dir, f"media_{msg_id}.json")
    if os.path.exists(cache_file):
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f"[MEDIA] Кэш для {msg_id}: текст: {data['text']}")
        return f"media_{media_type}({data['text']})"

    print(f"[MEDIA] Обработка {msg_id} (без кэша)")
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            wav_path = Path(tmp_wav.name)
        try:
            extract_audio_to_wav(file_path, wav_path)
            text = transcribe_audio(wav_path, whisper_model)
            if not text:
                text = "..."
        finally:
            if wav_path.exists():
                wav_path.unlink()
    except Exception as e:
        log(f"Ошибка распознавания медиа {msg_id}: {e}")
        return f"media_{media_type}(error: {str(e)[:50]})"

    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump({'text': text}, f, ensure_ascii=False)
    return f"media_{media_type}({text})"

# --------------------- Аудио (Whisper) ---------------------
def download_audio(url, output_path):
    url = str(url)
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
        raise RuntimeError("ffprobe не найден")
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Файл не найден: {file_path}")
    cmd = [
        FFPROBE_PATH,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe ошибка: {result.stderr}")
    duration = float(result.stdout.strip())
    return duration

def extract_audio_to_wav(input_path, output_wav_path):
    if not FFMPEG_PATH:
        raise RuntimeError("ffmpeg не найден")
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Файл не найден: {input_path}")
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
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg ошибка: {result.stderr}")

def transcribe_audio(wav_path, model):
    log(f"Распознавание речи: {wav_path}")
    result = model.transcribe(str(wav_path), language=LANGUAGE)
    text = result["text"].strip()
    log(f"Распознано: {text if text else '(пусто)'}")
    return text

def get_voice_text(msg_id, audio_url, model, voice_cache_dir):
    cache_file = os.path.join(voice_cache_dir, f"{msg_id}.json")
    if os.path.exists(cache_file):
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f"[VOICE] Кэш для {msg_id}: длительность {data['duration']:.1f} сек, текст: {data['text']}")
        return data['duration'], data['text']

    print(f"[VOICE] Обработка {msg_id} (без кэша)")
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp_audio:
            audio_path = Path(tmp_audio.name)
        download_audio(audio_url, audio_path)
        duration = get_audio_duration(audio_path)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            wav_path = Path(tmp_wav.name)
        try:
            extract_audio_to_wav(audio_path, wav_path)
            text = transcribe_audio(wav_path, model)
            if not text:
                text = "..."
        finally:
            if wav_path.exists():
                wav_path.unlink()
    finally:
        if audio_path.exists():
            audio_path.unlink()

    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump({'duration': duration, 'text': text}, f, ensure_ascii=False)
    return duration, text

def format_voice(duration, text):
    minutes = int(duration // 60)
    seconds = int(duration % 60)
    return f"{minutes:01d}:{seconds:02d} voice({text})"

# --------------------- Конкатенация ---------------------
def concat_audio_files(file_list, output_path):
    if not file_list:
        return False
    if not FFMPEG_PATH:
        print("[WARNING] ffmpeg не найден, конкатенация невозможна")
        return False
    existing = [f for f in file_list if os.path.exists(f)]
    if not existing:
        print("[WARNING] Нет существующих файлов для конкатенации")
        return False
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        list_file = f.name
        for path in existing:
            f.write(f"file '{os.path.abspath(path)}'\n")
    cmd = [
        FFMPEG_PATH,
        "-f", "concat",
        "-safe", "0",
        "-i", list_file,
        "-c", "copy",
        "-y",
        str(output_path)
    ]
    log(f"Конкатенация: {' '.join(cmd)}")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    os.remove(list_file)
    if result.returncode != 0:
        print(f"[ERROR] Конкатенация не удалась: {result.stderr}")
        return False
    print(f"[OK] Конкатенированный аудиофайл: {output_path}")
    return True

# --------------------- Основная логика форматирования ---------------------
def get_message_content(msg, users, my_id, msg_dict, whisper_model, media_dir, voice_cache_dir, voice_files):
    item_type = getattr(msg, 'item_type', 'unknown')
    log(f"Обработка {msg.id}, тип: {item_type}")

    if item_type in ('text', 'link'):
        if hasattr(msg, 'link') and msg.link and hasattr(msg.link, 'text') and msg.link.text:
            return msg.link.text
        if hasattr(msg, 'text') and msg.text:
            return msg.text
        return ""

    if item_type == 'voice_media':
        audio_url = None
        if hasattr(msg, 'media') and msg.media and hasattr(msg.media, 'audio_url'):
            audio_url_obj = msg.media.audio_url
            if hasattr(audio_url_obj, '_url'):
                audio_url = audio_url_obj._url
            else:
                audio_url = audio_url_obj
        if audio_url:
            voice_filename = f"voice_{msg.id}.ogg"
            voice_path = os.path.join(media_dir, voice_filename)
            if not os.path.exists(voice_path):
                download_audio(audio_url, voice_path)

            if BUILD_USER_VOICE and SAVE_MEDIA:
                if str(msg.user_id) == str(my_id):
                    voice_files['me'].append(voice_path)
                else:
                    voice_files['other'].append(voice_path)

            try:
                duration, text = get_voice_text(msg.id, audio_url, whisper_model, voice_cache_dir)
                # --- ИЗМЕНЕНИЕ: добавляем имя файла, если SAVE_MEDIA ---
                if SAVE_MEDIA:
                    return f"[voice_message {os.path.basename(voice_path)}] {format_voice(duration, text)}"
                else:
                    return format_voice(duration, text)
            except Exception as e:
                log(f"Ошибка распознавания голосового: {e}")
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
            if hasattr(msg, 'raw_xma') and msg.raw_xma and isinstance(msg.raw_xma, dict):
                story_data = msg.raw_xma.get('xma_story_share', [{}])[0]
                if story_data.get('caption_body_text'):
                    return f"(share story: {story_data['caption_body_text']})"
            username = "unknown"
        return f"(share story @{username})"

    if item_type in ('media', 'animated_media', 'visual_media', 'clip'):
        if not SAVE_MEDIA:
            media_type = getattr(msg, 'media_type', None)
            if media_type == 1:
                return "[photo]"
            elif media_type == 2:
                return "[video]"
            else:
                return "[media]"

        media_obj = getattr(msg, 'media', None)
        if not media_obj:
            return "[media]"

        url = None
        media_type = getattr(media_obj, 'media_type', None)
        log(f"media_type={media_type}")

        if media_type == 1:
            if hasattr(media_obj, 'thumbnail_url'):
                url_obj = media_obj.thumbnail_url
                if hasattr(url_obj, '_url'):
                    url = url_obj._url
                else:
                    url = url_obj
        elif media_type == 2:
            if hasattr(media_obj, 'video_url'):
                url_obj = media_obj.video_url
                if hasattr(url_obj, '_url'):
                    url = url_obj._url
                else:
                    url = url_obj
        else:
            if hasattr(media_obj, 'video_url'):
                media_type = 2
                url_obj = media_obj.video_url
                if hasattr(url_obj, '_url'):
                    url = url_obj._url
                else:
                    url = url_obj
            elif hasattr(media_obj, 'audio_url'):
                media_type = 3
                url_obj = media_obj.audio_url
                if hasattr(url_obj, '_url'):
                    url = url_obj._url
                else:
                    url = url_obj
            elif hasattr(media_obj, 'thumbnail_url'):
                media_type = 1
                url_obj = media_obj.thumbnail_url
                if hasattr(url_obj, '_url'):
                    url = url_obj._url
                else:
                    url = url_obj
            else:
                return "[media]"

        if not url:
            return "[media]"

        try:
            url_str = str(url)
            file_path = download_file_with_original_name(url_str, media_dir)

            real_format = get_real_format(file_path)
            log(f"real_format={real_format}")

            if real_format is None:
                ext = os.path.splitext(file_path)[1].lower()
                if ext == '.png':
                    real_format = 'png'
                elif ext == '.webp':
                    real_format = 'webp'
                elif ext in ('.jpg', '.jpeg'):
                    real_format = 'jpeg'
                elif ext == '.mp4':
                    real_format = 'mp4'
                elif ext == '.ogg':
                    real_format = 'ogg'

            # Определяем желаемый формат
            if media_type == 1:
                desired_format = 'png'
            elif media_type == 2:
                desired_format = 'mp4'
            else:
                ext = os.path.splitext(file_path)[1].lower()
                if ext in ('.png', '.webp', '.jpg', '.jpeg', '.gif'):
                    desired_format = 'png'
                elif ext in ('.mp4', '.mov', '.avi'):
                    desired_format = 'mp4'
                else:
                    desired_format = 'ogg'

            log(f"desired_format={desired_format}")

            # Если реальный формат не совпадает с желаемым – конвертируем
            if real_format and real_format != desired_format:
                base, ext = os.path.splitext(file_path)
                converted_path = base + '_conv.' + desired_format
                counter = 1
                while os.path.exists(converted_path):
                    converted_path = base + f'_conv_{counter}.' + desired_format
                    counter += 1

                if desired_format == 'png':
                    convert_to_png(file_path, converted_path)
                elif desired_format == 'mp4':
                    convert_to_mp4(file_path, converted_path)
                elif desired_format == 'ogg':
                    convert_to_ogg(file_path, converted_path)
                else:
                    converted_path = file_path

                if converted_path != file_path and os.path.exists(converted_path):
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    file_path = converted_path

            # Дополнительная проверка для PNG
            if os.path.splitext(file_path)[1].lower() == '.png':
                is_png = False
                if HAS_PIL:
                    try:
                        with Image.open(file_path) as img:
                            if img.format and img.format.lower() == 'png':
                                is_png = True
                    except:
                        pass
                if not is_png:
                    log(f"Файл {file_path} имеет расширение .png, но не является PNG. Конвертируем в PNG.")
                    base, ext = os.path.splitext(file_path)
                    converted_path = base + '_conv.png'
                    counter = 1
                    while os.path.exists(converted_path):
                        converted_path = base + f'_conv_{counter}.png'
                        counter += 1
                    convert_to_png(file_path, converted_path)
                    if os.path.exists(converted_path):
                        if os.path.exists(file_path):
                            os.remove(file_path)
                        file_path = converted_path

            final_name = os.path.basename(file_path)
            # --- ИЗМЕНЕНИЕ: формируем строку с именем файла ---
            base_str = f"[media {final_name}]"

            if PARSE_MEDIA:
                file_type = get_file_type(file_path)
                if file_type in ('video', 'audio'):
                    result = process_media_file(msg.id, file_path, whisper_model, voice_cache_dir)
                    if result is not None:
                        return f"{base_str} {result}"   # добавляем текст
            # Если распознавание не удалось или это изображение – просто имя
            return base_str

        except FileNotFoundError as e:
            log(f"Ошибка обработки медиа {msg.id}: файл не найден - {e}")
            return "[media]"
        except Exception as e:
            log(f"Ошибка обработки медиа {msg.id}: {e}")
            return "[media]"

    return f"[{item_type}]"

def format_message(msg, users, my_id, msg_dict, whisper_model, media_dir, voice_cache_dir, voice_files, is_reply=False):
    sender = get_sender_name(msg.user_id, my_id, users)
    time_str = format_datetime(msg.timestamp)
    content = get_message_content(msg, users, my_id, msg_dict, whisper_model, media_dir, voice_cache_dir, voice_files)

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
        reply_content = get_message_content(reply_msg, users, my_id, msg_dict, whisper_model, media_dir, voice_cache_dir, voice_files)
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

# --------------------- Сохранение диалога ---------------------
def save_thread_messages(client, thread, whisper_model, dialog_index=None, total_dialogs=None):
    users = thread.users
    my_id = client.user_id
    thread_title = getattr(thread, 'title', None)
    other_users = [u for u in users if u.pk != my_id]

    if DIRECT_ONLY and len(other_users) != 1:
        print(f"[SKIP] Пропуск общего чата (участников: {len(other_users)})")
        return

    if len(other_users) == 1:
        display_name = f"@{other_users[0].username}"
        base_name = other_users[0].username
    else:
        if thread_title:
            display_name = f"@{thread_title}"
            base_name = thread_title.replace(" ", "_")
        else:
            names = [u.username for u in other_users[:3]]
            if len(other_users) > 3:
                names.append("...")
            display_name = "@" + ",".join(names)
            base_name = "_".join(names)

    dialog_dir = os.path.join(DIRECT_FOLDER, base_name)
    media_dir = os.path.join(dialog_dir, MEDIA_FOLDER)
    voice_cache_dir = os.path.join(dialog_dir, VOICE_CACHE_FOLDER)
    os.makedirs(dialog_dir, exist_ok=True)
    os.makedirs(media_dir, exist_ok=True)
    os.makedirs(voice_cache_dir, exist_ok=True)

    filename_txt = base_name + ".txt"
    filename_json = base_name + "_dump.json"
    filepath_txt = os.path.join(dialog_dir, filename_txt)
    filepath_json = os.path.join(dialog_dir, filename_json)

    if dialog_index is not None and total_dialogs is not None:
        print(f"\n[{dialog_index}/{total_dialogs}] Обработка диалога: {display_name}")
    else:
        print(f"\nОбработка диалога: {display_name}")

    print("Загрузка сообщений...")
    messages = client.direct_messages(thread.id, amount=10000)
    if not messages:
        print("Нет сообщений.")
        return
    print(f"Загружено {len(messages)} сообщений. Сортировка...")
    messages_sorted = sorted(messages, key=lambda m: m.timestamp)
    msg_dict = {m.id: m for m in messages_sorted}
    total_msgs = len(messages_sorted)

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
    print(f"[OK] JSON-дамп: {filepath_json}")

    voice_files = {'me': [], 'other': []}

    print(f"Сохранение текстового лога: {filepath_txt}")
    with open(filepath_txt, "w", encoding="utf-8") as f:
        for idx, msg in enumerate(messages_sorted, 1):
            try:
                line = format_message(msg, users, my_id, msg_dict, whisper_model, media_dir, voice_cache_dir, voice_files)
                f.write(line + "\n")
                if idx % 10 == 0 or idx == total_msgs:
                    if dialog_index is not None and total_dialogs is not None:
                        print(f"[{display_name} {idx}/{total_msgs}sms {dialog_index}/{total_dialogs}usr]")
                    else:
                        print(f"  {idx}/{total_msgs} сообщений")
            except Exception as e:
                print(f"[ERROR] Ошибка в сообщении {msg.id}: {e}")
                f.write(f"[Ошибка: {e}]\n")

    if BUILD_USER_VOICE and SAVE_MEDIA and (voice_files['me'] or voice_files['other']):
        print("[VOICE] Создание конкатенированных голосовых...")
        if voice_files['me']:
            concat_audio_files(voice_files['me'], os.path.join(media_dir, "my_self_voices.ogg"))
        if voice_files['other']:
            other_username = other_users[0].username if other_users else "other"
            concat_audio_files(voice_files['other'], os.path.join(media_dir, f"{other_username}_voices.ogg"))

    print(f"[OK] Готово. Текстовый лог: {filepath_txt}")
    print(f"[OK] JSON-дамп: {filepath_json}")

# --------------------- Главная функция ---------------------
def main():
    try:
        client = login()
    except Exception as e:
        print(f"[ERROR] Не удалось войти: {e}")
        return

    print(f"\nЗагрузка модели Whisper ('{WHISPER_MODEL}')...")
    whisper_model = whisper.load_model(WHISPER_MODEL)
    print("[OK] Модель загружена.\n")

    print("Загрузка списка последних 100 диалогов...")
    try:
        threads = client.direct_threads(amount=100, thread_message_limit=0)
        if not threads:
            print("Диалогов не найдено.")
            return
        print(f"Получено {len(threads)} диалогов:\n")
        for idx, thread in enumerate(threads, 1):
            users = thread.users
            usernames = [u.username for u in users]
            print(f"{idx}. {', '.join(usernames)}")
        print("0. Сохранить ВСЕ диалоги")

        choice = input("\nВведите номер (0 - все, q - выход): ").strip()
        if choice.lower() == 'q':
            return

        if choice == '0':
            total = len(threads)
            for i, thread in enumerate(threads, 1):
                save_thread_messages(client, thread, whisper_model, dialog_index=i, total_dialogs=total)
            print(f"\n[OK] Все {total} диалогов сохранены.")
        else:
            try:
                idx = int(choice)
                if idx < 1 or idx > len(threads):
                    print("Неверный номер.")
                    return
                save_thread_messages(client, threads[idx-1], whisper_model)
            except ValueError:
                print("Введите число.")
    except Exception as e:
        print(f"[ERROR] Ошибка: {e}")

if __name__ == "__main__":
    main()