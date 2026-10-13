import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diguplinux.cli import parser
from diguplinux.config import worker_path
from diguplinux.runtime import DigupError, Worker, device_mode


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "embeddinggemma-2-Q8_0.gguf").write_bytes(b"test model placeholder")
        self.executable = self.root / "worker"

    def fake_worker(self, gpu):
        self.executable.write_text(
            "#!/usr/bin/python3\nimport json, sys\n"
            f"gpu = {gpu!r}\n"
            "print(json.dumps({'ready': True, 'gpu': gpu} | {'args': sys.argv}), flush=True)\n"
            "for line in sys.stdin:\n"
            "    print(json.dumps({'vector': [1.] + [0.] * 767}), flush=True)\n"
        )
        self.executable.chmod(0o700)

    def start(self, device=None):
        with (
            patch("diguplinux.runtime.worker_path", return_value=self.executable),
            patch("diguplinux.runtime.model_dir", return_value=self.root),
        ):
            return Worker(device=device)

    def test_gpu_request_rejects_silent_cpu_fallback(self):
        self.fake_worker(gpu=False)
        with self.assertRaisesRegex(DigupError, "refusing CPU fallback"):
            self.start("gpu")

    def test_gpu_mode_sets_offload_and_accepts_hardware_confirmation(self):
        self.fake_worker(gpu=True)
        worker = self.start("gpu")
        try:
            args = worker.info["args"]
            self.assertEqual(args[args.index("--gpu-layers") + 1], "999")
            self.assertEqual(len(worker.embed("text", "search query")), 768)
        finally:
            worker.close()

    def test_explicit_cpu_mode_overrides_inherited_gpu_layers(self):
        self.fake_worker(gpu=False)
        with patch.dict(os.environ, {"DIGUP_GPU_LAYERS": "999"}):
            worker = self.start("cpu")
        try:
            args = worker.info["args"]
            self.assertEqual(args[args.index("--gpu-layers") + 1], "0")
        finally:
            worker.close()

    def test_legacy_gpu_layers_environment_selects_gpu(self):
        self.fake_worker(gpu=True)
        with patch.dict(os.environ, {"DIGUP_GPU_LAYERS": "5"}):
            os.environ.pop("DIGUP_DEVICE", None)
            worker = self.start()
        try:
            args = worker.info["args"]
            self.assertEqual(args[args.index("--gpu-layers") + 1], "5")
        finally:
            worker.close()

    def test_gpu_mode_requires_nonzero_layers(self):
        self.fake_worker(gpu=True)
        with patch.dict(os.environ, {"DIGUP_GPU_LAYERS": "0"}):
            with self.assertRaisesRegex(DigupError, "nonzero"):
                self.start("gpu")

    def test_device_context_restores_environment_even_after_failure(self):
        with patch.dict(os.environ, {"DIGUP_DEVICE": "cpu"}):
            with self.assertRaises(RuntimeError):
                with device_mode("gpu"):
                    self.assertEqual(os.environ["DIGUP_DEVICE"], "gpu")
                    raise RuntimeError("test")
            self.assertEqual(os.environ["DIGUP_DEVICE"], "cpu")

    def test_backend_binaries_are_separate_and_override_still_works(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root)}):
            os.environ.pop("DIGUP_WORKER", None)
            cpu = worker_path()
            gpu = worker_path("gpu")
            self.assertEqual(cpu.name, "digup-worker")
            self.assertEqual(gpu.name, "digup-worker-vulkan")
            gpu.parent.mkdir(parents=True)
            gpu.touch()
            self.assertEqual(worker_path("gpu"), gpu)
            with patch.dict(os.environ, {"DIGUP_WORKER": str(self.executable)}):
                self.assertEqual(worker_path("cpu"), self.executable)
                self.assertEqual(worker_path("gpu"), self.executable)

    def test_cli_device_flag_before_or_after_command(self):
        for args in (["--device", "gpu", "index"], ["index", "--device", "gpu"]):
            self.assertEqual(parser().parse_args(args).device, "gpu")


if __name__ == "__main__":
    unittest.main()
