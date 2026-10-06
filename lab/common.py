import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def confined(root, relative):
    base = Path(root).resolve()
    candidate = (base / relative).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError("Path is outside its permitted directory")
    return candidate


class LabError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Halt(LabError):
    pass


class NotFound(ValueError):
    pass
