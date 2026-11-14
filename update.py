#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox 自建聚合源 —— 自动抓取 / 合并 / 播放测速 / 每日更新

特点:
  * 纯 Python 标准库实现, 不需要 curl / pip 安装任何东西
  * 保留爬虫站(type 3/4), 但使用本仓库自带的干净 CatVodSpider JAR,
    不再引用带 wexguard/扫码广告激活的第三方加固 JAR
  * type 3 站点按 JAR 实际类名过滤, 只保留能加载的类
  * 自动过滤命中"扫码/看广告/激活/卡密/付费"等关键字的站点、直播与解析
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
import zipfile
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

# ── 干净爬虫 JAR (CatVodSpider) ──
# 旧版会把上游配置里的 spider 字段原样合并。那个 JAR 常伪装成 .jpg;md5;xxx,
# 并用 wexguard 加固, 加载后会强制"扫码看广告才能激活"。
# 本仓库改为自带一个从公开源码编译的干净 CatVodSpider:
#   spider/custom_spider.jar   (来源/校验见 spider/README.md)
# 默认 spider 指向本仓库的 JAR, 不再引用任何第三方加固 JAR。
# type 3 站点会按 JAR 里的实际类名过滤, 不支持的类直接丢弃, 避免坏站。
SPIDER_JAR = os.path.join(WORK_DIR, "spider", "custom_spider.jar")
_ENV_SPIDER = os.environ.get("SPIDER_URL", "").strip()
if _ENV_SPIDER:
    SPIDER_URL = _ENV_SPIDER
elif os.environ.get("GITHUB_REPOSITORY"):
    _ref = os.environ.get("GITHUB_REF_NAME", "main")
    SPIDER_URL = (f"https://raw.githubusercontent.com/"
                  f"{os.environ['GITHUB_REPOSITORY']}/{_ref}/spider/custom_spider.jar")
elif os.path.exists(SPIDER_JAR):
    SPIDER_URL = "./spider/custom_spider.jar"
else:
    SPIDER_URL = ""
SPIDER_ENABLED = bool(SPIDER_URL)

_DEFAULT_TYPES = "0,1,3,4" if SPIDER_ENABLED else "0,1"
ALLOWED_TYPES = {int(x) for x in os.environ.get("ALLOWED_TYPES", _DEFAULT_TYPES).split(",")
                 if x.strip().isdigit()}
ALLOWED_TYPES &= ({0, 1, 3, 4} if SPIDER_ENABLED else {0, 1})
if not ALLOWED_TYPES:
    ALLOWED_TYPES = {0, 1, 3, 4} if SPIDER_ENABLED else {0, 1}

AD_BLOCK_DOMAINS = [x.strip().lower() for x in os.environ.get(
    "AD_BLOCK_DOMAINS", "clbug.com,moji.com,oss4liview.moji.com").split(",") if x.strip()]
AD_BLOCK_KEYWORDS = [x.strip() for x in os.environ.get(
    "AD_BLOCK_KEYWORDS",
    "扫码,扫码看,看广告,广告解锁,激活码,激活,卡密,付费,收费,打赏,公众号,加群"
).split(",") if x.strip()]

