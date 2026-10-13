"""Explicit setup and a local, process-isolated embedding worker."""

import contextlib
import fcntl
import hashlib
import json
import math
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from .config import (
    LLAMA_REVISION,
    MODEL_FILES,
    MODEL_REPOSITORY,
    MODEL_REVISION,
    cache_dir,
    model_dir,
    worker_path,
)


class DigupError(Exception):
    pass


@contextlib.contextmanager
def lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DigupError(f"Another operation holds {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def checksum(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def download_models():
    folder = model_dir()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock(folder / ".download.lock"):
        for name, (size, digest) in MODEL_FILES.items():
            target = folder / name
            if target.is_file() and target.stat().st_size == size and checksum(target) == digest:
                print(f"Verified {name}", file=sys.stderr)
                continue
            print(f"Downloading {name} ({size / 1e6:.0f} MB)…", file=sys.stderr)
            url = f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/{name}"
            partial = target.with_suffix(".gguf.part")
            try:
                with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as output:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
                if partial.stat().st_size != size or checksum(partial) != digest:
                    raise DigupError(f"Checksum mismatch for {name}; download was not installed")
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
            print(f"Verified {name}", file=sys.stderr)


def build_runtime(backend: str = "cpu", jobs: int = 4):
    for tool in ("git", "cmake", "c++"):
        if not shutil.which(tool):
            raise DigupError(f"Missing {tool}; install git, CMake and a C++ compiler")
    folder = cache_dir() / "runtime"
    source = folder / "llama.cpp"
    build = folder / f"build-{backend}"
    native = Path(__file__).parent / "native"
    environment = os.environ.copy()
    # AppImage launch variables can make system tools resolve their executable to the host app.
    for name in ("APPIMAGE", "APPDIR", "ARGV0"):
        environment.pop(name, None)

    def run(args, cwd=None):
        subprocess.run(args, cwd=cwd, check=True, stdout=sys.stderr, env=environment)

    with lock(folder / ".build.lock"):
        if not source.exists():
            run(
                [
                    "git",
                    "-c",
                    "advice.detachedHead=false",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    "b11461",
                    "https://github.com/ggml-org/llama.cpp",
                    str(source),
                ]
            )
        revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if revision != LLAMA_REVISION:
            raise DigupError(f"Runtime source revision differs from pinned {LLAMA_REVISION}: {source}")
        patch = str(native / "no-logits.patch")
        if (
            subprocess.run(["git", "apply", "--check", patch], cwd=source, capture_output=True).returncode
            == 0
        ):
            run(["git", "apply", patch], cwd=source)
        elif (
            subprocess.run(
                ["git", "apply", "--reverse", "--check", patch], cwd=source, capture_output=True
            ).returncode
            != 0
        ):
            raise DigupError("Cannot apply the required embedding memory patch to runtime source")
        args = [
            "cmake",
            "-S",
            str(native),
            "-B",
            str(build),
            f"-DLLAMA_SOURCE={source}",
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DGGML_CUDA={'ON' if backend == 'cuda' else 'OFF'}",
            f"-DGGML_VULKAN={'ON' if backend == 'vulkan' else 'OFF'}",
        ]
        run(args)
        run(["cmake", "--build", str(build), "--target", "digup-worker", "-j", str(jobs)])
        if override := os.environ.get("DIGUP_WORKER"):
            target = Path(override).expanduser()
        else:
            target = worker_path().with_name(
                "digup-worker" if backend == "cpu" else f"digup-worker-{backend}"
            )
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.with_suffix(".new")
        shutil.copy2(build / "digup-worker", temporary)
        temporary.replace(target)
        shutil.copy2(source / "LICENSE", target.parent / "llama.cpp.LICENSE")
        print(f"Installed worker: {target}", file=sys.stderr)


@contextlib.contextmanager
def device_mode(device):
    if device is None:
        yield
        return
    previous = os.environ.get("DIGUP_DEVICE")
    os.environ["DIGUP_DEVICE"] = device
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DIGUP_DEVICE", None)
        else:
            os.environ["DIGUP_DEVICE"] = previous


class Worker:
    def __init__(self, media: bool = False, device: str | None = None):
        self.process = None
        self.closed = False
        self.buffer = bytearray()
        device = device or os.environ.get("DIGUP_DEVICE")
        if device is None:
            device = "gpu" if os.environ.get("DIGUP_GPU_LAYERS", "0") != "0" else "cpu"
        if device not in {"cpu", "gpu"}:
            raise DigupError("DIGUP_DEVICE must be cpu or gpu")
        executable = worker_path(device)
        text = model_dir() / "embeddinggemma-2-Q8_0.gguf"
        mmproj = model_dir() / "mmproj-embeddinggemma-2-Q8_0.gguf"
        if not executable.is_file():
            remedy = (
                "`digup runtime --backend vulkan` or `--backend cuda`" if device == "gpu" else "`digup setup`"
            )
            raise DigupError(f"Missing {device} worker {executable}. Run {remedy}.")
        if not text.is_file() or (media and not mmproj.is_file()):
            raise DigupError(
                "Semantic search needs model files. Run `digup models download`, or use `--keyword-only`."
            )
        gpu_layers = os.environ.get("DIGUP_GPU_LAYERS", "999") if device == "gpu" else "0"
        try:
            accelerated = int(gpu_layers) != 0
        except ValueError as exc:
            raise DigupError("DIGUP_GPU_LAYERS must be an integer") from exc
        if device == "gpu" and not accelerated:
            raise DigupError("GPU mode requires nonzero DIGUP_GPU_LAYERS")
        args = [str(executable), "--model", str(text), "--gpu-layers", gpu_layers]
        if media:
            args += ["--media", str(mmproj)]
        for env, flag in (("DIGUP_THREADS", "--threads"),):
            if env in os.environ:
                args += [flag, os.environ[env]]
        self.log = tempfile.TemporaryFile()
        self.selector = selectors.DefaultSelector()
        try:
            environment = os.environ.copy()
            # EmbeddingGemma 2 activations require float32/bfloat16 rather than float16.
            environment.setdefault("GGML_VK_DISABLE_F16", "1")
            self.process = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, env=environment
            )
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            self.info = self.read(180)
            if not isinstance(self.info, dict) or not self.info.get("ready"):
                raise DigupError("Embedding worker failed to become ready")
            if accelerated and self.info.get("gpu") is not True:
                raise DigupError(
                    "Worker did not confirm hardware GPU offload; refusing CPU fallback. "
                    "Run `digup runtime --backend vulkan` or `--backend cuda`."
                )
        except BaseException:
            self.close()
            raise

    def read(self, timeout: float = 300):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise DigupError("Embedding worker timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                self.log.seek(0)
                details = self.log.read().decode(errors="replace")[-2000:]
                raise DigupError(f"Embedding worker stopped: {details}")
            self.buffer.extend(chunk)
        line, _, rest = self.buffer.partition(b"\n")
        self.buffer = bytearray(rest)
        try:
            return json.loads(line)
        except (ValueError, UnicodeDecodeError) as exc:
            raise DigupError("Invalid response from embedding worker") from exc

    def embed(self, kind: str, value: str) -> list[float]:
        if self.closed:
            raise DigupError("Embedding worker stopped; restart indexing to retry")
        request = {"type": kind, "text" if kind == "text" else "path": value}
        try:
            self.process.stdin.write((json.dumps(request) + "\n").encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.close()
            raise DigupError("Embedding worker stopped") from exc
        try:
            response = self.read()
        except (DigupError, OSError):
            # An interrupted request cannot be reused: a late reply could belong to the wrong file.
            self.close()
            raise
        if "error" in response:
            raise DigupError(response["error"])
        vector = response.get("vector")
        if (
            not isinstance(vector, list)
            or len(vector) != 768
            or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector)
        ):
            raise DigupError("Worker returned an invalid embedding")
        norm = math.sqrt(sum(x * x for x in vector))
        if norm == 0:
            raise DigupError("Worker returned an empty embedding")
        return [x / norm for x in vector]

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process:
            if self.process.stdin:
                try:
                    self.process.stdin.close()
                except BrokenPipeError:
                    pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            if self.process.stdout:
                self.process.stdout.close()
        self.selector.close()
        self.log.close()
