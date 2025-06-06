import os
import re
import shutil
import subprocess
import json
from datetime import datetime, timedelta
from urllib.parse import urlparse
from telethon import TelegramClient, events, Button
from mutagen import File

api_id = '10074048'
api_hash = 'a08b1ed3365fa3b04bcf2bcbf71aff4d'
session_name = 'beatport_downloader'

beatport_track_pattern = r'^https:\/\/www\.beatport\.com\/track\/[\w\-]+\/\d+$'
beatport_album_pattern = r'^https:\/\/www\.beatport\.com\/release\/[\w\-]+\/\d+$'
crates_pattern = r'^https:\/\/crates\.co\/track\/[\w\-]+\/\d+$'

state = {}
ADMIN_IDS = [616584208, 731116951]
PAYMENT_URL = "https://ko-fi.com/zackant"
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
    users[uid]["downloads"] = users[uid].get("downloads", 0) + 1
    save_users(users)

def whitelist_user(user_id):
    users = load_users()
    users[str(user_id)] = {"expiry": (datetime.utcnow() + timedelta(days=30)).strftime('%Y-%m-%d')}
    save_users(users)

client = TelegramClient(session_name, api_id, api_hash)

@client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    await event.reply("Hi! I'm Beatport Track Downloader.\n\n"
                      "Commands:\n"
                      "/download <track_or_album_url> - Download from Beatport or Crates.co.\n")

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

        input_text = event.message.text.split(maxsplit=1)[1].strip()
        is_track = re.match(beatport_track_pattern, input_text)
        is_album = re.match(beatport_album_pattern, input_text)
        is_crates = re.match(crates_pattern, input_text)

        if is_track or is_album or is_crates:
            if is_crates:
                input_text = input_text.replace('crates.co', 'www.beatport.com')
                is_track = True

            content_type = 'album' if is_album else 'track'
            state[event.chat_id] = {"url": input_text, "type": content_type}

            await event.reply("Please choose the format:", buttons=[
                [Button.inline("FLAC (16 Bit)", b"flac"), Button.inline("MP3 (320K)", b"mp3")]
            ])
        else:
            await event.reply('Invalid link.\nPlease send a valid Beatport track or album URL.')
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

        url = urlparse(input_text)
        components = url.path.split('/')
        release_id = components[-1]

        os.system(f'python orpheus.py {input_text}')

        if content_type == "album":
            root_path = f'downloads/{release_id}'

            # Check if it's a single-track album
            flac_files = [f for f in os.listdir(root_path) if f.lower().endswith('.flac')]
            album_path = root_path if flac_files else os.path.join(root_path, os.listdir(root_path)[0])
            files = os.listdir(album_path)

            for filename in files:
                if filename.lower().endswith('.flac'):
                    input_path = os.path.join(album_path, filename)
                    output_path = f"{input_path}.{format_choice}"

                    if format_choice == 'flac':
                        subprocess.run(['ffmpeg', '-i', input_path, output_path])
                    elif format_choice == 'mp3':
                        subprocess.run(['ffmpeg', '-i', input_path, '-b:a', '320k', output_path])

                    audio = File(output_path, easy=True)
                    artist = audio.get('artist', ['Unknown Artist'])[0]
                    title = audio.get('title', ['Unknown Title'])[0]

                    for field in ['artist', 'title', 'album', 'genre']:
                        if field in audio:
                            audio[field] = [value.replace(";", ", ") for value in audio[field]]
                    audio.save()

                    final_name = f"{artist} - {title}.{format_choice}".replace(";", ", ")
                    final_path = os.path.join(album_path, final_name)
                    os.rename(output_path, final_path)

                    await client.send_file(event.chat_id, final_path)

            for file in files:
                if file.lower().startswith('cover') and file.lower().endswith(('.jpg', '.png', '.jpeg')):
                    await client.send_file(event.chat_id, os.path.join(album_path, file))
                    break

            shutil.rmtree(root_path)
            increment_download(event.chat_id)
            del state[event.chat_id]

        else:
            download_dir = f'downloads/{components[-1]}'
            filename = os.listdir(download_dir)[0]
            filepath = f'{download_dir}/{filename}'

            converted_filepath = f'{download_dir}/{filename}.{format_choice}'
            if format_choice == 'flac':
                subprocess.run(['ffmpeg', '-i', filepath, converted_filepath])
            elif format_choice == 'mp3':
                subprocess.run(['ffmpeg', '-i', filepath, '-b:a', '320k', converted_filepath])

            audio = File(converted_filepath, easy=True)
            artist = audio.get('artist', ['Unknown Artist'])[0]
            title = audio.get('title', ['Unknown Title'])[0]

            for field in ['artist', 'title', 'album', 'genre']:
                if field in audio:
                    audio[field] = [value.replace(";", ", ") for value in audio[field]]
            audio.save()

            new_filename = f"{artist} - {title}.{format_choice}".replace(";", ", ")
            new_filepath = f'{download_dir}/{new_filename}'

            os.rename(converted_filepath, new_filepath)

            await client.send_file(event.chat_id, new_filepath)

            shutil.rmtree(download_dir)
            increment_download(event.chat_id)
            del state[event.chat_id]
    except Exception as e:
        await event.reply(f"An error occurred during conversion: {e}")

async def main():
    async with client:
        print("Client is running...")
        await client.run_until_disconnected()

if __name__ == '__main__':
    client.loop.run_until_complete(main())
