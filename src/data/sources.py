import hashlib
import json
import os
import shutil
from pathlib import Path


def fingerprint(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def archive_file(source, target, expected=None):
    expected = fingerprint(source) if expected is None else expected
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        # Publish only a fully verified copy. Windows hard links support exclusive creation.
        pending = target.with_name(target.name + ".tmp")
        destination = pending.open("xb")
        try:
            with source.open("rb") as original, destination:
                shutil.copyfileobj(original, destination, length=1024 * 1024)
            if fingerprint(pending) != expected:
                raise ValueError(f"Archive copy failed SHA256 verification: {source}")
            os.link(pending, target)
        finally:
            pending.unlink()
    if fingerprint(target) != expected:
        raise FileExistsError(f"Archive differs from source: {target}")
    return expected


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")
