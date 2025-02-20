import os
import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext
from flask import Flask, request, redirect
import threading

# Spotify API credentials
SPOTIFY_CLIENT_ID = "a4872e5eeb754c3eb0500feefa9568cd"
SPOTIFY_CLIENT_SECRET = "71ec3f74705d43fca81d5a9c6513c3c1"
REDIRECT_URI = "https://your-server.com/callback"  # Replace with your actual redirect URI

# Telegram bot token
TELEGRAM_BOT_TOKEN = "5707293090:AAHGLlHSx101F8T1DQYdcb9_MkRAjyCbt70"

# Storage for user access tokens (use a database in production)
user_tokens = {}

# Flask app for handling Spotify OAuth
app = Flask(__name__)

@app.route('/')
def home():
    return "Spotify Release Bot is running!"

@app.route('/login/<user_id>')
def spotify_login(user_id):
    sp_oauth = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope="user-follow-read"
    )
    auth_url = sp_oauth.get_authorize_url(state=user_id)
    return redirect(auth_url)

@app.route('/callback')
def spotify_callback():
    sp_oauth = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope="user-follow-read"
    )
    code = request.args.get("code")
    user_id = request.args.get("state")

    if not code:
        return "Authorization failed."

    token_info = sp_oauth.get_access_token(code)
    
    if "access_token" in token_info:
        user_tokens[user_id] = token_info["access_token"]
        return "Login successful! Return to Telegram and type /releases to get new music."
    else:
        return "Login failed."

# Telegram bot functions
def start(update: Update, context: CallbackContext):
    user_id = update.message.chat.id
    login_url = f"https://your-server.com/login/{user_id}"  # Replace with your actual server URL
    update.message.reply_text(f"Welcome! Log in to Spotify here: [Login]({login_url})", parse_mode="Markdown")

def get_releases(update: Update, context: CallbackContext):
    user_id = str(update.message.chat.id)

    if user_id not in user_tokens:
        update.message.reply_text("You need to log in first. Use /start to get your login link.")
        return

    # Fetch followed artists
    sp = spotipy.Spotify(auth=user_tokens[user_id])
    artists = sp.current_user_followed_artists(limit=10)["artists"]["items"]

    if not artists:
        update.message.reply_text("You are not following any artists.")
        return

    message = "Latest releases from your followed artists:\n\n"
    
    for artist in artists:
        artist_id = artist["id"]
        artist_name = artist["name"]

        # Fetch new releases for each followed artist
        albums = sp.artist_albums(artist_id, album_type="album", limit=1)["items"]
        
        if albums:
            album_name = albums[0]["name"]
            album_url = albums[0]["external_urls"]["spotify"]
            message += f"🎵 *{album_name}* by *{artist_name}*\n[Listen here]({album_url})\n\n"

    update.message.reply_text(message, parse_mode="Markdown")

# Start Telegram bot
def start_telegram_bot():
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("releases", get_releases))
    updater.start_polling()
    updater.idle()

# Start Flask server and Telegram bot in separate threads
if __name__ == "__main__":
    threading.Thread(target=start_telegram_bot).start()
    app.run(host="0.0.0.0", port=5000)
