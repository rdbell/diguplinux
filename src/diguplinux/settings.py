"""Folder selections live independently of the disposable search database."""

import json
import math
import os
import sqlite3
import tempfile
import tomllib
from pathlib import Path

from .config import config_path
from .ignore import validate_patterns
from .runtime import DigupError, lock

DEFAULT_OPTIONS = {
    "keyword_only": False,
    "excludes": [],
    "exclude_ext": [],
    "ignore_patterns": [],
    "language": "eng",
    "max_segments": 5000,
    "video_interval": 5,
}


def absolute_path(value):
    if not isinstance(value, str) or not value or "\0" in value or not Path(value).is_absolute():
        raise DigupError("Folder and index paths in config must be absolute strings")
    return os.path.abspath(value)


def folder_record(value):
    if not isinstance(value, dict):
        raise DigupError("Each configured folder must be a table")
    unknown = value.keys() - {"path", "collection", *DEFAULT_OPTIONS}
    if unknown:
        raise DigupError(f"Unknown folder settings: {', '.join(sorted(unknown))}")
    path = absolute_path(value.get("path"))
    collection = value.get("collection", "files")
    if not isinstance(collection, str) or collection not in {"files", "code"}:
        raise DigupError("Folder collection must be 'files' or 'code'")
    options = DEFAULT_OPTIONS | {k: v for k, v in value.items() if k in DEFAULT_OPTIONS}
    if type(options["keyword_only"]) is not bool:
        raise DigupError("keyword_only must be a boolean")
    for key in ("excludes", "exclude_ext"):
        if not isinstance(options[key], list) or any(
            not isinstance(v, str) or "\0" in v for v in options[key]
        ):
            raise DigupError(f"{key} must be an array of strings")
    options["excludes"] = [absolute_path(p) for p in options["excludes"]]
    options["ignore_patterns"] = validate_patterns(options["ignore_patterns"])
    options["exclude_ext"] = sorted({"." + ext.lower().lstrip(".") for ext in options["exclude_ext"] if ext})
    if (
        not isinstance(options["language"], str)
        or not options["language"].strip()
        or "\0" in options["language"]
    ):
        raise DigupError("language must be a nonempty string")
    if type(options["max_segments"]) is not int or options["max_segments"] <= 0:
        raise DigupError("max_segments must be a positive integer")
    interval = options["video_interval"]
    try:
        valid_interval = type(interval) in (int, float) and math.isfinite(interval) and interval > 0
    except OverflowError:
        valid_interval = False
    if not valid_interval:
        raise DigupError("video_interval must be a finite positive number")
    return {"path": path, "collection": collection, "options": options}


def legacy_folders(index_path: Path):
    """Import old settings once, using a read-only connection and never rebuilding the DB."""
    database = index_path / "index.sqlite"
    if not database.is_file():
        return []
    connection = None
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='roots'"
        ).fetchone()
        if not exists:
            return []
        result = []
        for path, collection, options in connection.execute(
            "SELECT path,collection,options FROM roots ORDER BY path,collection"
        ):
            options = json.loads(options)
            if not isinstance(options, dict):
                raise DigupError("Legacy folder options must be an object")
            result.append(folder_record(options | {"path": path, "collection": collection}))
        return result
    except (sqlite3.Error, ValueError, DigupError) as exc:
        raise DigupError(
            f"Cannot import folder settings from {database}: {exc}. "
            "Preserve the old index directory before creating a replacement."
        ) from exc
    finally:
        if connection:
            connection.close()


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    # JSON string/array syntax is also TOML syntax. Escape DEL, which TOML forbids literally.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


class Settings:
    def __init__(self, path: Path | None = None):
        # Follow an existing dotfile symlink; atomic saves update its target, preserving the symlink.
        self.path = (path or config_path()).resolve()

    def read(self):
        try:
            with self.path.open("rb") as handle:
                raw = tomllib.load(handle)
        except FileNotFoundError:
            return {"version": 1, "indexes": []}
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise DigupError(f"Invalid config {self.path}: {exc}; the file was not changed") from exc
        if raw.keys() - {"version", "indexes"}:
            raise DigupError(f"Unknown top-level settings in {self.path}")
        if type(raw.get("version")) is not int or raw["version"] != 1:
            raise DigupError(f"Unsupported config version in {self.path}")
        if not isinstance(raw.get("indexes", []), list):
            raise DigupError("Config indexes must be an array of tables")
        indexes, seen = [], set()
        for item in raw.get("indexes", []):
            if not isinstance(item, dict) or item.keys() - {"path", "folders"}:
                raise DigupError("Each config index must contain only path and folders")
            path = str(Path(absolute_path(item.get("path"))).resolve())
            if path in seen:
                raise DigupError(f"Duplicate configured index: {path}")
            seen.add(path)
            if not isinstance(item.get("folders", []), list):
                raise DigupError("Config folders must be an array of tables")
            folders, identities = [], set()
            for value in item.get("folders", []):
                folder = folder_record(value)
                identity = (folder["path"], folder["collection"])
                if identity in identities:
                    raise DigupError(
                        f"Duplicate configured folder: {folder['path']} ({folder['collection']})"
                    )
                identities.add(identity)
                folders.append(folder)
            indexes.append({"path": path, "folders": folders})
        return {"version": 1, "indexes": indexes}

    def folders(self, index_path):
        identity = str(index_path.resolve())
        for item in self.read()["indexes"]:
            if item["path"] == identity:
                return item["folders"]
        return []

    def _save(self, config):
        lines = ["version = 1", ""]
        if not config["indexes"]:
            lines += ["indexes = []", ""]
        for item in config["indexes"]:
            lines += ["[[indexes]]", f"path = {toml_value(item['path'])}"]
            if not item["folders"]:
                lines += ["folders = []"]
            lines.append("")
            for folder in item["folders"]:
                lines += [
                    "[[indexes.folders]]",
                    f"path = {toml_value(folder['path'])}",
                    f"collection = {toml_value(folder['collection'])}",
                ]
                lines += [f"{key} = {toml_value(value)}" for key, value in folder["options"].items()]
                lines.append("")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def remember(self, index_path, roots=(), code=False, options=None):
        """Merge requested folders under a lock, importing legacy settings only for unknown indexes."""
        identity = str(index_path.resolve())
        records = (
            [
                folder_record(
                    (options or {}) | {"path": str(root), "collection": "code" if code else "files"}
                )
                for root in roots
            ]
            if roots
            else []
        )
        with lock(self.path.with_name(self.path.name + ".lock")):
            config = self.read()
            entry = next((item for item in config["indexes"] if item["path"] == identity), None)
            if entry is None:
                imported = legacy_folders(index_path)
                if not records and not imported:
                    return []
                entry = {"path": identity, "folders": imported}
                config["indexes"].append(entry)
            elif not records:
                return entry["folders"]
            merged = {(folder["path"], folder["collection"]): folder for folder in entry["folders"]}
            for folder in records:
                merged[(folder["path"], folder["collection"])] = folder
            entry["folders"] = list(merged.values())
            self._save(config)
            return entry["folders"]
