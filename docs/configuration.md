# Configuration

Cuttlefish reads either CLI flags, environment variables, or a TOML config
file. CLI flags win, then config, then env defaults.

## Config file

```toml
# /etc/cuttlefish/cuttlefish.toml

# Path to the SQLite DB (default: $XDG_DATA_HOME/cuttlefish/cuttlefish.db)
db = "/var/lib/cuttlefish/cuttlefish.db"

[server]
host = "0.0.0.0"
port = 8000
with_worker = true          # run the encode worker in a background thread
ffmpeg = "ffmpeg"           # path to ffmpeg
# (the ASR worker now starts automatically — no flag needed)

[[library]]
name = "Media"
root = "/data/Media"
```

A library is just a folder. Cuttlefish auto-detects what each subfolder
contains — you can have movies, TV shows, and audiobooks all in the same
library, in whatever organization you already use.

Run with:

```bash
uv run cuttlefish serve --config /etc/cuttlefish/cuttlefish.toml
```

`serve --config` does the following before starting uvicorn:

1. Reads the file, validates it (each library entry must have name + root).
2. Initializes the schema if the DB is fresh.
3. Upserts each `[[library]]` entry into the `libraries` table — adds new
   ones, updates existing entries (matched by `name`).
4. Applies `[server]` defaults that the CLI didn't override.
5. Starts the embedded encode worker if `with_worker = true`.
6. Starts the embedded ASR worker (always — it's part of the core
   system).

## Environment variables

External-API credentials are environment-only (don't put secrets in the
config file you might commit somewhere):

| Variable | Used for |
|---|---|
| `TMDB_API_KEY` | TMDb metadata + posters |
| `OPENSUBTITLES_API_KEY` | OpenSubtitles search |
| `OPENSUBTITLES_USERNAME`, `OPENSUBTITLES_PASSWORD` | OpenSubtitles download |

## Web-only setup (no CLI needed)

You can also skip the config file entirely:

1. `uv run cuttlefish serve --with-worker`
2. Open `/register` and create the first user (becomes admin).
3. Open `/admin/libraries` and add libraries through the UI.
4. Click "Scan all libraries" — done.
