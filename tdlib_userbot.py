"""
tdlib_userbot.py

TDLib userbot singleton. Handles:
  - login / auth
  - gift enumeration and upgrade
  - allowlisted message tracking: new / edited / deleted
  - NO automatic forwarding of disappearing messages or media downloads

Uses raw TDLib API via aiotdlib for maximum speed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Set

import aiotdlib.utils as td_utils
import aiotdlib.client as td_client_mod
from aiotdlib import Client, ClientSettings
from aiotdlib.api import API, BaseObject
from aiotdlib.client import Client as _TDClient

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Monkeypatches
# ─────────────────────────────────────────────

_original_parse_td = td_utils.parse_tdlib_object


class UnknownUpdate:
    def __init__(self, data: dict):
        self.data = data
        self.ID = data.get("@type", "unknownUpdate")
        self.EXTRA = data.get("@extra") or {}

    def __getattr__(self, name: str):
        # Allow aiotdlib internals (e.g. client_cache) to access fields like
        # .chat_id, .message_id, etc. directly from the raw data dict.
        try:
            return self.data[name]
        except KeyError:
            raise AttributeError(f"'UnknownUpdate' object has no attribute '{name}'")


def _safe_parse_tdlib_object(data):
    if isinstance(data, dict):
        for key in ("message", "last_message"):
            node = data.get(key)
            if isinstance(node, dict) and node.get("@type") == "message":
                node.setdefault("message_thread_id", 0)
    try:
        obj = _original_parse_td(data)
    except Exception as e:
        if isinstance(data, dict):
            logger.debug("safe_parse: failed for type=%s: %s", data.get("@type"), e)
            return UnknownUpdate(data)
        raise
    if isinstance(obj, dict):
        return UnknownUpdate(obj)
    return obj


_orig_handle_pending = _TDClient._handle_pending_request


async def _safe_handle_pending_request(self: _TDClient, update):
    if not hasattr(update, "EXTRA"):
        logger.debug("Skip pending (no EXTRA): %s", getattr(update, "ID", type(update)))
        return
    try:
        await _orig_handle_pending(self, update)
    except Exception as e:
        logger.debug(
            "Error in _handle_pending_request for %s: %s",
            getattr(update, "ID", type(update)), e,
        )


td_utils.parse_tdlib_object = _safe_parse_tdlib_object
td_client_mod.parse_tdlib_object = _safe_parse_tdlib_object
_TDClient._handle_pending_request = _safe_handle_pending_request


# ─────────────────────────────────────────────
# Fix: td_set_log_message_callback access violation
# ─────────────────────────────────────────────

from aiotdlib.tdjson import CoreTDJson as _CoreTDJson

_orig_core_init = _CoreTDJson.__init__


def _patched_core_init(self, library_path):
    import ctypes
    import pathlib

    self.logger = logging.getLogger("aiotdlib.tdjson")
    library_path = pathlib.Path(library_path).resolve()
    if not library_path.exists():
        raise FileNotFoundError(f"Library path {library_path} does not exist")
    if not library_path.is_file():
        raise IsADirectoryError(f"Library path {library_path} must point to a binary file")

    self.logger.info('Using "%s" TDLib binary', library_path)
    self.library_path = library_path
    self._tdjson = ctypes.CDLL(str(self.library_path))

    self._td_create_client_id = self._tdjson.td_create_client_id
    self._td_create_client_id.restype = ctypes.c_int
    self._td_create_client_id.argtypes = []

    self._td_receive = self._tdjson.td_receive
    self._td_receive.restype = ctypes.c_char_p
    self._td_receive.argtypes = [ctypes.c_double]

    self._td_send = self._tdjson.td_send
    self._td_send.restype = None
    self._td_send.argtypes = [ctypes.c_int, ctypes.c_char_p]

    self._td_execute = self._tdjson.td_execute
    self._td_execute.restype = ctypes.c_char_p
    self._td_execute.argtypes = [ctypes.c_char_p]

    # Official TDLib ABI: (int max_verbosity_level, callback).
    # Keep the callback alive; never emit native account data into logs.
    callback_type = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_char_p)
    self._log_callback = callback_type(lambda level, message: None)
    self._td_set_log_message_callback = self._tdjson.td_set_log_message_callback
    self._td_set_log_message_callback.restype = None
    self._td_set_log_message_callback.argtypes = [ctypes.c_int, callback_type]
    self._td_set_log_message_callback(0, self._log_callback)
    self._td_execute(b'{"@type":"setLogVerbosityLevel","new_verbosity_level":0}')


_CoreTDJson.__init__ = _patched_core_init
logger.debug("CoreTDJson.__init__ patched for DLL compatibility")


# ─────────────────────────────────────────────
# Content helpers
# ─────────────────────────────────────────────

def _as_dict(obj: Any) -> dict:
    """Normalize any TDLib object to a plain dict best-effort."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "data") and isinstance(obj.data, dict):
        return obj.data
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True)
    return {}


