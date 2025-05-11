import os
import re
import shutil
import subprocess
import json
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zipfile import ZipFile
from telethon import TelegramClient, events, Button
from mutagen import File

# MTProto API credentials
api_id = '10074048'
api_hash = 'a08b1ed3365fa3b04bcf2bcbf71aff4d'
session_name = 'beatport_downloader'

# Regular expressions
track_pattern = r'^https:\/\/www\.beatport\.com\/track\/[\w\-]+\/(\d+)$'
album_pattern = r'^https:\/\/www\.beatport\.com\/release\/[\w\-]+\/(\d+)$'
crates_track_pattern = r'^https:\/\/crates\.co\/track\/[\w\-]+\/(\d+)$'
crates_album_pattern = r'^https:\/\/crates\.co\/release\/[\w\-]+\/(\d+)$'

# State dictionary
state = {}

# Admin IDs and payment URL
ADMIN_IDS = [616584208, 731116951]
PAYMENT_URL = "https://buymeacoffee.com/zackant"

# User data file
USERS_FILE = 'users.json'

def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, 'r') as f:
        return json.load(f)

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f)

def is_user_allowed(user_id):
    if user_id in ADMIN_IDS:
        return True
    users = load_users()
    user = users.get(str(user_id), {})
    expiry = user.get('expiry')
    if expiry:
        return datetime.strptime(expiry, '%Y-%m-%d') > datetime.utcnow()
    return user.get('downloads', 0) < 5

def increment_download(user_id):
    if user_id in ADMIN_IDS:
        return
    users = load_users()
    uid = str(user_id)
    if uid not in users:
        users[uid] = {"downloads": 0}
    users[uid]["downloads"] += 1
    save_users(users)

def whitelist_user(user_id):
    users = load_users()
    users[str(user_id)] = {"expiry": (datetime.utcnow() + timedelta(days=30)).strftime('%Y-%m-%d')}
    save_users(users)

def sanitize_metadata(text):
    return text.replace(";", ", ").strip()

def rename_by_metadata(filepath):
    audio = File(filepath, easy=True)
    if not audio:
        return filepath
    artist = sanitize_metadata(", ".join(audio.get('artist', ['Unknown Artist'])))
    title = sanitize_metadata(" ".join(audio.get('title', ['Unknown Title'])))
    new_filename = f"{artist} - {title}{os.path.splitext(filepath)[1]}"
    new_filepath = os.path.join(os.path.dirname(filepath), new_filename)
    os.rename(filepath, new_filepath)
    return new_filepath

# Telegram client
client = TelegramClient(session_name, api_id, api_hash)

@client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    await event.reply("Hi! I'm Beatport Track Downloader.\n\n"
                      "Use /download <track_or_album_url>\n\n"
                      "Example:\n"
                      "/download https://www.beatport.com/track/love-on-me/8557778\n"
                      "/download https://www.beatport.com/release/love-on-me-remixes/1883751")

@client.on(events.NewMessage(pattern='/add'))
async def add_user_handler(event):
    if event.sender_id not in ADMIN_IDS:
        await event.reply("You're not authorized to perform this action.")
        return
    try:
        user_id = int(event.message.text.split(maxsplit=1)[1])
        whitelist_user(user_id)
        await event.reply(f"User {user_id} has been granted access for 30 days.")
    except Exception as e:
        await event.reply(f"Failed to add user: {e}")

@client.on(events.NewMessage(pattern='/download'))
async def download_handler(event):
    try:
        user_id = event.chat_id
        if not is_user_allowed(user_id):
            await event.reply(
                "You've reached your 5 download limit.\nTo continue using the bot, please make a $5 payment.",
                buttons=[Button.url("Pay $5", PAYMENT_URL)]
            )
            return

        input_text = event.message.text.split(maxsplit=1)[1]
        is_track = re.match(track_pattern, input_text) or re.match(crates_track_pattern, input_text)
        is_album = re.match(album_pattern, input_text) or re.match(crates_album_pattern, input_text)

        if is_track or is_album:
            if "crates.co" in input_text:
                input_text = input_text.replace("crates.co", "www.beatport.com")

            state[event.chat_id] = input_text

            await event.reply("Choose output format and type:", buttons=[
                [Button.inline("FLAC - Individual", b"flac_individual"), Button.inline("MP3 - Individual", b"mp3_individual")],
                [Button.inline("FLAC - ZIP", b"flac_zip"), Button.inline("MP3 - ZIP", b"mp3_zip")]
            ])
        else:
            await event.reply('Invalid Beatport/Crates link.')
    except Exception as e:
        await event.reply(f"Error: {e}")

@client.on(events.CallbackQuery)
async def callback_query_handler(event):
    try:
        callback_data = event.data.decode('utf-8')
        fmt, send_as = callback_data.split("_")
        input_text = state.get(event.chat_id)

        await event.edit("Downloading and processing your request...")

        os.system(f'python orpheus.py "{input_text}"')

        match = re.search(r'\/(\d+)$', input_text)
        release_id = match.group(1)

        parent_folder = os.path.join("downloads", release_id)

        if "track" in input_text:
            files = [f for f in os.listdir(parent_folder) if f.lower().endswith('.flac')]
            if files:
                flac_path = os.path.join(parent_folder, files[0])
                converted_path = flac_path.replace(".flac", f".{fmt}")
                subprocess.run(['ffmpeg', '-i', flac_path, converted_path])
                converted_path = rename_by_metadata(converted_path)
                await client.send_file(event.chat_id, converted_path)
                increment_download(event.chat_id)
        else:
            subdirs = [d for d in os.listdir(parent_folder) if os.path.isdir(os.path.join(parent_folder, d))]
            if not subdirs:
                await event.reply("Album folder not found.")
                return
            album_folder = os.path.join(parent_folder, subdirs[0])
            files = [f for f in os.listdir(album_folder) if f.lower().endswith('.flac')]
            converted_files = []

            for flac_file in files:
                flac_path = os.path.join(album_folder, flac_file)
                converted_path = flac_path.replace(".flac", f".{fmt}")
                subprocess.run(['ffmpeg', '-i', flac_path, converted_path])
                converted_path = rename_by_metadata(converted_path)
                converted_files.append(converted_path)

            if send_as == "zip":
                zip_name = subdirs[0].strip() + ".zip"
                zip_path = os.path.join(album_folder, zip_name)
                with ZipFile(zip_path, 'w') as zipf:
                    for file in converted_files:
                        zipf.write(file, os.path.basename(file))
                await client.send_file(event.chat_id, zip_path)
            else:
                for file in converted_files:
                    await client.send_file(event.chat_id, file)

            increment_download(event.chat_id)
        del state[event.chat_id]
    except Exception as e:
        await event.reply(f"Error in callback: {e}")

async def main():
    async with client:
        print("Client is running...")
        await client.run_until_disconnected()

if __name__ == '__main__':
    client.loop.run_until_complete(main())
