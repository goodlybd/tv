"""
Scans EVERY channel in master.m3u (including ones that were dead before),
then writes iptv.m3u containing only the working channels.
master.m3u is never modified, so a revived channel comes back automatically.
"""
import json
import os
import re
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

MASTER = "master.m3u"    # source of truth (you edit this file)
OUTPUT = "iptv.m3u"      # what the website loads (generated)
STATE = "state.json"     # strike counter per channel URL
MAX_STRIKES = 2          # confirmed-dead scans in a row before hiding (2 = 12 hours). Use 1 to hide at once.
TIMEOUT = 12             # seconds per request
WORKERS = 30             # parallel checks
MIN_ALIVE_RATIO = 0.2    # safety: if fewer than 20% pass, assume a network problem and do nothing
UA = {"User-Agent": "Mozilla/5.0"}


def check_once(url):
    """Return 'ok' (works), 'dead' (confirmed dead) or 'unk' (cannot tell, keep it)."""
    u = url.split("|")[0].strip()           # drop header suffix like |User-Agent=...
    if not re.match(r"https?://", u, re.I):
        return "unk"                         # rtmp/udp etc. cannot be checked here
    try:
        req = urllib.request.Request(u, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            path = u.split("?")[0].lower()
            if re.search(r"\.m3u8?$", path):         # HLS: body must be a real playlist
                return "ok" if b"#EXTM3U" in r.read(4096) else "dead"
            if re.search(r"\.(mpd|md)$", path):      # DASH: body must be a real manifest
                return "ok" if b"<MPD" in r.read(4096) else "dead"
            return "ok"                              # other types: a valid response is enough
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 451):        # blocked for GitHub's location, may work in Bangladesh
            return "unk"
        return "dead"                        # 404, 410, 5xx and so on
    except (socket.timeout, TimeoutError):
        return "unk"                         # slow or silently blocked, not proof of death
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            return "unk"
        return "dead"                        # DNS failure, connection refused, bad SSL
    except Exception:
        return "unk"


def check(url):
    """Check twice before calling a channel dead, to avoid one-off glitches."""
    first = check_once(url)
    return check_once(url) if first == "dead" else first


# ---- 1. Read master.m3u: each entry = its tag lines + the URL line ----
header = "#EXTM3U"
entries = []     # list of (meta_lines, url)
buf = []
for line in open(MASTER, encoding="utf-8").read().splitlines():
    s = line.strip()
    if not s:
        continue
    if s.startswith("#EXTM3U"):
        header = s
    elif s.startswith("#"):
        buf.append(line)                     # #EXTINF, #KODIPROP, #EXTGRP ...
    else:
        entries.append((buf, s))
        buf = []

# ---- 2. Load strike counters ----
strikes = {}
if os.path.exists(STATE):
    try:
        strikes = json.load(open(STATE)).get("strikes", {})
    except Exception:
        strikes = {}

# ---- 3. Scan ALL channels in parallel ----
with ThreadPoolExecutor(WORKERS) as ex:
    results = list(ex.map(lambda e: check(e[1]), entries))

# ---- 4. Decide which channels stay visible ----
out = [header]
new_strikes = {}
alive = hidden = 0
for (meta, url), res in zip(entries, results):
    if res == "ok":
        n = 0                                # healthy: reset strikes
    elif res == "dead":
        n = strikes.get(url, 0) + 1          # confirmed dead: one more strike
    else:
        n = strikes.get(url, 0)              # unknown: keep old count, do not punish
    if n:
        new_strikes[url] = n
    if n >= MAX_STRIKES:
        hidden += 1                          # hidden from iptv.m3u but still in master.m3u
        continue
    alive += 1
    out += meta + [url]

print(f"total={len(entries)} shown={alive} hidden={hidden}")

# ---- 5. Safety net: never publish an almost-empty playlist ----
if entries and alive < len(entries) * MIN_ALIVE_RATIO:
    raise SystemExit("Too few working channels, likely a network problem. Playlist left unchanged.")

# ---- 6. Write results ----
open(OUTPUT, "w", encoding="utf-8").write("\n".join(out) + "\n")
json.dump(
    {"last_run": datetime.now(timezone.utc).isoformat(), "strikes": new_strikes},
    open(STATE, "w"),
)
