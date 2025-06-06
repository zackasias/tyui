import os
import re
import shutil
import subprocess
import json
from datetime import datetime, timedelta
from urllib.parse import urlparse
from telethon import TelegramClient, events, Button
from mutagen import File

# MTProto API credentials
api_id = '10074048'
api_hash = 'a08b1ed3365fa3b04bcf2bcbf71aff4d'
session_name = 'beatport_downloader'

# Regular expressions
beatport_pattern = '^https:\/\/www\.beatport\.com\/track\/[\w -]+\/\d+$'
crates_pattern = '^https:\/\/crates\.co\/track\/[\w -]+\/\d+$'

# State dictionary
state = {}

# Admin IDs and payment URL
ADMIN_IDS = [616584208, 731116951]  # Replace with your Telegram user IDs
PAYMENT_URL = "https://ko-fi.com/zackant"  # Replace with your custom payment URL

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
        return True  # Admins have unlimited access
    users = load_users()
    user = users.get(str(user_id), {})
    expiry = user.get('expiry')
    if expiry:
        return datetime.strptime(expiry, '%Y-%m-%d') > datetime.utcnow()
    return user.get('downloads', 0) < 5

def increment_download(user_id):
    if user_id in ADMIN_IDS:
        return  # Don't count downloads for admins
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

# Telegram client
client = TelegramClient(session_name, api_id, api_hash)

@client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    await event.reply("Hi! I'm Beatport Track Downloader.\n\n"
                      "Commands:\n"
                      "/download <track_url> - Download a track from Beatport or Crates.co.\n\n"
                      "Example:\n"
                      "/download https://www.beatport.com/track/take-me/17038421\n")

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
        is_beatport = re.match(rf'{beatport_pattern}', input_text)
        is_crates = re.match(rf'{crates_pattern}', input_text)

        if is_beatport or is_crates:
            if is_crates:
                input_text = input_text.replace('crates.co', 'www.beatport.com')

            state[event.chat_id] = input_text

            await event.reply("Please choose the format:", buttons=[
                [Button.inline("FLAC (16 Bit)", b"flac"), Button.inline("MP3 (320K)", b"mp3")]
            ])
        else:
            await event.reply('Invalid track link.\nPlease enter a valid track link.')
    except Exception as e:
        await event.reply(f"An error occurred: {e}")

@client.on(events.CallbackQuery)
async def callback_query_handler(event):
    try:
        format_choice = event.data.decode('utf-8')
        input_text = state.get(event.chat_id)
        if not input_text:
            await event.edit("No URL found. Please start the process again using /download.")
            return

        await event.edit(f"You selected {format_choice.upper()}. Downloading the file...")

        url = urlparse(input_text)
        components = url.path.split('/')

        os.system(f'python orpheus.py {input_text}')

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
