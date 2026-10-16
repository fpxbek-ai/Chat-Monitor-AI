"""Offline, read-only preflight for Chat Monitor AI.

Does not connect to Telegram, authorize accounts, or display credentials.
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path
import re


def _positive_int(value: str | None) -> int:
    try:
        number = int(str(value or '').strip().strip('"').strip("'"))
        return number if number > 0 else 0
    except (TypeError, ValueError):
        return 0


def main() -> int:
    print('Chat Monitor AI — offline preflight (Telegram will NOT be started)')
    errors: list[str] = []

    def check(name: str, ok: bool, fix: str = '') -> None:
        print(f"{'OK' if ok else 'MISSING'} — {name}")
        if not ok:
            errors.append(f'{name}: {fix}')

    config = configparser.ConfigParser(interpolation=None)
    local_path = Path('config.ini')
    if not local_path.is_file():
        check('Local config.ini exists', False, 'Create it from config.ini.exemple locally; never commit it.')
        print('\nNot ready: local configuration is missing.')
        return 1
    try:
        config.read(local_path, encoding='utf-8')
    except (OSError, configparser.Error) as error:
        check('Local config.ini readable', False, f'Fix INI syntax ({type(error).__name__}).')
        return 1
    check('Local config.ini readable', True)

    owner_id = _positive_int(config.get('telegram', 'user_id', fallback=''))
    test_id = _positive_int(config.get('monitor', 'test_user_id', fallback=''))
    check('Notification recipient ID set', bool(owner_id), 'Set [telegram] user_id to your bot notification recipient ID.')
    check('Test account ID set', bool(test_id),
          'Set [monitor] test_user_id to the numerical ID of the TEST account that will scan the QR code.')

    raw_chats = config.get('monitor', 'chat_ids', fallback='').strip().strip('"').strip("'")
    items = [s.strip() for s in raw_chats.split(',') if s.strip()]
    try:
        allowed_group = len(items) == 1 and int(items[0], 10) < 0 and items[0] not in ('-0',)
    except (ValueError, IndexError):
        allowed_group = False
    check('Exactly one test group allowlisted', allowed_group,
          'Set [monitor] chat_ids to one negative group ID from /chatid, with no other chats.')

    tdlib_path = config.get('tdlib', 'tdjson_path', fallback='').strip().strip('"')
    check('TDLib library exists', bool(tdlib_path) and Path(tdlib_path).is_file(),
          'Set [tdlib] tdjson_path to your installed libtdjson.so file.')
    check('QR authentication selected', config.get('tdlib', 'auth_mode', fallback='qr').strip('"').lower() == 'qr',
          'Set [tdlib] auth_mode = qr.')

    for name in ('CHAT_MONITOR_BOT_TOKEN', 'CHAT_MONITOR_API_HASH'):
        check(name + ' available', bool(os.environ.get(name)),
              'Configure this Codespaces secret and restart the Codespace.')
    check('CHAT_MONITOR_API_ID valid', bool(_positive_int(os.environ.get('CHAT_MONITOR_API_ID'))),
          'Configure numeric CHAT_MONITOR_API_ID as a Codespaces secret.')
    phone = os.environ.get('CHAT_MONITOR_TEST_PHONE', '')
    check('Test phone secret configured', bool(re.fullmatch(r'\+[1-9]\d{6,14}', phone)),
          'Create CHAT_MONITOR_TEST_PHONE as a Codespaces secret (+countrycode...) and restart.')

    if errors:
        print('\nNOT READY. Fix the items marked MISSING; no login was attempted:')
        for error in errors:
            print('  - ' + error)
        return 1
    print('\nREADY for the owner-controlled QR login. Nothing was started or authorized.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
