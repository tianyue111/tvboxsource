# 干净爬虫 JAR（CatVodSpider）

`custom_spider.jar` 是本项目**唯一**会分发给客户端的爬虫程序。

## 来源

| 项目 | 地址 |
|------|------|
| 上游仓库 | https://github.com/mymine/CatVodSpider |
| 上游提交 | `6634fe1b9ab85f9a2f1b1401ca7d3f407429c30c`（2026-11-08） |
| 上游说明 | fork 自 https://github.com/FongMi/CatVodSpider，spider 参考 https://github.com/bizhangjie/CatVodSpider |
| 许可证 | MIT（Copyright (c) 2024 lushunming） |

该 JAR 由上游公开源码构建，只包含 `classes.dex`，没有 `wexguard`、`moji.com` 等加固/广告组件。

## 校验

```text
MD5: f1c861be74a6a4fe85c692fafe1c55d9
文件: custom_spider.jar
```

仓库根目录执行：

```bash
cd spider
md5sum -c custom_spider.jar.md5
```

## 为什么替换原来的 JAR？

旧版 `tvbox_full.json` 的 `spider` 指向：

```text
http://oss4liview.moji.com/...jpg;md5;...
```

这个文件实际是伪装成 `.jpg` 的 APK/JAR，内部使用 `wexguard` 加固，加载后会出现
“扫码看广告才能激活”的逻辑。它是闭源、不可审计的，无法可靠地只删掉广告部分。

因此本项目改为：

1. 自带这个从公开源码构建的干净 `custom_spider.jar`；
2. `update.py` 生成配置时，`spider` 固定指向本文件（GitHub Actions 下是本仓库的 raw 地址）；
3. 只保留该 JAR 实际包含的 `csp_*` 类对应的 type 3 站点，其余不支持的类会被过滤；
4. 保留 JS / Python 爬虫站（`drpy`、`py` 等），它们由客户端引擎执行，不依赖 JAR。

## 如何更新 JAR

1. 从可信来源重新构建或下载新的 `custom_spider.jar`；
2. 覆盖本目录的 `custom_spider.jar`；
3. 更新校验文件：
   ```bash
   cd spider
   md5sum custom_spider.jar | awk '{print $1"  custom_spider.jar"}' > custom_spider.jar.md5
   ```
4. 提交并推送。`.github/workflows/update.yml` 监听了 `spider/**`，会自动重新生成配置。

> 不要使用任何带 `;md5;` 伪装、`wexguard` 加固或来源不明的 JAR。