def _deep_get(obj: Any, *attrs):
    """Walk a chain of attrs/keys, returning None if any step fails."""
    for a in attrs:
        if obj is None:
            return None
        if isinstance(obj, dict):
            obj = obj.get(a)
        else:
            obj = getattr(obj, a, None)
    return obj


def _extract_text_from_content(content: Any) -> str:
    """Return plain text from any TDLib message content."""
    d = _as_dict(content)
    # messageText → text.text
    t = d.get("text")
    if isinstance(t, dict):
        return t.get("text", "") or ""
    # media with caption
    c = d.get("caption")
    if isinstance(c, dict):
        return c.get("text", "") or ""
    return ""


# Maps TDLib content @type → (media_type_label, path_to_File_object_as_list_of_keys)
# Each entry says: to get the File object, traverse content[key0][key1]...
# For photo we traverse: content → photo → sizes[-1] → photo (which IS the File)
_CONTENT_MEDIA_MAP = {
    # type              label         traverse keys to reach File dict
    "messagePhoto":     ("photo",     None),       # special case: sizes array
    "messageVideo":     ("video",     ["video", "video"]),
    "messageVoiceNote": ("voice",     ["voice_note", "voice"]),
    "messageVideoNote": ("video_note",["video_note", "video"]),
    "messageAnimation": ("animation", ["animation", "animation"]),
    "messageSticker":   ("sticker",   ["sticker", "sticker"]),
    "messageDocument":  ("document",  ["document", "document"]),
    "messageAudio":     ("audio",     ["audio", "audio"]),
}


def _extract_tdlib_file(content: Any) -> tuple[int | None, str]:
    """
    Returns (tdlib_int_file_id, media_type).
    tdlib_int_file_id is the integer 'id' used in downloadFile requests.
    Returns (None, 'text') if no media found.
    """
    d = _as_dict(content)
    ctype = d.get("@type", "")

    entry = _CONTENT_MEDIA_MAP.get(ctype)
    if entry is None:
        return None, "text"

    media_label, keys = entry

    if ctype == "messagePhoto":
        # photo → sizes → last → photo (File)
        photo = d.get("photo", {})
        if isinstance(photo, dict):
            sizes = photo.get("sizes", [])
        else:
            sizes = getattr(photo, "sizes", []) or []
        if not sizes:
            return None, media_label
        last_size = sizes[-1]
        file_obj = _as_dict(last_size).get("photo") or {}
    else:
        # Walk the key path
        node = d
        for k in keys:
            node = (node.get(k) if isinstance(node, dict) else getattr(node, k, None)) or {}
            if not node:
                return None, media_label
        file_obj = node

    file_obj_d = _as_dict(file_obj) if not isinstance(file_obj, dict) else file_obj
    file_id = file_obj_d.get("id")
    if file_id is not None:
        return int(file_id), media_label
    return None, media_label


def _is_self_destruct(msg_dict: dict) -> bool:
    """Return True if message has a self-destruct timer or immediate type."""
    sdt = msg_dict.get("self_destruct_type")
    if not sdt:
        return False
    t = sdt.get("@type", "") if isinstance(sdt, dict) else getattr(sdt, "ID", "") or ""
    return t in ("messageSelfDestructTypeTimer", "messageSelfDestructTypeImmediately")


# ─────────────────────────────────────────────
# TdlibUserbot Class
# ─────────────────────────────────────────────