# ── 成人 / 非成人分类 ──
# 仅用于生成 tvbox_nonadult.json(非成人全部) 和 tvbox_adult.json(成人全部),
# 不影响 tvbox_full.json(全部)。分类是启发式的, 可用 ADULT_KEYWORDS 追加关键字。
ADULT_PATTERNS = [
    r"🔞", r"18\+", r"18禁", r"18av", r"18sex", r"18porn", r"18岁",
    r"18资源", r"18视频", r"181818", r"live18", r"r18", r"nsfw",
    r"成人", r"里番", r"无码", r"有码", r"番号", r"女优", r"麻豆",
    r"蜜桃", r"天美", r"果冻传媒", r"星空传媒", r"精东", r"探花",
    r"约炮", r"一夜情", r"三级片?", r"伦理片?", r"情色", r"色情",
    r"色播", r"色猫", r"色南国", r"色库", r"性爱", r"做爱", r"自慰",
    r"口交", r"淫", r"嫖", r"妓", r"黄片", r"黄网", r"裸",
    r"porn", r"pron", r"hentai", r"jable", r"javdb", r"hanime",
    r"missav", r"supjav", r"netflav", r"sukebei", r"xvideo", r"xnxx",
    r"pornhub", r"onlyfans", r"fansly", r"(?<![a-z])swag(?![a-z])",
    r"91麻豆", r"91精品", r"91porn", r"91av", r"91md", r"91自拍", r"91视频", r"91资源",
    r"(?<![a-z])av(?![a-z])", r"jav(?![a-z])", r"sex",
    r"av片", r"av网", r"av资源", r"成人av",
    r"souav", r"youav", r"owoav", r"avhub", r"avzy", r"kxgav",
    r"(?<![a-z])lav(?![a-z])",
    r"福利视频", r"福利片", r"福利影院", r"福利资源", r"肉视频",
    r"国产传媒", r"几个传媒", r"美腿丝袜", r"花活",
    r"(?<![a-z])xhm(?![a-z])", r"推特",
]
_extra_adult = os.environ.get("ADULT_KEYWORDS", "").strip()
if _extra_adult:
    ADULT_PATTERNS += [re.escape(x.strip()) for x in _extra_adult.split(",") if x.strip()]
ADULT_RE = re.compile("|".join(ADULT_PATTERNS), re.IGNORECASE)

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


def _host(url: str) -> str:
    try:
        return (urlparse(str(url)).hostname or "").lower()
    except Exception:
        return ""


def is_blocked_url(url: str) -> bool:
    """域名命中广告/扫码黑名单。"""
    h = _host(url)
    if not h:
        return False
    return any(h == d or h.endswith("." + d) for d in AD_BLOCK_DOMAINS)


def is_blocked_text(*values) -> bool:
    """名称/字段里出现扫码、激活、卡密等关键字。"""
    blob = " ".join(str(v) for v in values if v).lower()
    return any(k.lower() in blob for k in AD_BLOCK_KEYWORDS)


def is_adult_item(item: dict) -> bool:
    """启发式判断站点/直播是否成人内容。"""
    if not isinstance(item, dict):
        return False
    text = " ".join(str(item.get(k, "")) for k in
                    ("key", "name", "api", "url", "ext"))
    return bool(ADULT_RE.search(text))


def looks_like_binary_plugin(value) -> bool:
    """JAR / APK / DEX, 以及伪装成 .jpg;md5; 的可执行插件。"""
    text = str(value or "").lower()
    if not text:
        return False
    return bool(re.search(r"\.(jar|apk|dex)([?#;]|$)", text)) or ";md5;" in text


def looks_like_plugin(value) -> bool:
    """站点 ext 里可能塞入的 jar/js/apk 插件。"""
    text = str(value or "").lower()
    if not text:
        return False
    return looks_like_binary_plugin(text) or bool(re.search(r"\.js([?#;]|$)", text))


def is_ad_gated_spider(spider: str) -> bool:
    """上游 spider 是否指向需要扫码/广告激活的 JAR(或伪装 JAR)。"""
    s = str(spider or "").strip()
    if not s:
        return False
    return is_blocked_url(s) or is_blocked_text(s) or looks_like_binary_plugin(s)


def load_spider_classes(path: str):
    """从 DEX JAR 里提取 com.github.catvod.spider.* 类名。"""
    names = set()
    if not path or not os.path.exists(path):
        return names
    try:
        with zipfile.ZipFile(path) as z:
            for entry in z.namelist():
                if not entry.endswith(".dex"):
                    continue
                data = z.read(entry)
                for m in re.findall(rb"Lcom/github/catvod/spider/([A-Za-z0-9_$]+);", data):
                    names.add(m.decode("ascii", errors="ignore"))
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 读取爬虫 JAR 失败: {e}", file=sys.stderr)
    return names


