import os
import asyncio
import glob
import threading
import tempfile
import requests as req_lib
from dotenv import load_dotenv

import discord
from discord.ext import commands
from discord import app_commands

from flask import Flask, request, jsonify
from flask_cors import CORS

import cloudinary
import cloudinary.uploader
import cloudinary.api

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
MUSIC_DIR = os.path.join(os.path.dirname(__file__), "music")
os.makedirs(MUSIC_DIR, exist_ok=True)

# ── Cloudinary config ──────────────────────────────────────────────────────────
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME", "dzsvuxsqi"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)

# ── Flask app ──────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)
CORS(flask_app)

# ── Discord bot ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Shared state
voice_client: discord.VoiceClient | None = None
is_playing = False
current_track_index = 0
cloudinary_tracks: list[dict] = []  # [{name, url, public_id}]


def get_local_music_files() -> list[str]:
    patterns = ("*.mp3", "*.wav", "*.ogg", "*.flac", "*.m4a")
    files = []
    for pattern in patterns:
        files.extend(glob.glob(os.path.join(MUSIC_DIR, pattern)))
    return sorted(files)


def get_cloudinary_tracks() -> list[dict]:
    """Fetch all audio tracks from Cloudinary folder 'bot_music'."""
    try:
        result = cloudinary.api.resources(
            resource_type="video",  # Cloudinary uses 'video' for audio too
            prefix="bot_music/",
            max_results=100,
            type="upload"
        )
        tracks = []
        for r in result.get("resources", []):
            tracks.append({
                "public_id": r["public_id"],
                "url": r["secure_url"],
                "name": r["public_id"].replace("bot_music/", "").replace("_", " ")
            })
        return tracks
    except Exception as e:
        print(f"[cloudinary] Error fetching tracks: {e}")
        return []


async def play_next(guild: discord.Guild):
    global is_playing, current_track_index, voice_client, cloudinary_tracks

    if not is_playing or voice_client is None or not voice_client.is_connected():
        return

    # Refresh cloudinary tracks
    tracks = get_cloudinary_tracks()
    local = get_local_music_files()

    if not tracks and not local:
        print("[bot] No music files found.")
        is_playing = False
        return

    if tracks:
        track = tracks[current_track_index % len(tracks)]
        current_track_index = (current_track_index + 1) % len(tracks)
        print(f"[bot] Playing from Cloudinary: {track['name']}")

        # Download to temp file
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        r = req_lib.get(track["url"], stream=True)
        for chunk in r.iter_content(chunk_size=8192):
            tmp.write(chunk)
        tmp.close()
        track_path = tmp.name
    else:
        track_path = local[current_track_index % len(local)]
        current_track_index = (current_track_index + 1) % len(local)
        print(f"[bot] Playing local: {os.path.basename(track_path)}")

    source = discord.FFmpegPCMAudio(track_path)
    source = discord.PCMVolumeTransformer(source, volume=0.5)

    def after_track(error):
        if error:
            print(f"[bot] Playback error: {error}")
        if is_playing:
            asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)

    voice_client.play(source, after=after_track)


# ── Slash commands ─────────────────────────────────────────────────────────────
@tree.command(name="play", description="Join a voice channel and play ambient music")
@app_commands.describe(channel="Name of the voice channel")
async def play_cmd(interaction: discord.Interaction, channel: str):
    global voice_client, is_playing, current_track_index

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
        return

    vc = discord.utils.find(
        lambda c: isinstance(c, discord.VoiceChannel) and c.name.lower() == channel.lower(),
        guild.channels,
    )
    if vc is None:
        await interaction.response.send_message(f"❌ Voice channel **{channel}** not found.", ephemeral=True)
        return

    if voice_client and voice_client.is_connected():
        await voice_client.move_to(vc)
    else:
        voice_client = await vc.connect()

    is_playing = True
    current_track_index = 0
    await interaction.response.send_message(f"🎵 Playing music in **{vc.name}**...")
    await play_next(guild)


@tree.command(name="stop", description="Stop music and leave the voice channel")
async def stop_cmd(interaction: discord.Interaction):
    global voice_client, is_playing

    is_playing = False

    if voice_client and voice_client.is_connected():
        if voice_client.is_playing():
            voice_client.stop()
        await voice_client.disconnect()
        voice_client = None
        await interaction.response.send_message("⏹️ Stopped music and left the channel.")
    else:
        await interaction.response.send_message("❌ Bot is not in a voice channel.", ephemeral=True)


# ── Bot events ─────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    try:
        guild = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        print(f"[bot] Logged in as {bot.user} | Commands synced to guild {GUILD_ID}")
    except discord.Forbidden:
        await tree.sync()
        print(f"[bot] Logged in as {bot.user} | Commands synced globally")


# ── Flask routes ───────────────────────────────────────────────────────────────
@flask_app.route("/give-role", methods=["POST"])
def give_role():
    data = request.get_json(force=True)
    user_id = data.get("user_id")
    role_name = data.get("role_name")
    guild_id = data.get("guild_id")

    if not all([user_id, role_name, guild_id]):
        return jsonify({"error": "user_id, role_name and guild_id are required"}), 400

    async def _give():
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return {"error": f"Guild {guild_id} not found"}
        member = guild.get_member(int(user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id))
            except discord.NotFound:
                return {"error": f"User {user_id} not found in guild"}
        role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), guild.roles)
        if role is None:
            return {"error": f"Role '{role_name}' not found"}
        await member.add_roles(role)
        return {"success": True, "message": f"Role '{role.name}' given to {member.display_name}"}

    future = asyncio.run_coroutine_threadsafe(_give(), bot.loop)
    result = future.result(timeout=10)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result), 200