class TdlibUserbot:
    """
    Thin wrapper around aiotdlib.Client.
    Call start() once at bot startup. All methods are async-safe.
    """

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        phone_number: str,
        password: str,
        tdjson_path: str,
    ):
        self._settings = ClientSettings(
            api_id=api_id,
            api_hash=api_hash,
            phone_number=phone_number,
            library_path=tdjson_path,
            password=password,
            files_directory=os.path.abspath(".chat-monitor-test"),
            use_secret_chats=False,
            use_message_database=False,
            use_chat_info_database=False,
            use_file_database=False,
        )
        self.client: Client | None = None
        self._tracking_ready = False
        self.expected_test_user_id = None

    @staticmethod
    def _make_tty_input(prompt: str, *, secured: bool = False) -> str:
        """
        Read a line from the controlling terminal directly.
        Works on macOS/Linux even when sys.stdin is redirected or buffered oddly.
        Falls back to sys.stdin on Windows (no os.ctermid there).
        """
        import sys
        import os

        # Flush all outputs first so the prompt is visible
        sys.stdout.write(f"\n{prompt} ")
        sys.stdout.flush()
        try:
            sys.stderr.flush()
        except Exception:
            pass

        try:
            if secured:
                import getpass
                return getpass.getpass(prompt="")
            # Open the controlling terminal directly (works on macOS/Linux)
            tty_path = os.ctermid()  # '/dev/tty' on Unix
            with open(tty_path, "r") as tty:
                return tty.readline().strip()
        except (AttributeError, OSError):
            # Windows or no controlling terminal — fall back to sys.stdin
            return sys.stdin.readline().strip()

    def _patch_auth_input(self, use_qr: bool = True) -> None:
        """
        Monkey-patch aiotdlib Client auth helpers so input() works on macOS.
        use_qr=True  → intercept WaitPhoneNumber and request QR auth instead.
        use_qr=False → use normal phone number / SMS code flow.
        Must be called after self.client is created but before __aenter__.
        """
        import asyncio
        from types import MethodType

        client = self.client
        make_tty = self._make_tty_input

        async def _patched_auth_get_code(self_, *, code_type: str = "SMS") -> str:  # noqa: N807
            code = ""
            while len(code) != 5 or not code.isdigit():
                code = await asyncio.to_thread(make_tty, f"Enter {code_type} code:")
            return code

        async def _patched_auth_get_password(self_) -> str:  # noqa: N807
            password = self_.settings.password
            if not bool(password):
                return await asyncio.to_thread(make_tty, "Enter 2FA password:", secured=True)
            return password.get_secret_value()

        client._auth_get_code = MethodType(_patched_auth_get_code, client)
        client._auth_get_password = MethodType(_patched_auth_get_password, client)

        # ── Patch auth state handler to log every state and handle QR flow ──
        _orig_on_auth_state = client._on_authorization_state_update

        async def _patched_on_auth_state(self_, authorization_state) -> None:  # noqa: N807
            import sys
            state_id = getattr(authorization_state, "ID", type(authorization_state).__name__)
            logger.info("TDLib auth state: %s", state_id)
            if getattr(self, "_resume_only", False) and state_id in {"authorizationStateWaitPhoneNumber", "authorizationStateWaitCode", "authorizationStateWaitPassword", "authorizationStateWaitOtherDeviceConfirmation", "authorizationStateWaitEmailAddress", "authorizationStateWaitEmailCode", "authorizationStateWaitRegistration"}:
                if not self._auth_needed.done():
                    self._auth_needed.set_result(True)
                return

            # QR mode: intercept phone-number request → switch to QR auth
            if use_qr and state_id == "authorizationStateWaitPhoneNumber":
                logger.info("auth_mode=qr: requesting QR code authentication")
                try:
                    from aiotdlib.api import RequestQrCodeAuthentication
                    await self_.send(RequestQrCodeAuthentication(other_user_ids=[]))
                except Exception as e:
                    raise RuntimeError("QR authentication failed; phone fallback is disabled") from None
                return

            # Handle QR link (both modes: TDLib 1.8+ may send this regardless)
            if state_id == "authorizationStateWaitOtherDeviceConfirmation":
                link = getattr(authorization_state, "link", "") or ""
                if link:
                    TdlibUserbot._print_qr(link)
                else:
                    sys.stdout.write("\n📱 QR login requested but no link received from TDLib.\n")
                    sys.stdout.flush()
                return  # TDLib sends READY when scanned — no further action needed

            await _orig_on_auth_state(authorization_state)

        client._on_authorization_state_update = MethodType(_patched_on_auth_state, client)
        logger.debug("Auth input patched (use_qr=%s)", use_qr)

    @staticmethod
    def _print_qr(link: str) -> None:
        """Print QR code + link to stdout. Clears terminal first so refreshed codes replace the old one."""
        import sys
        import os
        from datetime import datetime

        # Clear terminal so each refresh replaces the previous QR
        if sys.stdout.isatty():
            os.system("cls" if os.name == "nt" else "clear")

        refreshed_at = datetime.now().strftime("%H:%M:%S")
        sys.stdout.write(f"\n📱 Scan in Telegram → Settings → Devices → Link Desktop Device  [{refreshed_at}]\n\n")

        try:
            import qrcode
            qr = qrcode.QRCode(border=1)
            qr.add_data(link)
            qr.make(fit=True)
            qr.print_ascii(out=sys.stdout, invert=True)
            sys.stdout.write("\n")
        except ImportError:
            pass  # qrcode not installed — just show the link below

        # Always print the raw link so user can open/copy it if QR is inconvenient
        # Authentication links must never be printed into persistent logs.
        sys.stdout.write("⏳ QR refreshes automatically every ~30s. Scan whenever ready.\n")
        sys.stdout.flush()

    async def start(self, use_qr: bool = True, on_client_created=None, allow_authorization=True) -> None:
        """Start TDLib client (triggers auth on first run).

        Args:
            use_qr: If True (default), use QR-code auth. If False, use phone/SMS code.
        """
        if not self.expected_test_user_id or self.expected_test_user_id <= 0:
            raise RuntimeError("An explicit test account ID is required")
        self._tracking_ready = False
        self.client = Client(settings=self._settings)
        if on_client_created is not None:
            on_client_created()  # Set allowlist handlers before authentication.
        self._patch_auth_input(use_qr=use_qr)
        self._resume_only = not allow_authorization
        self._auth_needed = asyncio.get_running_loop().create_future()
        self._ctx = self.client.__aenter__()
        entering = asyncio.create_task(self._ctx)
        try:
            if self._resume_only:
                done, _ = await asyncio.wait({entering, self._auth_needed}, timeout=45,
                                             return_when=asyncio.FIRST_COMPLETED)
                if self._auth_needed in done:
                    raise RuntimeError("TEST_LOGIN_REQUIRED: authorize the test account interactively")
                if entering not in done:
                    raise RuntimeError("TEST_SESSION_TIMEOUT: no ready session within 45 seconds")
            await entering
        finally:
            if not entering.done():
                entering.cancel()
                await asyncio.gather(entering, return_exceptions=True)
            self._auth_needed.cancel()
        me = await self.client.api.get_me()
        if me.id != self.expected_test_user_id:
            await self.stop()
            raise RuntimeError("Account mismatch: monitoring refused")
        self._tracking_ready = True
        logger.info("Test account verified; allowlisted tracking enabled")

        # Stay offline: undo the online=True that aiotdlib sets during auth
        try:
            await self._raw_request({
                "@type": "setOption",
                "name": "online",
                "value": {"@type": "optionValueBoolean", "value": False},
            })
            logger.info("Account set to offline mode")
        except Exception as e:
            logger.warning("Failed to set offline mode: %s", e)

    async def stop(self) -> None:
        """Gracefully close TDLib client."""
        self._tracking_ready = False
        if self.client:
            try:
                await self.client.__aexit__(None, None, None)
                logger.info("TDLib userbot stopped.")
            except Exception as e:
                logger.warning("Error stopping TDLib userbot: %s", e)

    # ─────────────────────────────────────────
    # Raw request helper
    # ─────────────────────────────────────────

    async def _raw_request(self, query: dict, timeout: float = 15.0) -> dict:
        """Send a raw TDLib JSON request and await the matching response."""
        import uuid
        import ujson

        request_id = uuid.uuid4().hex
        query["@extra"] = {"request_id": request_id}

        pending = asyncio.get_event_loop().create_future()

        class _FakePR:
            update = None
            def set_update(self, upd):
                self.update = upd
                if not pending.done():
                    pending.set_result(upd)

        self.client._pending_requests[request_id] = _FakePR()  # type: ignore
        await self.client.tdjson_client.send(ujson.dumps(query, ensure_ascii=False))

        try:
            result = await asyncio.wait_for(pending, timeout=timeout)
        except asyncio.TimeoutError:
            self.client._pending_requests.pop(request_id, None)
            raise TimeoutError(f"TDLib request timed out: {query.get('@type')}")

        if isinstance(result, dict):
            return result
        if hasattr(result, "data"):
            return result.data
        if hasattr(result, "model_dump"):
            return result.model_dump(by_alias=True)
        return {}

    # ─────────────────────────────────────────
    # File download helper
    # ─────────────────────────────────────────

    async def _download_file(self, tdlib_file_id: int, timeout: float = 30.0) -> str | None:
        """
        Download a file by its TDLib integer file ID.
        Returns the local filesystem path when complete, or None on failure.
        """
        result = await self._raw_request(
            {
                "@type": "downloadFile",
                "file_id": tdlib_file_id,
                "priority": 32,        # highest priority
                "synchronous": True,   # wait until fully downloaded
            },
            timeout=timeout,
        )

        if result.get("@type") == "error":
            logger.warning("downloadFile(%d) error: %s", tdlib_file_id, result.get("message"))
            return None

        local = result.get("local", {})
        if isinstance(local, dict):
            if local.get("is_downloading_completed"):
                path = local.get("path", "")
                return path if path else None
            else:
                logger.warning("downloadFile(%d): not fully downloaded yet", tdlib_file_id)
                return None

        # aiotdlib Pydantic model
        local_obj = result.get("local") if isinstance(result, dict) else getattr(result, "local", None)
        if local_obj:
            completed = getattr(local_obj, "is_downloading_completed", False)
            path = getattr(local_obj, "path", "")
            if completed and path:
                return path
        return None

    # ─────────────────────────────────────────
    # User info helper
    # ─────────────────────────────────────────

    async def _get_user_fullname(self, user_id: int) -> str:
        """
        Resolve a Telegram user_id to a human-readable name via TDLib getUser.
        Returns "FirstName LastName (@username)" where available.
        Falls back to str(user_id) if the request fails.
        """
        try:
            result = await self._raw_request(
                {"@type": "getUser", "user_id": user_id},
                timeout=10.0,
            )
            if result.get("@type") == "error":
                return str(user_id)
            first = result.get("first_name") or ""
            last  = result.get("last_name")  or ""
            uname = result.get("username")   or ""
            # Try usernames list as well (TDLib 1.8+)
            if not uname:
                usernames = result.get("usernames") or {}
                if isinstance(usernames, dict):
                    active = usernames.get("active_usernames") or []
                    if active:
                        uname = active[0]
            fullname = " ".join(filter(None, [first, last]))
            if uname:
                fullname = f"{fullname} (@{uname})" if fullname else f"@{uname}"
            return fullname or str(user_id)
        except Exception as e:
            logger.debug("_get_user_fullname(%d) failed: %s", user_id, e)
            return str(user_id)

    async def _is_bot_user(self, user_id: int) -> bool:
        """
        Return True if user_id belongs to a bot (userTypeBot).
        Returns False on any error (fail-open: don't skip if uncertain).
        """
        try:
            result = await self._raw_request(
                {"@type": "getUser", "user_id": user_id},
                timeout=10.0,
            )
            if result.get("@type") == "error":
                return False
            user_type = result.get("type") or {}
            utype = (
                user_type.get("@type") if isinstance(user_type, dict)
                else getattr(user_type, "@type", None) or getattr(user_type, "ID", None)
            )
            return utype == "userTypeBot"
        except Exception as e:
            logger.debug("_is_bot_user(%d) failed: %s", user_id, e)
            return False

    # ─────────────────────────────────────────
    # Message tracking handlers
    # ─────────────────────────────────────────

    def register_message_tracking_handlers(
        self,
        aiogram_bot,
        notify_user_id: int,
        timezone_local,
        messagesx_cls,
        watched_chat_ids: Set[int] | None = None,
        all_chats: bool = False,
        excluded_chat_ids: Set[int] | None = None,
    ) -> None:
        """Monitor explicitly allowed chats only.

        The dedicated SQLite store is keyed by (chat_id, message_id), with
        author_id saved separately. No auto-download or forwarding of media,
        disappearing messages, or messages from unlisted chats.
        """
        from datetime import datetime, timezone
        from html import escape

        client = self.client
        if client is None:
            raise RuntimeError("Call after TDLib Client creation")
        allowed = frozenset(watched_chat_ids or ())
        excluded = frozenset(excluded_chat_ids or ()) | {777000}
        if not allowed and not all_chats:
            logger.warning("No allowed chat IDs; all message updates will be ignored")

        # Both updateMessageEdited and updateMessageContent may occur for a
        # single edit. Serial processing and comparing contents deduplicates.
        change_lock = asyncio.Lock()

        def permitted(chat_id) -> bool:
            return (self._tracking_ready and type(chat_id) is int and chat_id != 0
                    and chat_id not in excluded and (all_chats or chat_id in allowed))

        def display_text(content) -> tuple[str, str]:
            text = _extract_text_from_content(content)
            _fid, kind = _extract_tdlib_file(content)
            if kind != "text":
                text = f"[{kind}] {text}".strip()
            return text, kind

        def author_from_message(msg: dict) -> int | None:
            sender = _as_dict(msg.get("sender_id"))
            # TDLib represents user senders as messageSenderUser.
            kind = sender.get("@type") or sender.get("ID")
            if all_chats and kind == "messageSenderChat":
                sender_chat = sender.get("chat_id")
                return sender_chat if type(sender_chat) is int and sender_chat < 0 else None
            if kind and kind != "messageSenderUser":
                return None  # chat/anonymous sender: never misattribute
            uid = sender.get("user_id")
            if isinstance(uid, int) and uid > 0:
                return uid
            return None

        def display_timestamp(iso: str) -> str:
            try:
                return datetime.fromisoformat(iso).astimezone(timezone_local).strftime("%d.%m.%Y %H:%M")
            except (TypeError, ValueError):
                return "unknown"

        async def notify(*, chat_id: int, old: str, new: str | None, record) -> None:
            # Escape all user-controlled text to prevent Telegram HTML failures.
            old = escape((old or "(empty)")[:600] + ("…" if len(old or "") > 600 else ""))
            author = escape((record.author_name or str(record.author_id))[:100])
            prefix = (
                f"{'✏️ Edited' if new is not None else '🗑 Deleted'} message\n"
                f"Chat: <code>{chat_id}</code>\n"
                f"Author: {author} (<code>{record.author_id}</code>)\n"
                f"Original time: {display_timestamp(record.timestamp)}\n"
            )
            if new is None:
                content = f"Before deletion: {old}"
            else:
                content = f"Before: {old}\nAfter: {escape((new or '(empty)')[:600] + ('…' if len(new or '') > 600 else ''))}"
            msg = prefix + content
            try:
                await aiogram_bot.send_message(notify_user_id, msg, parse_mode="HTML")
            except Exception:
                logger.warning("Unable to deliver notification; stored version retained")
                raise RuntimeError("Notification delivery failed") from None

        @client.on_event(API.Types.UPDATE_NEW_MESSAGE)
        async def on_new_message(_client, update):
            try:
                msg = getattr(update, "message", None)
                if msg is None:
                    msg = _as_dict(update).get("message")
                message = _as_dict(msg)
                chat_id = message.get("chat_id")
                msg_id = message.get("id")
                if not permitted(chat_id) or not isinstance(msg_id, int) or not msg_id:
                    return
                # Disappearing messages are excluded, not forwarded or stored.
                if _is_self_destruct(message):
                    return
                author_id = author_from_message(message)
                if author_id is None:
                    logger.debug("Ignoring non-user sender in chat %s", chat_id)
                    return
                async with change_lock:
                    if author_id > 0 and not all_chats and await self._is_bot_user(author_id):
                        return
                    text, media_type = display_text(message.get("content") or {})
                    date = message.get("date")
                    stamp = datetime.fromtimestamp(date, tz=timezone.utc) if isinstance(date, (int, float)) else datetime.now(timezone.utc)
                    # Name lookup is only performed after allowlist approval.
                    name = (await self._get_user_fullname(author_id) if author_id > 0
                            else f"Chat sender {author_id}")
                    added = messagesx_cls.add(
                        chat_id=chat_id, message_id=msg_id, author_id=author_id,
                        author_name=name, message_text=text,
                        timestamp=stamp.isoformat(), media_type=media_type,
                    )
                    if added:
                        logger.info("Stored message chat=%s msg=%s", chat_id, msg_id)
            except Exception:
                logger.warning("on_new_message failed")

        async def process_changed(update, *, new_content=None):
            try:
                d = _as_dict(update)
                chat_id = getattr(update, "chat_id", None) or d.get("chat_id")
                msg_id = getattr(update, "message_id", None) or d.get("message_id")
                if not permitted(chat_id) or not isinstance(msg_id, int) or not msg_id:
                    return
                async with change_lock:
                    record = messagesx_cls.get(chat_id=chat_id, message_id=msg_id)
                    if record is None:
                        return
                    if new_content is None:
                        raw_msg = await self._raw_request({
                            "@type": "getMessage", "chat_id": chat_id,
                            "message_id": msg_id,
                        })
                        if raw_msg.get("@type") == "error":
                            return
                        new_content = raw_msg.get("content")
                    if new_content is None:
                        return
                    new_text, kind = display_text(new_content)
                    if new_text == record.message_text and kind == record.media_type:
                        return  # edit-date and content updates can repeat
                    await notify(chat_id=chat_id, old=record.message_text,
                                 new=new_text, record=record)
                    messagesx_cls.update(chat_id=chat_id, message_id=msg_id,
                                         message_text=new_text, media_type=kind)
                    logger.info("Message edited chat=%s msg=%s", chat_id, msg_id)
            except Exception:
                logger.warning("process_changed failed")

        @client.on_event(API.Types.UPDATE_MESSAGE_EDITED)
        async def on_edited(_client, update):
            await process_changed(update)

        # TDLib often publishes actual text changes as updateMessageContent.
        content_update_type = getattr(API.Types, "UPDATE_MESSAGE_CONTENT", None)
        if content_update_type is not None:
            @client.on_event(content_update_type)
            async def on_content_changed(_client, update):
                d = _as_dict(update)
                content = getattr(update, "new_content", None)
                if content is None:
                    content = d.get("new_content")
                await process_changed(update, new_content=content)
        else:
            logger.warning("UPDATE_MESSAGE_CONTENT is unavailable in aiotdlib; edit coverage may be limited")

        @client.on_event(API.Types.UPDATE_DELETE_MESSAGES)
        async def on_deleted(_client, update):
            try:
                d = _as_dict(update)
                chat_id = getattr(update, "chat_id", None) or d.get("chat_id")
                if not permitted(chat_id):
                    return
                permanent = getattr(update, "is_permanent", None)
                if permanent is None:
                    permanent = d.get("is_permanent")
                if permanent is not True:
                    return  # uncertain/cache-only deletions are ignored
                ids = getattr(update, "message_ids", None)
                if ids is None:
                    ids = d.get("message_ids", [])
                async with change_lock:
                    for msg_id in ids or []:
                        if not isinstance(msg_id, int):
                            continue
                        record = messagesx_cls.get(chat_id=chat_id, message_id=msg_id)
                        if record is None:
                            continue
                        await notify(chat_id=chat_id, old=record.message_text,
                                     new=None, record=record)
                        messagesx_cls.delete(chat_id=chat_id, message_id=msg_id)
                        logger.info("Message deleted chat=%s msg=%s", chat_id, msg_id)
            except Exception:
                logger.warning("on_deleted failed")

        logger.info("Registered TDLib handlers; scope=%s", "all chats" if all_chats else len(allowed))


# ─────────────────────────────────────────────
# Global Singleton
# ─────────────────────────────────────────────

_userbot: TdlibUserbot | None = None


def init_userbot(
    api_id: int,
    api_hash: str,
    phone_number: str,
    password: str,
    tdjson_path: str,
) -> TdlibUserbot:
    global _userbot
    _userbot = TdlibUserbot(api_id, api_hash, phone_number, password, tdjson_path)
    return _userbot


def get_userbot() -> TdlibUserbot:
    if _userbot is None:
        raise RuntimeError("TDLib userbot not initialized. Call init_userbot() first.")
    return _userbot
