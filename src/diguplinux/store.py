import array
import contextlib
import os
import sqlite3
import sys
from pathlib import Path

from .runtime import DigupError

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY, path TEXT NOT NULL, collection TEXT NOT NULL, kind TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, pipeline TEXT NOT NULL,
    UNIQUE(path, collection));
CREATE TABLE IF NOT EXISTS segments(
    id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    path TEXT NOT NULL, text TEXT NOT NULL, location TEXT NOT NULL,
    line INTEGER, page INTEGER, seconds REAL, vector BLOB);
CREATE INDEX IF NOT EXISTS segments_file ON segments(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    path, text, content='segments', content_rowid='id', tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO search_fts(rowid,path,text) VALUES(new.id,new.path,new.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO search_fts(search_fts,rowid,path,text) VALUES('delete',old.id,old.path,old.text);
END;
CREATE TABLE IF NOT EXISTS failures(
    path TEXT NOT NULL, collection TEXT NOT NULL, error TEXT NOT NULL,
    PRIMARY KEY(path,collection));
"""


def pack(vector):
    values = array.array("f", vector)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def unpack(blob):
    values = array.array("f")
    values.frombytes(blob)
    if sys.byteorder != "little":
        values.byteswap()
    return values


class Store:
    def __init__(self, folder: Path, readonly: bool = False):
        self.folder = folder
        path = folder / "index.sqlite"
        if readonly:
            if not path.is_file():
                raise DigupError("No index yet. Run `digup index <folder>` first.")
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
            self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        try:
            self._initialize(readonly)
        except BaseException:
            self.db.close()
            raise

    def _initialize(self, readonly):
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        if not readonly:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema','1')")
        version = self.db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
        if not version or version[0] != "1":
            raise DigupError("Unsupported index schema; choose a new --index directory")

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def snapshot(self):
        """Keep a reader's view stable while indexing commits in another process."""
        started = not self.db.in_transaction
        if started:
            self.db.execute("BEGIN")
        try:
            yield
        finally:
            if started:
                self.db.rollback()

    def existing(self, collection):
        return {
            Path(row["path"]): row
            for row in self.db.execute("SELECT * FROM files WHERE collection=?", (collection,))
        }

    def replace(self, path, collection, kind, stat, pipeline, segments):
        with self.db:
            self.db.execute("DELETE FROM files WHERE path=? AND collection=?", (str(path), collection))
            cursor = self.db.execute(
                "INSERT INTO files(path,collection,kind,mtime_ns,size,pipeline) VALUES (?,?,?,?,?,?)",
                (str(path), collection, kind, stat.st_mtime_ns, stat.st_size, pipeline),
            )
            for segment, vector in segments:
                self.db.execute(
                    """INSERT INTO segments(file_id,path,text,location,line,page,seconds,vector)
                                   VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        cursor.lastrowid,
                        str(path),
                        segment.text,
                        segment.location,
                        segment.line,
                        segment.page,
                        segment.seconds,
                        pack(vector) if vector else None,
                    ),
                )
            self.db.execute("DELETE FROM failures WHERE path=? AND collection=?", (str(path), collection))

    def failure(self, path, collection, error):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO failures VALUES (?,?,?)", (str(path), collection, error))

    def status(self):
        with self.snapshot():
            return self._status()

    def _status(self):
        return {
            "index": str(self.folder),
            "files": self.db.execute("SELECT count(*) FROM files").fetchone()[0],
            "segments": self.db.execute("SELECT count(*) FROM segments").fetchone()[0],
            "vectors": self.db.execute("SELECT count(*) FROM segments WHERE vector IS NOT NULL").fetchone()[
                0
            ],
            "kinds": {r[0]: r[1] for r in self.db.execute("SELECT kind,count(*) FROM files GROUP BY kind")},
            "failures": [dict(r) for r in self.db.execute("SELECT * FROM failures ORDER BY path")],
        }
