import os
import re
import shutil
import subprocess
import json
from datetime import datetime, timedelta
from urllib.parse import urlparse
from telethon import TelegramClient, events, Button
from mutagen import File
import asyncio


# Orpheus sequential queue
orpheus_queue = asyncio.Queue()
orpheus_running = False

api_id = '10074048'
api_hash = 'a08b1ed3365fa3b04bcf2bcbf71aff4d'
session_name = 'beatport_downloader'


beatport_track_pattern    = r'^https:\/\/www\.beatport\.com(?:\/[a-z]{2})?\/track\/[\w\-]+\/\d+(?:\?.*)?$'
beatport_album_pattern    = r'^https:\/\/www\.beatport\.com(?:\/[a-z]{2})?\/release\/[\w\-]+\/\d+(?:\?.*)?$'
beatport_playlist_pattern = r'^https:\/\/www\.bert\.com(?:\/[a-z]{2})?\/(library\/playlists|playlists\/share)\/\d+(?:\?.*)?$'
beatport_chart_pattern    = r'^https:\/\/www\.port\.com(?:\/[a-z]{2})?\/chart\/[\w\-]+\/\d+(?:\?.*)?$'

state = {}
ADMIN_IDS = [616584208]
PAYMENT_URL = "https://ko-fi.com/zackant"
USERS_FILE = 'users.json'
WHITELIST_PAGES = {}


def extract_embedded_cover(filepath):
    """
    Returns (bytes, extension) if embedded artwork exists.
    Otherwise returns (None, None).
    """
    try:
        audio = File(filepath)

        # FLAC
        if filepath.lower().endswith(".flac"):
            if hasattr(audio, "pictures") and audio.pictures:
                pic = audio.pictures[0]
                ext = pic.mime.split("/")[-1]
                return pic.data, ext

        # MP3
        if filepath.lower().endswith(".mp3"):
            if audio.tags:
                for tag in audio.tags.values():
                    if tag.FrameID == "APIC":
                        ext = tag.mime.split("/")[-1]
                        return tag.data, ext

        return None, None
    except:
        return None, None


def safe_filename(name: str) -> str:
    return re.sub(r'[\/:*?"<>|]', '_', name)

def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, 'r') as f:
        return json.load(f)

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f)

def reset_if_needed(user):
    today_str = datetime.utcnow().strftime('%Y-%m-%d')
    if user.get("last_reset") != today_str:
        user["album_today"] = 0
        user["track_today"] = 0
        user["last_reset"] = today_str

def is_user_allowed(user_id, content_type):
    if user_id in ADMIN_IDS:
        return True
    users = load_users()
    user = users.get(str(user_id), {})
    reset_if_needed(user)
    if user.get('expiry'):
        if datetime.strptime(user['expiry'], '%Y-%m-%d') > datetime.utcnow():
            return True
    if content_type == 'album' and user.get("album_today", 0) >= 2:
        return False
    if content_type == 'track' and user.get("track_today", 0) >= 2:
        return False
    return True

def increment_download(user_id, content_type):
    if user_id in ADMIN_IDS:
        return
    users = load_users()
    uid = str(user_id)
    if uid not in users:
        users[uid] = {}
    user = users[uid]
    reset_if_needed(user)
    if content_type == 'album':
        user["album_today"] = user.get("album_today", 0) + 1
    elif content_type == 'track':
        user["track_today"] = user.get("track_today", 0) + 1
    save_users(users)

def whitelist_user(user_id):
    users = load_users()
    users[str(user_id)] = {
        "expiry": (datetime.utcnow() + timedelta(days=30)).strftime('%Y-%m-%d'),
        "album_today": 0,
        "track_today": 0,
        "last_reset": datetime.utcnow().strftime('%Y-%m-%d')
    }
    save_users(users)

def remove_user(user_id):
    users = load_users()
    if str(user_id) in users:
        users.pop(str(user_id))
        save_users(users)
        return True
    return False

client = TelegramClient(session_name, api_id, api_hash)


async def run_orpheus(user_id, url):
    global orpheus_running
    future = asyncio.get_event_loop().create_future()
    await orpheus_queue.put((user_id, url, future))
    await process_queue()
    await future


