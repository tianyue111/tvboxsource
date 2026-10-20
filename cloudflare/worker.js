/**
 * TVBox 源 —— Cloudflare Worker 代理 (可选)
 *
 * 用途 1: 作为 update.py 的 CF_PROXY, 突破采集站对 GitHub Actions 海外 IP 的封锁
 *   部署后在仓库 Secrets 里配置 CF_PROXY = https://<你的worker>.workers.dev
 *   update.py 会以 https://<worker>/?u=<编码后的URL> 形式请求
 *
 * 用途 2: 直接作为 TVBox 配置地址(国内加速)
 *   https://<worker>.workers.dev/tvbox.json   -> 回源到 GitHub raw
 *
 * 部署: Cloudflare Dashboard -> Workers -> 创建 -> 粘贴本文件 -> 部署
 */
const UPSTREAM = "https://raw.githubusercontent.com/你的用户名/tvboxsource/main";

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // 模式一: 通用代理 ?u=<encoded url>
    const target = url.searchParams.get("u");
    if (target) {
      return proxy(target, request);
    }

    // 模式二: 直接当配置源用, /tvbox.json -> 回源
    const path = url.pathname === "/" ? "/tvbox.json" : url.pathname;
    if (/^\/(tvbox(_full|_multi)?\.json|status\.json|index\.html)$/.test(path)) {
      return proxy(UPSTREAM + path, request);
    }

    return new Response("TVBox Proxy OK\n用法: /tvbox.json 或 /?u=<encoded-url>", {
      status: 200,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};

async function proxy(target, request) {
  let resp;
  try {
    resp = await fetch(target, {
      headers: {
        "User-Agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        Accept: "*/*",
      },
      redirect: "follow",
    });
  } catch (e) {
    return new Response("upstream error: " + e.message, { status: 502 });
  }
  const headers = new Headers(resp.headers);
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Cache-Control", "public, max-age=300");
  headers.delete("content-security-policy");
  return new Response(resp.body, { status: resp.status, headers });
}
