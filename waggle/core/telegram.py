"""Telegram Bot API client for the core: send and edit messages directly, stdlib only.

One keep-alive connection per BotApi and thread, IPv4 first. Every failure is a TelegramError whose `kind`
tells the outbox what to do: retryable (try again later), rate_limited (wait `retry_after`), permanent and
auth (stop, retrying cannot help), conflict (another getUpdates consumer). The bot token lives in the request
path, so it is scrubbed from every message this module produces and no exception is chained to the one that
carried the URL.
"""
import http.client
import json
import logging
import mimetypes
import os
import re
import socket
import ssl
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

log = logging.getLogger("waggle.telegram")

API_BASE = "https://api.telegram.org"
MAX_TEXT = 4096
MAX_CAPTION = 1024
TOKEN_KEY = "TELEGRAM_BOT_TOKEN"
HOME_KEY = "TELEGRAM_HOME_CHANNEL"

KINDS = ("retryable", "rate_limited", "permanent", "auth", "conflict")

_TOKEN_SHAPES = (
    re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}"),
)


def redact(text, token):
    """`text` with the bot token (and anything shaped like one) replaced, so it is safe to log or raise."""
    text = "" if text is None else str(text)
    if token:
        token = str(token)
        text = text.replace(token, "<token>")
        text = text.replace(quote(token, safe=""), "<token>")
    for shape in _TOKEN_SHAPES:
        text = shape.sub("<token>", text)
    return text


class TelegramError(Exception):
    def __init__(self, kind, description="", *, status=None, retry_after=None, sent=None):
        if kind not in KINDS:
            raise ValueError(f"unknown TelegramError kind {kind!r}")
        self.kind = kind
        self.status = status
        self.description = redact(description, None)
        self.retry_after = None if retry_after is None else float(retry_after)
        self.permanent = kind in ("permanent", "auth")
        self.sent = list(sent or [])  # message ids already delivered before a later part failed
        head = f"telegram {kind}" + (f" ({status})" if status is not None else "")
        super().__init__(f"{head}: {self.description}" if self.description else head)


def default_env_file():
    return Path.home() / ".hermes" / ".env"


def _read_env_file(path):
    out = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        out[key.strip()] = value
    return out


def _lookup(key, env_file):
    value = (os.environ.get(key) or "").strip()
    if value:
        return value
    path = env_file if env_file is not None else default_env_file()
    return (_read_env_file(path).get(key) or "").strip() or None


def load_token(env_file=None):
    token = _lookup(TOKEN_KEY, env_file)
    if not token:
        raise TelegramError("auth", f"{TOKEN_KEY} is not set in the environment or the env file")
    return token


def home_chat(env_file=None):
    return _lookup(HOME_KEY, env_file)


def split_text(text, limit=MAX_TEXT):
    """Cut `text` into parts of at most `limit` characters, at a paragraph break, else a line break, else a
    space, else mid-word. No non-whitespace text is lost and no part is empty; a part that would hold only
    whitespace is dropped.
    A boundary in the first quarter of the window is passed over so a stray early break does not leave a
    tiny part. Markdown/HTML entities that straddle a cut are the caller's problem: send_message falls
    back to plain text when Telegram cannot parse a part."""
    text = "" if text is None else str(text)
    if limit < 1:
        raise ValueError("limit must be at least 1")
    parts = []
    rest = text
    floor = limit // 4
    while len(rest) > limit:
        window = rest[:limit]
        end = 0
        for sep in ("\n\n", "\n", " "):
            at = window.rfind(sep)
            if at > floor and window[:at].strip():
                end = at + len(sep)
                break
        if not end:
            end = limit
        parts.append(rest[:end])
        rest = rest[end:]
    if rest:
        parts.append(rest)
    return [p for p in parts if p.strip()]


def _connect_ipv4_first(address, timeout=None, source_address=None):
    """socket.create_connection that resolves A records first and only then tries AAAA, within one overall
    connect budget."""
    host, port = address
    deadline = None if timeout is None else time.monotonic() + timeout
    last = None
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
        except socket.gaierror as e:
            last = e
            continue
        for fam, typ, proto, _canon, sockaddr in infos:
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                raise TimeoutError("connect timed out")
            sock = socket.socket(fam, typ, proto)
            try:
                sock.settimeout(left)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as e:
                sock.close()
                last = e
    raise last or OSError("no address to connect to")


