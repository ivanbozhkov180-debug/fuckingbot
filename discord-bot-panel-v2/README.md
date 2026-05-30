# Discord Bot + Web Panel

## Структура проекта
```
bot/
  bot.py            ← основной файл бота
  .env              ← токен и Guild ID (не коммитить в git!)
  requirements.txt  ← зависимости Python
  music/            ← кладёшь сюда .mp3/.wav/.ogg файлы
index.html          ← веб-панель управления
```

---

## Быстрый старт

### 1. Установи системные зависимости

**Ubuntu/Debian:**
```bash
sudo apt update && sudo apt install ffmpeg python3 python3-pip -y
```

**macOS:**
```bash
brew install ffmpeg python
```

**Windows:**
- Скачай FFmpeg: https://ffmpeg.org/download.html и добавь в PATH
- Установи Python 3.10+: https://python.org

---

### 2. Создай Discord-бота

1. Зайди на https://discord.com/developers/applications
2. New Application → дай имя
3. Bot → Add Bot → скопируй **Token**
4. OAuth2 → URL Generator:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Connect`, `Speak`, `Manage Roles`, `Send Messages`
5. Перейди по сгенерированной ссылке и добавь бота на сервер
6. В Bot: включи **Server Members Intent**

---

### 3. Настрой .env

```bash
cd bot
cp .env .env.bak   # на всякий случай
```

Отредактируй `bot/.env`:
```
DISCORD_TOKEN=твой_токен_сюда
GUILD_ID=id_твоего_сервера
```

**Как узнать Guild ID:** Discord → Настройки → Расширенные → включи Режим разработчика → правый клик на сервере → "Скопировать ID"

---

### 4. Установи зависимости Python

```bash
cd bot
pip install -r requirements.txt
```

---

### 5. Добавь музыку

Скопируй любые `.mp3`, `.wav`, `.ogg` файлы в папку `bot/music/`.

---

### 6. Запусти бота

```bash
cd bot
python bot.py
```

Бот запустится и Flask API поднимется на `http://localhost:5000`.

---

### 7. Открой веб-панель

Открой `index.html` в браузере (двойной клик или через локальный сервер).

- **Backend URL**: `http://localhost:5000`
- Нажми **PING** — должно появиться уведомление "Бот онлайн"

---

## Команды бота

| Команда | Описание |
|---------|----------|
| `/play [канал]` | Зайти в голосовой канал и играть музыку по кругу |
| `/stop` | Остановить музыку и выйти из канала |

---

## API эндпоинты (Flask :5000)

| Метод | Путь | Тело | Описание |
|-------|------|------|----------|
| POST | `/give-role` | `{user_id, role_name, guild_id}` | Выдать роль пользователю |
| POST | `/play` | `{channel}` | Начать воспроизведение |
| POST | `/stop` | — | Остановить воспроизведение |
| GET  | `/status` | — | Статус бота |

---

## Важно

- Бот должен иметь роль **выше** той роли, которую он выдаёт (иначе ошибка прав)
- `.env` файл **не** нужно коммитить в git — добавь его в `.gitignore`
- CORS включён для всех источников — ограничь при деплое в продакшн
