#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J учится по интернету: поиск, оценка, самостоятельная группировка по разделам, проверка владельцем.

Как это работает
  1. ТЕМЫ: владелец задаёт их командой, а J сама добавляет темы из вопросов, на которые не смогла ответить.
  2. ПОИСК: только по сайтам из белого списка (j_sources.json) через официальный API MediaWiki
     (Википедия, Minecraft Wiki…). Только https, таймауты, лимит размера и числа запросов.
  3. ОЦЕНКА: J сама решает, что стоит сохранить: релевантность теме, качество текста, доверие к источнику;
     дубликаты, страницы-неоднозначности и подозрительные тексты отбрасываются.
  4. РАЗДЕЛЫ (обучение без учителя): найденное группируется по сходству TF-IDF без заранее заданных тем;
     название раздела J придумывает по самым характерным словам.
  5. ПРОВЕРКА: находки попадают в «непроверенное». В основную память — только после !approve.
  6. ИСПОЛЬЗОВАНИЕ: одобренное J использует в ответах, всегда указывая источник.

Безопасность: всё скачанное — только данные, а не инструкции; J ничего не выполняет из страниц;
хранится короткая выжимка (до 700 символов) со ссылкой на источник, а не копия страницы.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

from j_ai import norm, stems

BASE = Path(__file__).resolve().parent
from j_paths import data_path

MAX_BYTES = 1_000_000      # больше этого ответ не читаем
MAX_TEXT = 700             # длина хранимой выжимки
MIN_SCORE = 0.6            # ниже — J сама отбрасывает находку
MAX_PENDING = 60           # сколько находок может ждать проверки
MAX_QUEUE = 30             # очередь тем
PER_TOPIC = 3              # лучших находок на тему
DAILY_CAP = 300            # запросов в сутки
CLUSTER_SIM = 0.08         # порог сходства для объединения в раздел (близкие тексты ~0.12, чужие ~0.03)

DEFAULT_SOURCES = [
    {"name": "Википедия (ru)", "api": "https://ru.wikipedia.org/w/api.php", "trust": 0.9, "license": "CC BY-SA 4.0"},
    {"name": "Wikipedia (en)", "api": "https://en.wikipedia.org/w/api.php", "trust": 0.9, "license": "CC BY-SA 4.0"},
    {"name": "Minecraft Wiki", "api": "https://minecraft.wiki/api.php", "trust": 0.8, "license": "см. лицензию на сайте"},
]

STOP_Q = set(stems("что такое кто такой такая это значит означает как работает где what is are who the a an of in how does was"))
STOP_WORDS = set(stems("который которые которая которой может могут если также этот этого этой после между более когда чтобы такой свои своей потому даже очень его ее их они она оно был была были быть для при про над под без чем что как так там тут уже еще или либо from with that this which have been also their there than into over such"))
INJECTION = re.compile(
    r"(ignore\s+(all\s+|any\s+)?(previous|above|prior)|disregard\s.{0,30}instruction|system\s+prompt|you\s+are\s+now"
    r"|игнорируй\s.{0,30}(инструкц|предыдущ)|забудь\s.{0,20}инструкц|ты\s+теперь\s)", re.I)
DISAMBIG = re.compile(r"(может означать|may refer to|may also refer to|значени[ея]:|disambiguation)", re.I)
PRIVATE = re.compile(r"(@|https?://|\d{6,})")
_WORDS = re.compile(r"[\w']+", re.UNICODE)


# ---------- утилиты ----------
def clean_text(raw: str) -> str:
    t = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    t = re.sub(r"\[[^\]]{1,12}\]", "", t)  # сноски вида [1]
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > MAX_TEXT:
        cut = t[:MAX_TEXT]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = cut[: end + 1] if end > 200 else cut.rstrip() + "…"
    return t