@flask_app.route("/play", methods=["POST"])
def play_route():
    data = request.get_json(force=True)
    channel_name = data.get("channel")
    if not channel_name:
        return jsonify({"error": "channel is required"}), 400

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return jsonify({"error": "Guild not found"}), 404

    async def _play():
        global voice_client, is_playing, current_track_index
        vc = discord.utils.find(
            lambda c: isinstance(c, discord.VoiceChannel) and c.name.lower() == channel_name.lower(),
            guild.channels,
        )
        if vc is None:
            return {"error": f"Channel '{channel_name}' not found"}

        tracks = get_cloudinary_tracks()
        local = get_local_music_files()
        if not tracks and not local:
            return {"error": "No audio files found"}

        if voice_client and voice_client.is_connected():
            await voice_client.move_to(vc)
        else:
            voice_client = await vc.connect()

        is_playing = True
        current_track_index = 0
        await play_next(guild)
        return {"success": True, "message": f"Playing in '{vc.name}'", "tracks": len(tracks) + len(local)}

    future = asyncio.run_coroutine_threadsafe(_play(), bot.loop)
    result = future.result(timeout=15)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result), 200


@flask_app.route("/stop", methods=["POST"])
def stop_route():
    async def _stop():
        global voice_client, is_playing
        is_playing = False
        if voice_client and voice_client.is_connected():
            if voice_client.is_playing():
                voice_client.stop()
            await voice_client.disconnect()
            voice_client = None
            return {"success": True, "message": "Stopped and disconnected"}
        return {"error": "Bot is not connected to a voice channel"}

    future = asyncio.run_coroutine_threadsafe(_stop(), bot.loop)
    result = future.result(timeout=10)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result), 200


@flask_app.route("/upload-track", methods=["POST"])
def upload_track():
    """Upload audio file to Cloudinary."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    track_name = request.form.get("name", file.filename.rsplit(".", 1)[0])
    # Sanitize name for public_id
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in track_name)

    try:
        result = cloudinary.uploader.upload(
            file,
            resource_type="video",
            folder="bot_music",
            public_id=safe_name,
            overwrite=True,
            use_filename=False
        )
        return jsonify({
            "success": True,
            "message": f"Track '{track_name}' uploaded!",
            "url": result["secure_url"],
            "public_id": result["public_id"]
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/tracks", methods=["GET"])
def list_tracks():
    tracks = get_cloudinary_tracks()
    local = [{"name": os.path.basename(f), "url": None, "public_id": None} for f in get_local_music_files()]
    return jsonify({"tracks": tracks, "local": local, "total": len(tracks) + len(local)}), 200


@flask_app.route("/delete-track", methods=["POST"])
def delete_track():
    data = request.get_json(force=True)
    public_id = data.get("public_id")
    if not public_id:
        return jsonify({"error": "public_id is required"}), 400
    try:
        cloudinary.uploader.destroy(public_id, resource_type="video")
        return jsonify({"success": True, "message": "Track deleted"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/create-webhook", methods=["POST"])
def create_webhook():
    data = request.get_json(force=True)
    channel_name = data.get("channel")
    webhook_name = data.get("webhook_name", "Bot Webhook")
    guild_id = data.get("guild_id", GUILD_ID)

    if not channel_name:
        return jsonify({"error": "channel is required"}), 400

    async def _create():
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return {"error": f"Guild {guild_id} not found"}
        channel = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel) and c.name.lower() == channel_name.lower(),
            guild.channels,
        )
        if channel is None:
            return {"error": f"Text channel '{channel_name}' not found"}
        try:
            webhook = await channel.create_webhook(name=webhook_name)
            return {"success": True, "webhook_url": webhook.url, "webhook_name": webhook.name, "channel": channel.name, "message": f"Webhook '{webhook.name}' created in #{channel.name}"}
        except discord.Forbidden:
            return {"error": "Bot doesn't have Manage Webhooks permission"}

    future = asyncio.run_coroutine_threadsafe(_create(), bot.loop)
    result = future.result(timeout=10)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result), 200


@flask_app.route("/list-webhooks", methods=["POST"])
def list_webhooks():
    data = request.get_json(force=True)
    channel_name = data.get("channel")
    guild_id = data.get("guild_id", GUILD_ID)

    if not channel_name:
        return jsonify({"error": "channel is required"}), 400

    async def _list():
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return {"error": f"Guild {guild_id} not found"}
        channel = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel) and c.name.lower() == channel_name.lower(),
            guild.channels,
        )
        if channel is None:
            return {"error": f"Text channel '{channel_name}' not found"}
        try:
            webhooks = await channel.webhooks()
            return {"success": True, "webhooks": [{"name": w.name, "url": w.url, "id": str(w.id)} for w in webhooks]}
        except discord.Forbidden:
            return {"error": "Bot doesn't have Manage Webhooks permission"}

    future = asyncio.run_coroutine_threadsafe(_list(), bot.loop)
    result = future.result(timeout=10)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result), 200


@flask_app.route("/status", methods=["GET"])
def status():
    tracks = get_cloudinary_tracks()
    local = get_local_music_files()
    return jsonify({
        "bot_ready": bot.is_ready(),
        "is_playing": is_playing,
        "in_voice": voice_client is not None and voice_client.is_connected() if voice_client else False,
        "music_files": len(tracks) + len(local),
        "cloudinary_tracks": len(tracks),
    })


# ── Entry point ────────────────────────────────────────────────────────────────
def run_flask():
    flask_app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("[flask] API server running on http://0.0.0.0:5000")
    bot.run(TOKEN)
