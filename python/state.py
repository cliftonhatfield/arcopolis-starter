"""Owner-only pending action state, shared in format with the Node starter."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
from typing import Any, Iterator
from uuid import uuid4

from actions import validate_action
from client import ApiError


def same_body(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            raw = handle.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("state is too large")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("schemaVersion") != 1 or value.get("status") not in ("pending", "completed"):
            raise ValueError("unknown state format")
        for name in ("agentId", "baseUrl", "createdAt", "idempotencyKey", "keyFingerprint"):
            if not isinstance(value.get(name), str) or not value[name]:
                raise ValueError(f"missing {name}")
        if len(value["idempotencyKey"]) > 256 or "\r" in value["idempotencyKey"] or "\n" in value["idempotencyKey"]:
            raise ValueError("invalid idempotency key")
        if re.fullmatch(r"[a-f0-9]{64}", value["keyFingerprint"]) is None:
            raise ValueError("invalid key fingerprint")
        validate_action(value.get("body"))
        if value["status"] == "completed" and not isinstance(value.get("response"), dict):
            raise ValueError("completed state is missing its receipt")
        return value
    except (OSError, ValueError, ApiError) as error:
        raise ApiError(0, "STATE_INVALID", f"Cannot safely read {path}: {error}. Preserve the file and resolve its pending action before starting another.") from None


def write_state(path: Path, value: dict[str, Any]) -> None:
    """Write a complete file atomically; a failed save cannot erase pending state."""
    temporary = path.with_name(path.name + ".tmp-" + str(uuid4()))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(handle.fileno(), 0o600)
            else:
                os.chmod(temporary, 0o600)
            json.dump(value, handle, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError) as error:
        raise ApiError(0, "STATE_WRITE_FAILED", f"Could not persist action state at {path}: {error}. Do not send another action with a new key.") from None
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def state_lock(path: Path) -> Iterator[None]:
    """Serialize one local state file across Python and Node processes."""
    lock_path = Path(str(path) + ".lock")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ApiError(0, "STATE_LOCKED", f"Another process holds {lock_path}. Let it finish; if it crashed, confirm it has stopped before removing only that lock file. Preserve the action state.") from None
    except OSError as error:
        raise ApiError(0, "STATE_LOCK_FAILED", f"Cannot lock {path}: {error}") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid()}, handle)
        yield
    finally:
        lock_path.unlink(missing_ok=True)
