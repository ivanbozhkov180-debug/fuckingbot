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
    guild = discord.Object(id=GUILD_ID)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"[bot] Logged in as {bot.user} | Commands synced to guild {GUILD_ID}")


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
            return {"success": True, "message": "Stopped and disconnected"}<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>BOT CONTROL — PANEL</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@300;400;500&display=swap" rel="stylesheet" />
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg:        #0a0a0a;
    --surface:   #111111;
    --border:    #222222;
    --border-hi: #333333;
    --amber:     #f59e0b;
    --amber-dim: #92600a;
    --amber-glow:#f59e0b33;
    --green:     #22c55e;
    --red:       #ef4444;
    --text:      #e5e5e5;
    --muted:     #555555;
    --label:     #888888;
  }
  html { height: 100%; }
  body {
    min-height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    line-height: 1.6;
    background-image: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(255,255,255,.012) 2px, rgba(255,255,255,.012) 4px);
  }
  .shell { max-width: 720px; margin: 0 auto; padding: 48px 24px 80px; }
  header { border-bottom: 1px solid var(--border); padding-bottom: 28px; margin-bottom: 40px; display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; }
  .logo-tag { font-size: 10px; letter-spacing: .2em; color: var(--amber); text-transform: uppercase; margin-bottom: 4px; }
  h1 { font-size: 22px; font-weight: 600; letter-spacing: .04em; color: #fff; }
  h1 span { color: var(--amber); }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); display: inline-block; margin-right: 6px; transition: background .4s, box-shadow .4s; }
  .status-dot.live { background: var(--green); box-shadow: 0 0 8px var(--green); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100% { box-shadow: 0 0 8px var(--green); } 50% { box-shadow: 0 0 16px var(--green); } }
  .status-label { font-size: 11px; color: var(--label); letter-spacing: .1em; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 2px; padding: 28px; margin-bottom: 20px; position: relative; transition: border-color .2s; }
  .card:hover { border-color: var(--border-hi); }
  .card::before { content: ''; position: absolute; left: 0; top: 0; width: 2px; height: 100%; background: var(--amber-dim); border-radius: 2px 0 0 2px; transition: background .2s; }
  .card:hover::before { background: var(--amber); }
  .card-title { font-size: 10px; letter-spacing: .2em; text-transform: uppercase; color: var(--amber); margin-bottom: 20px; display: flex; align-items: center; gap: 10px; }
  .card-title::after { content: ''; flex: 1; height: 1px; background: var(--border); }
  .field { margin-bottom: 16px; }
  .field:last-child { margin-bottom: 0; }
  label { display: block; font-size: 10px; letter-spacing: .15em; text-transform: uppercase; color: var(--label); margin-bottom: 6px; }
  input[type="text"], input[type="url"] { width: 100%; background: #0d0d0d; border: 1px solid var(--border); border-radius: 2px; color: var(--text); font-family: 'IBM Plex Mono', monospace; font-size: 13px; padding: 10px 14px; outline: none; transition: border-color .2s, box-shadow .2s; }
  input[type="text"]:focus, input[type="url"]:focus { border-color: var(--amber-dim); box-shadow: 0 0 0 2px var(--amber-glow); }
  input::placeholder { color: var(--muted); }
  .btn-row { display: flex; gap: 10px; margin-top: 20px; flex-wrap: wrap; }
  button { font-family: 'IBM Plex Mono', monospace; font-size: 11px; font-weight: 500; letter-spacing: .12em; text-transform: uppercase; border: none; border-radius: 2px; padding: 10px 22px; cursor: pointer; transition: opacity .15s, transform .1s, box-shadow .2s; position: relative; overflow: hidden; }
  button:active { transform: scale(.97); }
  .btn-primary { background: var(--amber); color: #000; }
  .btn-primary:hover { opacity: .9; box-shadow: 0 0 20px var(--amber-glow); }
  .btn-success { background: transparent; color: var(--green); border: 1px solid var(--green); }
  .btn-success:hover { background: rgba(34,197,94,.08); box-shadow: 0 0 16px rgba(34,197,94,.15); }
  .btn-danger { background: transparent; color: var(--red); border: 1px solid var(--red); }
  .btn-danger:hover { background: rgba(239,68,68,.08); box-shadow: 0 0 16px rgba(239,68,68,.15); }
  button:disabled { opacity: .35; cursor: not-allowed; transform: none; }
  .btn-spinner { display: none; width: 10px; height: 10px; border: 1.5px solid currentColor; border-top-color: transparent; border-radius: 50%; animation: spin .6s linear infinite; margin-right: 6px; vertical-align: middle; }
  button.loading .btn-spinner { display: inline-block; }
  @keyframes spin { to { transform: rotate(360deg); } }
  #toast-container { position: fixed; bottom: 24px; right: 24px; display: flex; flex-direction: column-reverse; gap: 10px; z-index: 999; pointer-events: none; }
  .toast { font-family: 'IBM Plex Mono', monospace; font-size: 12px; padding: 12px 18px; border-radius: 2px; border-left: 3px solid; max-width: 340px; backdrop-filter: blur(8px); animation: toast-in .25s ease forwards; pointer-events: all; }
  .toast.success { background: rgba(17,17,17,.95); border-color: var(--green); color: var(--green); }
  .toast.error { background: rgba(17,17,17,.95); border-color: var(--red); color: var(--red); }
  .toast.out { animation: toast-out .25s ease forwards; }
  @keyframes toast-in { from { opacity: 0; transform: translateX(20px); } to { opacity: 1; transform: translateX(0); } }
  @keyframes toast-out { from { opacity: 1; transform: translateX(0); } to { opacity: 0; transform: translateX(20px); } }
  .hint { font-size: 11px; color: var(--muted); margin-top: 8px; }
  .webhook-item { background: #0d0d0d; border: 1px solid var(--border); border-radius: 2px; padding: 12px 14px; margin-bottom: 8px; }
  .webhook-item-name { color: var(--amber); font-size: 12px; margin-bottom: 6px; }
  .webhook-item-url { color: var(--muted); font-size: 11px; word-break: break-all; cursor: pointer; transition: color .2s; }
  .webhook-item-url:hover { color: var(--text); }
  footer { margin-top: 48px; padding-top: 20px; border-top: 1px solid var(--border); font-size: 10px; color: var(--muted); letter-spacing: .1em; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
</style>
</head>
<body>
<div class="shell">

  <header>
    <div class="logo-block">
      <div class="logo-tag">Discord Bot</div>
      <h1>CONTROL<span>_</span>PANEL</h1>
    </div>
    <div>
      <span class="status-dot" id="status-dot"></span>
      <span class="status-label" id="status-label">OFFLINE</span>
    </div>
  </header>

  <!-- Backend URL -->
  <div class="card">
    <div class="card-title">⚙ Connection</div>
    <div class="field">
      <label>Backend URL</label>
      <input type="url" id="backend-url" placeholder="http://localhost:5000" />
      <p class="hint">Адрес сервера где запущен bot.py. Сохраняется автоматически.</p>
    </div>
    <div class="btn-row">
      <button class="btn-primary" onclick="checkStatus()">
        <span class="btn-spinner"></span>PING
      </button>
    </div>
  </div>

  <!-- Give Role -->
  <div class="card">
    <div class="card-title">👤 Выдать роль</div>
    <div class="field">
      <label>User ID</label>
      <input type="text" id="user-id" placeholder="123456789012345678" />
    </div>
    <div class="field">
      <label>Название роли</label>
      <input type="text" id="role-name" placeholder="Member" />
    </div>
    <div class="field">
      <label>Guild ID</label>
      <input type="text" id="guild-id" placeholder="987654321098765432" />
    </div>
    <div class="btn-row">
      <button class="btn-primary" id="btn-give" onclick="giveRole()">
        <span class="btn-spinner"></span>ВЫДАТЬ
      </button>
    </div>
  </div>

  <!-- Music -->
  <div class="card">
    <div class="card-title">🎵 Музыка</div>
    <div class="field">
      <label>Голосовой канал</label>
      <input type="text" id="channel-name" placeholder="General" />
    </div>
    <div class="btn-row">
      <button class="btn-success" id="btn-play" onclick="playMusic()">
        <span class="btn-spinner"></span>▶ ВКЛЮЧИТЬ
      </button>
      <button class="btn-danger" id="btn-stop" onclick="stopMusic()">
        <span class="btn-spinner"></span>■ ВЫКЛЮЧИТЬ
      </button>
    </div>
  </div>

  <!-- Webhooks -->
  <div class="card">
    <div class="card-title">🔗 Вебхуки</div>
    <div class="field">
      <label>Текстовый канал</label>
      <input type="text" id="webhook-channel" placeholder="general" />
    </div>
    <div class="field">
      <label>Название вебхука</label>
      <input type="text" id="webhook-name-input" placeholder="My Webhook" />
    </div>
    <div class="btn-row">
      <button class="btn-primary" id="btn-create-webhook" onclick="createWebhook()">
        <span class="btn-spinner"></span>+ СОЗДАТЬ
      </button>
      <button class="btn-success" id="btn-list-webhooks" onclick="listWebhooks()">
        <span class="btn-spinner"></span>≡ СПИСОК
      </button>
    </div>

    <div id="webhook-result" style="display:none; margin-top:20px;">
      <div style="border-top:1px solid var(--border); padding-top:16px;">
        <label>URL вебхука</label>
        <div style="display:flex; gap:8px; margin-top:6px;">
          <input type="text" id="webhook-url-output" readonly style="flex:1; color:var(--amber); cursor:pointer;" onclick="copyWebhookUrl()" />
          <button class="btn-primary" onclick="copyWebhookUrl()" style="padding:10px 14px; white-space:nowrap;">КОПИРОВАТЬ</button>
        </div>
        <p class="hint">Нажми на поле или кнопку чтобы скопировать URL</p>
      </div>
    </div>

    <div id="webhooks-list" style="display:none; margin-top:20px;">
      <div style="border-top:1px solid var(--border); padding-top:16px;">
        <label style="margin-bottom:10px; display:block;">Существующие вебхуки</label>
        <div id="webhooks-list-items"></div>
      </div>
    </div>
  </div>

  <footer>
    <span>BOT_CONTROL v1.0.0 · Flask API</span>
    <span>Python discord.py + FFmpeg</span>
  </footer>

</div>

<div id="toast-container"></div>

<script>
function getBase() {
  return (document.getElementById('backend-url').value || 'http://localhost:5000').replace(/\/$/, '');
}
function saveUrl() {
  localStorage.setItem('bot_backend_url', document.getElementById('backend-url').value);
}
function loadSaved() {
  const saved = localStorage.getItem('bot_backend_url');
  if (saved) document.getElementById('backend-url').value = saved;
  const savedGuild = localStorage.getItem('bot_guild_id');
  if (savedGuild) document.getElementById('guild-id').value = savedGuild;
}
document.getElementById('backend-url').addEventListener('input', saveUrl);
document.getElementById('guild-id').addEventListener('input', () => {
  localStorage.setItem('bot_guild_id', document.getElementById('guild-id').value);
});

function toast(msg, type = 'success') {
  const c = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = (type === 'success' ? '✓ ' : '✗ ') + msg;
  c.appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 300); }, 4000);
}

function setLoading(btn, state) {
  btn.disabled = state;
  btn.classList.toggle('loading', state);
}

async function api(endpoint, method = 'POST', body = null) {
  const res = await fetch(getBase() + endpoint, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json();
  if (!res.ok) throw new Error(json.error || `HTTP ${res.status}`);
  return json;
}

async function checkStatus() {
  const btn = event.currentTarget;
  setLoading(btn, true);
  try {
    const data = await api('/status', 'GET');
    const dot = document.getElementById('status-dot');
    const lbl = document.getElementById('status-label');
    if (data.bot_ready) {
      dot.className = 'status-dot live';
      lbl.textContent = data.is_playing ? 'PLAYING ♪' : 'ONLINE';
      toast(`Бот онлайн · ${data.music_files} треков в /music`);
    }
  } catch (e) {
    document.getElementById('status-dot').className = 'status-dot';
    document.getElementById('status-label').textContent = 'OFFLINE';
    toast('Нет соединения: ' + e.message, 'error');
  } finally {
    setLoading(btn, false);
  }
}

async function giveRole() {
  const btn = document.getElementById('btn-give');
  const userId   = document.getElementById('user-id').value.trim();
  const roleName = document.getElementById('role-name').value.trim();
  const guildId  = document.getElementById('guild-id').value.trim();
  if (!userId || !roleName || !guildId) { toast('Заполните User ID, название роли и Guild ID', 'error'); return; }
  setLoading(btn, true);
  try {
    const data = await api('/give-role', 'POST', { user_id: userId, role_name: roleName, guild_id: guildId });
    toast(data.message || 'Роль выдана успешно');
  } catch (e) {
    toast('Ошибка: ' + e.message, 'error');
  } finally {
    setLoading(btn, false);
  }
}

async function playMusic() {
  const btn = document.getElementById('btn-play');
  const ch = document.getElementById('channel-name').value.trim();
  if (!ch) { toast('Укажите название голосового канала', 'error'); return; }
  setLoading(btn, true);
  try {
    const data = await api('/play', 'POST', { channel: ch });
    toast(data.message || 'Воспроизведение начато');
    document.getElementById('status-label').textContent = 'PLAYING ♪';
    document.getElementById('status-dot').className = 'status-dot live';
  } catch (e) {
    toast('Ошибка: ' + e.message, 'error');
  } finally {
    setLoading(btn, false);
  }
}

async function stopMusic() {
  const btn = document.getElementById('btn-stop');
  setLoading(btn, true);
  try {
    const data = await api('/stop', 'POST');
    toast(data.message || 'Музыка остановлена');
    document.getElementById('status-label').textContent = 'ONLINE';
  } catch (e) {
    toast('Ошибка: ' + e.message, 'error');
  } finally {
    setLoading(btn, false);
  }
}

async function createWebhook() {
  const btn = document.getElementById('btn-create-webhook');
  const channel = document.getElementById('webhook-channel').value.trim();
  const name = document.getElementById('webhook-name-input').value.trim();
  const guildId = document.getElementById('guild-id').value.trim();
  if (!channel) { toast('Укажите название текстового канала', 'error'); return; }
  setLoading(btn, true);
  document.getElementById('webhook-result').style.display = 'none';
  try {
    const data = await api('/create-webhook', 'POST', {
      channel,
      webhook_name: name || 'Bot Webhook',
      guild_id: guildId
    });
    document.getElementById('webhook-url-output').value = data.webhook_url;
    document.getElementById('webhook-result').style.display = 'block';
    toast(data.message || 'Вебхук создан!');
  } catch (e) {
    toast('Ошибка: ' + e.message, 'error');
  } finally {
    setLoading(btn, false);
  }
}

async function listWebhooks() {
  const btn = document.getElementById('btn-list-webhooks');
  const channel = document.getElementById('webhook-channel').value.trim();
  const guildId = document.getElementById('guild-id').value.trim();
  if (!channel) { toast('Укажите название текстового канала', 'error'); return; }
  setLoading(btn, true);
  document.getElementById('webhooks-list').style.display = 'none';
  try {
    const data = await api('/list-webhooks', 'POST', { channel, guild_id: guildId });
    const container = document.getElementById('webhooks-list-items');
    container.innerHTML = '';
    if (data.webhooks.length === 0) {
      container.innerHTML = '<p class="hint">Вебхуков нет в этом канале</p>';
    } else {
      data.webhooks.forEach(w => {
        const item = document.createElement('div');
        item.className = 'webhook-item';
        item.innerHTML = `
          <div class="webhook-item-name">${w.name}</div>
          <div class="webhook-item-url" onclick="copyText('${w.url}')" title="Нажми чтобы скопировать">${w.url}</div>
        `;
        container.appendChild(item);
      });
    }
    document.getElementById('webhooks-list').style.display = 'block';
    toast(`Найдено вебхуков: ${data.webhooks.length}`);
  } catch (e) {
    toast('Ошибка: ' + e.message, 'error');
  } finally {
    setLoading(btn, false);
  }
}

function copyWebhookUrl() {
  const val = document.getElementById('webhook-url-output').value;
  copyText(val);
}

function copyText(text) {
  navigator.clipboard.writeText(text).then(() => toast('URL скопирован!')).catch(() => {
    const el = document.createElement('textarea');
    el.value = text;
    document.body.appendChild(el);
    el.select();
    document.execCommand('copy');
    document.body.removeChild(el);
    toast('URL скопирован!');
  });
}

loadSaved();
</script>
</body>
</html>

        return {"error": "Bot is not connected to a voice channel"}

    future = asyncio.run_coroutine_threadsafe(_stop(), bot.loop)
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
