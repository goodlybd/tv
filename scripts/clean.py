"""
Scans EVERY channel in master.m3u (including ones that were dead before), then writes:
  iptv.m3u : active channels (the website plays these)
  dead.m3u : dead channels   (the website shows them in the Dead section)
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
OUTPUT = "iptv.m3u"      # active channels (generated)
DEAD = "dead.m3u"        # dead channels (generated)
STATE = "state.json"     # strike counters per channel URL
MAX_STRIKES = 2          # confirmed-dead scans in a row before moving to dead (2 x 6h = 12h). Use 1 to move at once.
MAX_SLOW_STRIKES = 4     # silent timeouts in a row before moving to dead (4 x 6h = 24h)
TIMEOUT = 12             # seconds per request
WORKERS = 30             # parallel checks
MIN_ALIVE_RATIO = 0.2    # safety: if fewer than 20% pass, assume a network problem and do nothing
UA = {"User-Agent": "Mozilla/5.0"}


def check_once(url):
    """Return 'ok', 'dead' (confirmed), 'slow' (timeout) or 'unk' (cannot tell, stays active)."""
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
        return "slow"                        # silent timeout: counted separately
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            return "slow"
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
strikes, slow = {}, {}
if os.path.exists(STATE):
    try:
        st = json.load(open(STATE))
        strikes = st.get("strikes", {})
        slow = st.get("slow", {})
    except Exception:
        pass

# ---- 3. Scan ALL channels in parallel ----
with ThreadPoolExecutor(WORKERS) as ex:
    results = list(ex.map(lambda e: check(e[1]), entries))

# ---- 4. Split into active and dead ----
out = [header]
dead_out = [header]
new_strikes, new_slow = {}, {}
alive = hidden = 0
for (meta, url), res in zip(entries, results):
    n = strikes.get(url, 0)      # confirmed-dead count
    s = slow.get(url, 0)         # timeout count
    if res == "ok":
        n = s = 0                # healthy: reset both
    elif res == "dead":
        n += 1
    elif res == "slow":
        s += 1
    # "unk" (403 etc.): keep old counts, channel stays active
    if n:
        new_strikes[url] = n
    if s:
        new_slow[url] = s
    if n >= MAX_STRIKES or s >= MAX_SLOW_STRIKES:
        hidden += 1
        dead_out += meta + [url]   # goes to dead.m3u
        continue
    alive += 1
    out += meta + [url]            # goes to iptv.m3u

print(f"total={len(entries)} active={alive} dead={hidden}")

# ---- 5. Safety net: never publish an almost-empty playlist ----
if entries and alive < len(entries) * MIN_ALIVE_RATIO:
    raise SystemExit("Too few working channels, likely a network problem. Playlist left unchanged.")

# ---- 6. Write results ----
open(OUTPUT, "w", encoding="utf-8").write("\n".join(out) + "\n")
open(DEAD, "w", encoding="utf-8").write("\n".join(dead_out) + "\n")
json.dump(
    {"last_run": datetime.now(timezone.utc).isoformat(),
     "strikes": new_strikes, "slow": new_slow},
    open(STATE, "w"),
)
