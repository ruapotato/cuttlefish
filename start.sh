#!/usr/bin/env bash
# Starts cuttlefish with every worker that this machine can run.
#
# Usage:
#   ./start.sh                  # base install, ASR worker on if [asr] is installed
#   ./start.sh --asr            # also install [asr] (~2 GB) before starting
#   ./start.sh --no-asr-worker  # skip the ASR worker even if available
#   ./start.sh --host 0.0.0.0 --port 9000   # forward flags to cuttlefish serve
set -euo pipefail
cd "$(dirname "$0")"

# --- Preflight ---------------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is not installed." >&2
    echo "  install with: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "warning: ffmpeg not found on PATH." >&2
    echo "  encoding, thumbnail generation, and ASR will fail without it." >&2
    echo "  install:  sudo apt install ffmpeg   |   brew install ffmpeg" >&2
fi

# --- Parse our own flags, forward the rest to cuttlefish ---------------

INSTALL_ASR=false
SKIP_ASR_WORKER=false
PASSTHROUGH=()
for arg in "$@"; do
    case "$arg" in
        --asr)
            INSTALL_ASR=true
            ;;
        --no-asr-worker)
            SKIP_ASR_WORKER=true
            ;;
        *)
            PASSTHROUGH+=("$arg")
            ;;
    esac
done

# --- Sync deps ----------------------------------------------------------

if $INSTALL_ASR; then
    echo ">>> Syncing dependencies (with [asr] — first run may take a few minutes)..."
    uv sync --extra asr
else
    echo ">>> Syncing dependencies..."
    uv sync
fi

# --- Decide which worker flags to pass ---------------------------------

WORKER_FLAGS=("--with-worker")
if ! $SKIP_ASR_WORKER && uv run python -c "import nemo.collections.asr" >/dev/null 2>&1; then
    WORKER_FLAGS+=("--with-asr-worker")
    echo ">>> ASR dependencies detected — starting with --with-asr-worker."
else
    if ! $SKIP_ASR_WORKER; then
        echo ">>> ASR dependencies not installed; subtitle generation disabled."
        echo "    Re-run with ./start.sh --asr to install them."
    fi
fi

# --- Go ----------------------------------------------------------------

echo ">>> uv run cuttlefish serve ${WORKER_FLAGS[*]} ${PASSTHROUGH[*]}"
exec uv run cuttlefish serve "${WORKER_FLAGS[@]}" "${PASSTHROUGH[@]}"
