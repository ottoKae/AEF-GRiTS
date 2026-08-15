"""Cross-process request tokens and exclusive final-delivery leases."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
from typing import Iterator


class FileLease:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._stream = None

    def _try_lock(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt

                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, OSError):
            stream.close()
            return False
        self._stream = stream
        return True

    def acquire(self, timeout_seconds: float = 3600.0, poll_seconds: float = 0.1) -> None:
        deadline = time.monotonic() + timeout_seconds
        while not self._try_lock():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for resource lease: {self.path}")
            time.sleep(poll_seconds)

    def release(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            else:
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            stream.close()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.release()


class TokenPool:
    def __init__(self, root: str | Path, size: int, name: str = "token"):
        if size <= 0:
            raise ValueError("Token pool size must be positive")
        self.root = Path(root)
        self.size = int(size)
        self.name = name
        self._validate_contract()

    def _validate_contract(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        lease = FileLease(self.root / ".pool-contract.lock")
        lease.acquire(timeout_seconds=30)
        try:
            contract = self.root / "pool.json"
            expected = {"name": self.name, "size": self.size}
            if contract.exists():
                current = json.loads(contract.read_text(encoding="utf-8"))
                if current != expected:
                    raise ValueError(
                        f"Resource token contract differs at {contract}: "
                        f"existing={current}, requested={expected}"
                    )
            else:
                temporary = contract.with_suffix(f".tmp.{os.getpid()}")
                temporary.write_text(json.dumps(expected, sort_keys=True), encoding="utf-8")
                os.replace(temporary, contract)
        finally:
            lease.release()

    def acquire(self, timeout_seconds: float = 3600.0) -> FileLease:
        deadline = time.monotonic() + timeout_seconds
        while True:
            for index in range(self.size):
                lease = FileLease(self.root / f"{self.name}-{index:03d}.lock")
                if lease._try_lock():
                    return lease
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for a {self.name} token")
            time.sleep(0.05)

    @contextmanager
    def token(self, timeout_seconds: float = 3600.0) -> Iterator[None]:
        lease = self.acquire(timeout_seconds)
        try:
            yield
        finally:
            lease.release()


def default_resource_root(state_dir: str | Path) -> Path:
    configured = os.environ.get("AEF_GRITS_RESOURCE_STATE")
    if configured:
        return Path(configured).expanduser().absolute()
    state = Path(state_dir).expanduser().absolute()
    return state.parent / ".aef_resources"
