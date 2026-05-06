#!/usr/bin/env bash
# Starts cuttlefish with every worker that this machine can run.
#
# Usage:
#   ./start.sh                       # base install, ASR worker on if [asr] is installed
#   ./start.sh --asr                 # also install [asr] (~2 GB), then auto-detect
#                                    # your CUDA version and install the matching torch
#                                    # wheel so ASR runs on GPU
#   ./start.sh --asr --asr-cuda 12.4 # force a specific CUDA version for the torch
#                                    # wheel (e.g. when nvidia-smi isn't available)
#   ./start.sh --no-asr-worker       # skip the ASR worker even if available
#   ./start.sh --asr-cpu             # run ASR on CPU only (slow — for testing)
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
ASR_CUDA_OVERRIDE=""
PASSTHROUGH=()
i=0
args=("$@")
while [[ $i -lt ${#args[@]} ]]; do
    arg=${args[$i]}
    case "$arg" in
        --asr)
            INSTALL_ASR=true
            ;;
        --no-asr-worker)
            SKIP_ASR_WORKER=true
            ;;
        --asr-cpu)
            export CUTTLEFISH_ASR_CPU=1
            ;;
        --asr-cuda)
            i=$((i + 1))
            ASR_CUDA_OVERRIDE="${args[$i]:-}"
            ;;
        --asr-cuda=*)
            ASR_CUDA_OVERRIDE="${arg#--asr-cuda=}"
            ;;
        *)
            PASSTHROUGH+=("$arg")
            ;;
    esac
    i=$((i + 1))
done

# --- Sync deps ----------------------------------------------------------

if $INSTALL_ASR; then
    echo ">>> Syncing dependencies (with [asr] — first run may take a few minutes)..."
    uv sync --extra asr
else
    echo ">>> Syncing dependencies..."
    uv sync
fi

# --- Match torch to the local CUDA driver if one is present -------------
# Parakeet on CPU is slow enough to be unusable, so we go to real lengths
# to ensure the GPU path works. The default 'torch' wheel from PyPI is
# compiled for whichever CUDA version PyTorch ships by default (12.6+
# at time of writing). If your driver is older you'll get a
# 'NVIDIA driver too old' error at model load. Fix: install a torch
# wheel built for your driver's CUDA.
swap_torch_for_cuda() {
    local cuda_ver="" tag="" nvsmi_out=""
    echo ">>> Probing for a usable CUDA driver..."
    if [[ -n "$ASR_CUDA_OVERRIDE" ]]; then
        cuda_ver="$ASR_CUDA_OVERRIDE"
        echo ">>> Using --asr-cuda override: $cuda_ver"
    elif command -v nvidia-smi >/dev/null 2>&1; then
        nvsmi_out=$(nvidia-smi 2>/dev/null || true)
        if [[ "$nvsmi_out" =~ CUDA\ Version:[[:space:]]+([0-9]+\.[0-9]+) ]]; then
            cuda_ver="${BASH_REMATCH[1]}"
            echo ">>> nvidia-smi reports CUDA $cuda_ver"
        else
            echo ">>> nvidia-smi found but no 'CUDA Version: X.Y' line in its output"
            echo "    (pass --asr-cuda 12.4 or similar to skip detection)"
        fi
    else
        echo ">>> nvidia-smi not on PATH — assuming no GPU; ASR will use CPU."
    fi
    if [[ -z "$cuda_ver" ]]; then
        return 0
    fi
    case "$cuda_ver" in
        11.[0-8])     tag="cu118" ;;
        12.[0-3])     tag="cu121" ;;
        12.[4-5])     tag="cu124" ;;
        *)
            echo ">>> CUDA $cuda_ver: default torch wheel should work; no swap needed."
            return 0
            ;;
    esac

    # Skip the swap if torch is already on the right wheel.
    local cur_torch=""
    cur_torch=$(uv run --no-sync python -c "import torch; print(torch.__version__)" 2>/dev/null || true)
    if [[ "$cur_torch" == *"+${tag}" ]]; then
        echo ">>> torch is already built for $tag ($cur_torch); no swap needed."
        return 0
    fi

    echo ">>> Swapping torch (currently ${cur_torch:-default}) to a $tag wheel..."
    # The default torch from PyPI brings in the latest nvidia-cuda-* packages
    # (CUDA 13 at time of writing). Those don't get auto-removed when we
    # swap torch, and they conflict with what cu124 torch wants. Wipe them
    # explicitly so the cu124 install ends up with a coherent CUDA-12 stack.
    uv pip uninstall \
        nvidia-cublas nvidia-cuda-cupti nvidia-cuda-nvrtc nvidia-cuda-runtime \
        nvidia-cufft nvidia-cufile nvidia-curand nvidia-cusolver \
        nvidia-cusparse nvidia-nvjitlink nvidia-nvtx triton \
        nvidia-cublas-cu13 nvidia-cuda-cupti-cu13 nvidia-cuda-nvrtc-cu13 \
        nvidia-cudnn-cu13 nvidia-cufft-cu13 nvidia-curand-cu13 \
        nvidia-cusolver-cu13 nvidia-cusparse-cu13 nvidia-cusparselt-cu13 \
        nvidia-nccl-cu13 nvidia-nvjitlink-cu13 nvidia-nvshmem-cu13 \
        nvidia-nvtx-cu13 \
        >/dev/null 2>&1 || true

    # Install ONLY from PyTorch's wheel index — no --extra-index-url, which
    # was letting uv pick a newer torch off PyPI built for CUDA 13.
    echo ">>> Installing torch from https://download.pytorch.org/whl/$tag ..."
    if uv pip install --reinstall-package torch torch \
            --index-url "https://download.pytorch.org/whl/$tag"; then
        local new_torch
        new_torch=$(uv run --no-sync python -c "import torch; print(torch.__version__)" 2>/dev/null || true)
        echo ">>> torch installed: $new_torch"
        if [[ "$new_torch" == *"+${tag}" ]]; then
            echo ">>> ASR should run on GPU."
        else
            echo ">>> WARNING: torch reinstalled but not tagged $tag." >&2
            echo "    Likely the $tag index has no compatible wheel." >&2
            echo "    Falling back to CPU mode." >&2
            export CUTTLEFISH_ASR_CPU=1
        fi
    else
        echo ">>> WARNING: torch swap failed; falling back to CPU mode." >&2
        export CUTTLEFISH_ASR_CPU=1
    fi
    return 0
}

if $INSTALL_ASR; then
    swap_torch_for_cuda
fi

# --- Decide which worker flags to pass ---------------------------------

WORKER_FLAGS=("--with-worker")
if ! $SKIP_ASR_WORKER && uv run --no-sync python -c "import nemo.collections.asr" >/dev/null 2>&1; then
    WORKER_FLAGS+=("--with-asr-worker")
    echo ">>> ASR dependencies detected — starting with --with-asr-worker."
else
    if ! $SKIP_ASR_WORKER; then
        echo ">>> ASR dependencies not installed; subtitle generation disabled."
        echo "    Re-run with ./start.sh --asr to install them."
    fi
fi

# --- Go ----------------------------------------------------------------

# --no-sync: 'uv run' would otherwise reconcile the venv against pyproject
# + uv.lock, which would undo the manual torch wheel swap above.
echo ">>> uv run --no-sync cuttlefish serve ${WORKER_FLAGS[*]} ${PASSTHROUGH[*]}"
exec uv run --no-sync cuttlefish serve "${WORKER_FLAGS[@]}" "${PASSTHROUGH[@]}"
