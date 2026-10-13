"""Root-relative, case-sensitive ignore globs without filesystem access."""

from fnmatch import fnmatchcase
from functools import lru_cache

from .runtime import DigupError


def validate_patterns(patterns):
    if not isinstance(patterns, (list, tuple)) or any(
        not isinstance(p, str) or not p or "\0" in p for p in patterns
    ):
        raise DigupError("ignore_patterns must be an array of nonempty strings")
    for pattern in patterns:
        if pattern.startswith(("/", "!")) or ".." in pattern.split("/") or not pattern.rstrip("/"):
            raise DigupError(f"Ignore patterns must be relative globs without negation or '..': {pattern}")
    return list(patterns)


def matches(relative, pattern, directory=False):
    if pattern.endswith("/") and not directory:
        return False
    pattern = pattern.rstrip("/")
    if "/" not in pattern:
        return fnmatchcase(relative.name, pattern)
    parts, globs = relative.parts, tuple(pattern.split("/"))

    @lru_cache(maxsize=None)
    def match(i, j):
        if j == len(globs):
            return i == len(parts)
        if globs[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatchcase(parts[i], globs[j]) and match(i + 1, j + 1)

    return match(0, 0)