def safe_topic(text: str) -> str | None:
    """Тема для поиска уходит на внешний сайт, поэтому личное и подозрительное не пропускаем."""
    t = norm(text).strip(" ?!.,:;\"'«»")
    if not t or PRIVATE.search(t):
        return None
    words = [w for w in _WORDS.findall(t) if w[:5] not in STOP_Q]
    if not words or max(len(w) for w in words) < 3 or len(words) > 8:
        return None
    return " ".join(words)[:80]


def load_sources(path: Path | None = None) -> list[dict]:
    path = Path(path) if path else data_path("j_sources.json", "J_SOURCES")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        good = [s for s in data if isinstance(s, dict) and str(s.get("api", "")).startswith("https://")]
        if good:
            return good
    except (OSError, ValueError):
        pass
    try:  # создаём файл, чтобы список сайтов можно было править руками
        path.write_text(json.dumps(DEFAULT_SOURCES, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return list(DEFAULT_SOURCES)


# ---------- безопасная загрузка ----------
class WebFetcher:
    def __init__(self, sources: list[dict], min_interval: float = 1.0, daily_cap: int = DAILY_CAP,
                 opener=None, contact: str = "") -> None:
        self.sources = sources
        self.min_interval = min_interval
        self.daily_cap = daily_cap
        self._open = opener or urllib.request.urlopen
        self._last = 0.0
        self._day = ""
        self._count = 0
        self.ua = f"J-Study/0.1 (hobby bot; {contact or 'contact not set'})"

    def _allow(self) -> bool:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self._count = today, 0
        if self._count >= self.daily_cap:
            return False
        self._count += 1
        return True

    def _get(self, api: str, params: dict) -> dict | None:
        if not api.startswith("https://") or not self._allow():
            return None
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        host = urllib.parse.urlparse(api).hostname
        url = api + "?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept": "application/json"})
            with self._open(req, timeout=10) as resp:
                if urllib.parse.urlparse(resp.geturl()).hostname != host:  # редирект на чужой сайт — не доверяем
                    return None
                raw = resp.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return None
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return None

    @staticmethod
    def _page_url(api: str, title: str) -> str:
        return api.rsplit("api.php", 1)[0] + "index.php?title=" + urllib.parse.quote(title.replace(" ", "_"))

    def search(self, query: str, source: dict, limit: int = 3) -> list[dict]:
        api = source["api"]
        data = self._get(api, {
            "action": "query", "format": "json", "formatversion": "2", "generator": "search", "gsrsearch": query,
            "gsrlimit": str(limit), "gsrnamespace": "0", "prop": "extracts|info", "exintro": "1",
            "explaintext": "1", "exsentences": "4", "exlimit": "max", "inprop": "url", "redirects": "1",
        })
        pages = ((data or {}).get("query") or {}).get("pages") or []
        if isinstance(pages, dict):
            pages = list(pages.values())
        out = []
        for pg in sorted(pages, key=lambda p: p.get("index", 99)):
            if pg.get("title") and pg.get("extract"):
                out.append({"title": pg["title"], "text": clean_text(pg["extract"]),
                            "url": pg.get("fullurl") or self._page_url(api, pg["title"])})
        if out:
            return out
        data = self._get(api, {  # запасной путь: сниппеты поиска (если на вики нет расширения extracts)
            "action": "query", "list": "search", "srsearch": query, "srlimit": str(limit),
            "srnamespace": "0", "format": "json", "formatversion": "2",
        })
        for r in ((data or {}).get("query") or {}).get("search") or []:
            if r.get("title"):
                out.append({"title": r["title"], "text": clean_text(r.get("snippet", "")),
                            "url": self._page_url(api, r["title"])})
        return out


