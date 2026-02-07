# LinkDownloaderBotForGroups — Telegram-бот для скачивания видео по ссылкам в группах

**LinkDownloaderBotForGroups** — это Telegram-бот для групп/супергрупп: Вы кидаете ссылку на видео (YouTube/Instagram/TikTok/VK/X/Facebook/Telegram и др.), бот скачивает ролик через **yt-dlp**, публикует видео в чат и (опционально) удаляет исходное сообщение со ссылкой.

Ключевые слова (для поиска): **telegram bot downloader**, **yt-dlp telegram bot**, **скачать видео по ссылке в телеграм**, **бот для групп скачивает youtube instagram tiktok vk**.

---

## Что умеет

* ✅ Скачивает видео по ссылкам и отправляет в чат как **video** (с поддержкой streaming).
* ✅ Работает в **группах и супергруппах**, включая **темы** (topics) — отвечает в нужном треде.
* ✅ Поддерживает множество источников (зависит от yt-dlp и доступности контента).
* ✅ **Без лишних уведомлений** (silent) и без превью ссылок.
* ✅ **Очередь и воркеры**: несколько скачиваний параллельно, чтобы не блокировать чат.
* ✅ **Персональный opt-out**: участник может отключить авто-скачивание для себя.
* ✅ Настраиваемые ограничения: размер файла, папка, cookies, чат логов.
* ✅ Установка одной командой через `install.sh` (Docker-рекомендуемый режим).

---

## Как это выглядит в группе

1. Участник отправляет ссылку на видео.
2. Бот скачивает ролик.
3. Бот публикует видео в группу.
4. Бот удаляет исходное сообщение со ссылкой (если у бота есть право **Delete messages**).

Под подписью к видео бот добавляет:

* кликабельную ссылку на оригинал
* кто отправил ссылку

---

## Важно про права и приватность

Чтобы бот работал «по умолчанию» (видел **все** сообщения со ссылками и мог удалять сообщения):

### 1) Отключите Group Privacy (иначе бот не видит обычные сообщения)

В BotFather:

* **Bot Settings → Group Privacy → Off**

Если приватность включена — бот будет видеть только команды и упоминания, и «авто-скачивание без упоминания» работать не будет.

### 2) Дайте боту права администратора в группе

Минимально нужные права:

* ✅ **Delete messages** (чтобы удалять исходную ссылку)

Остальные права не требуются, но можно включать по желанию.

---

## Быстрый старт (Docker, рекомендовано)

### 1) Получите токен

* В Telegram откройте **@BotFather**
* Создайте бота (`/newbot`)
* Скопируйте **BOT_TOKEN**

> Совет по безопасности: токен храните только в `.env`, не коммитьте его в Git.

### 2) Установите через install.sh

На сервере (Ubuntu/Debian):

```bash
curl -fsSL https://raw.githubusercontent.com/Avazbek22/LinkDownloaderBotForGroups/main/install.sh -o install.sh
chmod +x install.sh
./install.sh
```

Скрипт:

* установит зависимости
* клонирует репозиторий
* попросит токен и запишет его в `.env`
* поднимет контейнер через Docker Compose

---

## Конфигурация

Настройки читаются из `.env` (или переменных окружения). Файл `config.py` **не содержит секретов** — он просто читает env.

### .env (пример)

```env
BOT_TOKEN=123456789:AA...your_token_here

# Optional:
# LOGS_CHAT_ID=123456789
# MAX_FILESIZE=52428800
# OUTPUT_FOLDER=/tmp/yt-dlp-telegram
# COOKIES_FILE=/app/cookies.txt
YTDLP_JS_RUNTIMES=node
YTDLP_REMOTE_COMPONENTS=ejs:github
YTDLP_INSTAGRAM_IMPERSONATE=chrome
YTDLP_INSTAGRAM_RETRIES=8
YTDLP_INSTAGRAM_FRAGMENT_RETRIES=8
YTDLP_INSTAGRAM_SOCKET_TIMEOUT=30
```

### Переменные окружения