SPIDER_CLASSES = load_spider_classes(SPIDER_JAR)


def spider_api_supported(api) -> bool:
    """判断 type 3 站点的 api 是否能被当前 JAR / JS 引擎加载。"""
    a = str(api or "").strip()
    if not a:
        return False
    if a.startswith("csp_"):
        cls = a[4:].split("_", 1)[0]
        return (not SPIDER_CLASSES) or cls in SPIDER_CLASSES
    low = a.lower()
    # JS/Python 爬虫由客户端引擎执行, 不依赖 JAR
    if (low.endswith(".js") or low.startswith(("js:", "py:", "hipy:", "./"))
            or low.startswith(("http://", "https://"))):
        return True
    # 纯类名
    return (not SPIDER_CLASSES) or a.split("_", 1)[0] in SPIDER_CLASSES


def sanitize_site(site: dict):
    """站点清洗: 保留 type 0/1, 以及当前干净 JAR 支持的 type 3/4。"""
    if not isinstance(site, dict):
        return None
    st = site.get("type", -1)
    if st not in ALLOWED_TYPES:
        return None
    if st in (3, 4) and not SPIDER_ENABLED:
        return None
    # 站点自带 spider/jar 字段的一律拒绝(防止绕过顶层 spider 检查)
    if is_ad_gated_spider(site.get("spider") or site.get("spiderUrl") or site.get("jar") or ""):
        return None
    api = site.get("api", "")
    if st in (0, 1) and not (isinstance(api, str) and api.startswith("http")):
        return None
    if st == 3 and not spider_api_supported(api):
        return None

    text = " ".join(str(site.get(k, "")) for k in
                    ("key", "name", "api", "ext", "jar", "spider", "spiderUrl"))
    if is_blocked_text(text) or is_blocked_url(api):
        return None

    clean = dict(site)
    for field in ("jar", "spider", "spiderUrl"):
        clean.pop(field, None)
    ext = clean.get("ext")
    if ext is not None:
        if looks_like_binary_plugin(ext) or is_blocked_url(str(ext)):
            clean.pop("ext", None)
        elif st in (0, 1) and looks_like_plugin(ext):
            # type 0/1 的 ext 只应是普通参数; 出现 .js 视为可疑插件
            clean.pop("ext", None)
    if not str(clean.get("api", "")).strip():
        return None
    return clean


def sanitize_live(live: dict):
    if not isinstance(live, dict):
        return None
    url = live.get("url", "")
    if not (isinstance(url, str) and url.startswith("http")):
        return None
    text = " ".join(str(live.get(k, "")) for k in ("name", "url", "ext"))
    if is_blocked_text(text) or is_blocked_url(url):
        return None
    clean = dict(live)
    ext = clean.get("ext")
    if ext is not None and (looks_like_binary_plugin(ext) or is_blocked_url(str(ext))):
        clean.pop("ext", None)
    return clean


