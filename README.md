# Cuttlefish

A self-hosted media server for movies, TV, and audiobooks. Streams to a phone,
laptop, or your smart TV's built-in browser. Pre-encodes your library to a
single compatible format instead of transcoding on the fly.

> **Status: works end-to-end.** Scan, encode, watch with captions, resume,
> cast between devices. Tested with 218 unit + integration tests against
> real ffmpeg. See "What's not implemented yet" at the bottom for the
> short list of gaps.

## What you get

- **One-shot pre-encoding** to H.264/AAC/MP4 1080p — every modern device
  plays directly, no on-the-fly transcoding.
- **Scanner** that auto-detects movies vs TV shows vs audiobooks from
  folder structure, picks up sidecar posters and subtitles, probes
  duration with ffprobe.
- **Web UI** that runs on smart TV browsers (no JS framework, server-rendered
  HTML), with poster grid, search, "Continue Watching", browser-native
  captions via WebVTT.
- **User accounts** with per-user resume across movies, TV episodes, and
  audiobook chapters.
- **Multi-device casting** — log into the same account on the TV and your
  phone, then control the TV from your phone (play / pause / seek).
- **Admin web UI** for everything: add libraries, scan, queue encodes,
  delete originals after re-encode, manage users, clean up cruft files.
- **TLS** via Let's Encrypt + DNS-01 (any [certbot DNS plugin](https://eff-certbot.readthedocs.io/en/stable/using.html#dns-plugins)).

## Try it (5 minutes)

### 1. Install [uv](https://docs.astral.sh/uv/) — one line

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

(uv manages the Python interpreter, the venv, and dependencies in one tool.
Already installed? Skip this.)

You also need **ffmpeg** on your `$PATH`:

```bash
sudo apt install ffmpeg          # Debian/Ubuntu
brew install ffmpeg              # macOS
sudo zypper install ffmpeg       # openSUSE
```

### 2. Get the code

```bash
git clone https://github.com/ruapotato/cuttlefish.git
cd cuttlefish
uv sync
```

`uv sync` reads `.python-version` and `pyproject.toml`, downloads the right
Python interpreter (3.11) if you don't have it, creates `.venv/`, and
installs every dependency. You don't need to activate anything.

### 3. Start the server

```bash
uv run cuttlefish serve --with-worker
```

The first time you run this, cuttlefish creates an admin account for you
and prints the password right at the top of the output:

