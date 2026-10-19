"""Offline configuration and filesystem boundaries for up to three accounts."""
from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from monitor_store import parse_chat_ids


@dataclass(frozen=True)
class Account:
    slot: str
    user_id: int
    all_chats: bool
    chat_ids: frozenset[int]
    timezone: str
    auth_mode: str
    enabled: bool = True

    @property
    def recipient_id(self) -> int:
        # Deliberately not configurable: no cross-account recipient routing.
        return self.user_id

    @property
    def phone_env(self) -> str:
        return f"CHAT_MONITOR_{self.slot.upper()}_PHONE"

    @property
    def password_env(self) -> str:
        return f"CHAT_MONITOR_{self.slot.upper()}_PASSWORD"


@dataclass(frozen=True)
class Profiles:
    config_path: Path
    tdjson_path: str
    accounts: tuple[Account, ...]

    @property
    def root(self) -> Path:
        return self.config_path.parent / '.chat-monitor-accounts'

    def directory(self, account: Account) -> Path:
        # Identity, not slot/name, determines storage. Reassigning a slot cannot
        # expose the previous account's session/database to a new account.
        return self.root / str(account.user_id)

    def select(self, slot: str) -> Account:
        for account in self.accounts:
            if account.slot == slot:
                return account
        raise ValueError('Unknown account slot')


def load_profiles(path: str | Path = 'accounts.ini') -> Profiles:
    path = Path(path).resolve()
    cfg = configparser.ConfigParser(interpolation=None)
    if not cfg.read(path):
        raise ValueError('Create local accounts.ini using accounts.ini.example')
    if not cfg.has_section('tdlib') or not cfg.get('tdlib', 'tdjson_path', fallback='').strip():
        raise ValueError('Set [tdlib] tdjson_path')
    accounts = []
    seen = set()
    for section in cfg.sections():
        if section == 'tdlib':
            continue
        if not re.fullmatch(r'account:[a-z][a-z0-9_]{0,23}', section):
            raise ValueError('Account sections must use account:slot format')
        slot = section.split(':', 1)[1]
        item = cfg[section]
        permitted = {'user_id', 'all_chats', 'chat_ids', 'timezone', 'auth_mode', 'enabled'}
        if set(item) - permitted:
            raise ValueError('Unknown account option; notification recipients cannot be overridden')
        uid = item.getint('user_id', fallback=0)
        if uid <= 0 or uid in seen:
            raise ValueError('Each account needs a distinct positive user_id')
        seen.add(uid)
        all_chats = item.getboolean('all_chats', fallback=False)
        ids = frozenset(parse_chat_ids(item.get('chat_ids', '')))
        if not all_chats and not ids:
            raise ValueError('Choose all_chats=true or a nonempty chat_ids list')
        tz = item.get('timezone', 'Asia/Tashkent')
        ZoneInfo(tz)
        mode = item.get('auth_mode', 'qr')
        if mode not in {'qr', 'code'}:
            raise ValueError('auth_mode must be qr or code')
        accounts.append(Account(slot, uid, all_chats, ids, tz, mode,
                                item.getboolean('enabled', fallback=True)))
    if not 1 <= len(accounts) <= 3:
        raise ValueError('Configure between one and three accounts')
    library = Path(cfg['tdlib']['tdjson_path']).expanduser()
    if not library.is_absolute():
        library = path.parent / library
    return Profiles(path, str(library.resolve()), tuple(accounts))


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError('Session directories must not be symlinks')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def prepare_account(profiles: Profiles, account: Account) -> Path:
    private_directory(profiles.root)
    directory = profiles.directory(account)
    private_directory(directory)
    private_directory(directory / 'session')
    for path in directory.rglob('*'):
        if path.is_symlink():
            raise ValueError('Account storage must not contain symlinks')
        path.chmod(0o700 if path.is_dir() else 0o600)
    return directory


class RuntimeLock:
    """Linux lock shared with the old launchers; held for process lifetime."""
    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def __enter__(self):
        import fcntl
        import os
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(self.fd, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError('Another monitor or authorization process is already running') from None
        return self

    def __exit__(self, *_):
        import os
        os.close(self.fd)
        self.fd = None
