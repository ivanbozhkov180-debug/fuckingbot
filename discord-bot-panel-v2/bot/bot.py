import os
import asyncio
import glob
import threading
from dotenv import load_dotenv

import discord
from discord.ext import commands
from discord import app_commands

from flask import Flask, request, jsonify
from flask_cors import CORS

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
MUSIC_DIR = os.path.join(os.path.dirname(__file__), "music")

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
music_files: list[str] = []
current_track_index = 0


def get_music_files() -> list[str]:
    """Return sorted list of audio files from MUSIC_DIR."""
    patterns = ("*.mp3", "*.wav", "*.ogg", "*.flac", "*.m4a")
    files = []
    for pattern in patterns:
        files.extend(glob.glob(os.path.join(MUSIC_DIR, pattern)))
    return sorted(files)


async def play_next(guild: discord.Guild):
    """Play the next track in the queue (loops forever)."""
    global is_playing, current_track_index, voice_client

    if not is_playing or voice_client is None or not voice_client.is_connected():
        return

    files = get_music_files()
    if not files:
        print("[bot] No music files found in /music directory.")
        is_playing = False
        return

    track = files[current_track_index % len(files)]
    current_track_index = (current_track_index + 1) % len(files)

    print(f"[bot] Playing: {os.path.basename(track)}")

    source = discord.FFmpegPCMAudio(track)
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

    files = get_music_files()
    if not files:
        await interaction.response.send_message("❌ No audio files found in `/music` folder.", ephemeral=True)
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
    # Sync to specific guild first (instant), then globally
    try:
        guild = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        print(f"[bot] Logged in as {bot.user} | Commands synced to guild {GUILD_ID}")
    except discord.Forbidden:
        # Fallback to global sync if guild sync fails
        await tree.sync()
        print(f"[bot] Logged in as {bot.user} | Commands synced globally (guild sync failed)")


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

        files = get_music_files()
        if not files:
            return {"error": "No audio files in /music folder"}

        if voice_client and voice_client.is_connected():
            await voice_client.move_to(vc)
        else:
            voice_client = await vc.connect()

        is_playing = True
        current_track_index = 0
        await play_next(guild)
        return {"success": True, "message": f"Playing in '{vc.name}'", "tracks": len(files)}

    future = asyncio.run_coroutine_threadsafe(_play(), bot.loop)
    result = future.result(timeout=10)

    if "error" in result:
        return jsonify(result), 404
    return jsonify(result), 200


@flask_app.route("/stop", methods=["POST"])
def stop_route():
    global voice_client, is_playing

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
            return {
                "success": True,
                "webhook_url": webhook.url,
                "webhook_name": webhook.name,
                "channel": channel.name,
                "message": f"Webhook '{webhook.name}' created in #{channel.name}"
            }
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
            return {
                "success": True,
                "webhooks": [{"name": w.name, "url": w.url, "id": str(w.id)} for w in webhooks]
            }
        except discord.Forbidden:
            return {"error": "Bot doesn't have Manage Webhooks permission"}

    future = asyncio.run_coroutine_threadsafe(_list(), bot.loop)
    result = future.result(timeout=10)

    if "error" in result:
        return jsonify(result), 400
    return jsonify(result), 200


@flask_app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "bot_ready": bot.is_ready(),
        "is_playing": is_playing,
        "in_voice": voice_client is not None and voice_client.is_connected() if voice_client else False,
        "music_files": len(get_music_files()),
    })


# ── Entry point ────────────────────────────────────────────────────────────────
def run_flask():
    flask_app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    # Start Flask in a background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("[flask] API server running on http://0.0.0.0:5000")

    # Run Discord bot (blocking)
    bot.run(TOKEN)
