"""fbm.credentials: OS keyring, file-store fallback, and auto-detection. Never touches the real keyring or data/."""

from __future__ import annotations

import json
import os
import stat

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail, null
from keyring.errors import KeyringLocked, PasswordDeleteError

from fbm import credentials


class MemoryKeyring(KeyringBackend):
    """A working in-memory keyring backend."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise PasswordDeleteError("not found")
        del self.store[(service, username)]


class LockedKeyring(MemoryKeyring):
    """Looks usable, but every call fails (e.g. a locked Secret Service on a headless box)."""

    def get_password(self, service, username, *args):
        raise KeyringLocked("locked")

    set_password = delete_password = get_password


@pytest.fixture
def use_keyring(monkeypatch):
    def _use(backend: KeyringBackend) -> KeyringBackend:
        monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
        monkeypatch.setattr(keyring, "get_password", backend.get_password)
        monkeypatch.setattr(keyring, "set_password", backend.set_password)
        monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
        credentials.reset_backend()
        return backend

    return _use


def _file() -> dict:
    return json.loads(credentials.secrets_path().read_text(encoding="utf-8"))


# ------------------------------------------------------------------ auto-detection


@pytest.mark.parametrize("placeholder", [fail.Keyring(), null.Keyring()])
def test_auto_uses_file_store_without_a_real_keyring(use_keyring, placeholder):
    use_keyring(placeholder)
    assert not credentials.keyring_usable()
    assert credentials.backend() == credentials.FILE
    assert "no OS keyring available" in credentials.describe()


def test_auto_uses_keyring_when_usable(use_keyring):
    use_keyring(MemoryKeyring())
    assert credentials.keyring_usable()
    assert credentials.backend() == credentials.KEYRING
    assert credentials.describe().startswith("OS keyring")


def test_env_forces_backend(use_keyring, monkeypatch):
    use_keyring(MemoryKeyring())
    monkeypatch.setenv(credentials.BACKEND_ENV, "file")
    assert credentials.backend() == credentials.FILE
    use_keyring(fail.Keyring())
    monkeypatch.setenv(credentials.BACKEND_ENV, "KEYRING")
    assert credentials.backend() == credentials.KEYRING
    monkeypatch.setenv(credentials.BACKEND_ENV, "auto")
    assert credentials.backend() == credentials.FILE


# ------------------------------------------------------------------ file store


def test_file_store_round_trip(use_keyring, capsys):
    use_keyring(fail.Keyring())  # what a headless Linux box without Secret Service gives
    assert credentials.get(credentials.YAHOO_REFRESH_TOKEN) is None
    credentials.put(credentials.YAHOO_REFRESH_TOKEN, "tok-123")
    credentials.put(credentials.GMAIL_ADDRESS, "me@example.com")
    assert credentials.get(credentials.YAHOO_REFRESH_TOKEN) == "tok-123"
    assert credentials.require(credentials.GMAIL_ADDRESS) == "me@example.com"
    assert _file()["secrets"] == {"gmail_address": "me@example.com", "yahoo_refresh_token": "tok-123"}
    credentials.delete(credentials.GMAIL_ADDRESS)
    credentials.delete("never-stored")
    assert credentials.get(credentials.GMAIL_ADDRESS) is None
    with pytest.raises(credentials.MissingCredentialError):
        credentials.require(credentials.GMAIL_ADDRESS)
    err = capsys.readouterr().err
    assert err.count("no OS keyring is available") == 1  # one-time warning, on the first write
    assert "tok-123" not in err and "FBM_CREDENTIALS_BACKEND=keyring" in err
    assert not list(credentials.secrets_path().parent.glob("*.lock"))
    assert not list(credentials.secrets_path().parent.glob("*.tmp"))


def test_file_store_large_values(use_keyring):
    use_keyring(null.Keyring())
    value = "x" * (credentials.CHUNK_CHARS * 2 + 7)
    credentials.put_large("big", value)
    assert credentials.get_large("big") == value
    credentials.put_large("big", "short")
    assert credentials.get_large("big") == "short" and "big.1" not in _file()["secrets"]
    credentials.delete_large("big")
    assert credentials.get_large("big") is None and _file()["secrets"] == {}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_file_store_permissions(use_keyring):
    use_keyring(fail.Keyring())
    credentials.put("k", "v")
    path = credentials.secrets_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_forced_file_store_is_quiet(use_keyring, monkeypatch, capsys):
    backend = use_keyring(MemoryKeyring())
    monkeypatch.setenv(credentials.BACKEND_ENV, "file")
    credentials.put("k", "v")
    assert credentials.get("k") == "v" and backend.store == {}
    assert capsys.readouterr().err == ""


def test_corrupt_file_is_a_clear_error(use_keyring):
    use_keyring(fail.Keyring())
    path = credentials.secrets_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(credentials.CredentialStoreError, match="not valid JSON"):
        credentials.get("k")
    with pytest.raises(credentials.CredentialStoreError):
        credentials.put("k", "v")
    assert path.read_text(encoding="utf-8") == "{not json"  # never overwritten


def test_stale_lock_is_cleared(use_keyring):
    use_keyring(fail.Keyring())
    path = credentials.secrets_path()
    path.parent.mkdir(parents=True)
    lock = path.with_name(path.name + ".lock")
    lock.write_text("", encoding="utf-8")
    old = lock.stat().st_mtime - 3600
    os.utime(lock, (old, old))
    credentials.put("k", "v")
    assert credentials.get("k") == "v" and not lock.exists()


def test_held_lock_times_out(use_keyring, monkeypatch):
    use_keyring(fail.Keyring())
    monkeypatch.setattr(credentials, "_LOCK_WAIT_S", 0.1)
    path = credentials.secrets_path()
    path.parent.mkdir(parents=True)
    path.with_name(path.name + ".lock").write_text("", encoding="utf-8")
    with pytest.raises(credentials.CredentialStoreError, match="held by another FBM process"):
        credentials.put("k", "v")


# ------------------------------------------------------------------ keyring backend


def test_keyring_round_trip(use_keyring):
    backend = use_keyring(MemoryKeyring())
    credentials.put("k", "v")
    assert backend.store == {("fbm", "k"): "v"}
    assert credentials.get("k") == "v"
    credentials.delete("k")
    credentials.delete("k")  # deleting twice is fine
    assert credentials.get("k") is None
    assert not credentials.secrets_path().exists()


def test_keyring_reads_secrets_saved_to_the_file_store(use_keyring, monkeypatch):
    use_keyring(fail.Keyring())
    credentials.put("old", "from-file")
    credentials.put("moved", "stale")
    backend = use_keyring(MemoryKeyring())  # a keyring backend was installed later
    assert credentials.get("old") == "from-file"
    credentials.put("moved", "fresh")  # saving again moves it into the keyring and off the disk
    assert backend.store[("fbm", "moved")] == "fresh" and "moved" not in _file()["secrets"]
    credentials.delete("old")
    assert credentials.get("old") is None
    monkeypatch.setenv(credentials.BACKEND_ENV, "keyring")
    credentials.reset_backend()
    credentials.put("other", "x")
    use_keyring(fail.Keyring())
    monkeypatch.delenv(credentials.BACKEND_ENV)
    credentials.reset_backend()
    assert credentials.get("other") is None  # forced keyring never wrote the file


def test_failing_keyring_falls_back_to_file(use_keyring):
    use_keyring(LockedKeyring())
    assert credentials.backend() == credentials.KEYRING
    credentials.put("k", "v")
    assert credentials.backend() == credentials.FILE
    assert credentials.get("k") == "v" and _file()["secrets"] == {"k": "v"}


def test_forced_keyring_raises_a_clear_error(use_keyring, monkeypatch):
    use_keyring(LockedKeyring())
    monkeypatch.setenv(credentials.BACKEND_ENV, "keyring")
    with pytest.raises(credentials.CredentialStoreError, match="unset FBM_CREDENTIALS_BACKEND"):
        credentials.get("k")
    use_keyring(fail.Keyring())
    with pytest.raises(credentials.CredentialStoreError):
        credentials.put("k", "v")
    assert not credentials.secrets_path().exists()


def test_broken_file_does_not_break_a_working_keyring(use_keyring):
    use_keyring(MemoryKeyring())
    path = credentials.secrets_path()
    path.parent.mkdir(parents=True)
    path.write_text("garbage", encoding="utf-8")
    assert credentials.get("k") is None
    credentials.put("k", "v")
    assert credentials.get("k") == "v"