* **BOT_TOKEN** *(обязательно)* — токен бота.
* **LOGS_CHAT_ID** *(опционально)* — чат/канал/диалог для логов запросов (число).
* **MAX_FILESIZE** *(опционально)* — максимальный размер файла в байтах (по умолчанию 50 MB).
* **OUTPUT_FOLDER** *(опционально)* — временная папка для загрузок (по умолчанию `/tmp/yt-dlp-telegram`).
* **COOKIES_FILE** *(опционально)* — путь к cookies-файлу (если нужно для сложных сайтов/авторизации).
* **YTDLP_JS_RUNTIMES** *(по умолчанию `node`)* — JS runtime для YouTube extractor.
* **YTDLP_REMOTE_COMPONENTS** *(по умолчанию `ejs:github`)* — удалённые EJS-компоненты для устойчивости YouTube.
* **YTDLP_INSTAGRAM_IMPERSONATE** *(по умолчанию `chrome`)* — профиль impersonation для Instagram.
* **YTDLP_INSTAGRAM_RETRIES** *(по умолчанию `8`)* — retries для Instagram.
* **YTDLP_INSTAGRAM_FRAGMENT_RETRIES** *(по умолчанию `8`)* — fragment retries для Instagram.
* **YTDLP_INSTAGRAM_SOCKET_TIMEOUT** *(по умолчанию `30`)* — socket timeout для Instagram.

---

## Управление ботом в группе

### Авто-скачивание по умолчанию

* Любая ссылка на видео → скачивание → отправка → удаление исходной ссылки.

### Отключить авто-скачивание для себя

В группе напишите:

* `@ИмяБота @ВашНик`
* или `@ИмяБота me`
* или `@ИмяБота я`

Повторите — включится обратно.

### Ручной режим (когда Вы отключились)

Если у Вас отключено авто — скачивание только с упоминанием:

* `@ИмяБота <ссылка>`

---

## Структура данных

Для сохранения настроек opt-out используется файл:

* `data/prefs.json`

Он монтируется в контейнер (Docker), поэтому переживает рестарты.

---

## Обновление на сервере (Docker)

```bash
cd /root/LinkDownloaderBotForGroups

docker compose version >/dev/null 2>&1 && COMPOSE="docker compose" || COMPOSE="docker-compose"

$COMPOSE -p linkdownloaderbotforgroups down

git fetch --all --prune
# Важно: если Вы ничего локально не правили, будет чисто
# Если правили — сохраните свои изменения отдельно

git pull --ff-only

$COMPOSE -p linkdownloaderbotforgroups up -d --build
$COMPOSE -p linkdownloaderbotforgroups logs -f --tail=200
```

### Как сменить токен

1. Обновите `.env`:

```bash
cd /root/LinkDownloaderBotForGroups
nano .env
```

2. Пересоберите и перезапустите:

```bash
docker compose version >/dev/null 2>&1 && COMPOSE="docker compose" || COMPOSE="docker-compose"
$COMPOSE -p linkdownloaderbotforgroups up -d --build
$COMPOSE -p linkdownloaderbotforgroups logs -f --tail=200
```

---

## Диагностика и логи

### Посмотреть статус

```bash
cd /root/LinkDownloaderBotForGroups

docker compose version >/dev/null 2>&1 && COMPOSE="docker compose" || COMPOSE="docker-compose"

$COMPOSE -p linkdownloaderbotforgroups ps
$COMPOSE -p linkdownloaderbotforgroups logs --tail=200
```

### Посмотреть ресурсы

```bash
docker stats --no-stream linkdownloaderbot
```

### Проверить, что токен не пустой (без вывода токена)

```bash
cd /root/LinkDownloaderBotForGroups

# Проверка .env (покажет длину и head/tail)
token="$(grep -m1 '^BOT_TOKEN=' .env | cut -d= -f2- | tr -d '\r\n')"
echo "BOT_TOKEN length: ${#token}"
echo "BOT_TOKEN head/tail: ${token:0:5}...${token: -5}"

# Проверка внутри контейнера (без печати токена)
docker exec -i linkdownloaderbot sh -lc 'python - <<PY\nimport os\nt=os.getenv("BOT_TOKEN","")\nprint(len(t))\nPY'
```

---

## Частые проблемы

### Бот не реагирует на ссылки в группе

* Проверьте **Group Privacy = Off** в BotFather.
* Проверьте, что бот добавлен в группу и видит сообщения.

### Бот не удаляет исходные сообщения

* Дайте боту права администратора: **Delete messages**.

### «Не удалось скачать»

* Ссылка может быть недоступна без авторизации или из-за геоблокировки.
* Попробуйте настроить **COOKIES_FILE**.
* Посмотрите логи контейнера.

### Файл слишком большой

* Telegram-боты имеют ограничения на размер загрузки.
* Уменьшите качество/формат или уменьшите `MAX_FILESIZE`.

---

## Безопасность

* Никогда не коммитьте `.env` и токены.
* Если токен утёк — **сразу перевыпустите** его в BotFather.

---

## MIT License

Copyright (c) 2026 Avazbek Olimov

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

## Автор

**Avazbek Olimov**

Репозиторий: [https://github.com/Avazbek22/LinkDownloaderBotForGroups](https://github.com/Avazbek22/LinkDownloaderBotForGroups)