```
======================================================================
  CUTTLEFISH FIRST-TIME SETUP

  An admin user has been created for you. Save this password —
  it will not be shown again. You can change it after logging
  in at /account.

    URL:      http://localhost:8000/login
    Username: admin
    Password: oTcFmfx0W7fkhGxItPvFfw
======================================================================

started embedded encode worker (thread=encode-worker)
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

Copy that password somewhere safe — you'll change it in step 5. `--with-worker`
runs the ffmpeg encode worker in a background thread inside the same
process, so a single command is enough. Want it on your LAN? Add
`--host 0.0.0.0`.

### 4. Log in and add a library

Open the URL from the banner and log in with `admin` + the printed
password. Once you're in, click **Admin → Libraries** in the header
(or open `/admin/libraries`).

Fill in the form:

| field | example |
|---|---|
| Name | `Media` |
| Root path | `/data/Media` (or wherever your files actually live) |

Click **Add**. Then click **Scan all libraries**. The page reloads and
your library is populated.

**A library is just a folder.** Cuttlefish figures out what each subfolder
contains by looking at it:

| You give it… | It treats it as… |
|---|---|
| A loose `Movie.mp4` at the library root | a movie |
| A folder containing one or more video files (e.g. `Movie/Movie.mp4`) | a movie |
| A folder containing audio files (e.g. `Book/01.mp3`) | an audiobook |
| A folder containing other folders (e.g. `Show/Season 01/...`) | a TV show — subfolders are seasons |
| A folder of folders that contain audio | an audiobook series — recurses |

So one library can hold movies and TV shows and audiobooks side by side.
You don't have to organize them into separate top-level folders unless
you want to.

### 5. Change the auto-generated password

Click your username in the top-right (or open `/account`). Set a
password you can actually remember.

While you're there: open **Admin → Users** to add accounts for family
members. Each gets their own per-user resume positions and casting state.

### 6. Watch something

Go to `/` — you'll see all of your media merged into one page,
organized into **Movies / TV Shows / Audiobooks** sections. Every
library you've added contributes to those sections. Click any title and
it plays in the browser. Captions appear automatically if a `.srt`
lives next to the video. Posters show up automatically if a sibling
`.jpg` (or `poster.jpg` in a folder) exists; for any video that has
neither, cuttlefish extracts a frame ~5 minutes in and uses that.

That's it. **Everything else below is optional**, and everything from
this point on is doable from the web UI — you don't need to drop back to
the terminal again.

---

## CLI shortcuts (for scripts and automation)

The web UI does everything; you don't need any of these for normal use.
They exist for backup scripts, cron jobs, declarative config, and
"I want to write a one-liner" moments:

```bash
uv run cuttlefish list-libraries
uv run cuttlefish list-media
uv run cuttlefish scan                  # rescan all
uv run cuttlefish scan Media            # one library by name
uv run cuttlefish add-library Media /data/Media
uv run cuttlefish encode-now <id>       # synchronous encode of one item
uv run cuttlefish encode-worker         # standalone worker (alt. to --with-worker)
```

## Re-encoding (the headline feature)

Cuttlefish's bet is "encode once, stream forever". Instead of transcoding
on the fly when grandma starts a movie, you re-encode each title once into
a clean Title/Title.mp4 layout that every modern device plays directly.

### From the web admin

1. **Admin → Encode media** lists everything not yet encoded.
2. Click **Enqueue encode** next to any item — it goes onto the worker's
   queue and the embedded worker picks it up.
3. **Admin → Jobs** shows the queue with status colors (queued / running
   / done / failed).
4. When done, **Admin → Cleanup originals** lists the originals that now
   have an encoded version on disk. Each row has its own **Delete original**
   button that asks for browser confirmation. Originals are *never*
   automatically deleted — this is always a manual step.

### Or one-shot from the CLI

```bash
uv run cuttlefish list-media           # find the id of the title
uv run cuttlefish encode-now 5         # encode it synchronously
```

Output for a 49 MB Blender short looks like:

```
encoded /data/Movies/Coffee Run/Coffee Run.mp4 (53506105 bytes)
```

The original `Coffee Run-PVGeM40dABA.mkv` stays on disk until you delete
it via the admin UI.

## Optional: real metadata + subtitles + ASR

Cuttlefish works great without any of these — it'll use whatever sidecar
files (`.srt`, `.jpg`) are next to your media. Plug these in to fetch
the rest from the internet.

### TMDb posters and titles

Get a free API key at <https://www.themoviedb.org/settings/api>, then:

```bash
export TMDB_API_KEY=your_key_here
uv run cuttlefish serve --with-worker
```

In the admin, hit `POST /api/admin/metadata/{media_id}` for any title that
has been encoded — it looks the title up on TMDb and downloads the poster
into the clean folder.

### OpenSubtitles

Get a free API key at <https://www.opensubtitles.com/en/consumers>, plus
a regular OpenSubtitles account for downloads:

```bash
export OPENSUBTITLES_API_KEY=...
export OPENSUBTITLES_USERNAME=...
export OPENSUBTITLES_PASSWORD=...
```

Then `POST /api/admin/subtitle/{media_id}` searches and downloads.

### ASR for content with no available subtitles

The fallback when neither a sidecar SRT nor OpenSubtitles has anything:
generate captions from the video's audio using
[NVIDIA Parakeet](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2).

```bash
uv sync --extra asr                          # ~2 GB of torch + nemo
uv run cuttlefish serve --with-worker --with-asr-worker
```

In the admin, `POST /api/admin/asr/{media_id}` enqueues an ASR job. The
worker writes the resulting SRT into the clean folder so the watch page
picks it up automatically.

GPU strongly recommended.

## Optional: TLS via Let's Encrypt

Use a [TOML config file](docs/configuration.md). Minimal example:

```toml
# /etc/cuttlefish/cuttlefish.toml
db = "/var/lib/cuttlefish/cuttlefish.db"

[server]
host = "0.0.0.0"
port = 443
with_worker = true

[tls]
enabled = true
domain = "media.example.com"
email = "you@example.com"
dns_provider = "cloudflare"     # any certbot-dns-* plugin
dns_credentials_file = "/etc/cuttlefish/cloudflare.ini"
cert_dir = "/etc/letsencrypt/live/media.example.com"

