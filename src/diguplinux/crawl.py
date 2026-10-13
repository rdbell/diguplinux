"""Walk only requested roots, honoring git ignores and skipping secrets/symlinks."""

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .ignore import matches, validate_patterns
from .runtime import DigupError

TEXT = {".txt", ".md", ".markdown", ".rst", ".csv", ".log", ".html", ".htm"}
DOCUMENT = {".docx", ".odt"}
IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif"}
AUDIO = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".aac", ".aiff"}
VIDEO = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
CODE = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".swift",
    ".rb",
    ".php",
    ".sh",
    ".bash",
    ".zsh",
    ".sql",
    ".lua",
    ".vue",
    ".svelte",
    ".css",
    ".scss",
    ".ex",
    ".exs",
    ".cs",
    ".fs",
    ".r",
    ".jl",
    ".ipynb",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
}
SKIP_DIRS = {
    "node_modules",
    "vendor",
    "target",
    "build",
    "dist",
    "__pycache__",
    "venv",
    "env",
    "cache",
    "caches",
    "lost+found",
}
SECRET_NAMES = {
    "credentials",
    "credentials.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "passwords.txt",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "authorized_keys",
    "known_hosts",
}
SECRET_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".keystore", ".kdbx", ".crt", ".cer"}
PROJECT_MARKERS = {".git", "package.json", "Cargo.toml", "pyproject.toml", "go.mod"}


def within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def secret(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.startswith(".")
        or name in SECRET_NAMES
        or name.startswith(("secrets.", "credentials.", "id_rsa"))
        or path.suffix.lower() in SECRET_EXTENSIONS
    )


def kind_of(path: Path, code: bool) -> str | None:
    if secret(path):
        return None
    ext = path.suffix.lower()
    if code:
        return (
            "code"
            if ext in CODE | TEXT or path.name in {"Makefile", "Dockerfile", "CMakeLists.txt"}
            else None
        )
    for kind, extensions in (("doc", TEXT | DOCUMENT), ("image", IMAGE), ("audio", AUDIO), ("video", VIDEO)):
        if ext in extensions:
            return kind
    return "pdf" if ext == ".pdf" else None


@dataclass
class Scan:
    files: dict[Path, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    skipped: int = 0


def scan(
    roots: list[Path],
    code: bool = False,
    excludes: list[Path] = (),
    exclude_ext: set[str] = frozenset(),
    ignore_patterns=(),
) -> Scan:
    patterns = validate_patterns(ignore_patterns)
    result = Scan()
    git_files: dict[Path, set[str] | None] = {}

    def git_root(path):
        for parent in [path, *path.parents]:
            if (parent / ".git").exists():
                return parent
        return None

    def allowed_by_git(path, repo):
        if repo not in git_files:
            try:
                output = subprocess.check_output(
                    ["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                    timeout=30,
                    stderr=subprocess.PIPE,
                )
                git_files[repo] = set(os.fsdecode(name) for name in output.split(b"\0") if name)
            except (OSError, subprocess.SubprocessError) as exc:
                result.errors.append(f"Cannot read git exclusions for {repo}: {exc}")
                git_files[repo] = None
        return git_files[repo] is not None and str(path.relative_to(repo)) in git_files[repo]

    def error(exc):
        result.errors.append(str(exc))

    def excluded(path):
        return any(within(path, ex) for ex in excludes)

    for root in roots:
        if root.is_symlink() or not root.is_dir():
            raise DigupError(f"Index root must be an existing directory, not a symlink: {root}")

        def ignored(path, directory=False):
            return any(matches(path.relative_to(root), p, directory) for p in patterns)

        initial_repo = git_root(root)
        project_dirs = set()
        for parent in [root, *root.parents]:
            if any((parent / marker).exists() for marker in PROJECT_MARKERS):
                project_dirs.add(parent)
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=error):
            folder = Path(directory)
            if excluded(folder):
                dirs[:] = []
                continue
            if PROJECT_MARKERS.intersection(files + dirs):
                project_dirs.add(folder)
            dirs[:] = sorted(
                d
                for d in dirs
                if not d.startswith(".")
                and d.lower() not in SKIP_DIRS
                and not (folder / d).is_symlink()
                and not excluded(folder / d)
                and not ignored(folder / d, directory=True)
            )
            repo = initial_repo or git_root(folder)
            project = any(within(folder, p) for p in project_dirs)
            for name in sorted(files):
                path = folder / name
                kind = kind_of(path, code)
                if (
                    kind is None
                    or path.is_symlink()
                    or excluded(path)
                    or path.suffix.lower() in exclude_ext
                    or ignored(path)
                    or (not code and project and kind != "image")
                ):
                    result.skipped += 1
                    continue
                if repo and not allowed_by_git(path, repo):
                    result.skipped += 1
                    continue
                result.files[path] = kind
    return result
