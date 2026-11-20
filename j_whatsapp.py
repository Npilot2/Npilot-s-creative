#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J в WhatsApp (через официальный WhatsApp Cloud API / Meta Graph API).
Работает на стандартной библиотеке Python (сторонние пакеты не нужны).

ПО УМОЛЧАНИЮ WhatsApp-бот — «тонкий клиент»: он передаёт сообщения и медиафайлы ядру J
(python j_core.py или python j_launcher.py start core), поэтому память, самомодель и знания
у J единые с Discord, Telegram и играми (профиль wa:<номер_телефона>).
Встроенный режим (без ядра): J_WHATSAPP_EMBEDDED=1.

Настройка в j.env:
    WHATSAPP_TOKEN=EAA...                 # токен доступа Meta Graph API
    WHATSAPP_PHONE_ID=123456789012345     # Phone Number ID из WhatsApp → API Setup
    WHATSAPP_VERIFY_TOKEN=my_secret_word  # произвольная строка для проверки webhook в Meta
    WHATSAPP_APP_SECRET=                  # необязательно: App Secret для проверки X-Hub-Signature-256
    WHATSAPP_PORT=8788                    # локальный порт webhook-сервера (проксируется через ngrok/туннель)
    J_WHATSAPP_ALLOWED=                   # необязательно: номера через запятую (пусто = отвечает всем)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from j_discord import JDiscordCore, classify_attachment, load_env

BASE = Path(__file__).resolve().parent
WA_MAX_LEN = 4000
_MEDIA_MAX_BYTES = 8 * 1024 * 1024
GRAPH_BASE = "https://graph.facebook.com/v21.0"


def resolve_mode(env: dict | None = None) -> str:
    """Определяет режим WhatsApp: 'web' (QR-код без токена) или 'cloud' (Meta Graph API)."""
    cfg = os.environ if env is None else env
    mode = (cfg.get("WHATSAPP_MODE") or "auto").strip().lower()
    if mode in ("web", "cloud"):
        return mode
    has_cloud = bool((cfg.get("WHATSAPP_TOKEN") or "").strip() and (cfg.get("WHATSAPP_PHONE_ID") or "").strip())
    return "cloud" if has_cloud else "web"


def chunks(text: str, size: int = WA_MAX_LEN):
    while text:
        yield text[:size]
        text = text[size:]


def ask_core(
    url: str,
    uid: str,
    name: str,
    text: str,
    timeout: float = 60.0,
    media: list[dict] | None = None,
) -> str:
    """Отправляет сообщение ядру J и возвращает ответ (профиль wa:<uid>)."""
    body: dict = {"user": name, "id": uid, "text": text, "game": "whatsapp"}
    if media:
        body["media"] = media
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/chat", payload, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("reply", "") or "…"
    except Exception as exc:
        return (
            "Ядро J не отвечает. Запусти его: python j_launcher.py start core "
            f"(адрес {url}; причина: {exc})"
        )


def _norm_phone(raw: str) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


