"""File access inside an agent's workspace that an agent cannot redirect.

An agent's workspace (``agents/<name>``) is bind-mounted read-write into the
agent's own container, so everything in it is agent-controlled, including
which entries are symlinks. miragend runs as a different, more trusted
process: a plain ``open()`` there follows a link the agent planted (say,
``agent.yaml -> /run/agents-host-secrets/miragen_internal_token``) and hands
the daemon's secrets to whoever reads the result, or writes through a link
onto the daemon's own files.

Every daemon-side access to an agent workspace therefore goes through here:

- the path is walked one component at a time from a directory fd, each
  opened with ``O_NOFOLLOW`` (``O_DIRECTORY`` for directories), so no
  component may be a symlink and nothing resolves outside the base. This is
  what ``openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS)`` gives, built from
  calls Python exposes everywhere;
- the final file is checked on the open fd (``fstat``): a regular file with
  a single link, so a hard link to a file elsewhere is refused too. There is
  no check-then-use window, because the check and the use are the same fd;
- writes create a fresh temp file in the same directory (``O_EXCL``,
  ``O_NOFOLLOW``) and ``os.replace`` it over the name, relative to the
  directory fd. A rename replaces a symlink rather than following it.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from collections.abc import Iterator
from pathlib import Path, PurePosixPath


class UnsafeAgentPath(Exception):
    """The path is not a plain relative path to a regular, singly-linked
    file inside the agent's workspace."""


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _parts(rel: str | os.PathLike) -> list[str]:
    path = PurePosixPath(os.fspath(rel))
    if path.is_absolute():
        raise UnsafeAgentPath("absolute paths are not allowed")
    parts = [p for p in path.parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise UnsafeAgentPath("path traversal not allowed")
    return parts


@contextlib.contextmanager
def _dir_fd(base: Path, parts: list[str], *, create: bool = False) -> Iterator[int]:
    """An fd for base/parts[...], every component opened without following
    a symlink. `create` makes missing directories (0o755)."""
    fd = os.open(base, _DIR_FLAGS)
    try:
        for part in parts:
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=fd)
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                # ELOOP / ENOTDIR: a symlink or a file where a directory goes.
                raise UnsafeAgentPath(f"'{part}' is not a plain directory") from exc
            os.close(fd)
            fd = nxt
        yield fd
    finally:
        os.close(fd)


def _check_regular(fd: int, name: str) -> os.stat_result:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeAgentPath(f"'{name}' is not a regular file")
    if st.st_nlink > 1:
        raise UnsafeAgentPath(f"'{name}' is hard-linked")
    return st


def open_regular(base: Path, rel: str | os.PathLike) -> int:
    """A read-only fd for a regular, singly-linked file under base; the
    caller closes it. FileNotFoundError when it does not exist."""
    *dirs, name = _parts(rel)
    with _dir_fd(base, dirs) as dfd:
        try:
            # O_NONBLOCK: opening a FIFO the agent planted must not hang the
            # daemon; fstat below refuses it (regular files ignore the flag).
            fd = os.open(name, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK
                         | getattr(os, "O_CLOEXEC", 0), dir_fd=dfd)
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise UnsafeAgentPath(f"'{name}' is not a regular file") from exc
        try:
            _check_regular(fd, name)
        except BaseException:
            os.close(fd)
            raise
        return fd


def read_bytes(base: Path, rel: str | os.PathLike, *, limit: int | None = None) -> bytes:
    fd = open_regular(base, rel)
    with os.fdopen(fd, "rb") as handle:
        return handle.read() if limit is None else handle.read(limit)


def read_text(base: Path, rel: str | os.PathLike) -> str:
    return read_bytes(base, rel).decode("utf-8")


def exists(base: Path, rel: str | os.PathLike) -> bool:
    """A regular, singly-linked file is there (a symlink or anything else
    counts as absent)."""
    try:
        os.close(open_regular(base, rel))
        return True
    except (FileNotFoundError, UnsafeAgentPath, NotADirectoryError):
        return False


def write_text(base: Path, rel: str | os.PathLike, content: str, *, mode: int = 0o644) -> None:
    """Replace base/rel with `content`: a temp file in the same directory,
    then a rename over the name. Never writes through a link."""
    *dirs, name = _parts(rel)
    data = content.encode("utf-8")
    with _dir_fd(base, dirs, create=True) as dfd:
        tmp = f".{name}.miragend-{secrets.token_hex(6)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, mode, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            raise


def unlink(base: Path, rel: str | os.PathLike) -> None:
    """Remove the entry itself (a symlink is removed, not followed)."""
    *dirs, name = _parts(rel)
    try:
        with _dir_fd(base, dirs) as dfd:
            os.unlink(name, dir_fd=dfd)
    except FileNotFoundError:
        pass
