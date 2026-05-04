# Cuttlefish

A self-hosted media server — movies, TV, audiobooks — served from your own machine through a clean web UI you can open on a phone, a laptop, or your smart TV's built-in browser.

> **Status: work in progress.** Cuttlefish is in active design / early scaffolding. Almost nothing in the planned-features list below is implemented yet. This README documents what cuttlefish *will be* so contributors and the author have a single source of truth while it's being built.

## Why another media server

Jellyfin and Emby are great, but cuttlefish makes a different bet:

- **Pre-encode, don't transcode.** Jellyfin/Emby transcode on the fly so they can adapt to whatever the client supports. Cuttlefish instead does a one-shot batch re-encode of your library to a format every modern device can play directly (H.264 + AAC in MP4, capped at 1080p). Smaller library, simpler server, no CPU spikes when grandma starts a movie.
- **Light enough for a smart TV's built-in browser.** No JS framework. Server-rendered HTML where it makes sense, vanilla JS where it doesn't. The same UI works on a 2018 Samsung TV browser, a phone, and a desktop.
- **Light, simple, clean** is the rule. We do less than Jellyfin on purpose.

## Planned features

All of the below are *planned*, not built.

- **Library scanner** that auto-detects movies vs. TV shows vs. audiobooks from folder structure (single file = movie; subdirs of files = TV show; folder of audio = audiobook; folder of folders = audiobook series/grouping).
- **Pre-encoding pipeline** (ffmpeg). One-shot batch: downscale 4K → 1080p, normalize to H.264/AAC/MP4, never re-encode at stream time. Originals are preserved on disk until manually deleted via the admin UI — cuttlefish never auto-deletes media.
- **Cleanup** of cruft files (`downloadedfrom.txt`, etc.). Manual confirm in the admin UI; no silent deletion.
- **Metadata + posters** via [TMDb](https://www.themoviedb.org/). Renames and posters land in a clean `Title/Title.{mp4,srt,jpg}` layout per item.
- **Subtitles** via a fallback chain: existing sidecar `.srt` → [OpenSubtitles](https://www.opensubtitles.com/) API → [NVIDIA Parakeet](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2) ASR (each step opt-in).
- **TLS via Let's Encrypt** with the ACME DNS-01 challenge. Pluggable DNS provider (Cloudflare is the documented default; Route53, DigitalOcean, etc. work via certbot plugins). Handles 90-day renewal automatically.
- **User accounts** with per-user resume position across all media types.
- **Multi-device session control** — log into the same account on TV and phone; use the phone as a remote for the TV.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Server | Python 3.11 + [FastAPI](https://fastapi.tiangolo.com/) | Async streaming with HTTP range requests built in. Healthy ecosystem for ffmpeg/ML integration. |
| Database | SQLite | Single file, zero infra. Also serves as the job queue for workers — no Redis/Celery. |
| Encoding worker | Python + ffmpeg | Pulls jobs from SQLite, shells out to ffmpeg. |
| ASR worker | Python + [NeMo](https://github.com/NVIDIA/NeMo) + Parakeet-TDT | Optional. Generates SRTs for content with no available subtitles. Heavy ML deps live only in this worker. |
| Frontend | HTML + CSS + vanilla JS | No build step, no framework. Must run on smart TV browsers. |
| TLS | [certbot](https://certbot.eff.org/) DNS-01 | Pluggable DNS provider; Let's Encrypt 90-day certs. |
| Env / packaging | [uv](https://docs.astral.sh/uv/) | One-line install, manages Python interpreter + venv + lockfile in a single tool. |

## Setup

### Prerequisites

- **ffmpeg** on your `$PATH`. (`apt install ffmpeg`, `brew install ffmpeg`, etc.)
- A **domain you control** with API access at its DNS host. (Required for TLS. Cuttlefish supports any DNS provider with a [certbot DNS plugin](https://eff-certbot.readthedocs.io/en/stable/using.html#dns-plugins).)
- For the optional ASR worker only: an **NVIDIA GPU** with CUDA. CPU works but is slow.

### Install

Cuttlefish uses [uv](https://docs.astral.sh/uv/) for everything Python — interpreter, venv, dependencies. Install it once:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then clone and sync:

```bash
git clone <this-repo-url> cuttlefish
cd cuttlefish
uv sync
```

`uv sync` reads `.python-version` and `pyproject.toml`, downloads the right Python interpreter if you don't have it, creates a `.venv/` in the repo, and installs every dependency. No virtualenv activation required.

### Run

Always run cuttlefish through `uv run`. This guarantees the project's pinned Python and dependencies are used — no system-Python or wrong-venv surprises.

```bash
uv run cuttlefish --help
```

Once a server entry point exists, the typical command will be:

```bash
uv run cuttlefish serve --config /path/to/config.toml
```

> **Don't `pip install` cuttlefish or activate the venv manually.** The whole point of `uv run` is that the right environment is selected automatically every invocation. If you find yourself reaching for `source .venv/bin/activate`, that's a sign something's wrong with the setup, not a workaround.

### ASR worker (optional)

The ASR worker pulls in `torch` and `nemo_toolkit` (~2 GB on disk). Skip it unless you actually want subtitle generation for content with no available SRTs.

```bash
uv sync --extra asr
uv run cuttlefish-asr-worker
```

## Configuration

External API credentials are read from environment variables:

| Variable | Used for | Required |
|---|---|---|
| `TMDB_API_KEY` | Movie/show metadata + posters | Optional (lookups no-op without it) |
| `OPENSUBTITLES_API_KEY` | Subtitle search | Optional |
| `OPENSUBTITLES_USERNAME` / `OPENSUBTITLES_PASSWORD` | Subtitle download | Required if you want OpenSubtitles to actually download files |

The DB defaults to `$XDG_DATA_HOME/cuttlefish/cuttlefish.db` (typically
`~/.local/share/cuttlefish/cuttlefish.db`). Override with `--db PATH`.

## CLI

```bash
uv run cuttlefish init-db
uv run cuttlefish add-library open_movies /path/to/movies --kind movies
uv run cuttlefish scan
uv run cuttlefish list-media
uv run cuttlefish serve --host 0.0.0.0 --port 8000
uv run cuttlefish encode-worker        # background re-encode loop
uv run cuttlefish encode-now <id>      # one-shot synchronous encode
uv run cuttlefish asr-worker           # background subtitle generator (needs --extra asr)
```

## Deferred designs

- [TLS](docs/tls.md) — Let's Encrypt + DNS-01 via certbot, pluggable DNS provider.
- [Casting / multi-device control](docs/casting.md) — websocket-based session control.

## License

[AGPL-3.0](LICENSE). If you run a modified cuttlefish as a network service, your modifications must be available to your users.
