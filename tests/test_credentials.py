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
    assert err.count("no usable OS keyring") == 1  # one-time warning, when the file is created
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


@pytest.mark.parametrize("store", ["file", "keyring"])
def test_large_value_read_overlapping_a_save_is_the_old_or_new_value_never_a_mix(use_keyring, monkeypatch, store):
    """Another process re-saves (e.g. a rotated Flaim token) between this process's chunk reads."""
    use_keyring(null.Keyring() if store == "file" else MemoryKeyring())
    old, new = "a" * 2500, "b" * 2500
    credentials.put_large("big", old)
    real_get, chunk_reads = credentials.get, []

    def get(name):
        if name != "big":
            chunk_reads.append(name)
            if len(chunk_reads) == 2:  # chunk 0 already read: the other process saves now
                monkeypatch.setattr(credentials, "get", real_get)
                credentials.put_large("big", new)
                monkeypatch.setattr(credentials, "get", get)
        return real_get(name)

    monkeypatch.setattr(credentials, "get", get)
    assert credentials.get_large("big") == new


@pytest.mark.parametrize("store", ["file", "keyring"])
def test_a_save_killed_before_its_switch_keeps_the_old_value(use_keyring, monkeypatch, store):
    """A job killed mid-save (timeout, sleep) must not lose a sign-in the provider has already rotated away."""
    use_keyring(null.Keyring() if store == "file" else MemoryKeyring())
    credentials.put_large("big", "a" * 2500)
    real_put = credentials.put

    def put(name, value):
        if name == "big":
            raise KeyboardInterrupt  # killed just before the head is written
        real_put(name, value)

    monkeypatch.setattr(credentials, "put", put)
    with pytest.raises(KeyboardInterrupt):
        credentials.put_large("big", "b" * 2500)
    monkeypatch.setattr(credentials, "put", real_put)
    assert credentials.get_large("big") == "a" * 2500
    credentials.put_large("big", "c")  # the next save works normally
    assert credentials.get_large("big") == "c"
    credentials.delete_large("big")  # signing out also removes the killed save's leftover chunks (file store)
    if store == "file":
        assert _file()["secrets"] == {}


def test_large_values_saved_in_the_older_layout_still_read_and_are_replaced(use_keyring):
    use_keyring(null.Keyring())
    credentials.put("big", "chunks:2")
    credentials.put("big.0", "ab")
    credentials.put("big.1", "cd")
    assert credentials.get_large("big") == "abcd"
    credentials.put_large("big", "new")
    secrets = _file()["secrets"]
    assert credentials.get_large("big") == "new" and "big.0" not in secrets and "big.1" not in secrets


def test_process_lock_is_exclusive_takes_over_stale_locks_and_never_blocks_for_good(use_keyring):
    use_keyring(null.Keyring())
    lock = credentials.secrets_path().with_name("job.lock")
    with credentials.process_lock("job", wait_s=0) as held:
        assert held and lock.exists()
        with credentials.process_lock("job", wait_s=0.1) as again:
            assert again is False  # the caller goes on unlocked
        assert lock.exists()  # a waiter that gave up doesn't release someone else's lock
    assert not lock.exists()
    lock.write_text("")
    os.utime(lock, (0, 0))  # left behind by a killed process
    with credentials.process_lock("job", wait_s=0, stale_s=60) as held:
        assert held


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


def test_an_existing_secrets_file_stays_the_store(use_keyring, monkeypatch):
    use_keyring(fail.Keyring())
    credentials.put("old", "from-file")
    backend = use_keyring(MemoryKeyring())  # a keyring backend was installed later
    assert credentials.backend() == credentials.FILE
    assert "only read for secrets saved there earlier" in credentials.describe()
    assert credentials.get("old") == "from-file"
    credentials.put("old", "fresh")
    credentials.put("new", "n")
    assert backend.store == {} and _file()["secrets"] == {"new": "n", "old": "fresh"}
    monkeypatch.setenv(credentials.BACKEND_ENV, "keyring")  # moving to the keyring is explicit
    credentials.put("other", "x")
    assert backend.store == {("fbm", "other"): "x"} and "other" not in _file()["secrets"]
    assert credentials.get("old") is None  # forced keyring never reads the file