def sanitize_parse(parse: dict):
    if not isinstance(parse, dict):
        return None
    url = parse.get("url", "")
    if not (isinstance(url, str) and url.startswith("http")):
        return None
    text = " ".join(str(parse.get(k, "")) for k in ("name", "url", "ext"))
    if is_blocked_text(text) or is_blocked_url(url) or looks_like_binary_plugin(url):
        return None
    clean = dict(parse)
    ext = clean.get("ext")
    if ext is not None and (looks_like_binary_plugin(ext) or is_blocked_url(str(ext))):
        clean.pop("ext", None)
    return clean


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
            if isinstance(u, str) and u.startswith("http") and not is_blocked_url(u):
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
    collect_sources = {}  # api -> (source_name, stype)
    dropped = {"jar_type": 0, "unsupported": 0, "ad": 0, "live": 0, "parse": 0}
    multi_safe = set()    # 未声明 spider JAR 的源, 才允许进入多仓版

    for name, url, lat, body in avail:
        data = parse_json(body.decode("utf-8", errors="replace"))
        if not data:
            continue

        # 上游声明的 spider 只用来判断该源是否安全, 绝不写入输出
        upstream_spider = data.get("spider", "")
        before = dict(dropped)
        sites_before = len(all_sites)

        for s in (data.get("sites") or []):
            if not isinstance(s, dict):
                continue
            key = s.get("key", "")
            if not key or key in site_keys:
                continue
            st = s.get("type", -1)
            if st not in ALLOWED_TYPES:
                dropped["jar_type"] += 1
                continue
            if st == 3 and not spider_api_supported(s.get("api", "")):
                dropped["unsupported"] += 1
                continue
            clean = sanitize_site(s)
            if not clean:
                dropped["ad"] += 1
                continue
            site_keys.add(key)
            clean["name"] = f"[{lat}ms|{name}] {clean.get('name', key)}"
            clean["_lat"] = lat
            all_sites.append(clean)
            api = clean.get("api", "")
            if st in (0, 1) and isinstance(api, str) and api.startswith("http") \
                    and api not in collect_sources:
                collect_sources[api] = (name, st)

        for l in (data.get("lives") or []):
            clean = sanitize_live(l)
            if not clean:
                dropped["live"] += 1
                continue
            u = clean.get("url", "")
            if u in live_keys:
                continue
            live_keys.add(u)
            all_lives.append(clean)
        for p in (data.get("parses") or []):
            clean = sanitize_parse(p)
            if not clean:
                dropped["parse"] += 1
                continue
            u = clean.get("url", "")
            if u in parse_keys:
                continue
            parse_keys.add(u)
            all_parses.append(clean)

        # 多重仓版审核: 该源必须无 spider、无任何被过滤项, 且有可用内容
        if (not upstream_spider
                and dropped == before
                and (len(all_sites) > sites_before or data.get("storeHouse"))):
            multi_safe.add(url)

    print(f"  合并: {len(all_sites)} 站点 / {len(all_lives)} 直播 / {len(all_parses)} 解析")
    print(f"  去扫码广告过滤: 丢弃 {dropped['jar_type']} 个类型不允许的站点 / "
          f"{dropped['unsupported']} 个当前 JAR 不支持的爬虫站 / "
          f"{dropped['ad']} 个命中广告关键字 / {dropped['live']} 直播 / {dropped['parse']} 解析")
    print(f"  干净爬虫 JAR: {len(SPIDER_CLASSES)} 个类, spider={SPIDER_URL or '无'}")

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

    # 成人 / 非成人分类（用于额外输出两个分类配置）
    adult_sites = [s for s in all_sites if is_adult_item(s)]
    nonadult_sites = [s for s in all_sites if not is_adult_item(s)]
    adult_lives = [l for l in all_lives if is_adult_item(l)]
    nonadult_lives = [l for l in all_lives if not is_adult_item(l)]
    nonadult_parses = [p for p in all_parses if not is_adult_item(p)]

    # ── 4. 全量版 tvbox_full.json ──
    # 全量版保留爬虫站(type 3/4), 但 spider 指向本仓库自带的干净 JAR,
    # 不再引用任何第三方加固 JAR, 因此不会出现"扫码看广告才能激活"。
    full_json = {"spider": SPIDER_URL, "sites": all_sites,
                 "lives": all_lives, "parses": all_parses}
    _write(os.path.join(OUT_DIR, "tvbox_full.json"), full_json)
    types = {}
    for s in all_sites:
        types[s.get("type", -1)] = types.get(s.get("type", -1), 0) + 1
    print(f"  全量版: {len(all_sites)} 站点 (采集:{types.get(0,0)+types.get(1,0)} "
          f"爬虫:{types.get(3,0)+types.get(4,0)}) spider={SPIDER_URL or '未配置'}")

    # ── 4.1 非成人全部 / 成人全部 ──
    nonadult_json = {"spider": SPIDER_URL, "sites": nonadult_sites,
                     "lives": nonadult_lives, "parses": nonadult_parses}
    _write(os.path.join(OUT_DIR, "tvbox_nonadult.json"), nonadult_json)
    adult_json = {"spider": SPIDER_URL, "sites": adult_sites,
                  "lives": adult_lives, "parses": all_parses}
    _write(os.path.join(OUT_DIR, "tvbox_adult.json"), adult_json)
    print(f"  非成人全部: {len(nonadult_sites)} 站点 / {len(nonadult_lives)} 直播")
    print(f"  成人全部: {len(adult_sites)} 站点 / {len(adult_lives)} 直播")

    # ── 5. 多仓版 tvbox_multi.json ──
    # 先放本仓库自己的两个配置(含干净爬虫), 再放未声明 spider JAR 的上游源
    base = _base_url()
    store = []
    if base:
        store.extend([
            {"sourceName": "全部(含干净爬虫)", "sourceUrl": base + "tvbox_full.json"},
            {"sourceName": "非成人全部", "sourceUrl": base + "tvbox_nonadult.json"},
            {"sourceName": "成人全部", "sourceUrl": base + "tvbox_adult.json"},
            {"sourceName": "简洁版(无爬虫)", "sourceUrl": base + "tvbox.json"},
        ])
    pinned_repos = set()
    for api_key, (src_name, _) in collect_sources.items():
        for kw in PINNED:
            if kw in api_key:
                pinned_repos.add(src_name)
    safe_avail = [(n, u, l) for n, u, l, _ in avail
                  if u in multi_safe and not is_blocked_url(u)]
    pa = [x for x in safe_avail if x[0] in pinned_repos]
    oa = [x for x in safe_avail if x[0] not in pinned_repos]
    store += [{"sourceName": f"[{lat}ms] {name}", "sourceUrl": url}
              for name, url, lat in pa + oa]
    multi = {"storeHouse": store}
    _write(os.path.join(OUT_DIR, "tvbox_multi.json"), multi)
    print(f"  多仓版: {len(store)} 个仓库 (含本仓库干净爬虫配置)")

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
            "sites_full": len(all_sites), "spider": SPIDER_URL,
            "spider_kind": "clean-catvodspider",
            "spider_classes": len(SPIDER_CLASSES),
            "sites_nonadult": len(nonadult_sites),
            "sites_adult": len(adult_sites),
            "lives_nonadult": len(nonadult_lives),
            "lives_adult": len(adult_lives),
            "unsupported_spider_sites_removed": dropped["unsupported"],
            "disallowed_type_sites_removed": dropped["jar_type"],
            "ad_keyword_sites_removed": dropped["ad"]}
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
<span class="badge">全量站点 {meta['sites_full']}</span>
<span class="badge">非成人 {meta.get('sites_nonadult', 0)} / 成人 {meta.get('sites_adult', 0)}</span>
<span class="badge">干净爬虫 / 无扫码广告</span></p>
<ul>
  <li><b>简洁版</b>(推荐): {link('tvbox.json')}</li>
  <li><b>全部</b>(含爬虫): {link('tvbox_full.json')}</li>
  <li><b>非成人全部</b>: {link('tvbox_nonadult.json')}</li>
  <li><b>成人全部</b>: {link('tvbox_adult.json')}</li>
  <li><b>多仓版</b>: {link('tvbox_multi.json')}</li>
  <li>状态: {link('status.json')}</li>
</ul>
<p>把上面地址复制到 TVBox / 影视仓 / 丰米(FongMi) 的配置地址即可。</p>
</body></html>"""
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    sys.exit(main())