async def process_queue():
    global orpheus_running
    if orpheus_running:
        return

    while not orpheus_queue.empty():
        orpheus_running = True
        user_id, url, future = await orpheus_queue.get()

        try:
            proc = await asyncio.create_subprocess_exec(
                'python', 'orpheus.py', url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                print(f"[{user_id}] Orpheus failed: {stderr.decode()}")
            else:
                print(f"[{user_id}] Orpheus finished successfully")

        except Exception as e:
            print(f"[{user_id}] Error running Orpheus: {e}")
        finally:
            future.set_result(True)
            orpheus_queue.task_done()

    orpheus_running = False


async def handle_conversion_and_sending(event, format_choice, input_text, content_type):
    try:
        from urllib.parse import urlparse
        import os, subprocess, shutil
        from mutagen import File
        from datetime import datetime

        url = urlparse(input_text)
        components = url.path.split('/')
        release_id = components[-1]

        # Handle ALBUM, TRACK, PLAYLIST, CHART
        if content_type in ["album", "playlist", "chart"]:
            root_path = f'downloads/{release_id}'
            if not os.path.exists(root_path):
                await event.reply("Download folder not found, something went wrong.")
                return

            subfolders = [f.path for f in os.scandir(root_path) if f.is_dir()]
            main_folder = subfolders[0] if subfolders else root_path
            title_name = os.path.basename(main_folder) if content_type in ["playlist", "chart"] else None

            # Collect all FLAC files
            flac_files = []
            for root, _, files in os.walk(main_folder):
                flac_files.extend([os.path.join(root, f) for f in files if f.lower().endswith('.flac')])

            if not flac_files:
                await event.reply("No FLAC files found in download.")
                return

            # Metadata aggregation
            all_artists = "Various Artists" if content_type in ["playlist", "chart"] else set()
            genres, labels, dates, bpms, keys = set(), set(), [], [], set()

            for f in flac_files:
                audio = File(f, easy=True)
                if audio:
                    if content_type not in ["playlist", "chart"]:
                        for key_field in ('artist', 'performer', 'albumartist'):
                            if key_field in audio:
                                all_artists.update(audio[key_field])
                    if 'genre' in audio: genres.update(audio['genre'])
                    if 'label' in audio: labels.update(audio['label'])
                    if 'date' in audio:
                        try:
                            d = datetime.strptime(audio['date'][0], '%Y-%m-%d')
                            dates.append(d)
                        except: pass
                    if 'bpm' in audio:
                        try: bpms.append(float(audio['bpm'][0]))
                        except: pass
                    if 'initialkey' in audio: keys.update(audio['initialkey'])
                    elif 'key' in audio: keys.update(audio['key'])

            if content_type not in ["playlist", "chart"]:
                artists_str = ", ".join(sorted(all_artists)) or "Various Artists"
            else:
                artists_str = "Various Artists"

            genre_str = ", ".join(sorted(genres)) if genres else "Unknown Genre"
            label_str = ", ".join(sorted(labels)) if labels else "--"
            key_str = ", ".join(sorted(keys)) if keys else "--"
            date_str = f"{min(dates).strftime('%Y-%m-%d')} - {max(dates).strftime('%Y-%m-%d')}" if len(dates) > 1 else dates[0].strftime('%Y-%m-%d') if dates else "--"
            bpm_str = f"{int(min(bpms))}-{int(max(bpms))}" if len(bpms) > 1 else str(int(bpms[0])) if bpms else "--"

            if content_type == "album":
                sample_file = flac_files[0]
                metadata = File(sample_file, easy=True) or {}
                title_name = metadata.get('album', ['Unknown Album'])[0]

            caption = (
                f"<b>\U0001F3B6 {content_type.capitalize()}:</b> {title_name}\n"
                f"<b>\U0001F464 Artists:</b> {artists_str}\n"
                f"<b>📻 Genre:</b> {genre_str}\n"
                f"<b>🏷️ Label:</b> {label_str}\n"
                f"<b>\U0001F3B9 Key:</b> {key_str}\n"
                f"<b>\U0001F4C5 Release Date:</b> {date_str}\n"
                f"<b>\U0001F9E9 BPM:</b> {bpm_str}\n"
            )

            # -----------------------------------------
            # UPDATED: EMBEDDED COVER SUPPORT (ALBUM)
            # -----------------------------------------
            embedded_cover_data, embedded_ext = extract_embedded_cover(flac_files[0])
            if embedded_cover_data:
                cover_path = f"{main_folder}/embedded_cover.{embedded_ext}"
                with open(cover_path, "wb") as img:
                    img.write(embedded_cover_data)
                await client.send_file(event.chat_id, cover_path, caption=caption, parse_mode='html')
            else:
                # fallback to folder cover
                cover_file = None
                for root, _, files in os.walk(main_folder):
                    for f in files:
                        if f.lower().startswith('cover') and f.lower().endswith(('.jpg', '.jpeg', '.png')):
                            cover_file = os.path.join(root, f)
                            break
                if cover_file:
                    await client.send_file(event.chat_id, cover_file, caption=caption, parse_mode='html')
                else:
                    await event.reply(caption, parse_mode='html')

            # Convert & send tracks
            for input_path in flac_files:
                output_path = f"{input_path}.{format_choice}"

                if format_choice == 'flac':
                    subprocess.run(['ffmpeg', '-n', '-i', input_path, output_path])
                    audio = File(output_path, easy=True)
                    artist = audio.get('artist', ['Unknown Artist'])[0]
                    title = audio.get('title', ['Unknown Title'])[0]
                    for field in ['artist', 'title', 'album', 'genre']:
                        if field in audio:
                            audio[field] = [value.replace(";", ", ") for value in audio[field]]
                    audio.save()
                    final_name = safe_filename(f"{artist} - {title}.{format_choice}".replace(";", ", "))
                    final_path = os.path.join(os.path.dirname(input_path), final_name)
                    os.rename(output_path, final_path)
                    await client.send_file(event.chat_id, final_path)

                elif format_choice == 'mp3':
                    subprocess.run(['ffmpeg', '-n', '-i', input_path, '-b:a', '320k', output_path])
                    audio = File(output_path, easy=True)
                    artist = audio.get('artist', ['Unknown Artist'])[0]
                    title = audio.get('title', ['Unknown Title'])[0]
                    for field in ['artist', 'title', 'album', 'genre']:
                        if field in audio:
                            audio[field] = [value.replace(";", ", ") for value in audio[field]]
                    audio.save()
                    final_name = safe_filename(f"{artist} - {title}.{format_choice}".replace(";", ", "))
                    final_path = os.path.join(os.path.dirname(input_path), final_name)
                    os.rename(output_path, final_path)
                    await client.send_file(event.chat_id, final_path)

                elif format_choice == 'wav':
                    subprocess.run(['ffmpeg', '-n', '-i', input_path, output_path])
                    original_audio = File(input_path, easy=True)
                    artists = original_audio.get('artist', ['Unknown Artist'])
                    clean_artists = ", ".join([a.strip() for a in ";".join(artists).split(";")])
                    track_title = original_audio.get('title', ['Unknown Title'])[0]
                    final_name = safe_filename(f"{clean_artists} - {track_title}.wav")
                    final_path = os.path.join(os.path.dirname(input_path), final_name)
                    os.rename(output_path, final_path)
                    await client.send_file(event.chat_id, final_path, force_document=True)

            shutil.rmtree(root_path)
            increment_download(event.chat_id, content_type)
            del state[event.chat_id]

        # -------------------------------
        # TRACK MODE
        # -------------------------------
        elif content_type == "track":
            download_dir = f'downloads/{components[-1]}'
            filename = os.listdir(download_dir)[0]
            filepath = f'{download_dir}/{filename}'
            converted_filepath = f'{download_dir}/{filename}.{format_choice}'

            audio = File(filepath, easy=True) or {}
            title_name = audio.get('title', ['Unknown Title'])[0]
            artists = ", ".join(audio.get('artist', ['Unknown Artist']))
            genre = ", ".join(audio.get('genre', ['Unknown Genre']))
            album = audio.get('album', ['--'])[0]
            label = ", ".join(audio.get('label', ['--']))
            date = audio.get('date', ['--'])[0]
            bpm = str(int(float(audio['bpm'][0]))) if 'bpm' in audio else '--'
            key_str = audio.get('initialkey', audio.get('key', ['--']))[0]

            caption = (
                f"<b>\U0001F3B5 Track:</b> {title_name}\n"
                f"<b>\U0001F464 Artist:</b> {artists}\n"
                f"<b>📻 Genre:</b> {genre}\n"
                f"<b>💽 Album:</b> {album}\n"
                f"<b>\U0001F3B9 Key:</b> {key_str}\n"
                f"<b>🏷️ Label:</b> {label}\n"
                f"<b>\U0001F4C5 Release Date:</b> {date}\n"
                f"<b>\U0001F9E9 BPM:</b> {bpm}\n"
            )

            # -----------------------------------------
            # UPDATED: EMBEDDED COVER SUPPORT (TRACK)
            # -----------------------------------------
            embedded_cover_data, embedded_ext = extract_embedded_cover(filepath)
            if embedded_cover_data:
                cover_path = f"{download_dir}/embedded_cover.{embedded_ext}"
                with open(cover_path, "wb") as img:
                    img.write(embedded_cover_data)
                await client.send_file(event.chat_id, cover_path, caption=caption, parse_mode='html')
            else:
                cover_file = None
                for f in os.listdir(download_dir):
                    if f.lower().startswith('cover') and f.lower().endswith(('.jpg', '.jpeg', '.png')):
                        cover_file = os.path.join(download_dir, f)
                        break

                if cover_file:
                    await client.send_file(event.chat_id, cover_file, caption=caption, parse_mode='html')
                else:
                    await event.reply(caption, parse_mode='html')

            # -------------------
            # CONVERSION REMAINS SAME
            # -------------------
            if format_choice == 'flac':
                subprocess.run(['ffmpeg', '-n', '-i', filepath, converted_filepath])
                audio = File(converted_filepath, easy=True)
                artist = audio.get('artist', ['Unknown Artist'])[0]
                title = audio.get('title', ['Unknown Title'])[0]
                for field in ['artist', 'title', 'album', 'genre']:
                    if field in audio:
                        audio[field] = [value.replace(";", ", ") for value in audio[field]]
                audio.save()
                new_filename = safe_filename(f"{artist} - {title}.{format_choice}".replace(";", ", "))
                new_filepath = f'{download_dir}/{new_filename}'
                os.rename(converted_filepath, new_filepath)
                await client.send_file(event.chat_id, new_filepath)

            elif format_choice == 'mp3':
                subprocess.run(['ffmpeg', '-n', '-i', filepath, '-b:a', '320k', converted_filepath])
                audio = File(converted_filepath, easy=True)
                artist = audio.get('artist', ['Unknown Artist'])[0]
                title = audio.get('title', ['Unknown Title'])[0]
                for field in ['artist', 'title', 'album', 'genre']:
                    if field in audio:
                        audio[field] = [value.replace(";", ", ") for value in audio[field]]
                audio.save()
                new_filename = safe_filename(f"{artist} - {title}.{format_choice}".replace(";", ", "))
                new_filepath = f'{download_dir}/{new_filename}'
                os.rename(converted_filepath, new_filepath)
                await client.send_file(event.chat_id, new_filepath)

            elif format_choice == 'wav':
                subprocess.run(['ffmpeg', '-n', '-i', filepath, converted_filepath])
                original_audio = File(filepath, easy=True)
                artists = original_audio.get('artist', ['Unknown Artist'])
                clean_artists = ", ".join([a.strip() for a in ";".join(artists).split(";")])
                track_title = original_audio.get('title', ['Unknown Title'])[0]
                new_filename = safe_filename(f"{clean_artists} - {track_title}.wav")
                new_filepath = os.path.join(download_dir, new_filename)
                os.rename(converted_filepath, new_filepath)
                await client.send_file(event.chat_id, new_filepath, force_document=True)

            shutil.rmtree(download_dir)
            increment_download(event.chat_id, content_type)
            del state[event.chat_id]

    except Exception as e:
        await event.reply(f"An error occurred during conversion: {e}")

# === START HANDLER WITH IMAGE & BUTTONS ===
@client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    banner_path = 'banner.gif'  # Your banner image/gif in working dir
    caption = (
        "🎧 Hey DJ! 🎶\n\n"
        "Welcome to Beatport Downloader Bot – your assistant for downloading full Beatport tracks, albums, playlists & charts.\n\n"
        "❓ What I Can Do:\n"
        "🎵 Download original-quality Beatport releases\n"
        "📁 Send you organized, tagged audio files\n\n"
        "📋 Commands:\n"
        "➤ /download beatport url – Start download\n"
        "➤ /myaccount – Check daily usage\n\n"
        "🚀 Paste a Beatport link now and let’s get those bangers!"
    )
    buttons = [
        [Button.url("💟 Support", PAYMENT_URL), Button.url("📨 Contact", "https://t.me/zackantdev")],
        [Button.url("📢 Join our channel", "https://t.me/+UsTE5Ufq1W4wOWE1")]
    ]
    if os.path.exists(banner_path):
        await client.send_file(event.chat_id, banner_path, caption=caption, buttons=buttons)
    else:
        await event.reply(caption, buttons=buttons)
        
@client.on(events.NewMessage(pattern=r'^/add'))
async def add_user_handler(event):
    if event.sender_id not in ADMIN_IDS:
        await event.reply("⛔ You're not authorized to perform this action.")
        return
    try:
        parts = event.message.text.split()
        if len(parts) < 2:
            await event.reply("⚠️ Usage: /add <user_id> [days]\nExample: /add 123456789 15")
            return

        user_id = int(parts[1])
        days = int(parts[2]) if len(parts) > 2 else 30
        expiry_date = (datetime.utcnow() + timedelta(days=days)).strftime('%Y-%m-%d')

        users = load_users()
        user = users.get(str(user_id), {})

        # Update / Add premium info
        user["expiry"] = expiry_date
        user["album_today"] = user.get("album_today", 0)
        user["track_today"] = user.get("track_today", 0)
        user["last_reset"] = user.get("last_reset", datetime.utcnow().strftime('%Y-%m-%d'))
        user["premium"] = True
        user["ever_premium"] = True

        users[str(user_id)] = user
        save_users(users)

        await event.reply(
            f"✅ Premium activated for user `{user_id}` for **{days} days**.\n"
            f"📆 Expires on: {expiry_date}",
            parse_mode="markdown"
        )

        # Notify the user privately if possible
        try:
            await client.send_message(
                user_id,
                f"🎉 You’ve been upgraded to **Premium** for {days} days!\n"
                f"Expires on: `{expiry_date}`"
            )
        except Exception:
            pass

    except Exception as e:
        await event.reply(f"⚠️ Failed to add user: {e}")
@client.on(events.NewMessage(pattern='/remove'))
async def remove_user_handler(event):
    if event.sender_id not in ADMIN_IDS:
        await event.reply("❌ You're not authorized to perform this action.")
        return
    try:
        user_id = int(event.message.text.split(maxsplit=1)[1])
        removed = remove_user(user_id)
        if removed:
            await event.reply(f"✅ User {user_id} has been removed and now has daily limits.")
        else:
            await event.reply(f"ℹ️ User {user_id} was not found in the whitelist.")
    except Exception as e:
        await event.reply(f"⚠️ Failed to remove user: {e}")
        

@client.on(events.NewMessage(pattern=r'^/myaccount$'))
async def myaccount_handler(event):
    user_id = str(event.chat_id)
    users = load_users()
    user = users.get(user_id, {})

    # Reset usage if needed
    reset_if_needed(user)

    # Check if user is premium
    expiry = user.get("expiry")
    if expiry:
        try:
            expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        except ValueError:
            expiry_date = None
    else:
        expiry_date = None

    # Premium user
    if expiry_date and expiry_date > datetime.utcnow().date():
        message = (
            f"✨ **Premium User**\n\n"
            f"✅ Unlimited downloads — no daily limits!\n"
            f"📆 **Expires:** {expiry_date}\n\n"
            f"💟 Thank you for supporting the project!"
        )
        await event.reply(message, parse_mode="markdown")

    # Free user
    else:
        album_used = user.get("album_today", 0)
        track_used = user.get("track_today", 0)
        message = (
            f"🎧 **Free User**\n\n"
            f"🎵 **Tracks used today:** {track_used}/2\n"
            f"💿 **Albums used today:** {album_used}/2\n"
            f"📅 **Reset every 24 hours**\n\n"
            f"🔥 Upgrade to **Premium ($5)** for unlimited downloads!"
        )

        buttons = [
            [Button.url("💳 Pay $5 Here", "https://ko-fi.com/zackant")]
        ]

        await event.reply(message, buttons=buttons, parse_mode="markdown")


# === AUTO-DETECT BEATPORT LINKS (DIRECT TRIGGER) ===
@client.on(events.NewMessage())
async def direct_link_trigger(event):
    text = event.message.message.strip()

    # Ignore commands to avoid interfering
    if text.startswith("/"):
        return

    # Detect Beatport links
    is_track = re.match(beatport_track_pattern, text)
    is_album = re.match(beatport_album_pattern, text)
    is_playlist = re.match(beatport_playlist_pattern, text)
    is_chart = re.match(beatport_chart_pattern, text)

    if not (is_track or is_album or is_playlist or is_chart):
        return  # Not a supported link → ignore message

    user_id = event.chat_id

    # Determine type
    if is_album:
        content_type = "album"
    elif is_track:
        content_type = "track"
    elif is_playlist:
        content_type = "playlist"
    elif is_chart:
        content_type = "chart"

    # Premium check for playlist / chart
    if content_type in ["playlist", "chart"]:
        users = load_users()
        user = users.get(str(user_id), {})
        reset_if_needed(user)
        expiry = user.get("expiry")

        if not expiry or datetime.strptime(expiry, "%Y-%m-%d") <= datetime.utcnow():
            await event.reply(
                "🚫 Playlist & chart downloads are only for **Premium users**.\n"
                "Unlock full access for just **$5**!",
                buttons=[[Button.url("💳 Pay $5", PAYMENT_URL)]]
            )
            return

    # Daily limits for free users
    if content_type in ["album", "track"] and not is_user_allowed(user_id, content_type):
        await event.reply(
            "🚫 **Daily Limit Reached!**\n\n"
            "👉 Upgrade to **Premium ($5)** for **downloads** downloads and send the payment proof to @zackantdev",
            buttons=[[Button.url("💳 Pay $5 Here", PAYMENT_URL)]]
        )
        return

    # Save the request just like /download does
    state[event.chat_id] = {"url": text, "type": content_type}

    # Ask the format (same buttons as /download)
    await event.reply(
        "Please choose the format:",
        buttons=[
            [Button.inline("MP3 (320 kbps)", b"mp3"),
             Button.inline("FLAC (16 Bit)", b"flac")],
            [Button.inline("WAV (Lossless)", b"wav")]
        ]
    )

@client.on(events.NewMessage(pattern='/download'))
async def download_handler(event):
    try:
        user_id = event.chat_id
        input_text = event.message.text.split(maxsplit=1)[1].strip()

        # Check type of Beatport link
        is_track = re.match(beatport_track_pattern, input_text)
        is_album = re.match(beatport_album_pattern, input_text)
        is_playlist = re.match(beatport_playlist_pattern, input_text)
        is_chart = re.match(beatport_chart_pattern, input_text)

        if is_track or is_album or is_playlist or is_chart:
            # Determine content type
            if is_album:
                content_type = "album"
            elif is_track:
                content_type = "track"
            elif is_playlist:
                content_type = "playlist"
            elif is_chart:
                content_type = "chart"

            # Restrict playlists/charts to whitelisted users
            if content_type in ["playlist", "chart"]:
                users = load_users()
                user = users.get(str(user_id), {})
                reset_if_needed(user)
                expiry = user.get("expiry")
                if not expiry or datetime.strptime(expiry, "%Y-%m-%d") <= datetime.utcnow():
                    await event.reply(
                        "🚫 Playlist and chart downloads are available only for premium users.\n"
                        "Please support with a $5 payment to unlock playlist & chart downloading",
                        buttons=[Button.url("💳 Pay $5", PAYMENT_URL)]
                    )
                    return

            # Check daily limits for free users
            if content_type in ["album", "track"] and not is_user_allowed(user_id, content_type):
                await event.reply(
                    "🚫 **Daily Limit Reached!**\n\n"
                    "👉 Upgrade to **Premium ($5)** for **downloads** downloads and send the payment proof to @zackantdev",
                    buttons=[
                        [Button.url("💳 Pay $5 Here", PAYMENT_URL)],
                        
                    ]
                )
                return

            # Save state and ask format
            state[event.chat_id] = {"url": input_text, "type": content_type}
            await event.reply(
                "Please choose the format:",
                buttons=[
                    [Button.inline("MP3 (320 kbps)", b"mp3"), Button.inline("FLAC (16 Bit)", b"flac")],
                    [Button.inline("WAV (Lossless)", b"wav")]
                ]
            )
        else:
            await event.reply('Not available')
    except Exception as e:
        await event.reply(f"An error occurred: {e}")
            
@client.on(events.CallbackQuery)
async def callback_query_handler(event):
    try:
        format_choice = event.data.decode('utf-8')
        url_info = state.get(event.chat_id)
        if not url_info:
            await event.edit("No URL found. Please start again using /download.")
            return

        input_text = url_info["url"]
        content_type = url_info["type"]
        await event.edit(f"You selected {format_choice.upper()}. Downloading...")

        # 🔹 Run Orpheus sequentially (queued)
        await run_orpheus(event.chat_id, input_text)

        # 🔹 After Orpheus finishes, start conversion independently
        asyncio.create_task(handle_conversion_and_sending(event, format_choice, input_text, content_type))

    except Exception as e:
        await event.reply(f"An error occurred during processing: {e}")

@client.on(events.NewMessage(pattern='/broadcast'))
async def broadcast_handler(event):
    if event.sender_id not in ADMIN_IDS:
        await event.reply("❌ You're not authorized to use this command.")
        return

    try:
        args = event.message.text.split(maxsplit=1)
        if len(args) < 2:
            await event.reply("⚠️ Please provide a message to broadcast. Usage:\n<b>/broadcast Your message here</b>", parse_mode='html')
            return
        broadcast_message = args[1]

        users = load_users()
        count = 0
        failed = 0

        for uid, data in users.items():
            if int(uid) in ADMIN_IDS:
                continue
            if 'expiry' in data:
                try:
                    expiry = datetime.strptime(data['expiry'], '%Y-%m-%d')
                    if expiry > datetime.utcnow():
                        continue  # Skip whitelisted (premium) users
                except:
                    pass
            try:
                await client.send_message(
                    int(uid),
                    f"📢 <b>Announcement</b>\n\n{broadcast_message}",
                    parse_mode='html'
                )
                count += 1
                await asyncio.sleep(1)  # ✅ 1-second delay between messages
            except Exception as e:
                print(f"Failed to send to {uid}: {e}")
                failed += 1

        await event.reply(
            f"✅ Broadcast sent to <b>{count}</b> users.\n❌ Failed to send to <b>{failed}</b> users.",
            parse_mode='html'
        )

    except Exception as e:
        await event.reply(f"⚠️ An error occurred while broadcasting: {e}")


@client.on(events.NewMessage(pattern='/adminlist'))
async def admin_list_handler(event):
    if event.sender_id not in ADMIN_IDS:
        await event.reply("❌ You're not authorized to use this command.")
        return
    lines = ["<b>👑 Admin Users:</b>\n"]
    for admin_id in ADMIN_IDS:
        try:
            user = await client.get_entity(admin_id)
            username = f"@{user.username}" if user.username else "No username"
            lines.append(f"• <code>{admin_id}</code> – {username}")
        except Exception:
            lines.append(f"• <code>{admin_id}</code> – [Could not fetch username]")
    await event.reply("\n".join(lines), parse_mode='html')


@client.on(events.NewMessage(pattern=r'^/whitelist$'))
async def whitelist_handler(event):
    if event.sender_id not in ADMIN_IDS:
        await event.reply("⛔ You are not authorized to use this command.")
        return

    users = load_users()
    now = datetime.utcnow()
    premium_users = []

    for uid, data in users.items():
        expiry_str = data.get("expiry")
        if not expiry_str:
            continue

        try:
            expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d")
            if expiry_date > now:
                try:
                    user_entity = await client.get_entity(int(uid))
                    username = f"@{user_entity.username}" if user_entity.username else "No username"
                except Exception:
                    username = "No username"
                premium_users.append((uid, username, expiry_str))
        except:
            continue

    if not premium_users:
        await event.reply("💎 No active Premium users found.")
        return

    total_count = len(premium_users)

    # Pagination (25 per page)
    page_size = 25
    pages = []

    for i in range(0, total_count, page_size):
        chunk = premium_users[i:i + page_size]

        # Page header
        if len(pages) == 0:
            # Only FIRST PAGE shows total count
            text = (
                f"💎 **Total Premium Users: {total_count}**\n"
                f"📄 Page {len(pages) + 1}\n\n"
            )
        else:
            text = f"📄 Page {len(pages) + 1}\n\n"

        # Add users for this page
        for uid, username, expiry in chunk:
            text += f"👤 `{uid}` — {username}\n📆 Expires: {expiry}\n\n"

        pages.append(text.strip())

    WHITELIST_PAGES[event.sender_id] = {
        "pages": pages,
        "current": 0,
    }

    # Buttons
    buttons = [
        [Button.inline("➡️ Next", data="wl_next")]
    ] if len(pages) > 1 else None

    await event.reply(pages[0], parse_mode="markdown", buttons=buttons)

@client.on(events.CallbackQuery(pattern=r"wl_(next|prev)"))
async def whitelist_pagination(event):
    admin_id = event.sender_id

    if admin_id not in ADMIN_IDS:
        await event.answer("Not allowed.", alert=True)
        return

    if admin_id not in WHITELIST_PAGES:
        await event.answer("Session expired. Send /whitelist again.", alert=True)
        return

    action = event.data.decode().split("_")[1]
    session = WHITELIST_PAGES[admin_id]

    if action == "next":
        if session["current"] < len(session["pages"]) - 1:
            session["current"] += 1

    elif action == "prev":
        if session["current"] > 0:
            session["current"] -= 1

    page_index = session["current"]

    # Build buttons dynamically
    buttons = []
    if page_index > 0:
        buttons.append(Button.inline("⬅️ Previous", data="wl_prev"))
    if page_index < len(session["pages"]) - 1:
        buttons.append(Button.inline("➡️ Next", data="wl_next"))

    await event.edit(
        session["pages"][page_index],
        parse_mode="markdown",
        buttons=[buttons] if buttons else None
    )
# === NEW COMMAND: /totalusers ===
@client.on(events.NewMessage(pattern='/totalusers'))
async def total_users_handler(event):
    if event.sender_id not in ADMIN_IDS:
        await event.reply("❌ You're not authorized to use this command.")
        return
    users = load_users()
    total = len(users)
    await event.reply(f"👥 Total registered users: <b>{total}</b>", parse_mode='html')

@client.on(events.NewMessage(pattern='/updates'))
async def updates_handler(event):
    caption = (
        "📢 Stay tuned for the latest bot updates, fixes, and new features!\n\n"
        "👉 Join our official channel for updates: https://t.me/+UsTE5Ufq1W4wOWE1"
    )
    await event.reply(caption)


@client.on(events.NewMessage(pattern='/reminder'))
async def reminder_handler(event):
    if event.sender_id not in ADMIN_IDS:
        await event.reply("❌ You're not authorized to use this command.")
        return

    users = load_users()
    now = datetime.utcnow().date()
    expired_users = []

    for uid, data in users.items():
        premium_status = data.get("premium", False)
        ever_premium = data.get("ever_premium", False)
        expiry_str = data.get("expiry")

        # Skip users who never had premium
        if not ever_premium:
            continue

        # Include users who lost premium (manual removal or expired)
        if not premium_status:
            # Check expiry date (optional)
            if expiry_str:
                try:
                    expiry = datetime.strptime(expiry_str, "%Y-%m-%d").date()
                    if expiry <= now:
                        expired_users.append(int(uid))
                        continue
                except:
                    pass
            else:
                # No expiry date but once premium
                expired_users.append(int(uid))

    if not expired_users:
        await event.reply("✅ No expired or removed Premium users found who need reminders.")
        return

    await event.reply(f"📬 Sending renewal reminders to {len(expired_users)} expired users...")

    reminder_text = (
        "😢 <b>Hey there, music lover!</b>\n\n"
        "Your <b>Premium access has expired</b> 💔\n"
        "We miss having you in our VIP zone!\n\n"
        "Renew Premium and enjoy <b>unlimited track & album downloads</b> again 🎶🔥\n\n"
        "If you’ve already paid, please message @zackantdev."
    )

    sent = 0
    failed = 0
    for uid in expired_users:
        try:
            await client.send_message(
                uid,
                reminder_text,
                parse_mode="html",
                buttons=[
                    [Button.url("💳 Renew Premium", "https://ko-fi.com/zackant")],
                    [Button.url("📨 Contact @zackantdev", "https://t.me/zackantdev")]
                ]
            )
            sent += 1
            await asyncio.sleep(1)  # 1 second delay between messages
        except Exception as e:
            print(f"⚠️ Failed to send reminder to {uid}: {e}")
            failed += 1
            continue

    await event.reply(
        f"✅ Reminder sent to <b>{sent}</b> expired users.\n❌ Failed for <b>{failed}</b> users.",
        parse_mode="html"
    )
@client.on(events.NewMessage(pattern='/alert'))
async def alert_expiry_handler(event):
    if event.sender_id not in ADMIN_IDS:
        await event.reply("❌ You're not authorized to use this command.")
        return

    users = load_users()
    now = datetime.utcnow().date()
    notified = 0
    failed = 0

    for uid, data in users.items():
        expiry_str = data.get('expiry')
        if not expiry_str:
            continue

        try:
            expiry = datetime.strptime(expiry_str, '%Y-%m-%d').date()
            days_left = (expiry - now).days

            if days_left in [1, 2, 3]:
                if days_left == 3:
                    message = (
                        f"⏳ <b>Heads up!</b>\n\n"
                        f"Your premium access will expire in <b>3 days</b> on <b>{expiry_str}</b>.\n"
                        f"Renew early to enjoy uninterrupted downloads! If you've paid, send a message to @zackantdev."
                    )
                elif days_left == 2:
                    message = (
                        f"⏳ <b>Reminder:</b>\n\n"
                        f"Your premium access will expire in <b>2 days</b> on <b>{expiry_str}</b>.\n"
                        f"Don’t forget to renew and keep the music flowing! If you've paid, send a message to @zackantdev."
                    )
                elif days_left == 1:
                    message = (
                        f"⚠️ <b>Final Reminder:</b>\n\n"
                        f"Your premium access expires <b>TOMORROW</b> (<b>{expiry_str}</b>).\n"
                        f"Renew now to avoid losing your unlimited access. If you've paid, send a message to @zackantdev."
                    )

                try:
                    await client.send_message(
                        int(uid),
                        message,
                        parse_mode='html',
                        buttons=[
                            [Button.url("💳 Donate Here", PAYMENT_URL)],
                            [Button.url("📨 Contact @zackantdev", "https://t.me/zackantdev")]
                        ]
                    )
                    notified += 1
                    await asyncio.sleep(1)  # ✅ Wait 1 second between messages
                except Exception as e:
                    print(f"❌ Failed to message {uid}: {e}")
                    failed += 1
        except Exception as e:
            print(f"⚠️ Error parsing expiry for user {uid}: {e}")
            continue

    await event.reply(
        f"✅ Expiry alerts sent to <b>{notified}</b> users.\n❌ Failed for <b>{failed}</b> users.",
        parse_mode='html'
    )
    
async def main():
    async with client:
        print("Client is running...")
        await client.run_until_disconnected()

if __name__ == '__main__':
    client.loop.run_until_complete(main())
