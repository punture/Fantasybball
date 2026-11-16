"""Secret storage: the OS keyring (Windows Credential Manager / macOS Keychain / Linux Secret Service), or a local
file on machines without one (e.g. headless Linux).

Backend choice (`FBM_CREDENTIALS_BACKEND`): `keyring` forces the OS keyring, `file` forces the file store, unset (or
`auto`) uses the file store once the secrets file exists or when the keyring has no usable backend, and the keyring
otherwise (switching to the file store if a keyring call fails). The file store is data/secrets/credentials.json
(gitignored; `FBM_CREDENTIALS_FILE` overrides the path; folder 0700, file 0600 on POSIX; atomic writes under a lock
file).

Once the secrets file exists it stays this install's store in auto mode, so every process agrees on one store: a
Linux desktop's terminal (which sees the desktop keyring) and its cron jobs (which can't) read and refresh the same
tokens. A name missing from the file is still read from a usable keyring (secrets saved there before the file
existed), and deleting a name removes both copies. To move to the keyring, set `FBM_CREDENTIALS_BACKEND=keyring`, sign
in again and delete the file.

Secrets never touch the repo, logs, or reports, and nothing here prints a secret value.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from secrets import token_hex

import keyring
from keyring.backends import fail as _fail_backend
from keyring.backends import null as _null_backend
from keyring.errors import KeyringError, PasswordDeleteError

from fbm.config import DATA_DIR, PROJECT_ROOT
from fbm.fileio import write_text_atomic

SERVICE = "fbm"

YAHOO_CLIENT_ID = "yahoo_client_id"
YAHOO_CLIENT_SECRET = "yahoo_client_secret"
YAHOO_REFRESH_TOKEN = "yahoo_refresh_token"
YAHOO_WEB_SESSION = "yahoo_web_session"
HASHTAG_WEB_SESSION = "hashtag_web_session"  # Hashtag Basketball Premium sign-in cookies (put_large/get_large)
GMAIL_ADDRESS = "gmail_address"
GMAIL_APP_PASSWORD = "gmail_app_password"
FLAIM_OAUTH = "flaim_oauth"  # Flaim sign-in: OAuth client + tokens as one JSON blob (put_large/get_large)
TELEGRAM_BOT_TOKEN = "telegram_bot_token"  # @BotFather token for direct Telegram alerts (`fbm notify setup`)

# Windows Credential Manager caps one secret at 1280 characters; larger values are split across numbered entries.
CHUNK_CHARS = 1000
_CHUNKS_PREFIX = "chunks:"

BACKEND_ENV = "FBM_CREDENTIALS_BACKEND"  # keyring | file | auto (default)
FILE_ENV = "FBM_CREDENTIALS_FILE"  # optional path override for the file store
SECRETS_FILE = DATA_DIR / "secrets" / "credentials.json"
KEYRING, FILE = "keyring", "file"

_LOCK_WAIT_S = 10.0
_LOCK_STALE_S = 30.0

FILE_STORE_WARNING = (
    "FBM: no usable OS keyring here, so secrets are kept in {path} from now on (only your user account can read it; "
    "it is gitignored and never backed up by `fbm backup`). To use an OS keyring instead, install and unlock one "
    "(Linux: gnome-keyring or KWallet with a Secret Service session), set {env}=keyring, sign in again and delete "
    "that file. This note is shown once, when the file is created."
)


class MissingCredentialError(RuntimeError):
    pass


class CredentialStoreError(RuntimeError):
    """The secret store can't be used (forced keyring unavailable, unreadable or locked secrets file)."""


# ------------------------------------------------------------------ backend choice

_auto_choice: str | None = None  # this process's auto-detected backend
_warned = False
_keyring_down = False  # a keyring call failed in this process: don't touch the keyring again


def _forced() -> str | None:
    value = os.environ.get(BACKEND_ENV, "").strip().lower()
    return value if value in (KEYRING, FILE) else None


def keyring_usable() -> bool:
    """True when keyring resolved to a real backend (not the 'fail' or 'null' placeholder)."""
    try:
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001 - any keyring init problem means "no usable keyring"
        return False
    return not isinstance(backend, _fail_backend.Keyring | _null_backend.Keyring)


def backend() -> str:
    """'keyring' or 'file' for this process (see the module docstring)."""
    global _auto_choice
    forced = _forced()
    if forced:
        return forced
    if _auto_choice is None:
        _auto_choice = FILE if secrets_path().exists() or not keyring_usable() else KEYRING
    return _auto_choice


def secrets_path() -> Path:
    override = os.environ.get(FILE_ENV, "").strip()
    return Path(override) if override else SECRETS_FILE


