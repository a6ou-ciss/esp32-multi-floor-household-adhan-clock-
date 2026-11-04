#!/usr/bin/env python3
"""
raspberry_adhan_player.py - Headless Adhan clock for Raspberry Pi using the
Kimono Labs Mawaqit API.

Plays an Adhan audio file at Fajr, Dhuhr, Asr, Maghrib and Isha for a chosen
mosque, using the mosque's own timezone. Prayer times are cached to disk so
the Pi keeps working through internet outages.

Configuration precedence: CLI flags > environment variables > config.json > defaults

    Setting            Environment variable    config.json key
    -----------------  ----------------------  -----------------
    mosque slug        MAWAQIT_MOSQUE          mosque
    mosque search      MAWAQIT_SEARCH          search
    API base URL       KIMONO_BASE_URL         base_url
    adhan audio        ADHAN_AUDIO             adhan_audio
    fajr audio         FAJR_AUDIO              fajr_audio
    volume (0-100)     ADHAN_VOLUME            volume
    grace period (s)   GRACE_SECONDS           grace_seconds
    player backend     ADHAN_PLAYER            player   (auto|mpg123|ffplay|aplay|pygame)
    audio device       ADHAN_AUDIO_DEVICE      audio_device  (e.g. hw:1,0 for mpg123/aplay)
    timezone override  TIMEZONE                timezone (e.g. Europe/Paris)
    cache file         CACHE_FILE              cache_file
    log file           LOG_FILE                log_file
    log level          LOG_LEVEL               log_level
    max play seconds   ADHAN_MAX_SECONDS       max_play_seconds
    days to prefetch   CACHE_DAYS_AHEAD        days_ahead

Relative paths are resolved against the directory containing this script.

Examples:
    python3 raspberry_adhan_player.py --search Paris
    python3 raspberry_adhan_player.py --mosque grande-mosquee-de-paris --show-times
    python3 raspberry_adhan_player.py --test-audio
    python3 raspberry_adhan_player.py            # run the service

Exit code 78 means a configuration error (systemd should not restart on it).
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None  # type: ignore

log = logging.getLogger("adhan")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_BASE_URL = "https://www.kimonolabs.dev"
EX_CONFIG = 78
REFRESH_INTERVAL = 12 * 3600  # re-fetch times at least every 12 hours
HTTP_TIMEOUT = 15
PRAYERS = ["fajr", "dhuhr", "asr", "maghrib", "isha"]

DEFAULTS: dict = {
    "mosque": None,
    "search": None,
    "base_url": DEFAULT_BASE_URL,
    "adhan_audio": "athan.mp3",
    "fajr_audio": None,
    "volume": 80,
    "grace_seconds": 90,
    "player": "auto",
    "audio_device": None,
    "timezone": None,
    "cache_file": "prayer_cache.json",
    "log_file": None,
    "log_level": "INFO",
    "max_play_seconds": 600,
    "days_ahead": 3,
}

ENV_MAP = {
    "mosque": "MAWAQIT_MOSQUE",
    "search": "MAWAQIT_SEARCH",
    "base_url": "KIMONO_BASE_URL",
    "adhan_audio": "ADHAN_AUDIO",
    "fajr_audio": "FAJR_AUDIO",
    "volume": "ADHAN_VOLUME",
    "grace_seconds": "GRACE_SECONDS",
    "player": "ADHAN_PLAYER",
    "audio_device": "ADHAN_AUDIO_DEVICE",
    "timezone": "TIMEZONE",
    "cache_file": "CACHE_FILE",
    "log_file": "LOG_FILE",
    "log_level": "LOG_LEVEL",
    "max_play_seconds": "ADHAN_MAX_SECONDS",
    "days_ahead": "CACHE_DAYS_AHEAD",
}


class ConfigError(Exception):
    pass


# =============================================================================
# Configuration
# =============================================================================
def load_config(config_path: Optional[str], overrides: dict) -> dict:
    cfg = dict(DEFAULTS)

    # config.json (accepts either "mosque" or "MAWAQIT_MOSQUE" style keys)
    aliases = {env.lower(): field for field, env in ENV_MAP.items()}
    aliases.update({field: field for field in DEFAULTS})
    explicit = config_path or os.environ.get("ADHAN_CONFIG")
    path = Path(explicit) if explicit else BASE_DIR / "config.json"
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            raise ConfigError(f"Cannot read config file {path}: {e}")
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a JSON object")
        for k, v in raw.items():
            field = aliases.get(str(k).lower())
            if field and v not in (None, ""):
                cfg[field] = v
    elif explicit:
        raise ConfigError(f"Config file not found: {path}")

    # environment variables
    for field, env in ENV_MAP.items():
        val = os.environ.get(env)
        if val not in (None, ""):
            cfg[field] = val

    # CLI flags
    for field, val in overrides.items():
        if val not in (None, ""):
            cfg[field] = val

    # coercion / validation
    for field in ("volume", "grace_seconds", "max_play_seconds", "days_ahead"):
        try:
            cfg[field] = int(cfg[field])
        except (TypeError, ValueError):
            raise ConfigError(f"'{field}' must be an integer, got {cfg[field]!r}")
    cfg["volume"] = max(0, min(100, cfg["volume"]))
    cfg["grace_seconds"] = max(0, cfg["grace_seconds"])
    cfg["days_ahead"] = max(1, min(14, cfg["days_ahead"]))
    cfg["player"] = str(cfg["player"]).lower()
    cfg["base_url"] = str(cfg["base_url"]).rstrip("/")

    def resolve(p: Optional[str]) -> Optional[Path]:
        if not p:
            return None
        p = Path(os.path.expanduser(str(p)))
        return p if p.is_absolute() else BASE_DIR / p

    for field in ("adhan_audio", "fajr_audio", "cache_file", "log_file"):
        cfg[field] = resolve(cfg[field])
    return cfg


def setup_logging(level: str, log_file: Optional[Path]) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    handlers: list = [logging.StreamHandler(sys.stdout)]
    if log_file:
        try:
            handlers.append(
                logging.handlers.RotatingFileHandler(
                    log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
                )
            )
        except OSError as e:
            print(f"Warning: cannot open log file {log_file}: {e}", file=sys.stderr)
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)


# =============================================================================
# Mawaqit API client (same logic as mawaqit_client.py)
# =============================================================================
class MawaqitError(Exception):
    pass


class MawaqitClient:
    def __init__(self, base_url: str, timeout: int = HTTP_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(clean)}"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "raspberry-adhan/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace").strip()[:300]
            except Exception:
                pass
            raise MawaqitError(f"HTTP {e.code} {e.reason} for {url} {detail}".strip()) from e
        except urllib.error.URLError as e:
            raise MawaqitError(f"Network error reaching {url}: {e.reason}") from e
        except (socket.timeout, TimeoutError) as e:
            raise MawaqitError(f"Timed out after {self.timeout}s: {url}") from e
        except (OSError, http.client.HTTPException) as e:
            raise MawaqitError(f"Connection problem for {url}: {e}") from e
        try:
            return json.loads(body)
        except ValueError as e:
            raise MawaqitError(f"Invalid JSON from {url}: {e}") from e

    def search_mosques(self, query: str) -> Any:
        return self._get("/api/tools/mawaqit/search", {"q": query})

    def get_prayer_times(self, mosque: str, day: Optional[str] = None) -> Any:
        return self._get("/api/tools/mawaqit/prayer-times", {"mosque": mosque, "date": day})


# =============================================================================
# Defensive response parsing
# =============================================================================
_ALIASES = {
    "fajr": "fajr", "fadjr": "fajr", "subh": "fajr", "sobh": "fajr",
    "dhuhr": "dhuhr", "duhr": "dhuhr", "dohr": "dhuhr", "zuhr": "dhuhr", "dhur": "dhuhr",
    "asr": "asr",
    "maghrib": "maghrib", "magrib": "maghrib",
    "isha": "isha", "ishaa": "isha", "icha": "isha",
}
_TIME_RE = re.compile(r"^\s*(\d{1,2})\s*[:hH]\s*(\d{2})(?!\d)")
_TZ_KEYS = {"timezone", "timezonename", "time_zone", "tz", "tzid"}


def _canon(name: Any) -> Optional[str]:
    return _ALIASES.get(re.sub(r"[\s_\-]", "", str(name).lower()))


def _norm_time(v: Any) -> Optional[str]:
    if isinstance(v, dict):
        for k in ("time", "value", "at"):
            if k in v:
                return _norm_time(v[k])
        return None
    if isinstance(v, str):
        m = _TIME_RE.match(v)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if h < 24 and mi < 60:
                return f"{h:02d}:{mi:02d}"
    return None


def _sane(t: dict) -> bool:
    """All five present and strictly increasing through the day."""
    if not all(p in t for p in PRAYERS):
        return False
    vals = [t[p] for p in PRAYERS]  # zero-padded HH:MM compares correctly as text
    return all(a < b for a, b in zip(vals, vals[1:]))


def _from_dict(d: dict) -> Optional[dict]:
    out: dict = {}
    for k, v in d.items():
        p = _canon(k)
        if p and p not in out:
            t = _norm_time(v)
            if t:
                out[p] = t
    return out if _sane(out) else None


def _from_list(lst: list) -> Optional[dict]:
    if not lst:
        return None
    if all(isinstance(x, dict) for x in lst):
        out: dict = {}
        for x in lst:
            p = _canon(x.get("name") or x.get("prayer") or x.get("label") or x.get("key") or "")
            t = _norm_time(x)
            if p and t and p not in out:
                out[p] = t
        return out if _sane(out) else None
    vals = [_norm_time(x) for x in lst]
    if None in vals:
        return None
    if len(vals) == 6:      # fajr, sunrise, dhuhr, asr, maghrib, isha
        vals.pop(1)
    if len(vals) == 5:
        out = dict(zip(PRAYERS, vals))
        return out if _sane(out) else None
    return None


def extract_times(obj: Any) -> Optional[dict]:
    """Find {'fajr': 'HH:MM', ... 'isha': 'HH:MM'} anywhere in an API response."""
    if isinstance(obj, dict):
        found = _from_dict(obj)
        if found:
            return found
        for v in obj.values():
            found = extract_times(v)
            if found:
                return found
    elif isinstance(obj, list):
        found = _from_list(obj)
        if found:
            return found
        for v in obj:
            found = extract_times(v)
            if found:
                return found
    return None


def load_tz(name: str):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def find_timezone(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in _TZ_KEYS and isinstance(v, str) and load_tz(v):
                return v
        for v in obj.values():
            r = find_timezone(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_timezone(v)
            if r:
                return r
    return None


def parse_mosques(data: Any) -> list:
    items = data
    if isinstance(data, dict):
        items = next(
            (data[k] for k in ("results", "mosques", "data", "items") if isinstance(data.get(k), list)),
            [],
        )
    out = []
    for m in items if isinstance(items, list) else []:
        if not isinstance(m, dict):
            continue
        slug = m.get("slug") or m.get("uuid") or m.get("id")
        if not slug:
            continue
        loc = ", ".join(str(m[k]) for k in ("city", "localisation", "address", "country") if m.get(k))
        out.append({"name": m.get("name") or m.get("label") or "(unnamed)", "slug": str(slug), "location": loc})
    return out


def parse_hhmm(s: str) -> Optional[dtime]:
    m = _TIME_RE.match(s or "")
    return dtime(int(m.group(1)), int(m.group(2))) if m else None


# =============================================================================
# Audio playback
# =============================================================================
class AudioPlayer:
    """Plays a file via mpg123 / ffplay / aplay / pygame, whichever is available."""

    def __init__(self, preference: str, volume: int, device: Optional[str], max_seconds: int):
        self.pref = preference
        self.volume = volume
        self.device = device
        self.max_seconds = max_seconds

    @staticmethod
    def _available(backend: str) -> bool:
        if backend == "pygame":
            try:
                import importlib.util
                return importlib.util.find_spec("pygame") is not None
            except Exception:
                return False
        return shutil.which(backend) is not None

    def _candidates(self, path: Path) -> list:
        if self.pref != "auto":
            order = [self.pref]
        elif path.suffix.lower() == ".wav":
            order = ["aplay", "ffplay", "pygame", "mpg123"]
        else:
            order = ["mpg123", "ffplay", "pygame"]
        return [b for b in order if self._available(b)]

    def play(self, path: Path, stop: threading.Event) -> bool:
        path = Path(path)
        if not path.is_file():
            log.error("Audio file not found: %s", path)
            return False
        backends = self._candidates(path)
        if not backends:
            log.error("No audio backend available (install mpg123: sudo apt install mpg123)")
            return False
        for backend in backends:
            log.debug("Trying audio backend: %s", backend)
            try:
                ok = self._run_pygame(path, stop) if backend == "pygame" else self._run_proc(backend, path, stop)
            except Exception as e:
                log.error("Backend %s crashed: %s", backend, e)
                ok = False
            if ok:
                return True
            if stop.is_set():
                return False
            log.warning("Backend %s failed; trying next one if available", backend)
        return False

    def _command(self, backend: str, path: Path) -> list:
        if backend == "mpg123":
            scale = int(32768 * self.volume / 100)  # mpg123 -f: 32768 = 100%
            cmd = ["mpg123", "-q", "-f", str(scale)]
            if self.device:
                cmd += ["-a", self.device]
            return cmd + [str(path)]
        if backend == "ffplay":
            return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                    "-volume", str(self.volume), str(path)]
        if backend == "aplay":  # no volume option: uses the system mixer level
            cmd = ["aplay", "-q"]
            if self.device:
                cmd += ["-D", self.device]
            return cmd + [str(path)]
        raise ValueError(f"unknown backend {backend}")

    def _run_proc(self, backend: str, path: Path, stop: threading.Event) -> bool:
        proc = subprocess.Popen(
            self._command(backend, path),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + self.max_seconds
        while proc.poll() is None:
            if stop.is_set():
                self._kill(proc)
                return False
            if time.monotonic() > deadline:
                log.warning("Playback exceeded %ds; stopping", self.max_seconds)
                self._kill(proc)
                return True
            stop.wait(0.25)
        if proc.returncode != 0:
            err = (proc.stderr.read() or b"").decode("utf-8", errors="replace").strip()
            log.error("%s exited with code %s: %s", backend, proc.returncode, err[:300])
            return False
        return True

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _run_pygame(self, path: Path, stop: threading.Event) -> bool:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        import pygame
        try:
            pygame.mixer.init()
            pygame.mixer.music.load(str(path))
            pygame.mixer.music.set_volume(self.volume / 100)
            pygame.mixer.music.play()
            deadline = time.monotonic() + self.max_seconds
            while pygame.mixer.music.get_busy():
                if stop.is_set():
                    pygame.mixer.music.stop()
                    return False
                if time.monotonic() > deadline:
                    pygame.mixer.music.stop()
                    return True
                stop.wait(0.25)
            return True
        except pygame.error as e:
            log.error("pygame error: %s", e)
            return False
        finally:
            try:
                pygame.mixer.quit()
            except Exception:
                pass


# =============================================================================
# Cache
# =============================================================================
class Cache:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("Ignoring unreadable cache %s: %s", self.path, e)
            return {}

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("Could not write cache %s: %s", self.path, e)


# =============================================================================
# Service
# =============================================================================
class AdhanService:
    def __init__(self, cfg: dict, stop: threading.Event):
        self.cfg = cfg
        self.stop = stop
        self.client = MawaqitClient(cfg["base_url"])
        self.cache = Cache(cfg["cache_file"])
        self.player = AudioPlayer(cfg["player"], cfg["volume"], cfg["audio_device"], cfg["max_play_seconds"])
        self.slug: Optional[str] = None
        self.tz = None            # tzinfo, or None meaning "system local time"
        self.tz_ready = False
        self.next_refresh = 0.0   # time.monotonic() deadline
        self.fails = 0
        self.last_date: Optional[date] = None
        self.handled: set = set()

    # ---- mosque / timezone ---------------------------------------------------
    def _sync_mosque(self, slug: str) -> None:
        """Discard cached times if they belong to a different mosque."""
        if self.cache.data.get("mosque") != slug:
            keep = self.cache.data.get("resolved_search")
            self.cache.data = {"version": 1, "mosque": slug}
            if keep:
                self.cache.data["resolved_search"] = keep
            self.tz_ready = False
            self.tz = None

    def _resolve_slug(self) -> Optional[str]:
        if self.slug:
            return self.slug
        if self.cfg["mosque"]:
            self.slug = str(self.cfg["mosque"])
        else:
            query = str(self.cfg["search"])
            rs = self.cache.data.get("resolved_search") or {}
            if rs.get("query") == query and rs.get("slug"):
                self.slug = rs["slug"]
            else:
                results = parse_mosques(self.client.search_mosques(query))
                if not results:
                    raise MawaqitError(f"No mosque found for search '{query}'")
                if len(results) > 1:
                    log.warning("Search '%s' matched %d mosques; using the first: %s (%s). "
                                "Set MAWAQIT_MOSQUE to pin one.", query, len(results),
                                results[0]["name"], results[0]["slug"])
                self.slug = results[0]["slug"]
                self.cache.data["resolved_search"] = {"query": query, "slug": self.slug}
                self.cache.save()
        self._sync_mosque(self.slug)
        return self.slug

    def _init_tz(self, slug: Optional[str], allow_network: bool) -> None:
        for name in (self.cfg["timezone"], self.cache.data.get("timezone")):
            if not name:
                continue
            if name == "local":
                self.tz, self.tz_ready = None, True
                return
            tz = load_tz(str(name))
            if tz:
                self.tz, self.tz_ready = tz, True
                return
            log.warning("Unknown timezone '%s'", name)
        if not allow_network or not slug:
            return
        data = self.client.get_prayer_times(slug)  # undated request, only to learn the timezone
        name = find_timezone(data)
        if name:
            self.tz, self.cache.data["timezone"] = load_tz(name), name
            log.info("Mosque timezone: %s", name)
        else:
            log.warning("API did not report a timezone; using the Pi's local time. "
                        "Set TIMEZONE (e.g. Europe/Paris) if the Pi's clock zone differs from the mosque's.")
            self.tz, self.cache.data["timezone"] = None, "local"
        self.tz_ready = True
        self.cache.save()

    # ---- fetching ------------------------------------------------------------
    def _refresh(self) -> bool:
        slug = self._resolve_slug()
        if not self.tz_ready:
            self._init_tz(slug, allow_network=True)
        today = datetime.now(self.tz).date()
        days = self.cache.data.setdefault("days", {})
        all_ok = True
        for i in range(self.cfg["days_ahead"] + 1):
            if self.stop.is_set():
                return False
            d = (today + timedelta(days=i)).isoformat()
            try:
                data = self.client.get_prayer_times(slug, d)
            except MawaqitError as e:
                log.warning("Fetching %s failed: %s", d, e)
                all_ok = False
                break
            times = extract_times(data)
            if not times:
                preview = list(data)[:8] if isinstance(data, dict) else type(data).__name__
                log.error("Could not find 5 valid prayer times in the response for %s "
                          "(top-level: %s). Refusing to use it.", d, preview)
                all_ok = False
                break
            days[d] = times
        # prune old entries
        cutoff = (today - timedelta(days=1)).isoformat()
        for k in [k for k in days if k < cutoff]:
            del days[k]
        played = self.cache.data.get("played", {})
        pcut = (today - timedelta(days=2)).isoformat()
        for k in [k for k in played if k.split(":")[0] < pcut]:
            del played[k]
        self.cache.data["last_fetch"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self.cache.save()
        return all_ok

    def try_refresh(self) -> bool:
        try:
            ok = self._refresh()
        except MawaqitError as e:
            log.warning("Refresh failed: %s", e)
            ok = False
        if ok:
            self.fails = 0
            self.next_refresh = time.monotonic() + REFRESH_INTERVAL
            log.info("Prayer times refreshed")
        else:
            self.fails += 1
            delay = min(60 * 2 ** (self.fails - 1), 900)
            self.next_refresh = time.monotonic() + delay
            log.info("Will retry in %ds (running from cache where available)", delay)
        return ok

    # ---- scheduling ----------------------------------------------------------
    def _events(self, now: datetime) -> list:
        days = self.cache.data.get("days", {})
        events = []
        for off in (0, 1):
            d = now.date() + timedelta(days=off)
            times = days.get(d.isoformat())
            if not times:
                continue
            for p in PRAYERS:
                t = parse_hhmm(times.get(p, ""))
                if t:
                    events.append((datetime.combine(d, t, tzinfo=self.tz), p, f"{d.isoformat()}:{p}"))
        return sorted(events)

    def _already_played(self, key: str) -> bool:
        return key in self.cache.data.get("played", {})

    def _mark_played(self, key: str) -> None:
        self.cache.data.setdefault("played", {})[key] = datetime.now().astimezone().isoformat(timespec="seconds")
        self.cache.save()

    def _play(self, prayer: str, key: str) -> None:
        self._mark_played(key)  # mark first so a crash loop can't replay it
        fajr = self.cfg["fajr_audio"]
        audio = fajr if (prayer == "fajr" and fajr and Path(fajr).is_file()) else self.cfg["adhan_audio"]
        log.info("It is time for %s - playing %s at %d%% volume", prayer.capitalize(), audio.name, self.cfg["volume"])
        if not self.player.play(audio, self.stop) and not self.stop.is_set():
            log.error("Adhan playback for %s failed", prayer)
        else:
            log.info("Finished %s adhan", prayer)

    def run(self) -> None:
        # Offline-capable start-up: slug and timezone from config/cache only
        if self.cfg["mosque"]:
            self.slug = str(self.cfg["mosque"])
        else:
            rs = self.cache.data.get("resolved_search") or {}
            if rs.get("query") == self.cfg["search"]:
                self.slug = rs.get("slug")
        if self.slug:
            self._sync_mosque(self.slug)
            self._init_tz(self.slug, allow_network=False)

        log.info("Adhan service started (mosque=%s, grace=%ds, volume=%d%%, API=%s)",
                 self.slug or f"search:{self.cfg['search']}", self.cfg["grace_seconds"],
                 self.cfg["volume"], self.cfg["base_url"])

        while not self.stop.is_set():
            today = datetime.now(self.tz).date() if self.tz_ready else None
            if today != self.last_date:  # new day (or first loop): refresh soon
                self.last_date = today
                self.next_refresh = 0.0
            if time.monotonic() >= self.next_refresh:
                self.try_refresh()

            if not self.tz_ready:
                self.stop.wait(30)
                continue

            now = datetime.now(self.tz)
            wait_s = 30.0  # cap so wall-clock jumps (NTP sync) are noticed
            played_something = False
            for dt, prayer, key in self._events(now):
                if key in self.handled or self._already_played(key):
                    continue
                delta = (now - dt).total_seconds()
                if delta < 0:
                    wait_s = min(wait_s, -delta)
                    break
                self.handled.add(key)
                if delta <= self.cfg["grace_seconds"]:
                    self._play(prayer, key)
                    played_something = True
                    break
                if delta < 3600:
                    log.warning("Missed %s by %ds (grace %ds); not playing",
                                prayer, int(delta), self.cfg["grace_seconds"])
                else:
                    log.debug("Skipping past event %s", key)
            if played_something:
                continue
            self.stop.wait(max(wait_s, 0.05))

        log.info("Adhan service stopped")

    # ---- diagnostics ---------------------------------------------------------
    def show_times(self) -> int:
        try:
            ok = self._refresh()
        except MawaqitError as e:
            log.error("%s", e)
            ok = False
        days = self.cache.data.get("days", {})
        if not days:
            print("No prayer times available.", file=sys.stderr)
            return 1
        print(f"Mosque:   {self.slug}")
        print(f"Timezone: {self.cache.data.get('timezone') or 'local'}")
        if not ok:
            print("(some data may be from cache - the last refresh had errors)")
        for d in sorted(days):
            print(f"\n{d}")
            for p in PRAYERS:
                print(f"  {p.capitalize():8} {days[d].get(p, '--')}")
        return 0


# =============================================================================
# CLI
# =============================================================================
def do_search(cfg: dict, query: str) -> int:
    try:
        results = parse_mosques(MawaqitClient(cfg["base_url"]).search_mosques(query))
    except MawaqitError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if not results:
        print(f"No mosques found for '{query}'.")
        return 1
    print(f"Found {len(results)} mosque(s):\n")
    for i, m in enumerate(results, 1):
        print(f"{i}. {m['name']}\n   slug: {m['slug']}" + (f"\n   location: {m['location']}" if m["location"] else ""))
    print("\nUse a slug with:  export MAWAQIT_MOSQUE=<slug>   (or set \"mosque\" in config.json)")
    return 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Headless Adhan player for Raspberry Pi (Mawaqit).")
    ap.add_argument("--config", help="Path to config.json (default: next to this script)")
    ap.add_argument("--mosque", help="Mosque slug (overrides MAWAQIT_MOSQUE)")
    ap.add_argument("--search", dest="search_query", metavar="QUERY",
                    help="Search mosques by name/city, print slugs, and exit")
    ap.add_argument("--test-audio", nargs="?", const="", default=None, metavar="FILE",
                    help="Play the adhan (or FILE) once at the configured volume, then exit")
    ap.add_argument("--show-times", action="store_true",
                    help="Fetch and print upcoming prayer times (to verify them), then exit")
    ap.add_argument("--volume", type=int, help="Volume 0-100")
    ap.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config, {
            "mosque": args.mosque, "volume": args.volume, "log_level": args.log_level,
        })
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return EX_CONFIG
    setup_logging(cfg["log_level"], cfg["log_file"])

    stop = threading.Event()

    def handle_signal(signum, _frame):
        log.info("Received %s, shutting down...", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    if args.search_query:
        return do_search(cfg, args.search_query)

    if args.test_audio is not None:
        path = Path(args.test_audio).expanduser() if args.test_audio else cfg["adhan_audio"]
        player = AudioPlayer(cfg["player"], cfg["volume"], cfg["audio_device"], cfg["max_play_seconds"])
        log.info("Testing audio: %s (volume %d%%)", path, cfg["volume"])
        ok = player.play(path, stop)
        log.info("Audio test %s", "succeeded" if ok else "FAILED")
        return 0 if ok else 1

    if not cfg["mosque"] and not cfg["search"]:
        log.error("No mosque configured. Set MAWAQIT_MOSQUE (or MAWAQIT_SEARCH) or use config.json. "
                  "Find a slug with: --search <query>")
        return EX_CONFIG
    if not cfg["adhan_audio"].is_file():
        log.error("Adhan audio file not found: %s", cfg["adhan_audio"])
        return EX_CONFIG
    if cfg["fajr_audio"] and not Path(cfg["fajr_audio"]).is_file():
        log.warning("FAJR_AUDIO %s not found; the regular adhan will be used for Fajr", cfg["fajr_audio"])

    service = AdhanService(cfg, stop)
    if args.show_times:
        return service.show_times()
    try:
        service.run()
    except Exception:
        log.exception("Fatal error in service loop")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())