def test_file_store_still_reads_and_deletes_older_keyring_secrets(use_keyring):
    backend = use_keyring(MemoryKeyring())
    credentials.put("kept", "in-keyring")
    credentials.put("both", "stale")
    backend.store[("fbm", "big")] = "chunks:1"
    backend.store[("fbm", "big.0")] = "abc"
    use_keyring(LockedKeyring())  # a keyring call fails: this process switches to the file store
    credentials.put("both", "fresh")
    use_keyring(backend)  # next process: the keyring works again, but the file exists now
    assert credentials.backend() == credentials.FILE
    assert credentials.get("kept") == "in-keyring" and credentials.get_large("big") == "abc"
    assert credentials.get("both") == "fresh"  # the file copy wins over the older keyring copy
    credentials.delete("both")
    assert credentials.get("both") is None and ("fbm", "both") not in backend.store  # no stale copy resurfaces
    credentials.delete_large("big")
    assert credentials.get_large("big") is None and not any(k[1].startswith("big") for k in backend.store)


def test_linux_desktop_terminal_and_cron_share_the_file_store(use_keyring, monkeypatch):
    """The terminal sees the desktop keyring, cron jobs don't: after one sign-in with the file store, both agree."""
    desktop = MemoryKeyring()

    def process(kind, env=None):
        use_keyring(desktop if kind == "terminal" else fail.Keyring())
        monkeypatch.delenv(credentials.BACKEND_ENV, raising=False)
        if env:
            monkeypatch.setenv(credentials.BACKEND_ENV, env)
        credentials.reset_backend()

    process("terminal")
    credentials.put("token", "v1")  # first sign-in: the desktop keyring
    process("cron")
    assert credentials.get("token") is None  # cron can't reach it
    process("terminal", "file")
    credentials.put("token", "v2")  # documented fix: sign in once with FBM_CREDENTIALS_BACKEND=file
    process("terminal")
    assert credentials.get("token") == "v2"  # not the stale keyring copy
    credentials.put("token", "v3")  # a terminal command refreshes the token
    credentials.put("bot", "b")  # a new secret saved later from a terminal
    process("cron")
    assert credentials.get("token") == "v3" and credentials.get("bot") == "b"
    credentials.put("token", "v4")  # cron refreshes it
    process("terminal")
    assert credentials.get("token") == "v4"


def test_warning_is_shown_once_per_install(use_keyring, capsys):
    use_keyring(fail.Keyring())
    credentials.put("a", "1")
    credentials.reset_backend()  # a later process, e.g. a scheduled job refreshing a token
    credentials.put("a", "2")
    assert capsys.readouterr().err.count("no usable OS keyring") == 1


def test_unwritable_secrets_file_is_a_clear_error(use_keyring, monkeypatch):
    use_keyring(fail.Keyring())

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(credentials.os, "replace", boom)
    with pytest.raises(credentials.CredentialStoreError, match="Can't write"):
        credentials.put("k", "secret-value")
    assert not list(credentials.secrets_path().parent.glob("*.tmp"))


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
    assert credentials.backend() == credentials.KEYRING  # chosen before the file appeared
    path = credentials.secrets_path()
    path.parent.mkdir(parents=True)
    path.write_text("garbage", encoding="utf-8")
    assert credentials.get("k") is None
    credentials.put("k", "v")
    assert credentials.get("k") == "v"
    credentials.reset_backend()  # a later process: the (corrupt) file is the store now, and says so
    with pytest.raises(credentials.CredentialStoreError, match="not valid JSON"):
        credentials.get("k")


def test_broken_keyring_does_not_break_the_file_store(use_keyring, monkeypatch):
    use_keyring(fail.Keyring())
    credentials.put("k", "v")
    backend = use_keyring(MemoryKeyring())

    def boom(*args):
        raise RuntimeError("D-Bus went away")

    monkeypatch.setattr(keyring, "get_password", boom)
    monkeypatch.setattr(keyring, "delete_password", boom)
    assert credentials.get("k") == "v" and credentials.get("missing") is None
    credentials.delete("k")
    assert credentials.get("k") is None and backend.store == {}