def describe() -> str:
    """Where secrets live, for status output (no secret values)."""
    if backend() == KEYRING:
        try:
            name = type(keyring.get_keyring()).__module__.rsplit(".", 1)[-1]
        except Exception:  # noqa: BLE001
            name = "unknown"
        return f"OS keyring ({name})"
    if _forced() == FILE:
        why = f"{BACKEND_ENV}=file"
    elif _keyring_readable():
        why = "secrets file in use; the OS keyring is only read for secrets saved there earlier"
    else:
        why = "no OS keyring available"
    return f"file {_display(secrets_path())} ({why})"


def reset_backend() -> None:
    """Forget the auto-detected backend and the one-time warning (tests; after installing a keyring backend)."""
    global _auto_choice, _warned, _keyring_down
    _auto_choice, _warned, _keyring_down = None, False, False


def _keyring_failed(err: KeyringError) -> None:
    """A keyring call failed: in auto mode use the file store for the rest of the process; forced keyring raises."""
    global _auto_choice, _keyring_down
    if _forced() == KEYRING:
        raise CredentialStoreError(
            f"{BACKEND_ENV}=keyring but the OS keyring failed ({type(err).__name__}). Unlock or install a keyring "
            f"backend, or unset {BACKEND_ENV} to use FBM's file store."
        ) from err
    _auto_choice, _keyring_down = FILE, True


def _keyring_readable() -> bool:
    """Auto mode with the file store in use: may older secrets still be read from (or deleted in) the keyring?"""
    return _forced() is None and not _keyring_down and keyring_usable()


def _keyring_extra_get(name: str) -> str | None:
    """Best effort: a broken or locked keyring must never break the file store."""
    if not _keyring_readable():
        return None
    try:
        return keyring.get_password(SERVICE, name)
    except Exception:  # noqa: BLE001
        return None


def _keyring_extra_delete(name: str) -> None:
    if not _keyring_readable():
        return
    try:
        keyring.delete_password(SERVICE, name)
    except Exception:  # noqa: BLE001 - PasswordDeleteError (nothing stored) or a keyring that can't be reached
        pass


def _warn_file_store(path: Path) -> None:
    """Once, when auto mode creates the secrets file (not on every token refresh of a scheduled job)."""
    global _warned
    if _warned or _forced() == FILE or path.exists():
        return
    _warned = True
    print(FILE_STORE_WARNING.format(path=_display(path), env=BACKEND_ENV), file=sys.stderr)


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


# ------------------------------------------------------------------ file store


def _read_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise CredentialStoreError(f"Can't read {_display(path)} ({type(err).__name__}).") from err
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as err:
        raise CredentialStoreError(
            f"{_display(path)} is not valid JSON. Move it aside, then sign in again (e.g. `fbm notify setup`)."
        ) from err
    secrets = data.get("secrets", {}) if isinstance(data, dict) else None
    if not isinstance(secrets, dict):
        raise CredentialStoreError(f"{_display(path)} has an unexpected layout. Move it aside, then sign in again.")
    return {str(k): str(v) for k, v in secrets.items()}


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)


def _write_file(path: Path, secrets: dict[str, str]) -> None:
    """Atomic replace: a 0600 temp file next to the target (mkstemp), fsync'd, renamed over it."""
    _private_dir(path.parent)
    payload = json.dumps({"version": 1, "service": SERVICE, "secrets": secrets}, indent=1, sort_keys=True)
    write_text_atomic(path, payload + "\n")
    if os.name == "posix":
        os.chmod(path, 0o600)