def _multipart(fields, files):
    """multipart/form-data by hand. fields: {name: str}; files: {name: (filename, bytes, content_type)}."""
    while True:
        boundary = uuid.uuid4().hex
        tag = boundary.encode()
        if not any(tag in data for _n, (_f, data, _c) in files.items()):
            break
    out = []
    for name, value in fields.items():
        out.append(b"--" + tag + b"\r\n")
        out.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.append(str(value).encode("utf-8") + b"\r\n")
    for name, (filename, data, ctype) in files.items():
        safe = filename.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
        out.append(b"--" + tag + b"\r\n")
        out.append(f'Content-Disposition: form-data; name="{name}"; filename="{safe}"\r\n'
                   f"Content-Type: {ctype}\r\n\r\n".encode("utf-8"))
        out.append(data + b"\r\n")
    out.append(b"--" + tag + b"--\r\n")
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def _chat_id(chat):
    s = str(chat).strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def _is_parse_failure(err):
    d = err.description.lower()
    return err.status == 400 and ("parse entities" in d or "find end of the entity" in d
                                  or "unsupported start tag" in d)


def _kind_for(status):
    if status == 429:
        return "rate_limited"
    if status in (401, 404):
        return "auth"
    if status == 409:
        return "conflict"
    if status < 400 or status >= 500:
        return "retryable"
    return "permanent"


class BotApi:
    def __init__(self, token=None, *, base=API_BASE, connect_timeout=3.0, read_timeout=30.0):
        self._token = token.strip() if isinstance(token, str) and token.strip() else None
        parts = urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("base must be an http(s) URL")
        self.base = base.rstrip("/")
        self._scheme = parts.scheme
        self._host = parts.hostname
        self._port = parts.port
        self._prefix = parts.path.rstrip("/")
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._local = threading.local()
        self._ssl = None

    def __repr__(self):
        return f"BotApi(base={self.base!r}, token={'<set>' if self._token else '<not loaded>'})"

    def get_me(self):
        return self._call("getMe")

    def send_message(self, chat_id, text, *, parse_mode=None, reply_to=None, disable_preview=True):
        parts = split_text(text)
        if not parts:
            raise TelegramError("permanent", "message text is empty")
        ids = []
        for n, part in enumerate(parts):
            payload = {"chat_id": _chat_id(chat_id), "text": part}
            if disable_preview:
                payload["link_preview_options"] = {"is_disabled": True}
            if reply_to is not None and n == 0:
                payload["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
            try:
                try:
                    result = self._call("sendMessage", {**payload, **({"parse_mode": parse_mode} if parse_mode else {})})
                except TelegramError as e:
                    if not (parse_mode and _is_parse_failure(e)):
                        raise
                    log.info("telegram sendMessage: entities rejected, resending as plain text")
                    result = self._call("sendMessage", payload)
            except TelegramError as e:
                e.sent = ids
                raise
            ids.append(int(result["message_id"]))
        return ids

    def edit_message_text(self, chat_id, message_id, text, *, parse_mode=None):
        payload = {"chat_id": _chat_id(chat_id), "message_id": int(message_id), "text": text}
        try:
            try:
                self._call("editMessageText", {**payload, **({"parse_mode": parse_mode} if parse_mode else {})})
            except TelegramError as e:
                if not (parse_mode and _is_parse_failure(e)):
                    raise
                self._call("editMessageText", payload)
        except TelegramError as e:
            if e.status == 400 and "message is not modified" in e.description.lower():
                return True
            raise
        return True

    def send_chat_action(self, chat_id, action="typing"):
        self._call("sendChatAction", {"chat_id": _chat_id(chat_id), "action": action})
        return True

    def send_voice(self, chat_id, path, *, caption=None, duration=None, reply_to=None):
        extra = {} if duration is None else {"duration": int(duration)}
        return self._send_file("sendVoice", "voice", chat_id, path, caption, reply_to, extra)

    def send_document(self, chat_id, path, *, caption=None, reply_to=None):
        return self._send_file("sendDocument", "document", chat_id, path, caption, reply_to, {})

    def get_updates(self, offset=None, *, timeout=25, limit=100):
        payload = {"timeout": int(timeout), "limit": int(limit)}
        if offset is not None:
            payload["offset"] = int(offset)
        return self._call("getUpdates", payload, read_timeout=max(self.read_timeout, timeout + 10))

    def close(self):
        """Close this thread's connection."""
        self._drop()

    def _send_file(self, method, field, chat_id, path, caption, reply_to, extra):
        p = Path(path)
        try:
            data = p.read_bytes()
        except OSError:
            raise TelegramError("permanent", f"cannot read file {p.name}") from None
        fields = {"chat_id": str(_chat_id(chat_id)), **{k: str(v) for k, v in extra.items()}}
        if caption:
            fields["caption"] = caption
        if reply_to is not None:
            fields["reply_parameters"] = json.dumps({"message_id": int(reply_to), "allow_sending_without_reply": True})
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        result = self._call(method, files=({field: (p.name, data, ctype)}, fields))
        return int(result["message_id"])

    def _get_token(self):
        if self._token is None:
            self._token = load_token()
        if re.search(r"[\s/?#]", self._token):
            raise TelegramError("auth", "the bot token is malformed")
        return self._token

    def _call(self, method, params=None, *, files=None, read_timeout=None):
        token = self._get_token()
        if files:
            file_parts, fields = files
            body, ctype = _multipart(fields, file_parts)
        else:
            body, ctype = json.dumps(params or {}, ensure_ascii=False).encode("utf-8"), "application/json"
        path = f"{self._prefix}/bot{token}/{method}"
        headers = {"Content-Type": ctype, "Accept": "application/json", "Connection": "keep-alive"}
        try:
            status, resp_headers, data = self._exchange(method, path, body, headers,
                                                        read_timeout or self.read_timeout, token)
            return self._interpret(method, status, resp_headers, data, token)
        except TelegramError as e:
            log.info("telegram %s failed: %s", method, e)
            raise

    def _new_conn(self):
        if self._scheme == "https":
            if self._ssl is None:
                self._ssl = ssl.create_default_context()
            conn = http.client.HTTPSConnection(self._host, self._port, timeout=self.connect_timeout,
                                               context=self._ssl)
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self.connect_timeout)
        conn._create_connection = _connect_ipv4_first
        return conn

    def _drop(self):
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def _exchange(self, method, path, body, headers, read_timeout, token):
        failure = None
        for attempt in (0, 1):
            conn = getattr(self._local, "conn", None)
            if conn is None:
                conn = self._local.conn = self._new_conn()
            reused = conn.sock is not None
            try:
                if conn.sock is None:
                    conn.connect()
                conn.sock.settimeout(read_timeout)
                conn.request("POST", path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                if resp.will_close:
                    self._drop()
                return resp.status, resp.headers, data
            except http.client.InvalidURL:
                self._drop()
                failure = TelegramError("auth", "the bot token is malformed")
                break
            except TimeoutError:
                self._drop()
                failure = TelegramError("retryable", f"{method} timed out")
                break
            except (OSError, http.client.HTTPException) as e:
                self._drop()
                # A dead keep-alive connection fails before any response; a fresh one that fails is a real outage.
                if attempt == 0 and reused:
                    continue
                failure = TelegramError("retryable", redact(f"{method} network error: {type(e).__name__}: {e}", token))
                break
        raise failure

    def _interpret(self, method, status, headers, data, token):
        try:
            payload = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeError):
            payload = None
        if not isinstance(payload, dict):
            payload = None
        if status == 200 and payload is not None and payload.get("ok"):
            return payload.get("result")
        if status == 200 and payload is None:
            raise TelegramError("retryable", f"{method} answered with something that is not JSON", status=status)
        description = redact((payload or {}).get("description") or f"HTTP {status}", token)
        retry_after = ((payload or {}).get("parameters") or {}).get("retry_after")
        if retry_after is None and status == 429:
            retry_after = headers.get("Retry-After")
        try:
            retry_after = None if retry_after is None else float(retry_after)
        except (TypeError, ValueError):
            retry_after = None
        if status == 200:
            status = (payload or {}).get("error_code") or 400
        raise TelegramError(_kind_for(status), description, status=status, retry_after=retry_after)


