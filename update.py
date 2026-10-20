#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox 自建聚合源 —— 自动抓取 / 合并 / 播放测速 / 每日更新

特点:
  * 纯 Python 标准库实现, 不需要 curl / pip 安装任何东西
  * 不依赖任何"扫码看广告"的接口, 源清单由本目录 sources.txt 维护
  * 支持自动发现: 从抓到的多仓配置(storeHouse)里继续发现新的源地址
  * 真实播放测速: m3u8 -> 媒体列表 -> 下载 ts 分片 -> 计算持续速度
  * 输出 tvbox.json / tvbox_full.json / tvbox_multi.json

可在 GitHub Actions 或 Unraid(Docker) 中运行。
所有路径/行为均可用环境变量覆盖, 见下方 CONFIG 区。
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

# ────────────────────────────── CONFIG ──────────────────────────────
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("OUT_DIR", WORK_DIR)
SEED_FILE = os.environ.get("SEED_FILE", os.path.join(WORK_DIR, "sources.txt"))

# 可选: Cloudflare Worker 代理, 用于突破采集站对海外 IP 的封锁, 形如 https://xxx.workers.dev
CF_PROXY = os.environ.get("CF_PROXY", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

TIMEOUT = int(os.environ.get("TIMEOUT", "12"))          # 抓取单个源超时(秒)
MAX_SOURCES = int(os.environ.get("MAX_SOURCES", "300"))  # 最多抓取多少源
MAX_COLLECT = int(os.environ.get("MAX_COLLECT", "80"))   # 最多对多少个采集站做播放测速
SIMPLE_LIMIT = int(os.environ.get("SIMPLE_LIMIT", "10"))  # 简洁版保留多少个站
WORKERS = int(os.environ.get("WORKERS", "16"))            # 并发线程数
DISCOVER = os.environ.get("DISCOVER", "1") == "1"         # 是否自动发现新源
DISCOVER_LIMIT = int(os.environ.get("DISCOVER_LIMIT", "80"))

# 置顶采集站(按关键字匹配 api 域名), 会排在最前面
PINNED = [x for x in os.environ.get("PINNED", "suoniapi.com,360zy.com").split(",") if x]

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


# ────────────────────────────── HTTP ──────────────────────────────
def _proxied(url: str) -> str:
    if not CF_PROXY:
        return url
    sep = "&" if "?" in CF_PROXY else "?"
    return CF_PROXY + sep + "u=" + urllib.parse.quote(url, safe="")


def fetch(url: str, timeout: int = TIMEOUT, via_proxy: bool = False,
          max_bytes: int = 0, retries: int = 1):
    """返回 (status, body_bytes, elapsed_ms)。失败 status=0。"""
    target = _proxied(url) if via_proxy else url
    last_ms = 0
    for attempt in range(retries):
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(target, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
                if max_bytes:
                    body = r.read(max_bytes)
                else:
                    body = r.read()
                # 处理 gzip(部分服务器无视请求头直接压缩)
                if r.headers.get("Content-Encoding") == "gzip":
                    try:
                        body = gzip.decompress(body)
                    except Exception:
                        pass
                return r.status, body, int((time.perf_counter() - t0) * 1000)
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except Exception:
                body = b""
            return e.code, body, int((time.perf_counter() - t0) * 1000)
        except Exception:
            last_ms = int((time.perf_counter() - t0) * 1000)
            if attempt + 1 < retries:
                time.sleep(0.5)
    return 0, b"", last_ms


def download(url: str, timeout: int = 20, cap: int = 4 * 1024 * 1024):
    """下载并返回 (status, bytes, seconds)，用于分片测速。"""
    t0 = time.perf_counter()
    total = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            status = r.status
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total >= cap:
                    break
        return status, total, time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        return e.code, total, time.perf_counter() - t0
    except Exception:
        return 0, total, time.perf_counter() - t0


# ────────────────────────────── 解析工具 ──────────────────────────────
def parse_json(raw: str):
    if not raw:
        return None
    raw = raw.lstrip("\ufeff")
    raw = re.sub(r",(\s*[}\]])", r"\1", raw)
    try:
        return json.loads(raw, strict=False)
    except Exception:
        s, e = raw.find("{"), raw.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(raw[s:e + 1], strict=False)
            except Exception:
                pass
    return None


def resolve_spider(spider: str, source_url: str) -> str:
    if not spider:
        return ""
    if spider.startswith("http"):
        return spider
    if spider.startswith("./"):
        p = urlparse(source_url)
        return f"{p.scheme}://{p.netloc}{spider[1:]}"
    return spider


def resolve_url(base: str, path: str) -> str:
    if path.startswith("http"):
        return path
    if path.startswith("/"):
        p = urlparse(base)
        return f"{p.scheme}://{p.netloc}{path}"
    return urljoin(base, path)


def extract_m3u8(t: str):
    return re.findall(r'(https?://[^\s"\'<>#\$]+?\.m3u8[^\s"\'<>#\$]*)', t)


def get_segments(media: str, media_url: str):
    urls, lines = [], media.strip().split("\n")
    for i, line in enumerate(lines):
        if line.startswith("#EXTINF") and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt and not nxt.startswith("#"):
                urls.append(resolve_url(media_url, nxt))
    return urls


def build_url(base: str, params: str) -> str:
    return base.rstrip("/") + ("&" if "?" in base else "?") + params


# ────────────────────────────── 播放测速 ──────────────────────────────
def test_play_speed(api: str, stype: int, use_proxy: bool = False):
    """真实播放测速。返回 (首帧ms, 速度KB/s, 状态)。"""
    base = re.sub(r'[?&]ac=list.*', '', api.rstrip("/"))
    st, body, _ = fetch(build_url(base, "ac=list"), 15, via_proxy=use_proxy)
    if not body or len(body) < 50:
        return 0, 0, "列表失败"
    text = body.decode("utf-8", errors="replace")

    if stype == 0:
        vids = re.findall(r'<id>(\d+)</id>', text)[:3]
    else:
        j = parse_json(text)
        if not j:
            return 0, 0, "解析失败"
        vids = [str(v.get("vod_id")) for v in (j.get("list") or [])[:3] if v.get("vod_id")]
    if not vids:
        return 0, 0, "无ID"

    for vid in vids:
        st, detail_b, _ = fetch(build_url(base, f"ac=detail&ids={vid}"), 15, via_proxy=use_proxy)
        if not detail_b:
            continue
        detail = detail_b.decode("utf-8", errors="replace")
        m3u8s = []
        if stype == 0:
            m3u8s = extract_m3u8(detail)
        else:
            dj = parse_json(detail)
            if not dj:
                continue
            for v in (dj.get("list") or []):
                m3u8s.extend(extract_m3u8(v.get("vod_play_url", "")))
        if not m3u8s:
            continue

        for play in m3u8s[:2]:
            t0 = time.perf_counter()
            st, master_b, _ = fetch(play, 15, via_proxy=use_proxy)
            ttfb = int((time.perf_counter() - t0) * 1000)
            if not master_b:
                continue
            master = master_b.decode("utf-8", errors="replace")
            media_url = None
            if "#EXT-X-STREAM-INF" in master:
                lines = master.strip().split("\n")
                for i, line in enumerate(lines):
                    if "STREAM-INF" in line:
                        sub = lines[i + 1].strip() if i + 1 < len(lines) else ""
                        if sub and not sub.startswith("#"):
                            media_url = resolve_url(play, sub)
                            break
            elif "#EXTINF" in master:
                media_url = play
            if not media_url:
                continue

            t1 = time.perf_counter()
            st, media_b, _ = fetch(media_url, 15, via_proxy=use_proxy)
            mms = int((time.perf_counter() - t1) * 1000)
            media = media_b.decode("utf-8", errors="replace")
            if "#EXTINF" not in media:
                continue
            segs = get_segments(media, media_url)
            if not segs:
                continue

            tb, tt, ok = 0, 0.0, 0
            for s in segs[:8]:
                if ok >= 3:
                    break
                seg_url = _proxied(s) if use_proxy else s
                code, sz, dl = download(seg_url, timeout=20)
                if code and 200 <= code < 300 and sz > 1000:
                    tb += sz
                    tt += dl
                    ok += 1
            if ok >= 2 and tt > 0:
                speed = int((tb / 1024) / tt)
                return ttfb + mms, speed, "OK"
    return 0, 0, "全部失败"


# ────────────────────────────── 源清单 ──────────────────────────────
def load_seed(path: str):
    """支持两种格式:
       1) 每行一个 URL (# 开头为注释)
       2) [延迟] 名称 + 下一行 URL  (与旧版 sources.txt 兼容)
    """
    out, pending = [], ""
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r'^\[[^\]]*\]\s*(.*)$', line)
            if m:
                pending = m.group(1).strip()
                continue
            if line.startswith("http"):
                name = pending or urlparse(line).netloc
                out.append((name, line))
                pending = ""
    # 去重保序
    seen, res = set(), []
    for n, u in out:
        if u in seen:
            continue
        seen.add(u)
        res.append((n, u))
    return res


def discover_from(data, source_url: str):
    """从多仓配置(storeHouse)中发现新的源地址。"""
    found = []
    if isinstance(data, dict):
        for item in (data.get("storeHouse") or []):
            u = item.get("sourceUrl") or item.get("url") or ""
            if isinstance(u, str) and u.startswith("http"):
                found.append((item.get("sourceName") or urlparse(u).netloc, u))
    return found


# ────────────────────────────── 主流程 ──────────────────────────────
def main() -> int:
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] 开始更新 ...  (OUT_DIR={OUT_DIR})")
    os.makedirs(OUT_DIR, exist_ok=True)

    seed = load_seed(SEED_FILE)[:MAX_SOURCES]
    print(f"  种子源清单: {len(seed)} 个 (上限 {MAX_SOURCES})")

    # ── 1. 并发测延迟 + 抓取 ──
    avail, discovered = [], []

    def check(item):
        name, url = item
        st, body, ms = fetch(url, timeout=10, max_bytes=3 * 1024 * 1024)
        if st and 200 <= st < 400 and len(body) > 20:
            return (name, url, ms, body)
        return None

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(check, it): it for it in seed}
        done = 0
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            if r:
                avail.append(r)
            sys.stdout.write(f"\r  抓取: {done}/{len(seed)}  可用:{len(avail)}")
            sys.stdout.flush()
    print()

    # 自动发现(第一轮)
    if DISCOVER:
        for name, url, ms, body in avail:
            data = parse_json(body.decode("utf-8", errors="replace"))
            if data:
                discovered.extend(discover_from(data, url))
        have = {u for _, u in seed}
        extra = []
        for n, u in discovered:
            if u not in have:
                have.add(u)
                extra.append((n, u))
            if len(extra) >= DISCOVER_LIMIT:
                break
        if extra:
            print(f"  自动发现新源: {len(extra)} 个, 继续抓取 ...")
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futs = {ex.submit(check, it): it for it in extra}
                for fut in as_completed(futs):
                    r = fut.result()
                    if r:
                        avail.append(r)

    avail.sort(key=lambda x: x[2])
    print(f"  可用源: {len(avail)}/{len(seed)}")

    # ── 2. 合并所有站点 ──
    all_sites, all_lives, all_parses = [], [], []
    site_keys, live_keys, parse_keys = set(), set(), set()
    spider_jars = {}
    collect_sources = {}  # api -> (source_name, stype)

    for name, url, lat, body in avail:
        data = parse_json(body.decode("utf-8", errors="replace"))
        if not data:
            continue
        spider = data.get("spider", "")
        if spider:
            abs_spider = resolve_spider(spider, url)
            if abs_spider:
                spider_jars[abs_spider] = spider_jars.get(abs_spider, 0) + 1

        for s in (data.get("sites") or []):
            key = s.get("key", "")
            if not key or key in site_keys:
                continue
            site_keys.add(key)
            s["name"] = f"[{lat}ms|{name}] {s.get('name', key)}"
            s["_lat"] = lat
            all_sites.append(s)
            st = s.get("type", -1)
            api = s.get("api", "")
            if st in (0, 1) and isinstance(api, str) and api.startswith("http") \
                    and api not in collect_sources:
                collect_sources[api] = (name, st)

        for l in (data.get("lives") or []):
            u = l.get("url", "")
            if u and u not in live_keys:
                live_keys.add(u)
                all_lives.append(l)
        for p in (data.get("parses") or []):
            u = p.get("url", "")
            if u and u not in parse_keys:
                parse_keys.add(u)
                all_parses.append(p)

    print(f"  合并: {len(all_sites)} 站点 / {len(all_lives)} 直播 / {len(all_parses)} 解析")

    # ── 3. 采集站播放测速 ──
    apis = list(collect_sources.items())[:MAX_COLLECT]
    print(f"  播放测速: 共 {len(apis)} 个采集站 (上限 {MAX_COLLECT}) ...")
    collect_results = []

    def speed_one(api, stype, src_name):
        for attempt in range(3):
            use_proxy = (attempt == 2 and CF_PROXY)
            ttfb, speed, st = test_play_speed(api, stype, use_proxy=use_proxy)
            if st == "OK":
                return (ttfb, speed, api, stype)
            if attempt < 2:
                time.sleep(1)
        return None

    with ThreadPoolExecutor(max_workers=max(4, WORKERS // 2)) as ex:
        futs = {ex.submit(speed_one, api, st, nm): api for api, (nm, st) in apis}
        done = 0
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            if r:
                collect_results.append(r)
            sys.stdout.write(f"\r  测速进度: {done}/{len(apis)}  可用:{len(collect_results)}")
            sys.stdout.flush()
    print()

    collect_results.sort(key=lambda x: (-x[1], x[0]))

    # 置顶规则
    pinned = [[] for _ in PINNED]
    rest = []
    for item in collect_results:
        api = item[2]
        placed = False
        for i, kw in enumerate(PINNED):
            if kw in api:
                pinned[i].append(item)
                placed = True
                break
        if not placed:
            rest.append(item)
    collect_results = [x for g in pinned for x in g] + rest

    speed_map = {api: (ttfb, speed) for ttfb, speed, api, _ in collect_results}
    for s in all_sites:
        api = s.get("api", "")
        if api in speed_map:
            s["_speed"] = speed_map[api][1]
            s["_speed_ttfb"] = speed_map[api][0]

    def full_sort_key(s):
        st = s.get("type", -1)
        if st in (0, 1):
            return (0, -s.get("_speed", 0), s.get("_speed_ttfb", 99999), s.get("_lat", 99999))
        return (1, 0, 0, s.get("_lat", 99999))

    all_sites.sort(key=full_sort_key)

    pinned_sites = [[] for _ in PINNED]
    other_collect, other_sites = [], []
    for s in all_sites:
        if s.get("type") not in (0, 1):
            other_sites.append(s)
            continue
        api = s.get("api", "")
        placed = False
        for i, kw in enumerate(PINNED):
            if kw in api:
                pinned_sites[i].append(s)
                placed = True
                break
        if not placed:
            other_collect.append(s)
    all_sites = [x for g in pinned_sites for x in g] + other_collect + other_sites
    for s in all_sites:
        s.pop("_lat", None)
        s.pop("_speed", None)
        s.pop("_speed_ttfb", None)

    # ── 4. 全量版 tvbox_full.json ──
    best_spider = max(spider_jars, key=spider_jars.get) if spider_jars else ""
    full_json = {"spider": best_spider, "sites": all_sites,
                 "lives": all_lives, "parses": all_parses}
    _write(os.path.join(OUT_DIR, "tvbox_full.json"), full_json)
    types = {}
    for s in all_sites:
        types[s.get("type", -1)] = types.get(s.get("type", -1), 0) + 1
    print(f"  全量版: {len(all_sites)} 站点 (采集:{types.get(0,0)+types.get(1,0)} "
          f"爬虫:{types.get(3,0)}) spider={best_spider or '无'}")

    # ── 5. 多仓版 tvbox_multi.json ──
    pinned_repos = set()
    for api_key, (src_name, _) in collect_sources.items():
        for kw in PINNED:
            if kw in api_key:
                pinned_repos.add(src_name)
    pa = [(n, u, l) for n, u, l, _ in avail if n in pinned_repos]
    oa = [(n, u, l) for n, u, l, _ in avail if n not in pinned_repos]
    multi = {"storeHouse": [{"sourceName": f"[{lat}ms] {name}", "sourceUrl": url}
                            for name, url, lat in pa + oa]}
    _write(os.path.join(OUT_DIR, "tvbox_multi.json"), multi)
    print(f"  多仓版: {len(multi['storeHouse'])} 个仓库")

    # ── 6. 简洁版 tvbox.json ──
    collect_sites = []
    for ttfb, speed, api, stype in collect_results[:SIMPLE_LIMIT]:
        clean_name = api.split("/")[2] if "//" in api else api
        for s in all_sites:
            if s.get("api") == api:
                clean_name = re.sub(r'^\[.*?\]\s*', '', s.get("name", clean_name))
                break
        stable = "稳" if speed > 500 else "中" if speed > 100 else "慢"
        collect_sites.append({
            "key": clean_name,
            "name": f"[{speed}KB/s|{ttfb}ms|{stable}] {clean_name}",
            "type": stype, "api": api,
            "searchable": 1, "quickSearch": 1, "filterable": 0,
        })
    simple = {"spider": "", "sites": collect_sites, "lives": [], "parses": []}
    _write(os.path.join(OUT_DIR, "tvbox.json"), simple)
    print(f"  简洁版: {len(collect_sites)}/{SIMPLE_LIMIT} 站 (共 {len(collect_results)} 个可用)")
    for i, (ttfb, speed, api, _) in enumerate(collect_results[:SIMPLE_LIMIT], 1):
        host = api.split("/")[2] if "//" in api else api
        print(f"    #{i} [{speed}KB/s|{ttfb}ms] {host}")

    # ── 7. 元数据 + 落地页 ──
    meta = {"updated": ts, "sources_available": len(avail),
            "collect_available": len(collect_results),
            "sites_full": len(all_sites), "spider": best_spider}
    _write(os.path.join(OUT_DIR, "status.json"), meta)
    _write_index(OUT_DIR, meta)

    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 更新完成!")
    return 0


def _write(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _base_url() -> str:
    if os.environ.get("PUBLIC_BASE"):
        return os.environ["PUBLIC_BASE"].rstrip("/") + "/"
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        ref = os.environ.get("GITHUB_REF_NAME", "main")
        return f"https://raw.githubusercontent.com/{repo}/{ref}/"
    return ""


def _write_index(out_dir: str, meta: dict) -> None:
    base = _base_url()
    def link(f):
        return f'<a href="{base}{f}">{f}</a>'
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TVBox 自建源</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
max-width:760px;margin:40px auto;padding:0 16px;line-height:1.7;color:#1f2937}}
h1{{font-size:1.6rem}} code,pre{{background:#f3f4f6;border-radius:6px}}
code{{padding:2px 6px}} pre{{padding:12px;overflow:auto}}
.badge{{display:inline-block;background:#eef2ff;color:#4338ca;border-radius:999px;
padding:2px 10px;font-size:.8rem;margin-right:6px}}</style></head><body>
<h1>📺 TVBox 自建聚合源</h1>
<p><span class="badge">更新于 {meta['updated']}</span>
<span class="badge">可用源 {meta['sources_available']}</span>
<span class="badge">全量站点 {meta['sites_full']}</span></p>
<ul>
  <li><b>简洁版</b>(推荐): {link('tvbox.json')}</li>
  <li><b>全量版</b>: {link('tvbox_full.json')}</li>
  <li><b>多仓版</b>: {link('tvbox_multi.json')}</li>
  <li>状态: {link('status.json')}</li>
</ul>
<p>把上面地址复制到 TVBox / 影视仓 / 丰米(FongMi) 的配置地址即可。</p>
</body></html>"""
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    sys.exit(main())
