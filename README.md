# video-transcription-extractor

Локальна офлайн-транскрипція відео/аудіо з опціональним LLM-сумаризатором.

- Однією командою з CLI, правого кліку в Finder/Explorer або через MCP-tool.
- `faster-whisper` (CTranslate2) на CPU, CUDA (Windows/Linux) або MPS/CPU (Apple Silicon).
- На виході три файли: `<name>.raw.txt` (верба́тимний Whisper з `[MM:SS]`), `<name>.clean.md` (почищений транскрипт з розділами) та `<name>.summary.md` (Overview + Ключові факти + Наміри + По чаптерах).
- Cleanup-шар: rule-based (dedup + стичку речень + стиснення філлерів) або опційно LLM, валідований word-subsequence чеком (fails-safe у rule-based).
- Сумаризатор: швидкий extractive (без LLM) або локальний Ollama у JSON-mode з fallback на extractive.
- Мережа заблокована за замовчуванням (тільки loopback для Ollama).

## Зміст

- [5-хвилинний Quickstart](#5-хвилинний-quickstart)
- [Системні вимоги](#системні-вимоги)
- [Встановлення](#встановлення)
- [Вихідні файли](#вихідні-файли)
- [Використання CLI](#використання-cli)
- [Cleanup-режими](#cleanup-режими)
- [Сумаризація](#сумаризація)
- [Контекстне меню Windows](#контекстне-меню-windows)
- [Quick Action на macOS](#quick-action-на-macos)
- [MCP-сервер](#mcp-сервер)
- [Міграція з попередньої схеми](#міграція-з-попередньої-схеми)
- [Тести](#тести)
- [Pre-push checklist](#pre-push-checklist)

## 5-хвилинний Quickstart

Все що потрібно, аби отримати транскрипт тестового відео.

```bash
# 1. Клонуй репозиторій
git clone <repo-url> video-transcription-extractor
cd video-transcription-extractor

# 2. Створи віртуальне середовище (.venv в корені — цей шлях очікують скрипти контекстного меню)
python3 -m venv .venv
source .venv/bin/activate          # Linux / macOS
# .venv\Scripts\activate            # Windows PowerShell

# 3. Постав залежності (CPU-only работає з коробки на всіх платформах)
python -m pip install -r requirements.txt

# 4. Переконайся, що ffmpeg на PATH
ffmpeg -version

# 5. Запусти на прикладі
python -m app transcribe --input ./video-sample/1.mp4 --profile best --language uk --progress
```

Файли результату опиняться поруч з відео:

- `video-sample/1.raw.txt` — верба́тимний Whisper-вихід, по одному utterance на рядок з префіксом `[MM:SS]`. Ніякого dedup чи стикування речень — це ground truth.
- `video-sample/1.clean.md` — почищений транскрипт з тематичними розділами (`## [MM:SS] Chapter N: Title`), придатний для читання.
- `video-sample/1.summary.md` — Overview + Ключові факти + Наміри / дії + По чаптерах.

Детальніше про відмінність між файлами — у секції [Вихідні файли](#вихідні-файли). Далі можна налаштувати [контекстне меню Explorer/Finder](#контекстне-меню-windows), [MCP-інтеграцію](#mcp-сервер), або перейти на [LLM-сумаризатор через Ollama](#сумаризація).

## Системні вимоги

| Компонент | Мінімум | Рекомендовано | Примітки |
| --- | --- | --- | --- |
| Python | 3.10 | 3.11+ | Тип-підказки і `asyncio` вимагають 3.10. |
| RAM | 4 GB | 8 GB | `large-v3` на CPU тримає ~2 GB. |
| Диск | 2 GB | 5 GB | Модель Whisper + кеш HuggingFace. |
| ffmpeg | будь-яка сучасна | > 4.4 | Має бути в `PATH`. |
| ОС | Windows 10 / macOS 12 / Ubuntu 20.04 | актуальні версії | Підтримуються Windows 10+, macOS (Intel + Apple Silicon), більшість Linux. |
| GPU (опціонально) | — | NVIDIA з CUDA 12 / Apple Silicon M1+ | Використовується автоматично, якщо доступна. |
| Ollama (опціонально) | — | 0.3+ | Для якісного LLM-сумаризатора. |

Офлайн-режим: застосунок обмежує мережу до loopback (`127.0.0.0/8` / `::1`). Непотрібні звернення до інтернету та DNS-резолви поза loopback блокуються на рівні `socket.getaddrinfo`.

## Встановлення

### Базова установка (CPU)

Працює на Windows/macOS/Linux без додаткових драйверів.

```bash
python -m pip install -r requirements.txt
```

Залежності: `httpx` (для Ollama-клієнта) та `faster-whisper` (CTranslate2 backend для Whisper).

### NVIDIA GPU (CUDA, Windows/Linux)

```bash
python -m pip install -r requirements.txt -r requirements-gpu.txt
```

GPU підхоплюється автоматично, якщо `CUDA_PATH` виставлений і є сумісна версія cuDNN. Якщо GPU не знайдено — пайплайн тихо падає на CPU.

### Apple Silicon (M1/M2/M3)

Apple Silicon працює на стандартних wheels з `requirements.txt` — `faster-whisper` та `CTranslate2` публікують arm64-колеса на PyPI. GPU-прискорення через Metal/MPS не потрібне: CTranslate2 має оптимізовані ARM-ядра, що майже так само швидкі як CUDA на референсних M-моделях.

Типові дрібниці на macOS:

```bash
# Встанови ffmpeg через brew
brew install ffmpeg

# Переконайся, що використовуєш нативний arm64 Python, а не Rosetta
python3 -c "import platform; print(platform.machine())"   # очікуємо 'arm64'
```

Якщо в тебе emulation через Rosetta (`platform.machine() == 'x86_64'`), переустанови Python з офіційного інсталятора або через `brew install python@3.11`.

### Встановлення моделі Whisper (офлайн-кеш)

За замовчуванням використовується `large-v3`. При першому запуску модель буде завантажена в `~/.cache/huggingface/hub` (або в `%USERPROFILE%\.cache\huggingface\hub` на Windows). Це **єдиний** онлайн-крок — надалі офлайн-блокування активне.

Щоб заздалегідь прогріти кеш без запуску транскрипції:

```bash
python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', download_root='.models')"
```

Потім передай `--model-cache-dir .models`, якщо хочеш локальний кеш замість домашнього.

### Ollama (опціонально, для якісних переказів)

1. Встанови Ollama з [ollama.com](https://ollama.com) (Windows/macOS/Linux).
2. Стягни мовно-сильну модель:

   ```bash
   ollama pull llama3.1:8b        # універсальна, 5 GB
   ollama pull qwen2.5:7b         # краща для не-англ, ~5 GB
   ollama pull gemma2:9b          # альтернатива, 6 GB
   ```

3. Переконайся, що сервер слухає на `127.0.0.1:11434` (дефолт).
4. Додай `--summary ollama --ollama-model llama3.1:8b` до команди CLI.

Якщо Ollama недоступна (не запущена, мережа обмежена, timeout) — пайплайн автоматично падає на extractive-сумаризатор і продовжує без помилки.

## Вихідні файли

Для кожного обробленого медіа-файлу пайплайн пише три артефакти поруч (або у `--output-dir`, якщо задано):

| Файл | Що всередині | Призначення |
| --- | --- | --- |
| `<name>.raw.txt` | Верба́тимний вихід Whisper. По одному utterance на рядок, кожен із префіксом `[MM:SS]` (або `[HH:MM:SS]` для довших за годину відео). Без dedup, без злиття речень. | Ground truth. Референс для дебагу, коли cleanup здається надто агресивним. |
| `<name>.clean.md` | Почищений транскрипт з тематичними розділами: `## [MM:SS] Chapter N: Title`. Спікер-теги прибрані, розбиті речення склеєні, повтори виведені. | Те, що людина читає. |
| `<name>.summary.md` | Markdown з чотирма секціями: `## Overview`, `## Ключові факти`, `## Наміри / дії`, `## По чаптерах`. Кожен bullet має таймкод назад у `.raw.txt` / `.clean.md`. | Швидкий огляд і посилання на моменти. |

Будь-який із цих файлів можна вимкнути:

```bash
python -m app transcribe --input video.mp4 --no-raw-file        # пропустити .raw.txt
python -m app transcribe --input video.mp4 --no-clean-file      # пропустити .clean.md
python -m app transcribe --input video.mp4 --no-summary-file    # пропустити .summary.md
python -m app transcribe --input video.mp4 --summary none       # повністю вимкнути сумаризатор
```

## Використання CLI

Базовий запуск:

```bash
python -m app transcribe --input path/to/video.mp4 --language uk --progress
```

Профілі:

```bash
# Швидко, нижча якість
python -m app transcribe --input ./video-sample/1.mp4 --profile fast --language uk --progress

# Повільніше, краще (default)
python -m app transcribe --input ./video-sample/1.mp4 --profile best --language uk --progress
```

Пакетна обробка каталогу:

```bash
python -m app transcribe \
    --input /path/to/Videos \
    --ext mp4,mov,mkv \
    --output-dir /path/to/Transcripts \
    --profile best --language uk
```

Корисні прапорці:

- `--model large-v3` — явно задати Whisper-модель.
- `--model-cache-dir .models` — локальний кеш замість `~/.cache/huggingface`.
- `--timeout 600` — hard-deadline в секундах на файл.
- `--no-chapters` — плаский транскрипт без розділів.
- `--stdout` — додатково надрукувати транскрипт в stdout.
- `--verbose` — детальні логи прогресу.
- `--clean-mode {raw,rule-based,llm}` — як будувати `.clean.md`, див. [Cleanup-режими](#cleanup-режими).
- `--no-raw-file` / `--no-clean-file` / `--no-summary-file` — вимкнути відповідний вихідний файл.

## Cleanup-режими

Аргумент `--clean-mode` контролює, наскільки агресивно чиститься `.clean.md`. Сам `.raw.txt` залишається верба́тимним завжди.

| Режим | Що робить | Коли використовувати |
| --- | --- | --- |
| `raw` | Нічого не чистить. Тільки групує utterance-и у розділи за таймкодом. | Коли потрібен відформатований документ без жодних змін слів. |
| `rule-based` *(default)* | Exact dedup (у вікні ±5 с, один і той же спікер), rolling-overlap dedup через 3-word shingles (поріг 60%), склейка розбитих речень (мид-speaker protection), стиснення серій з 3+ однакових коротких філлерів. Ніколи не додає і не перефразовує слова. | Звичайний випадок. Whisper часто дублює chunk-и на межах сегментів — rule-based це прибирає без ризику вигадати нові слова. |
| `llm` | Спочатку rule-based, потім Ollama LLM-polish з `format: "json"`. Результат перевіряється word-subsequence чеком (кожне слово у відповіді LLM має бути у вхідному тексті, у тому самому порядку). Якщо перевірка не проходить — автоматично повертаємось на rule-based. | Довгі, розмовні транскрипти з багатьма повторами і обривами, які rule-based не бере. Потребує запущеної Ollama на `127.0.0.1:11434`. |

Якщо Ollama недоступна для `--clean-mode=llm`, cleanup тихо падає на rule-based (в логах буде рядок `Ollama not reachable; --clean-mode=llm will fall back to rule-based.`).

Rule-based cleanup диаризаційно-акуратний: ніколи не схлопує utterance-и різних спікерів і не склеює речення через зміну спікера.

## Сумаризація

Сумаризатор вмикається за замовчуванням у режимі `ollama` (падає на `extractive`, якщо Ollama недоступна). Результат пишеться в окремий файл `<name>.summary.md` з чотирма секціями:

- `## Overview` — 3–5 речень, синтез всього відео.
- `## Ключові факти` — bullet-и з конкретними числами/фактами, кожен з таймкодом.
- `## Наміри / дії` — чого мовець хоче досягти або що збирається робити (таймкод на кожному bullet-і).
- `## По чаптерах` — по одному короткому bullet-у на розділ, із номером розділу та його таймкодом.

```bash
# Default: спробувати Ollama, впасти на extractive
python -m app transcribe --input ./video-sample/1.mp4 --language uk

# Явно — тільки offline extractive
python -m app transcribe --input ./video-sample/1.mp4 --summary extractive --language uk

# Явно — Ollama з конкретною моделлю
python -m app transcribe --input ./video-sample/1.mp4 \
    --summary ollama --ollama-model qwen2.5:7b --language uk

# Повністю вимкнути (залишаться тільки .raw.txt і .clean.md)
python -m app transcribe --input ./video-sample/1.mp4 --summary none
```

Тонкі налаштування:

- `--summary-per-chapter 3` — речень на розділ у `.summary.md` (екстрактивний шлях) / bullet-ів по чаптерах.
- `--summary-overview 5` — речень у верхньому Overview.
- `--no-summary-file` — не створювати окремий `.summary.md` (але сумаризатор все одно може відрефайнити заголовки розділів у `.clean.md`).
- `--title-style keywords|snippet` — heuristic заголовків перед LLM-рефайном (`keywords` — топові терміни, `snippet` — перша змістовна фраза розділу).
- `--title-max-words 6` — обмежити довжину заголовків після LLM-рефайну.

## Контекстне меню Windows

Додає пункт **"Створити транскрипцію"** у правий клік Explorer для всіх типових відео- і аудіо-файлів. Працює без admin-прав — ключі пишуться тільки в `HKCU`.

### Встановлення

Запусти в PowerShell з кореня проекту:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
    -File .\scripts\windows\install_context_menu.ps1
```

За замовчуванням реєструються розширення:

- Відео: `.mp4 .mkv .avi .mov .webm .wmv .flv .m4v .mpeg .mpg .ts .3gp`
- Аудіо: `.mp3 .wav .flac .m4a .aac .ogg .opus .wma`

Повний приклад з кастомним лейблом та звуженим списком форматів:

```powershell
.\scripts\windows\install_context_menu.ps1 `
    -MenuLabel 'Create transcription' `
    -Extensions @('mp4','mkv','mp3','wav')
```

### Іконка (опціонально)

Якщо покласти `app.ico` або `icon.ico` в `scripts\windows\` (або `app\resources\app.ico`) — інсталятор автоматично пропише її як іконку пункту меню.

### Видалення

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
    -File .\scripts\windows\uninstall_context_menu.ps1
```

### Як це працює

Інсталятор пише під кожне розширення гілку:

```
HKCU:\Software\Classes\SystemFileAssociations\.<ext>\shell\CreateTranscription
    (Default)  = "Створити транскрипцію"
    Icon       = "<project>\scripts\windows\app.ico"
    \command
        (Default) = 'powershell -NoProfile -ExecutionPolicy Bypass -File
                     "<project>\scripts\windows\Invoke-CreateTranscription.ps1" -FilePath "%1"'
```

Worker-скрипт (`Invoke-CreateTranscription.ps1`) резолвить корінь проекту, віддає перевагу `.venv\Scripts\python.exe`, а якщо його немає — бере `python` з `PATH`, і запускає `python -m app transcribe --input "<file>" --progress`.

### Траблшутинг

| Симптом | Причина і що робити |
| --- | --- |
| `running scripts is disabled on this system` | Лонч скрипту зашкарбано PowerShell-політикою. Запускай через `-ExecutionPolicy Bypass` як у прикладах або виконай `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` одноразово. |
| Пункт меню виводиться крабозябрами | Скрипти мають бути в UTF-8 з BOM. Якщо переписував — збережи знову в UTF-8 (BOM) через VS Code / Notepad++. |
| Пункт зʼявляється тільки після "Показати додаткові параметри" (Windows 11) | Це очікувано — старий контекстний API живе під пунктом "Show more options" (`Shift+F10`). Для "нового" меню потрібен Shell Extension; поза скоупом цього проекта. |
| Клік нічого не робить | Перевір `Invoke-CreateTranscription.ps1` — він зберігає консоль при помилці (`Read-Host`). Найчастіше — відсутній `.venv` або `python` не на PATH. |
| Пункт залишається після видалення репозиторію | Запусти `uninstall_context_menu.ps1` **до** видалення папки. Інакше вичисти вручну під `HKCU:\Software\Classes\SystemFileAssociations\.<ext>\shell\CreateTranscription`. |
| Конфлікт з іншими пунктами меню | Ключ `CreateTranscription` унікальний, не перетнеться з чужими, але змінити назву можна через `-MenuLabel`. |

## Quick Action на macOS

На macOS аналог контекстного меню — **Quick Actions** (Services), які встановлюються в `~/Library/Services/`. Нічого не треба підписувати, ставити Automator.app окремо або виходити з sandbox — інсталятор сам зкладає бандл і підхоплює його через Services.

### Встановлення

```bash
cd scripts/macos
chmod +x install_context_menu.sh uninstall_context_menu.sh
./install_context_menu.sh
```

Далі в Finder клацни правою кнопкою на відео-файлі → **Quick Actions / Швидкі дії** → **"Створити транскрипцію"**.

Якщо пункт не зʼявився одразу:

1. `System Settings → Privacy & Security → Extensions → Finder`
2. Увімкни галочку **"Створити транскрипцію"**

### Кастомний лейбл

```bash
MENU_LABEL="Transcribe Media" ./install_context_menu.sh
```

### Видалення

```bash
./uninstall_context_menu.sh
```

### Як це працює

Інсталятор копіює `scripts/macos/CreateTranscription.workflow/` в `~/Library/Services/CreateTranscription.workflow/` і підміняє плейсхолдер `__PROJECT_ROOT__` в `Contents/document.wflow` на абсолютний шлях до проекту. Після цього робить `pbs -flush` і `pluginkit -e use`, щоб macOS перечитав Services без релогіну.

Сам workflow — мінімальна "Run Shell Script" дія, яка:

1. Бере `.venv/bin/python`, якщо є, інакше — `python3` з `PATH`.
2. Запускає `python -m app transcribe --input "$f" --progress` з кореня проекту.
3. Пише нотифікацію "Готово" через `osascript`.

### Траблшутинг macOS

| Симптом | Причина і що робити |
| --- | --- |
| Пункту немає в контекстному меню | Перевір `System Settings → Extensions → Finder` та мітку біля "Створити транскрипцію". |
| `operation not permitted` при доступі до папок типу Desktop/Documents/Downloads | macOS TCC. При першому запуску дозволь Terminal/Automator повний доступ до диску (`Full Disk Access`) або перенеси відео в іншу папку. |
| Quick Action виконується, але нічого не відбувається | Виконай ту ж команду вручну з терміналу, щоб побачити помилку: `cd <project> && .venv/bin/python -m app transcribe --input /path/to/video.mp4 --progress`. |
| Rosetta замість arm64 | `python3 -c "import platform; print(platform.machine())"` має видати `arm64`. Якщо `x86_64` — переустанови Python нативно. |
| "Bad CPU type in executable" | Якийсь пакет у `.venv` зібрався під Rosetta. Перестворити venv на нативному Python: `rm -rf .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`. |

## MCP-сервер

Той самий пайплайн доступний через MCP-tool `transcribe_media` — Claude Desktop, Claude Code або будь-який інший MCP-клієнт може попросити транскрипцію без CLI.

### Встановлення SDK

```bash
python -m pip install "mcp>=1.0.0"
```

Без MCP SDK решта проекту (CLI + тести) працює як і раніше — SDK потрібен тільки для запуску сервера.

### Запуск

```bash
python -m mcp_server
```

Сервер говорить по stdio, тож запускається клієнтом напряму, не руками.

### Конфіг Claude Desktop

**Windows** — `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "video-transcription-extractor": {
      "command": "C:\\path\\to\\video-transcription-extractor\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server"],
      "cwd": "C:\\path\\to\\video-transcription-extractor"
    }
  }
}
```

**macOS** — `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "video-transcription-extractor": {
      "command": "/Users/you/projects/video-transcription-extractor/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "cwd": "/Users/you/projects/video-transcription-extractor"
    }
  }
}
```

**Linux** — `~/.config/Claude/claude_desktop_config.json` (та сама структура, корекуй шляхи).

> Завжди вказуй абсолютний шлях до `python` з `.venv`. Якщо покласти просто `"python"`, Claude Desktop не знайде його через різницю в PATH.

### Конфіг Claude Code (`.claude.json`)

Claude Code читає `.claude.json` у корені проекту (або глобально `~/.claude.json`):

```json
{
  "mcpServers": {
    "video-transcription-extractor": {
      "command": "${workspaceFolder}/.venv/bin/python",
      "args": ["-m", "mcp_server"]
    }
  }
}
```

На Windows заміни `bin/python` на `Scripts\\python.exe`.

### Tool `transcribe_media`

Параметри:

| Параметр | Тип | Default | Опис |
| --- | --- | --- | --- |
| `file_path` | `string` (обовʼязковий, абсолютний) | — | Шлях до медіа-файлу. Мусить існувати і мати дозволене розширення. |
| `output_dir` | `string` (абсолютний) | `null` | Куди писати `.raw.txt` / `.clean.md` / `.summary.md`. За замовч. — поруч з `file_path`. |
| `summary_mode` | `"ollama"` \| `"extractive"` \| `"none"` | `"ollama"` | Режим сумаризатора. `"none"` пропускає `.summary.md`. |
| `clean_mode` | `"raw"` \| `"rule-based"` \| `"llm"` | `"rule-based"` | Cleanup-режим для `.clean.md`. Див. [Cleanup-режими](#cleanup-режими). |
| `write_raw` | `boolean` | `true` | Чи писати `.raw.txt`. |
| `write_clean` | `boolean` | `true` | Чи писати `.clean.md`. |
| `chapters` | `boolean` | `true` | Включати розділи в `.clean.md`. |
| `language` | `string` (ISO 639-1) | `null` (auto) | Мова. Наприклад `"uk"`, `"en"`. |
| `profile` | `"fast"` \| `"best"` | `"best"` | Whisper-профіль. |
| `model` | `string` | `null` | Явна Whisper-модель (`"large-v3"`, `"small"`, ...). |
| `title_style` | `"keywords"` \| `"snippet"` | `"keywords"` | Стратегія базових заголовків. |
| `timeout_sec` | `integer` | `0` | Hard-deadline (`0` = без таймауту). |

Успішна відповідь:

```json
{
  "ok": true,
  "result": {
    "transcript_path":     "/abs/path/to/1.clean.md",
    "raw_transcript_path": "/abs/path/to/1.raw.txt",
    "summary_path":        "/abs/path/to/1.summary.md",
    "duration_seconds": 623.4,
    "chapter_count": 12,
    "utterance_count": 187
  }
}
```

- `transcript_path` вказує на почищений `.clean.md` (breaking change у порівнянні з попередньою версією, де він показував на `.transcript.txt`).
- `raw_transcript_path` — новий. Вказує на верба́тимний `.raw.txt`. Буде `null`, якщо переданий `write_raw: false`.
- `transcript_path` буде `null`, якщо `write_clean: false`.
- `summary_path` буде `null`, якщо `summary_mode: "none"` або `write_summary: false`.

Помилки повертаються структурованим payload-ом:

```json
{
  "ok": false,
  "error": {
    "code": "unsupported_format",
    "message": "file_path extension '.txt' is not supported"
  }
}
```

Коди помилок:

| Код | Що означає |
| --- | --- |
| `invalid_argument` | Некоректні параметри (не-абсолютний шлях, невідомий `summary_mode`, тощо). |
| `not_found` | `file_path` не існує або не є regular file. |
| `unsupported_format` | Розширення не в allow-list. |
| `pipeline_error` | Whisper/ffmpeg/Ollama кинули виняток — `message` містить оригінальний текст. |

### Приклад запиту

```json
{
  "name": "transcribe_media",
  "arguments": {
    "file_path": "/Users/you/Movies/talk.mp4",
    "summary_mode": "ollama",
    "language": "uk",
    "profile": "best",
    "timeout_sec": 1200
  }
}
```

Під час виконання сервер надсилає `notifications/progress` з процентом (`0–100`) та `notifications/message` з breadcrumb-логами (`Extracting audio…`, `Transcribing…`, `Summarizing…`) — Claude Desktop показує їх у UI.

## Міграція з попередньої схеми

До цього релізу пайплайн писав один файл `<name>.transcript.txt` + опційний `<name>.summary.md`. Тепер замість `.transcript.txt` виходять **два** файли — `<name>.raw.txt` (верба́тим) і `<name>.clean.md` (почищений). `.summary.md` залишився, але його внутрішня структура змінена (чотири секції замість одного bullet-переказу).

Що це означає для користувачів:

- **Старі `.transcript.txt` файли** не видаляються автоматично і не мігруються. Тримай їх поряд або видали вручну — новий пайплайн їх не чіпає і не читає.
- **CLI-флаги `--summary-file` / `--no-summary-file`** залишились. Додались `--raw-file` / `--no-raw-file` та `--clean-file` / `--no-clean-file`.
- **CLI-флаг `--clean-mode`** новий; за замовчуванням `rule-based`.
- **MCP-поле `transcript_path`** тепер вказує на `.clean.md`, а не `.transcript.txt`. Клієнти, які відкривали файл за цим шляхом, отримають markdown з `## ` розділами замість плоского `.txt`. Додано нове поле `raw_transcript_path` для тих, кому потрібен саме верба́тимний текст.
- **Формат `.summary.md`** став структурованим: `## Overview`, `## Ключові факти`, `## Наміри / дії`, `## По чаптерах`. Якщо ти парсив попередній free-form формат — доведеться оновити парсер.

Автоматичного інструменту для регенерації старих транскриптів немає — просто перезапусти пайплайн на відео, і поруч з ним зʼявляться три нові файли.

## Тести

```bash
python -m unittest discover -s tests -v
```

- `test_pipeline.py` — core `run_pipeline` логіка (три-файловий вихід, toggle-прапорці, cleanup-режими).
- `test_mcp_adapter.py` — валідація параметрів + форма відповіді MCP.
- `test_cleanup.py` — rule-based cleanup (dedup, rolling-overlap, join, filler-collapse) + subsequence-валідатор.
- `test_raw_writer.py`, `test_writer.py`, `test_summarizer.py` — writers і сумаризатор (включно із summary-writer-ом чотирьох секцій).
- `test_cli.py`, `test_e2e_1mp4.py` — CLI-парсер і наскрізний sample-файл.

E2E на `video-sample/1.mp4` пропускається, якщо файла немає.

## Pre-push checklist

- `python -m app transcribe --help` відкривається без трасбеків.
- `python -m unittest discover -s tests` — все зелене.
- Жодних секретів у трекнутих файлах (`.env`, токени, ключі).
- Немає `__pycache__` / `build/` / `.venv` в `git status`.
- Оновлені приклади в README, якщо змінив імена прапорців.
