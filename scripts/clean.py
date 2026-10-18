import re, json, os, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
SRC, STATE, MAX_STRIKES = "iptv.m3u", "state.json", 3
UA = {"User-Agent": "Mozilla/5.0"}

def check(url):
    """True=ভালো, False=নিশ্চিত মৃত, None=অনিশ্চিত (রাখা হবে)"""
    u = url.split("|")[0].strip()
    if not re.match(r"https?://", u): return None
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=15) as r:
            if re.search(r"\.m3u8?($|\?)", u, re.I):
                return b"#EXTM3U" in r.read(4096)
            return True
    except urllib.error.HTTPError as e:
        return False if e.code in (404, 410) else None  # 403 = geo-block হতে পারে
    except Exception:
        return None

lines = open(SRC, encoding="utf-8").read().splitlines()
head, entries, buf = [], [], []
for l in lines:
    if l.startswith("#EXTM3U"): head.append(l)
    elif l.startswith("#"): buf.append(l)
    elif l.strip(): entries.append((buf, l)); buf = []
state = json.load(open(STATE)) if os.path.exists(STATE) else {}
with ThreadPoolExecutor(20) as ex:
    res = list(ex.map(lambda e: check(e[1]), entries))
out, removed = list(head), 0
for (meta, url), ok in zip(entries, res):
    if ok is True: state.pop(url, None)
    elif ok is False: state[url] = state.get(url, 0) + 1
    if state.get(url, 0) >= MAX_STRIKES: removed += 1; continue
    out += meta + [url]
open(SRC, "w", encoding="utf-8").write("\n".join(out) + "\n")
json.dump(state, open(STATE, "w"))
print("removed", removed)
