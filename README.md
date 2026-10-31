# Audiobook Generator — terminal edition

Converts e-books (**EPUB, PDF, MOBI, AZW3, TXT**) into audiobooks (**M4B, MP3, Opus, FLAC, WAV**) right from the terminal — on **Windows, macOS and Linux** — using the **same conversion engine** as [Audiobook Generator](https://github.com/thenauan/audiobook-generator): chapters, cover art, Microsoft neural voices (313 voices, 73 languages), translation and synced text for audiobook players.

The interface speaks **English, Spanish, Portuguese, Russian, Arabic, French, Italian, Turkish, Hindi, Chinese and Urdu** and starts in your system language (English if your language isn't on the list).

```
  ◆ Step 6/6  🚀  Converting
  ─────────────────────────────────────────────────────────────────────────────
  ✔ Dom Casmurro               Done
  ● O Alienista                ━━━━━━━━━━━━━━━━━━━━━━━━  42.5%  Narrating · 3m 10s · 14/32 parts narrated
  ● Memórias Póstumas de Br…   ━━━━━━━━━━━━━━━━━━━━━━━━   0.0%  Queued · calculating
```

## Download

Get the latest version from **[Releases](https://github.com/thenauan/audiobook-generator-terminal/releases/latest)** (`.zip` for Windows, `.tar.gz` for macOS and Linux), or clone the repository:

```bash
git clone https://github.com/thenauan/audiobook-generator-terminal.git
cd audiobook-generator-terminal
```

## Installation

You need **Python 3.10+** and **FFmpeg** (FFmpeg joins the chapters and builds the final file, in every format).

| System | Python and FFmpeg | Python libraries |
|---|---|---|
| **Windows** | [python.org](https://www.python.org/downloads/) (check *Add Python to PATH*) and `winget install Gyan.FFmpeg` | `python -m pip install -r requirements.txt` |
| **macOS** | `brew install python python-tk ffmpeg` | `python3 -m pip install -r requirements.txt` |
| **Linux** (Debian/Ubuntu) | `sudo apt install python3 python3-pip python3-tk ffmpeg` | `python3 -m pip install -r requirements.txt` |
| **Linux** (Fedora) | `sudo dnf install python3 python3-pip python3-tkinter ffmpeg` | `python3 -m pip install -r requirements.txt` |
| **Linux** (Arch) | `sudo pacman -S python python-pip tk ffmpeg` | `python3 -m pip install -r requirements.txt` |

> On distributions that block the system `pip` (*externally-managed-environment* error), use a virtual environment:
> `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`, and run the app with `.venv` activated.

`tk` (tkinter) is optional: with it, books and the output folder are picked in a system dialog; without it (or on a server over SSH), just type or drag the files into the terminal.

## Usage

| System | How to start |
|---|---|
| **Windows** | double-click `Audiobook Generator (terminal).bat` |
| **macOS** | double-click `Audiobook Generator (terminal).command`, or run `./audiobook-cli.sh` in Terminal |
| **Linux** | `./audiobook-cli.sh` |
| **Any** | `python audiobook_cli.py` (or `python3`) |

You can also pass the books and the output folder on the command line:

```bash
./audiobook-cli.sh book.epub other.pdf -o ~/Audiobooks           # macOS / Linux
python audiobook_cli.py book.epub other.pdf -o "C:\Audiobooks"   # Windows
```

### Interface language

The language is detected from the system (Windows display language, macOS preferred languages, or `LANGUAGE` / `LC_ALL` / `LANG` on Linux). To choose it yourself:

```bash
./audiobook-cli.sh --lang es          # or: AUDIOBOOK_LANG=es ./audiobook-cli.sh
python audiobook_cli.py --lang zh     # en, es, pt, ru, ar, fr, it, tr, hi, zh, ur
```

Translations live in `dados/i18n/<language>.json` (English is the base and the fallback for any missing text).

> **macOS:** the first time, Gatekeeper may block the `.command` file downloaded from the internet. Right-click → **Open**, or run `xattr -d com.apple.quarantine *.command *.sh` in the folder.
> If you get *permission denied*, run `chmod +x audiobook-cli.sh "Audiobook Generator (terminal).command"`.

## How it works

| Step | What happens |
|---|---|
| 1. E-books | Opens the file picker (or uses the files from the command line, or files dragged into the terminal). Title, author, genre, subgenre, year, language and chapters are read automatically. |
| 2. Each book | Shows what was detected and asks whether you want to edit it. Then: **translate** (to any of the 11 interface languages), **synced text** (yes/no) and **voice** (`o 3` plays voice 3 reading an excerpt of the book itself). The choices can be reused for the other books in the same language. |
| 3. Format | M4B (default), MP3, MP3 per chapter, Opus, FLAC or WAV. |
| 4. Folder | Opens the folder picker (default: `Documents/Audiobook Generator`). |
| 5. Summary | Review everything before starting. |
| 6. Conversion | One progress bar per book, with stage, time remaining, narrated parts and real speed. |

The cover comes from the e-book itself and chapters are detected automatically — there is no cover or advanced metadata editing, to keep things straight to the point.

### Cancel and resume later

During conversion, **Ctrl+C** pauses the progress panel and asks whether to cancel:

- **n** (or Enter) — the conversion continues where it was;
- **y** — cancels and stops the engine and FFmpeg (no processes are left running);
- **Ctrl+C again** — confirms the cancellation.

Everything already narrated is kept in `.cache_audiobooks` inside the output folder: convert the same book again, with the same voice and folder, and it resumes where it stopped.

## Colors and icons

| Color | Used for | | Icon | Used for |
|---|---|---|---|---|
| cyan | steps, numbers and keys | | ◆ | step |
| white | data | | › | question |
| gray | hints | | ✔ / ✖ / ▲ | done / error / warning |
| green | success | | 📚 🎙 🌐 📝 💾 🎧 | books, voice, language, text, folder, format |
| yellow | warnings | | | |
| red | errors | | | |

On the legacy Windows console and the Linux text console (no emoji), icons fall back to simple symbols. To turn colors off: `NO_COLOR=1`.

## Requirements

- Python 3.10 or newer
- FFmpeg on `PATH`
- Internet connection (neural voices and translation are online services)

## Maintenance

The engine (`motor/`) is a copy of the app's engine. After changing the app, update it with:

```bash
python atualizar_motor.py [path-to-the-app]
```

Language and region names in every interface language (`dados/idiomas.json`) are generated from CLDR — only needed when the voice catalog changes:

```bash
python -m pip install babel
python gerar_idiomas.py
```
