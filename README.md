# TVBox 自建聚合源

自动抓取公开 TVBox 源 → 过滤广告插件 → 合并 → 真实播放测速 → 每天由 GitHub Actions 自动更新。
**不加载任何外部 spider JAR，不需要扫码看广告激活。**

> 本项目参考了 [25175/tvyuan](https://github.com/25175/tvyuan) 的思路。原项目/上游配置里的
> `spider` JAR（常伪装成 `.jpg;md5;...`，并用 `wexguard` 加固）可能内置
> “扫码看广告才能激活”的逻辑，本仓库已将其彻底移除：
> 输出配置的 `spider` 永远为空，只保留 type 0/1 直连采集站。
> 源清单由本仓库的 [`sources.txt`](./sources.txt) 自行维护，并支持从多仓配置里自动发现新源。

---

## 零、本次改写：去掉了什么？

| 位置 | 旧行为 | 现在的行为 |
|------|--------|------------|
| `spider` 字段 | 原样合并上游 JAR，可能弹扫码/广告激活 | **永远输出空字符串**，客户端不会下载任何 JAR |
| type 3/4 站点 | 依赖 JAR 才能播放 | 默认全部丢弃，只保留 type 0/1 直连采集 API |
| 站点/直播/解析 | 原样合并 | 命中 `扫码/看广告/激活/卡密/付费/收费/公众号/加群` 等关键字直接过滤 |
| 域名 | 无限制 | `clbug.com`、`moji.com` 等广告/白嫖域名加入黑名单 |
| 多仓版 | 收录所有源 | 只收录**未声明 spider JAR** 的源，避免切换仓库又加载到广告插件 |
| Actions | 生成后直接提交 | 生成后先跑校验：`spider` 必须为空、站点必须是 type 0/1、无广告域名，否则拒绝提交 |

> 如果你有自己编译、可审计的可信 JAR，可以用环境变量 `SPIDER_URL` 显式指定；
> 默认为空，**普通用户不要设置**。

---

## 一、它到底是怎么工作的？

1. 读取 `sources.txt` 里维护的一批公开 TVBox 配置地址（GitHub / Gitee / 各类镜像）。
2. 并发测延迟并抓取每个配置，只保留 `sites` / `lives` / `parses`，**丢弃上游 `spider`**。
3. 过滤广告/扫码/激活类站点、直播、解析和多仓源（见上表）。
4. 对采集站（type 0/1）做**真实播放测速**：请求列表 → 详情 → 拉 m3u8 → 下载 ts 分片 → 算持续速度。
5. 按速度排序，输出三个文件：
   - `tvbox.json`：简洁版（最快的前 10 个采集站，无需 JAR，开箱即用）
   - `tvbox_full.json`：全量版（只含 type 0/1 采集站，`spider` 为空）
   - `tvbox_multi.json`：多仓版（只收录无 spider JAR 的源，可在客户端里切换）
6. 从抓到的多仓配置（`storeHouse`）里自动发现新的源，越跑越多。

**为什么不再需要扫码？** 需要扫码看广告的只是 `clbug.com` 的源清单页面，以及某些
加固 JAR 里的激活逻辑。本仓库把源清单固定在自己的 `sources.txt`，并且从不加载 JAR。

---

## 二、方案 A：GitHub Actions 全自动（推荐，零成本）

### 1. 创建仓库
把本目录全部文件推送到你的 GitHub 仓库（例如 `你的用户名/tvboxsource`，分支 `main`）。

```bash
git init
git add -A
git commit -m "init"
git branch -M main
git remote add origin https://github.com/你的用户名/tvboxsource.git
git push -u origin main
```

### 2. 开启 Actions 写权限
仓库 → **Settings → Actions → General → Workflow permissions** →
选 **Read and write permissions** → Save。

> 这样 `.github/workflows/update.yml` 才能把生成的 JSON 提交回仓库。

### 3. 运行一次
仓库 → **Actions → 更新 TVBox 源 → Run workflow**。
几分钟后仓库根目录会出现 `tvbox.json` / `tvbox_full.json` / `tvbox_multi.json`。

### 4. 拿到配置地址
把下面地址复制到 TVBox / 影视仓 / 丰米(FongMi) 的配置地址：

| 版本 | 地址 |
|------|------|
| 简洁版（推荐） | `https://raw.githubusercontent.com/你的用户名/tvboxsource/main/tvbox.json` |
| 全量版 | `https://raw.githubusercontent.com/你的用户名/tvboxsource/main/tvbox_full.json` |
| 多仓版 | `https://raw.githubusercontent.com/你的用户名/tvboxsource/main/tvbox_multi.json` |

**国内加速**（GitHub raw 可能慢）：
- jsDelivr：`https://cdn.jsdelivr.net/gh/你的用户名/tvboxsource@main/tvbox.json`
- gh-proxy：`https://gh-proxy.com/https://raw.githubusercontent.com/你的用户名/tvboxsource/main/tvbox.json`
- 或开启 GitHub Pages：Settings → Pages → Deploy from branch（main / root），
  得到 `https://你的用户名.github.io/tvboxsource/tvbox.json`

### 5. 自动更新频率
默认每天 **UTC 20:17（北京时间 04:17）** 跑一次。改 `.github/workflows/update.yml` 里的
`cron` 即可（支持小时级，如 `17 * * * *` 表示每小时）。

---

## 三、方案 B：Unraid 本地部署（Docker）

适合想完全自己掌控、或不想依赖 GitHub 的情况。容器内自带定时器，每天自动更新，
并通过 HTTP 直接给局域网里的电视盒子提供配置。

### 方式 1：直接用 GitHub Actions 构建好的镜像（最省事）

本项目的 `.github/workflows/docker.yml` 会在每次推送时把镜像发布到 GHCR：
`ghcr.io/你的用户名/tvboxsource:latest`。

在 Unraid 里：
1. **Docker → Add Container**
2. Name：`tvboxsource`
3. Repository：`ghcr.io/你的用户名/tvboxsource:latest`
4. Network Type：`Bridge`
5. Port：容器 `8787` → 主机 `8787`
6. Path：容器 `/data` → 主机 `/mnt/user/appdata/tvboxsource`
7. 环境变量：
   - `TZ=Asia/Shanghai`
   - `UPDATE_INTERVAL_HOURS=24`
   - `RUN_ON_START=1`
8. Apply。

电视盒子配置地址：`http://<Unraid的IP>:8787/tvbox.json`

### 方式 2：docker compose（Unraid 终端里）

```bash
# 编辑 docker-compose.yml 把镜像名/路径改成自己的
docker compose up -d
```

### 方式 3：本地构建镜像

```bash
git clone https://github.com/你的用户名/tvboxsource.git
cd tvboxsource
docker build -t tvboxsource:latest .
docker run -d --name tvboxsource --restart unless-stopped \
  -p 8787:8787 \
  -e TZ=Asia/Shanghai \
  -e UPDATE_INTERVAL_HOURS=24 \
  -v /mnt/user/appdata/tvboxsource:/data \
  tvboxsource:latest
```

### 常用操作
- 查看状态：`http://<Unraid的IP>:8787/status.json`
- 立即刷新一次：浏览器打开 `http://<Unraid的IP>:8787/update`
- 查看日志：Unraid Docker 页面点容器图标 → Logs

---

## 四、自定义源清单

编辑 [`sources.txt`](./sources.txt)，两种写法都支持：

```
# 每行一个地址
https://raw.githubusercontent.com/xxx/yyy/main/config.json

# 也可以带名字（[..] 开头那行会被当作名字）
[我的源]
https://example.com/tvbox.json
```

- 删除某行即可不再抓取该源。
- 推送到 GitHub 会自动触发一次更新（workflow 里配置了 `push` 触发）。
- Unraid 版改完 `sources.txt` 后重启容器即可。

---

## 五、可选：Cloudflare Worker（突破海外 IP 封锁 / 国内加速）

很多采集站会封锁 GitHub Actions 的海外 IP，导致测速全部失败。用一个免费 CF Worker
代理即可解决，同时也能给国内客户端加速。

1. 打开 Cloudflare Dashboard → Workers → 创建。
2. 粘贴 [`cloudflare/worker.js`](./cloudflare/worker.js)，把里面的 `UPSTREAM` 改成你的仓库地址，部署。
3. 得到 `https://xxx.workers.dev`：
   - **给更新脚本用**：在仓库 Settings → Secrets → Actions 新建 `CF_PROXY = https://xxx.workers.dev`
     （Unraid 版则加环境变量 `CF_PROXY`）。
   - **给客户端用**：直接填 `https://xxx.workers.dev/tvbox.json`。

---

## 六、常见问题

**Q：还需要扫码看广告吗？**
不需要。`tvbox.json` / `tvbox_full.json` 的 `spider` 永远为空，客户端不会下载任何 JAR；
只保留 type 0/1 直连采集站。workflow 还会在提交前自动校验，发现 `spider`、
type 3/4 站点或广告域名就直接失败，不会把带广告的配置推回仓库。

**Q：为什么全量版的站点变少了？**
旧版全量版里约 1000 个 type 3 站点都依赖同一个第三方 JAR（就是带扫码广告激活的那个）。
去掉 JAR 后这些站点无法播放，所以全部丢弃；保留的 type 0/1 采集站不需要任何插件。

**Q：我想恢复爬虫站怎么办？**
不建议。如果你确实有自己编译、可审计的 JAR，可在 workflow 的 `SPIDER_URL` 填入地址；
默认请保持为空。

**Q：为什么有些源测速失败？**
部分采集站封锁海外 IP / 需要特定线路。配置 `CF_PROXY` 后脚本会自动用代理重试一次。

**Q：简洁版只有几个站？**
简洁版只保留测速成功且最快的采集站，宁缺毋滥。想看全部请用全量版。

**Q：GitHub Actions 会被限流吗？**
每天一次、单次约几分钟，远低于免费额度，基本不会。建议不要设成每小时。

**Q：Unraid 端口被占用？**
把容器端口 `8787` 映射到其他主机端口，如 `8790:8787`。

**Q：数据来源是否合规？**
本项目仅聚合公开可访问的 TVBox 配置地址，不存储、不转发任何影视内容，请自行评估使用。

---

## 七、文件说明

| 文件 | 说明 |
|------|------|
| `update.py` | 核心脚本：抓取 / 过滤广告插件 / 测速 / 生成 JSON（纯标准库，无第三方依赖） |
| `app.py` | Unraid/Docker 服务：内置定时更新 + HTTP 提供配置 |
| `sources.txt` | 源清单（自行维护） |
| `Dockerfile` / `docker-compose.yml` | 容器构建 |
| `cloudflare/worker.js` | 可选代理 |
| `.github/workflows/update.yml` | 每天自动更新 + 去广告校验 |
| `.github/workflows/docker.yml` | 自动构建并推送镜像到 GHCR |