_default = None
_default_lock = threading.Lock()


def default_api():
    global _default
    with _default_lock:
        if _default is None:
            _default = BotApi()
        return _default


def send_row(row, api=None):
    """Deliver one outbox row; returns the provider message id (the last part's, for long text)."""
    kind = row.get("kind")
    if kind not in ("text", "voice", "file"):
        raise TelegramError("permanent", f"no Telegram transport for kind {kind!r}")
    meta = row.get("meta") or {}
    chat = str(row.get("chat") or "").strip() or home_chat()
    if not chat:
        raise TelegramError("permanent", "no chat on the row and no home chat configured")
    api = api or default_api()
    body = row.get("body") or ""
    parse_mode = meta.get("parse_mode")
    reply_to = meta.get("reply_to")
    if kind == "text":
        return str(api.send_message(chat, body, parse_mode=parse_mode, reply_to=reply_to)[-1])

    file = row.get("file")
    if not file or not Path(file).is_file():
        if meta.get("text_fallback"):
            return str(api.send_message(chat, body, parse_mode=parse_mode, reply_to=reply_to)[-1])
        raise TelegramError("permanent", f"file for the {kind} message is missing")
    long_caption = len(body) > MAX_CAPTION
    caption = None if long_caption else (body or None)
    if kind == "voice":
        msg_id = api.send_voice(chat, file, caption=caption, duration=meta.get("duration"), reply_to=reply_to)
    else:
        msg_id = api.send_document(chat, file, caption=caption, reply_to=reply_to)
    if long_caption:
        api.send_message(chat, body, parse_mode=parse_mode)
    return str(msg_id)
