import os
from pathlib import Path

MODEL_REPOSITORY = "ggml-org/embeddinggemma-2-GGUF"
MODEL_REVISION = "bfcd298762cc34d0357ece5ebdd31791a3a374d8"
LLAMA_REVISION = "4d756bc72bf00a4aacf410ae15a2d315f3db400d"  # b11461
MODEL_FILES = {
    "embeddinggemma-2-Q8_0.gguf": (
        309_855_456,
        "2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135",
    ),
    "mmproj-embeddinggemma-2-Q8_0.gguf": (
        554_821_024,
        "c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f",
    ),
}
# Bump when prompts, extraction, preprocessing or inference changes.
PIPELINE_VERSION = "eg2-q8-b11461-linux-v1"


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "diguplinux"


def state_dir() -> Path:
    configured = os.environ.get("XDG_STATE_HOME", "")
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".local/state"
    return base / "diguplinux"


def config_path() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".config"
    return base / "diguplinux/config.toml"


def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "diguplinux"


def model_dir() -> Path:
    return Path(os.environ.get("DIGUP_MODELS", str(data_dir() / "models"))).expanduser()


def worker_path(device: str = "cpu") -> Path:
    if override := os.environ.get("DIGUP_WORKER"):
        return Path(override).expanduser()
    folder = data_dir() / "bin"
    if device == "gpu":
        for backend in ("cuda", "vulkan"):
            candidate = folder / f"digup-worker-{backend}"
            if candidate.is_file():
                return candidate
        return folder / "digup-worker-vulkan"
    return folder / "digup-worker"