class WhatsAppBot:
    """Обработчик входящих сообщений WhatsApp Cloud API с поддержкой текста и медиа."""

    def __init__(
        self,
        token: str,
        phone_id: str,
        verify_token: str = "",
        app_secret: str = "",
        core_url: str = "http://127.0.0.1:8765",
        embedded: bool = False,
        allowed: set[str] | None = None,
        core: JDiscordCore | None = None,
    ) -> None:
        self.token = token.strip()
        self.phone_id = phone_id.strip()
        self.verify_token = verify_token.strip()
        self.app_secret = app_secret.strip()
        self.core_url = core_url.rstrip("/")
        self.core = core if core is not None else (JDiscordCore() if embedded else None)
        self.allowed = {_norm_phone(x) for x in (allowed or set()) if _norm_phone(x)}

    def verify_signature(self, raw_body: bytes, sig_header: str | None) -> bool:
        if not self.app_secret:
            return True
        if not sig_header or not sig_header.startswith("sha256="):
            return False
        expected = hmac.new(self.app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig_header.split("=", 1)[1].strip().lower())

    def _is_allowed(self, wa_id: str) -> bool:
        if not self.allowed:
            return True
        return _norm_phone(wa_id) in self.allowed

    def send_text(self, to: str, text: str, reply_to: str | None = None) -> dict:
        url = f"{GRAPH_BASE}/{self.phone_id}/messages"
        payload: dict = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        with urllib.request.urlopen(req, timeout=20.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def download_media(self, media_id: str, max_bytes: int = _MEDIA_MAX_BYTES) -> bytes | None:
        meta_req = urllib.request.Request(
            f"{GRAPH_BASE}/{media_id}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(meta_req, timeout=20.0) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
        media_url = meta.get("url")
        if not media_url:
            return None
        file_size = int(meta.get("file_size") or 0)
        if file_size > max_bytes:
            return None
        dl_req = urllib.request.Request(media_url, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(dl_req, timeout=30.0) as resp:
            raw = resp.read(max_bytes + 1)
        return raw if len(raw) <= max_bytes else None

    def _extract_media(self, msg: dict) -> tuple[list[dict], str]:
        mtype = msg.get("type") or ""
        if mtype not in ("image", "video", "audio", "document", "sticker"):
            return [], ""
        obj = msg.get(mtype)
        if not isinstance(obj, dict):
            return [], ""
        caption = str(obj.get("caption") or "").strip()
        default_names = {
            "image": "image.jpg",
            "video": "video.mp4",
            "audio": "audio.ogg",
            "document": "document.bin",
            "sticker": "sticker.webp",
        }
        default_mimes = {
            "image": "image/jpeg",
            "video": "video/mp4",
            "audio": "audio/ogg",
            "document": "application/octet-stream",
            "sticker": "image/webp",
        }
        fname = str(obj.get("filename") or default_names[mtype])
        mime_raw = str(obj.get("mime_type") or default_mimes[mtype])
        kind, mime = classify_attachment(fname, mime_raw)
        item: dict = {"filename": fname, "mime": mime, "kind": kind, "size": 0}
        media_id = obj.get("id")
        if media_id:
            try:
                raw = self.download_media(str(media_id))
            except Exception:
                raw = None
            if raw is not None:
                item["size"] = len(raw)
                if kind == "text" and len(raw) <= 256 * 1024:
                    item["text_preview"] = raw.decode("utf-8", errors="replace")[:1500]
                elif kind in ("image", "video", "audio") or mime == "application/pdf":
                    item["data_b64"] = base64.b64encode(raw).decode("ascii")
        return [item], caption

    def _ask(self, uid: str, name: str, text: str, media: list[dict] | None) -> str:
        if self.core is not None:
            return self.core.handle(f"wa:{uid}", name, text, media=media)
        return ask_core(self.core_url, uid, name, text, media=media)

    def process_payload(self, payload: dict) -> list[tuple[str, str]]:
        """Обрабатывает webhook JSON от Meta WhatsApp Cloud API и возвращает список (wa_id, reply)."""
        results: list[tuple[str, str]] = []
        for entry in payload.get("entry") or []:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes") or []:
                val = (change or {}).get("value") if isinstance(change, dict) else None
                if not isinstance(val, dict):
                    continue
                contacts = {
                    str(c.get("wa_id")): ((c.get("profile") or {}).get("name") or "")
                    for c in (val.get("contacts") or [])
                    if isinstance(c, dict) and c.get("wa_id")
                }
                for msg in val.get("messages") or []:
                    if not isinstance(msg, dict):
                        continue
                    wa_id = str(msg.get("from") or "").strip()
                    if not wa_id or not self._is_allowed(wa_id):
                        continue
                    msg_id = str(msg.get("id") or "") or None
                    name = contacts.get(wa_id) or wa_id
                    mtype = msg.get("type") or ""
                    text = ""
                    media: list[dict] = []
                    if mtype == "text":
                        text = str((msg.get("text") or {}).get("body") or "").strip()
                    elif mtype == "interactive":
                        inter = msg.get("interactive") or {}
                        btn = inter.get("button_reply") or inter.get("list_reply") or {}
                        text = str(btn.get("title") or btn.get("id") or "").strip()
                    else:
                        media, text = self._extract_media(msg)
                    if not text and not media:
                        continue
                    reply = self._ask(wa_id, name, text, media or None)
                    if reply:
                        for part in chunks(reply):
                            self.send_text(wa_id, part, reply_to=msg_id)
                        results.append((wa_id, reply))
        return results


def make_whatsapp_handler(bot: WhatsAppBot):
    """HTTP-обработчик webhook WhatsApp Cloud API (GET для верификации, POST для сообщений)."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: bytes | str, code: int = 200, ctype: str = "application/json; charset=utf-8") -> None:
            raw = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args) -> None:
            pass

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                self._send(json.dumps({"ok": True}))
                return
            if parsed.path != "/webhook":
                self._send("Not found", 404, "text/plain; charset=utf-8")
                return
            qs = urllib.parse.parse_qs(parsed.query)
            mode = (qs.get("hub.mode") or [""])[0]
            token = (qs.get("hub.verify_token") or [""])[0]
            challenge = (qs.get("hub.challenge") or [""])[0]
            if mode == "subscribe" and bot.verify_token and token == bot.verify_token:
                self._send(challenge, 200, "text/plain; charset=utf-8")
            else:
                self._send("Forbidden", 403, "text/plain; charset=utf-8")

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path != "/webhook":
                self._send("Not found", 404, "text/plain; charset=utf-8")
                return
            try:
                size = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                self._send("Bad Request", 400, "text/plain; charset=utf-8")
                return
            if size < 0 or size > 2 * 1024 * 1024:
                self._send("Payload Too Large", 413, "text/plain; charset=utf-8")
                return
            raw = self.rfile.read(size) if size else b""
            if not bot.verify_signature(raw, self.headers.get("X-Hub-Signature-256")):
                self._send("Invalid signature", 403, "text/plain; charset=utf-8")
                return
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, ValueError):
                self._send("Invalid JSON", 400, "text/plain; charset=utf-8")
                return
            if isinstance(data, dict):
                try:
                    bot.process_payload(data)
                except Exception as exc:
                    print("[whatsapp] ошибка обработки:", exc)
            self._send(json.dumps({"ok": True}))

    return Handler


def run_web_mode() -> int:
    """Запускает WhatsApp Web клиент (j_whatsapp_web.js) с подключением по QR-коду без токена Meta."""
    script = BASE / "j_whatsapp_web.js"
    if not script.is_file():
        print(f"Не найден файл {script}.")
        return 1
    node = shutil.which("node")
    if not node:
        print("Не найден Node.js (нужен для WhatsApp Web по QR-коду). Поставь Node.js LTS с nodejs.org.")
        return 1
    if not (BASE / "node_modules" / "whatsapp-web.js").is_dir():
        print("Для работы WhatsApp без токена (по QR-коду) установи пакеты в папке D:\\SDJ:")
        print("  npm install whatsapp-web.js qrcode-terminal")
        return 1
    return subprocess.run([node, str(script)], cwd=str(BASE)).returncode


def main() -> None:
    load_env()
    mode = resolve_mode()
    if mode == "web":
        code = run_web_mode()
        if code:
            raise SystemExit(code)
        return
    token = (os.environ.get("WHATSAPP_TOKEN") or "").strip()
    phone_id = (os.environ.get("WHATSAPP_PHONE_ID") or "").strip()
    if not token or not phone_id:
        raise SystemExit("Выбран WHATSAPP_MODE=cloud, но не заданы WHATSAPP_TOKEN или WHATSAPP_PHONE_ID в j.env.")
    verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN") or ""
    app_secret = os.environ.get("WHATSAPP_APP_SECRET") or ""
    core_url = os.environ.get("J_CORE_URL") or "http://127.0.0.1:8765"
    embedded = os.environ.get("J_WHATSAPP_EMBEDDED", "0").strip().lower() in ("1", "true", "yes", "on", "да")
    allowed = {x.strip() for x in os.environ.get("J_WHATSAPP_ALLOWED", "").split(",") if x.strip()}
    try:
        port = int(os.environ.get("WHATSAPP_PORT", "8788") or 8788)
    except ValueError:
        port = 8788
    host = (os.environ.get("WHATSAPP_HOST") or "127.0.0.1").strip() or "127.0.0.1"

    bot = WhatsAppBot(
        token=token,
        phone_id=phone_id,
        verify_token=verify_token,
        app_secret=app_secret,
        core_url=core_url,
        embedded=embedded,
        allowed=allowed,
    )
    server = ThreadingHTTPServer((host, port), make_whatsapp_handler(bot))
    print(f"J в WhatsApp: webhook слушает http://{host}:{port}/webhook (Phone ID: {phone_id})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
