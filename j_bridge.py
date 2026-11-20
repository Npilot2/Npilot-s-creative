#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Мост между клиентами (Discord, Minecraft, Garry's Mod, Left 4 Dead 2, голос…) и единым мозгом J.

Клиент говорит с ядром по HTTP на 127.0.0.1 (или через файлы — их обслуживает j_filelink.py):
  POST /chat   {"user": "Ник", "text": "...", "game": "minecraft", "id": "стабильный ID (необязательно)"}
         ->    {"reply": "...", "actions": [{"type": "chop", "count": 8}, ...]}
  POST /event  {"type": "chop_done", "user": "", "data": {"have": 8}, "game": "gmod"}
         ->    {"reply": "..."}
  GET  /health -> {"ok": true, "version": "...", "uptime": секунды}
  GET  /status -> подробности: игры, очередь изучения, число профилей, самомодель

Профиль игрока определяется игрой: см. j_games.py (mc:<ник>, gm:<SteamID64>, ro:<имя>…; у Discord — чистый ID).
Всё, что специфично для игры, лежит в реестре игр, а не здесь.
"""
from __future__ import annotations

import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler

from j_discord import JDiscordCore
from j_games import ACTION_CANT, ACTION_DOING, get_game
from j_nlu import ActionNLU

VERSION = "2.0.0"
MAX_BODY = 12 * 1024 * 1024


class SafeDict(dict):
    def __missing__(self, key):
        return "?"


class Bridge:
    def __init__(self, core: JDiscordCore | None = None) -> None:
        self.core = core or JDiscordCore()
        self.nlu = ActionNLU(self.core.brain)
        self.lock = threading.RLock()  # запросы приходят из разных потоков (HTTP и файлы)
        self.started = time.time()

    # ---------- приказы: оставляем только то, что умеет игра ----------
    def _for_game(self, res: dict, game_key: str) -> dict:
        game = get_game(game_key)
        allowed = game.supported
        if allowed is None or not res["actions"]:
            return res
        if not allowed:  # игра без приказов
            return {"reply": f"В {game.title} я пока только разговариваю и комментирую игру. Приказы — позже.",
                    "actions": []}
        keep = [a for a in res["actions"] if a["type"] in allowed]
        dropped = [a for a in res["actions"] if a["type"] not in allowed]
        if not dropped:
            return res
        parts = []
        if keep:
            parts.append("Приступаю: " + ", ".join(ACTION_DOING.get(a["type"], a["type"]) for a in keep) + ".")
        names = sorted({ACTION_CANT.get(a["type"], a["type"]) for a in dropped})
        parts.append(f"В {game.title} я этого не умею: {', '.join(names)}.")
        return {"reply": " ".join(parts), "actions": keep}

    # ---------- разговор ----------
    def chat(self, user: str, text: str, game: str = "minecraft", ident: str = "",
             media: list[dict] | None = None) -> dict:
        with self.lock:
            return self._chat(user, text, game, ident, media=media)

    def _chat(self, user: str, text: str, game_key: str, ident: str,
              media: list[dict] | None = None) -> dict:
        game = get_game(game_key)
        uid = game.uid(ident or user)
        brain = self.core.brain
        if hasattr(brain, "resolve_uid"):
            uid = brain.resolve_uid(uid)
        p = brain.profile(uid)
        if not p["name"]:  # ник/имя из клиента берём как имя — без знакомства
            p["name"] = user[:24]
        if not p["lang"]:
            p["lang"] = "ru"
        try:
            # правила проверяются первыми: запретный запрос не должен «просочиться» через разбор приказов
            gate_ok = self.core.policy.assess(text)["decision"] == "allow"
            # если идёт мини-игра (камень-ножницы-бумага, викторина…), это не приказ
            if gate_ok and game.orders and not brain.session(uid).state:
                res = self.nlu.understand(uid, user, text)
                if res is not None:
                    self.core.me.observe("understood")  # приказ разобран — это тоже удачное понимание
                    brain.save()
                    return self._for_game(res, game_key)
        except Exception as exc:  # понимание приказов не должно ломать обычный разговор
            print("NLU error:", exc)
        return {"reply": self.core.handle(uid, user, text or ("" if media else "привет"), media=media), "actions": []}

    # ---------- события игры ----------
    _DONE_EVENTS = {"finale_win", "place_saved", "place_reached", "charged"}
    _RECOVER_EVENTS = {"extinguished"}
    _FAIL_EVENTS = {"stuck", "danger_abort", "hardware_error", "overheat", "battery_low", "no_water",
                    "mine_no_tool", "pickup_fail", "craft_no_materials", "craft_no_table", "craft_unknown",
                    "weapon_none", "attack_miss", "miss"}

    def event(self, etype: str, user: str, data: dict, game: str = "minecraft") -> str:
        with self.lock:
            return self._event(etype, user, data, game)

    def _event(self, etype: str, user: str, data: dict, game_key: str) -> str:
        game = get_game(game_key)
        if etype in self._RECOVER_EVENTS:
            self.core.me.observe("recovered", task=etype, game=game_key)
        elif etype in self._DONE_EVENTS or etype.endswith("_done"):
            self.core.me.observe("task_done", task=etype, game=game_key)
        elif etype in self._FAIL_EVENTS or etype.endswith(("_fail", "_none")):
            self.core.me.observe("task_fail", task=etype, game=game_key)
        if etype == "status":
            return game.status_text(data)
        lines = game.event_lines(etype)
        if not lines:
            return ""
        ctx = SafeDict(data)
        if user or "user" not in ctx:
            ctx["user"] = user or ""
        return random.choice(lines).format_map(ctx)

    # ---------- состояние ----------
    def health(self) -> dict:
        return {"ok": True, "version": VERSION, "uptime": int(time.time() - self.started)}


def make_handler(bridge: Bridge, status_fn=None):
    """HTTP-обработчик ядра. status_fn() — необязательные дополнительные сведения для /status."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, payload: dict, code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                if self.path == "/health":
                    self._send(bridge.health())
                elif self.path == "/status":
                    info = bridge.health()
                    if status_fn:
                        info.update(status_fn())
                    self._send(info)
                else:
                    self.send_error(404)
            except Exception as exc:
                self._send({"error": str(exc)}, 500)

        def do_POST(self):
            try:
                try:
                    size = int(self.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    self._send({"error": "invalid content-length"}, 400)
                    return
                if size < 0:
                    self._send({"error": "invalid content-length"}, 400)
                    return
                if size > MAX_BODY:
                    self._send({"error": "payload too large"}, 413)
                    return
                raw = self.rfile.read(size) if size else b""
                try:
                    data = json.loads(raw.decode("utf-8") or "{}")
                except (UnicodeDecodeError, ValueError):
                    self._send({"error": "invalid json"}, 400)
                    return
                if not isinstance(data, dict):
                    self._send({"error": "expected json object"}, 400)
                    return
                if self.path == "/chat":
                    media = data.get("media") if isinstance(data.get("media"), list) else None
                    out = bridge.chat(str(data.get("user", "player")), str(data.get("text", "")),
                                      str(data.get("game", "minecraft")), str(data.get("id", "") or ""),
                                      media=media)
                elif self.path == "/event":
                    extra = data.get("data") if isinstance(data.get("data"), dict) else {}
                    out = {"reply": bridge.event(str(data.get("type", "")), str(data.get("user", "")), extra,
                                                 str(data.get("game", "minecraft")))}
                else:
                    self.send_error(404)
                    return
                self._send(out)
            except Exception as exc:  # ядро не должно падать из-за одного кривого запроса
                self._send({"error": str(exc)}, 500)

        def log_message(self, *args):
            pass

    return Handler