[[library]]
name = "Media"
root = "/data/Media"
```

Run with:

```bash
uv run cuttlefish serve --config /etc/cuttlefish/cuttlefish.toml
```

On startup cuttlefish provisions/renews the cert via certbot, configures
uvicorn with the cert + key, and runs a daily renewal-check thread. Full
details + DNS provider table in [docs/tls.md](docs/tls.md).

## Casting (control the TV from your phone)

1. Log in on the TV browser, open something to watch.
2. Log in as the same user on your phone, open `/cast`.
3. The TV is listed as a target. Tap **Pause / Play / -10s / +30s**.

Limitations of the MVP: you can't *start* playback on the TV from the
phone yet, only control whatever is already playing there. The schema
and websocket bus are in place; richer launch flow is a follow-on.
See [docs/casting.md](docs/casting.md) for design notes.

## What you can hit

### HTML pages

| URL | What |
|---|---|
| `/` | Library index, poster grid |
| `/library/{id}` | Items in one library |
| `/show/{id}` | Episodes grouped by season |
| `/book/{id}` | Audiobook chapter playlist with auto-advance |
| `/watch/{id}` | Movie player (auto-redirects for shows/books) |
| `/watch/episode/{id}` | TV episode player |
| `/search?q=...` | Search across titles |
| `/continue-watching` | What you've started, with mark-watched + reset |
| `/cast` | Multi-device controller |
| `/login`, `/register` | Auth |
| `/admin`, `/admin/{libraries,users,encode,jobs,cleanup,cruft}` | Admin |

### JSON API (a selection — full list at `/api/docs`)

```
GET    /api/libraries
GET    /api/media[?library=&kind=]
GET    /api/media/{id}
GET    /stream/{id}                # range-aware
GET    /stream/episode/{id}
GET    /stream/track/{id}
GET    /subtitle/{id}              # served as WebVTT
GET    /poster/{id}
GET    /api/search?q=
GET    /api/continue-watching
PUT    /api/progress/{id}          # also episode/{id} and book/{id}
POST   /api/auth/{register,login,logout}
GET    /api/me
POST   /api/admin/libraries
POST   /api/admin/scan[/{id}]
POST   /api/admin/encode/{id}
GET    /api/admin/cleanup-candidates
DELETE /api/admin/originals/{id}
GET    /health
```

## Stack

| Layer | Choice |
|---|---|
| Server | Python 3.11 + [FastAPI](https://fastapi.tiangolo.com/) |
| Database | SQLite (also serves as the job queue) |
| Encoder | ffmpeg (H.264 High@L4.0, CRF 22, AAC 128k, MP4 +faststart) |
| ASR (optional) | NVIDIA Parakeet via [NeMo](https://github.com/NVIDIA/NeMo) |
| Frontend | Plain HTML + CSS + vanilla JS, no build step |
| TLS | Let's Encrypt via certbot, DNS-01 challenge |
| Env / packaging | [uv](https://docs.astral.sh/uv/) |

## Development

```bash
uv sync --extra dev          # adds pytest + ruff
uv run pytest -q             # 218 tests, ~25 seconds
uv run ruff check .
```

CI runs the same on every push and PR — see `.github/workflows/test.yml`.

The codebase is organized as:

```
cuttlefish/
  __main__.py           CLI entry point
  db.py                 SQLite schema + idempotent migrations
  scanner.py            filesystem walker
  titles.py             filename → display title cleanup
  probe.py              ffprobe wrapper
  cruft.py              non-media file detection
  subtitles.py          SRT → WebVTT
  config.py             TOML loader
  tls.py                certbot wrapper
  auth.py               scrypt + sessions
  clients/              tmdb, opensubtitles
  workers/              encoder, asr
  server/
    app.py              FastAPI routes
    streaming.py        HTTP range request handler
    cast.py             websocket pub/sub bus
tests/                  one test file per module
docs/                   tls, casting, configuration
```

## What's not implemented yet

Real but small follow-ons:

- Auto-fetching TMDb metadata during scan (right now it's an admin click).
- TV episode encode buttons on the show page.
- "Cast → start playing X on the TV" — currently casting can only control
  playback already in progress on the target.
- Dockerfile / systemd unit (works fine without; just standard Python +
  uv + ffmpeg).

## License

[AGPL-3.0](LICENSE). If you run a modified cuttlefish as a network service,
your modifications must be available to your users.