# ---------- знания ----------
class Knowledge:
    def __init__(self, path: Path | None = None, sources: list[dict] | None = None,
                 fetcher: WebFetcher | None = None) -> None:
        self.path = Path(path) if path else data_path("j_knowledge.json", "J_KNOWLEDGE")
        self.lock = threading.RLock()
        self.sources = sources if sources is not None else load_sources()
        self.fetcher = fetcher or WebFetcher(self.sources, contact=os.environ.get("J_CONTACT", ""))
        self.min_score = MIN_SCORE  # J подстраивает его сама по тому, как часто её находки одобряют (j_self.py)
        self.words: dict[str, Counter] = defaultdict(Counter)
        self.data = {"items": [], "queue": [], "done": [], "rejected": [], "next_id": 1}
        self._load()
        self._recluster()

    # --- хранение ---
    def _load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.update({k: v for k, v in loaded.items() if k in self.data})
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            try:
                self.path.replace(self.path.with_suffix(".bak"))
            except OSError:
                pass

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    # --- темы ---
    def queue_topic(self, text: str) -> tuple[bool, str]:
        topic = safe_topic(text)
        if not topic:
            return False, "invalid"
        with self.lock:
            if topic in self.data["queue"] or topic in self.data["done"]:
                return False, "known"
            if len(self.data["queue"]) >= MAX_QUEUE:
                return False, "full"
            self.data["queue"].append(topic)
            self._save()
        return True, topic

    def study_next(self) -> dict | None:
        with self.lock:
            if not self.data["queue"] or self._pending_count() >= MAX_PENDING:
                return None
            topic = self.data["queue"].pop(0)
        return self.study_topic(topic)

    def _pending_count(self) -> int:
        return sum(1 for i in self.data["items"] if i["status"] == "pending")

    # --- изучение одной темы ---
    def study_topic(self, topic: str) -> dict:
        stats = {"topic": topic, "found": 0, "added": 0, "skipped": 0}
        candidates = []
        for src in self.sources:  # сеть — вне блокировки
            for c in self.fetcher.search(topic, src):
                c["source"] = src
                candidates.append(c)
        with self.lock:
            scored = []
            for c in candidates:
                stats["found"] += 1
                blob = c["title"] + " " + c["text"]
                bad = (INJECTION.search(blob) or DISAMBIG.search(c["text"]) or len(c["text"]) < 60
                       or self._hash(c["url"]) in self.data["rejected"])
                score = 0.0 if bad else self._score(topic, c)
                if score < self.min_score:
                    stats["skipped"] += 1
                    continue
                scored.append((score, c))
            scored.sort(key=lambda x: x[0], reverse=True)
            for score, c in scored[:PER_TOPIC]:
                if self._pending_count() >= MAX_PENDING or self._is_duplicate(c):
                    stats["skipped"] += 1
                    continue
                self.data["items"].append({
                    "id": self.data["next_id"], "status": "pending", "topic": topic, "title": c["title"],
                    "text": c["text"], "url": c["url"], "source": c["source"]["name"],
                    "license": c["source"].get("license", ""), "fetched": time.strftime("%Y-%m-%d"),
                    "score": round(score, 2), "section": "",
                })
                self.data["next_id"] += 1
                stats["added"] += 1
            if topic not in self.data["done"]:
                self.data["done"] = (self.data["done"] + [topic])[-200:]
            self._recluster()
            self._save()
        return stats

    @staticmethod
    def _hash(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]

    def _score(self, topic: str, c: dict) -> float:
        q = {t for t in stems(topic) if t not in STOP_Q}
        title = set(stems(re.sub(r"\(.*?\)", "", c["title"])))
        body = set(stems(c["text"]))
        rel = max(len(q & title) / len(q), 0.7 * len(q & body) / len(q)) if q else 0.0
        n = len(c["text"])
        quality = 1.0 if 120 <= n <= MAX_TEXT + 5 else 0.5 if n >= 60 else 0.1
        return 0.5 * rel + 0.25 * quality + 0.25 * float(c["source"].get("trust", 0.5))

    def _is_duplicate(self, c: dict) -> bool:
        h = self._hash(c["url"])
        a = set(stems(c["text"]))
        for it in self.data["items"]:
            if self._hash(it["url"]) == h:
                return True
            b = set(stems(it["text"]))
            if a and b and len(a & b) / len(a | b) > 0.8:
                return True
        return False

    # --- проверка владельцем ---
    def pending(self, limit: int = 5) -> list[dict]:
        with self.lock:
            return [dict(i) for i in self.data["items"] if i["status"] == "pending"][:limit]

    def approve(self, ids: list[int] | None = None) -> int:
        with self.lock:
            n = 0
            for it in self.data["items"]:
                if it["status"] == "pending" and (ids is None or it["id"] in ids):
                    it["status"] = "approved"
                    n += 1
            self._save()
            return n

    def reject(self, ids: list[int] | None = None) -> int:
        """ids=None — отклонить все непроверенные. Отклонённый источник больше не вернётся."""
        with self.lock:
            keep, n = [], 0
            for it in self.data["items"]:
                hit = (ids is None and it["status"] == "pending") or (ids is not None and it["id"] in ids)
                if hit:
                    n += 1
                    self.data["rejected"] = (self.data["rejected"] + [self._hash(it["url"])])[-500:]
                else:
                    keep.append(it)
            self.data["items"] = keep
            self._recluster()
            self._save()
            return n

    # --- использование знаний ---
    def answer(self, query: str, threshold: float = 0.5) -> dict | None:
        qs = {t for t in stems(query) if t not in STOP_Q}
        if not qs:
            return None
        best = None
        with self.lock:
            for it in self.data["items"]:
                if it["status"] != "approved":
                    continue
                ts = set(stems(re.sub(r"\(.*?\)", "", it["title"])))
                bs = set(stems(it["text"]))
                contain = len(qs & ts) / len(ts) if ts else 0.0
                jac_t = len(qs & ts) / len(qs | ts) if ts else 0.0
                jac_b = len(qs & bs) / len(qs | bs) if bs else 0.0
                sim = max(0.9 if contain >= 0.99 else 0.0, jac_t, 1.2 * jac_b)
                if sim >= threshold and (best is None or sim > best["sim"]):
                    best = {**it, "sim": round(sim, 2)}
        return best

    # --- разделы: обучение без учителя ---
    def _recluster(self) -> None:
        items = self.data["items"]
        if not items:
            return
        docs = []
        for it in items:
            blob = it["title"] + " " + it["text"]
            for w in _WORDS.findall(norm(blob)):
                self.words[w[:5]][w] += 1
            docs.append(Counter(t for t in stems(blob) if t not in STOP_Q and t not in STOP_WORDS and len(t) > 2))
        n = len(docs)
        df = Counter()
        for d in docs:
            df.update(d.keys())
        idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
        clusters: list[dict] = []
        for idx, d in enumerate(docs):
            v = {t: (1 + math.log(c)) * idf[t] for t, c in d.items()}
            norm_v = math.sqrt(sum(x * x for x in v.values())) or 1.0
            v = {t: x / norm_v for t, x in v.items()}
            best, best_sim = None, 0.0
            for cl in clusters:
                cn = math.sqrt(sum(x * x for x in cl["c"].values())) or 1.0
                sim = sum(x * cl["c"].get(t, 0.0) for t, x in v.items()) / cn
                if sim > best_sim:
                    best, best_sim = cl, sim
            if best and best_sim >= CLUSTER_SIM:
                best["members"].append(idx)
                for t, x in v.items():
                    best["c"][t] = best["c"].get(t, 0.0) + x
            else:
                clusters.append({"c": dict(v), "members": [idx]})
        for cl in clusters:
            top = sorted(cl["c"], key=cl["c"].get, reverse=True)[:3]
            name = ", ".join(self._word(t) for t in top)
            for i in cl["members"]:
                items[i]["section"] = name

    def _word(self, stem: str) -> str:
        c = self.words.get(stem)
        return c.most_common(1)[0][0] if c else stem

    def sections(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        with self.lock:
            for it in self.data["items"]:
                s = out.setdefault(it["section"] or "—", {"approved": 0, "pending": 0})
                s[it["status"]] += 1
        return out

    def status(self) -> dict:
        with self.lock:
            return {
                "queue": list(self.data["queue"]), "pending": self._pending_count(),
                "approved": sum(1 for i in self.data["items"] if i["status"] == "approved"),
                "studied": len(self.data["done"]),
            }