def _acquire(lock: Path, wait_s: float, stale_s: float) -> bool:
    """Create `lock` exclusively, waiting up to `wait_s`; a lock older than `stale_s` (its holder died) is taken
    over. False when another process still holds it after the wait."""
    _private_dir(lock.parent)
    deadline = time.monotonic() + wait_s
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_s:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                return False
            time.sleep(0.05)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Cross-process lock for read-modify-write (scheduled jobs may refresh tokens at the same time)."""
    lock = path.with_name(path.name + ".lock")
    if not _acquire(lock, _LOCK_WAIT_S, _LOCK_STALE_S):
        raise CredentialStoreError(
            f"{_display(lock)} is held by another FBM process. If no FBM command is running, delete it."
        )
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


@contextmanager
def process_lock(name: str, wait_s: float = 50.0, stale_s: float = 100.0) -> Iterator[bool]:
    """A named cross-process lock beside the secrets file, whatever the backend (e.g. around an OAuth token refresh,
    so two FBM processes never spend the same single-use refresh token). Yields True when held; False when it could
    not be taken within `wait_s` (or the folder isn't writable): the caller goes on unlocked rather than fail."""
    lock = secrets_path().with_name(f"{name}.lock")
    try:
        held = _acquire(lock, wait_s, stale_s)
    except OSError:
        held = False
    try:
        yield held
    finally:
        if held:
            lock.unlink(missing_ok=True)


def _file_get(name: str) -> str | None:
    return _read_file(secrets_path()).get(name)


def _file_put(name: str, value: str) -> None:
    path = secrets_path()
    _warn_file_store(path)
    with _locked(path):
        secrets = _read_file(path)
        secrets[name] = value
        try:
            _write_file(path, secrets)
        except OSError as err:
            raise CredentialStoreError(f"Can't write {_display(path)} ({type(err).__name__}).") from err


def _file_delete(name: str) -> None:
    path = secrets_path()
    if not path.exists():
        return
    with _locked(path):
        secrets = _read_file(path)
        if secrets.pop(name, None) is not None:
            try:
                _write_file(path, secrets)
            except OSError as err:
                raise CredentialStoreError(f"Can't write {_display(path)} ({type(err).__name__}).") from err


# ------------------------------------------------------------------ public API


def _also_file(op: Callable[[str], str | None], name: str) -> str | None:
    """Keyring mode (auto): the file store is a best-effort extra; a broken secrets file must not break the keyring."""
    try:
        return op(name)
    except CredentialStoreError:
        return None


def get(name: str) -> str | None:
    if backend() == KEYRING:
        try:
            value = keyring.get_password(SERVICE, name)
        except KeyringError as err:
            _keyring_failed(err)
        else:
            if value is not None or _forced() == KEYRING:
                return value
            return _also_file(_file_get, name)  # another process created the secrets file meanwhile
    value = _file_get(name)
    return value if value is not None else _keyring_extra_get(name)  # saved to the keyring before the file existed


def put(name: str, value: str) -> None:
    if backend() == KEYRING:
        try:
            keyring.set_password(SERVICE, name, value)
        except KeyringError as err:
            _keyring_failed(err)
        else:
            return
    _file_put(name, value)


def delete(name: str) -> None:
    if backend() == KEYRING:
        try:
            keyring.delete_password(SERVICE, name)
        except PasswordDeleteError:
            pass  # nothing stored under this name
        except KeyringError as err:
            _keyring_failed(err)  # forced keyring raises; auto mode uses the file store from now on
        if backend() == KEYRING:
            if _forced() != KEYRING:
                _also_file(_file_delete, name)
            return
    _file_delete(name)
    _keyring_extra_delete(name)  # an older keyring copy must not resurface through get()


def require(name: str) -> str:
    value = get(name)
    if not value:
        raise MissingCredentialError(f"Missing credential '{name}'. Run `fbm auth` first.")
    return value


def _chunk_names(name: str, head: str | None) -> list[str]:
    """The chunk entries a head names: `chunks:<gen>:<n>` -> `name.<gen>.0..n-1` (older heads, `chunks:<n>`, name
    `name.0..n-1`); [] when `head` is not a chunk head."""
    if not head or not head.startswith(_CHUNKS_PREFIX):
        return []
    gen, _, count = head[len(_CHUNKS_PREFIX) :].rpartition(":")
    try:
        n = int(count)
    except ValueError:
        return []
    prefix = f"{name}.{gen}." if gen else f"{name}."
    return [f"{prefix}{i}" for i in range(n)]


def put_large(name: str, value: str) -> None:
    """Store a value of any length: chunks under a new generation (`name.<gen>.0`, `name.<gen>.1`, ...), then `name`
    (the head: generation and chunk count) as the switch, then the old generation's chunks are removed. A reader in
    another process sees the old value or the new one, never a mix, and a save killed midway keeps the old value."""
    old = _chunk_names(name, get(name))
    gen = token_hex(4)
    parts = [value[i : i + CHUNK_CHARS] for i in range(0, len(value), CHUNK_CHARS)] or [""]
    for i, part in enumerate(parts):
        put(f"{name}.{gen}.{i}", part)
    put(name, f"{_CHUNKS_PREFIX}{gen}:{len(parts)}")
    for chunk in old:
        delete(chunk)


def get_large(name: str) -> str | None:
    """The value put_large stored (None when missing or incomplete). A read overlapping a save in another process
    (its old chunks removed after the head was read) is retried on the new head."""
    for attempt in range(3):
        head = get(name)
        names = _chunk_names(name, head)
        if not names:
            return head
        parts = [get(chunk) for chunk in names]
        if all(p is not None for p in parts):
            return "".join(p or "" for p in parts)
        if get(name) == head:  # nothing changed meanwhile: a chunk is really missing
            return None
        time.sleep(0.05 * (attempt + 1))
    return None


def delete_large(name: str) -> None:
    for chunk in _chunk_names(name, get(name)):
        delete(chunk)
    delete(name)
    if backend() == FILE:  # chunks of a save killed before its switch (the file can be listed; a keyring can't)
        try:
            orphans = [k for k in _read_file(secrets_path()) if k.startswith(f"{name}.")]
        except CredentialStoreError:
            orphans = []
        for chunk in orphans:
            _file_delete(chunk)
