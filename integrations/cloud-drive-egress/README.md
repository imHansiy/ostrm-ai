# 可选网盘云端出口

用于服务器能访问 Cloudflare、但不能连接阿里/夸克/UC 官方 API、指定下载 CDN 或国内 ModelBest API 的场景。先确认凭据有效，再验证这个出口能否连接目标。它依赖你的 Cloudflare 账户和部署服务器，不依赖个人电脑常驻，不运行模型，也不替代 OpenList 的登录和文件权限。

`OpenList / 指定媒体来源 → 本机鉴权 CONNECT 代理 → WSS → Cloudflare Worker → 官方 HTTPS`

原始 TLS 在 OpenList/识别服务与网盘或模型 API 之间建立；不安装根证书、不关闭证书校验，模型密钥不交给 Worker。Worker 只接受源码中的官方主机和 443 端口。其他 HTTPS 请求经本机代理直接连接。代理只监听 `[::1]:43647`，需要单独的随机口令；不要通过公网反代暴露它。

## 1. 部署 Worker

在本目录安装 Node.js 依赖并登录你自己的 Cloudflare 账户：

```sh
npm install
npx wrangler login
npx wrangler deploy --dry-run
npx wrangler deploy
npx wrangler secret put RELAY_SECRET
```

输入至少 32 字符的随机密钥，保存部署返回的 `workers.dev` 地址。也可以使用限定账户与 Workers 编辑权限的 API Token，通过环境变量交给 Wrangler。Cloudflare 管理密钥仅用于部署，**不能写入 Worker、桥接配置或 OpenList**。

## 2. 安装服务器桥接

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp bridge.example.json bridge.private.json
chmod 600 bridge.private.json
```

修改 `bridge.private.json`：`url` 使用刚部署的 WSS 地址；`secret` 与 `RELAY_SECRET` 一致；`proxy_secret` 使用另一个随机口令。通过平台进程管理器运行 `.venv/bin/python /绝对路径/bridge.py`。FreeBSD 可使用 `daemon -r`，Linux 可使用 systemd。配置开机自启和进程检查，退出 SSH 不应结束桥接。

## 3. 接入与验证

只在 OpenList 进程环境中设置：

```dotenv
HTTPS_PROXY=http://openlist:YOUR_PROXY_SECRET@[::1]:43647
NO_PROXY=localhost,127.0.0.1,::1
```

可将已经能直连的其他网盘域名加入 `NO_PROXY`，避免无关连接占用桥接并发。使用这组环境变量时，保持 OpenList 的 `proxy_address` 为空，以保留 `NO_PROXY` 的例外规则。

若 AI 服务需要直读受阻的阿里 CDN，可仅为该来源设置：

```dotenv
MEDIA_PROXY_URL=http://openlist:YOUR_PROXY_SECRET@[::1]:43647
MEDIA_PROXY_HOSTS=dl1.aliyundrive.cloud
```

同时把实际媒体主机加入已有 `MEDIA_ALLOWED_HOSTS`，保留原有其他主机。夸克/UC 通常由 OpenList 自身代理视频，识别服务继续读取 OpenList 提供的地址，不应提取 Cookie 发给浏览器。

等正在识别的任务完成，再重启配置变更的服务。分别确认：API 鉴权成功、强制刷新目录成功、Range 请求实际读到视频数据、完整识别产生摘要和标签。只返回文件列表不能证明视频读取正常。重试时只选择失败任务；成功结果与人工标签应保留。

## 传输限制与排错

### 可选：HTTP 范围读取

原 WSS 通道正常但大文件传输过慢或频繁断开时，可先独立验证 `/media-fetch`，再只为指定 CDN 启用：

```dotenv
MEDIA_FETCH_RELAY_URL=https://YOUR_WORKER.workers.dev/media-fetch
MEDIA_FETCH_RELAY_TOKEN=YOUR_RELAY_SECRET
MEDIA_FETCH_RELAY_HOSTS=dl*.aliyundrive.cloud,dl-pc-*.drive.quark.cn
```

此模式由你自己的 Worker 通过 HTTPS 读取媒体，再流式回传。**签名链接及必要的 Cookie 等请求头会在该 Worker 中处理**，与原 WSS 端到端 TLS 透传不同；仅在信任并控制该 Worker 时使用。接口沿用 `RELAY_SECRET` 鉴权，只有列出的 CDN 可访问，拒绝重定向、未知请求头和超过 8 MiB 的单次范围，不写日志、不缓存私有响应。

AI 端先读取最多 256 KiB，再连续读取最多 4 MiB 的区段，只有强 ETag、总长度、起止偏移一致才拼接；输入变化立即失败。源站返回合法的较短区段时继续读取下一段，不把它误判为文件变化。网络中断最多有限重试，并继续遵守原有单片总流量与时间预算。关闭这三个变量即可回退。该功能只改变下载传输，模型仍收到一份完整压缩视频，模型端点、规格及提示词不变。

验证：`node --test media-fetch.test.js` 和 `npm run check`，再对真实文件比较内容摘要校验值、传输时间及整片识别结果；不要只依据 HTTP 200 判断成功。

国内模型服务需要中转时，在 AI 服务环境中单独配置：

```dotenv
MODEL_BASE_URL=https://api.modelbest.cn/v1
MODEL_API_KEY=YOUR_DOMESTIC_MODELBEST_KEY
MODEL_PROXY_URL=http://openlist:YOUR_PROXY_SECRET@[::1]:43647
```

国内和海外密钥需匹配各自区域。已有安装还应在「模型与扫描」中修改已保存的 API 地址；不要只修改环境默认值。先做连接测试，再用真实视频验证。中转不改变模型规格、提示词、整片压缩方式或原视频。

- API 通道最长 120 秒，接收上限 16 MiB；指定 CDN 通道最长 30 分钟，接收上限 3 GiB。仍受插件本身的单片流量、压缩、磁盘和超时限制。
- `api.modelbest.cn` 使用独立的双向流控，单连接最长 10 分钟、上传上限 64 MiB、下行上限 16 MiB；上传未确认窗口不超过 512 KiB，每帧最多 32 KiB，避免视频上传挤满 Worker 内存。64 MiB 是传输保护上限，插件和模型服务本身更小的请求体限制仍然生效。
- CDN 使用确认帧限制发送窗口，避免慢客户端导致无限缓存。单帧至多 32 KiB，最多约 2 MiB 或 1,024 帧未确认；按字节限制避免上游小包导致吞吐过低。
- 下载期间客户端可能只接收而不再发送数据。不能给上传方向设置固定读取超时，否则会中断正常长视频。桥接保留上游空闲超时和连接总时长限制。
- `401` 表示中转密钥不匹配；`403` 表示目标不在白名单或下载通道未启用流控；`502` 表示云端尚未连通目标。网盘自身的令牌过期仍需按驱动规则刷新。
- 不自动启用付费套餐。实际可用性、限额和费用取决于 Cloudflare 账户与到目标网盘的网络；运行时更新也可能断开长连接，失败任务应保留并有限重试。
- 排错日志只应记录主机、错误类别和数量，不要记录 Cookie、Authorization、签名下载 URL 或 Cloudflare 管理凭据。

回退时先删除相关进程的代理环境变量并重启，再停桥接，最后删除你账户中的这个 Worker。原有挂载和识别数据库不需要删除。

测试：`python -m pytest test_bridge_stream.py`（需安装 pytest 和 requirements.txt）。回归测试验证持续下载时上传方向静默不会提前断流，以及大请求上传的确认窗口和内容完整性；另用 `npm run check` 验证 Worker 构建。
## 长视频直链过期

AI 服务在私有 HTTP 媒体通道遇到 403 时，可以通过原 OpenList 授权重新获取同一路径的媒体地址。刷新前核对文件指纹，后续每个 Range 仍须符合原来的强 ETag、文件总长度和连续字节偏移；不允许把变化后的文件拼接到旧数据中。每次分段请求只允许一次地址刷新，且仍受任务总超时及读取流量上限限制。权限确实不足、地址未变化或刷新后仍拒绝访问时会停止，不无限重试。

此处理在 AI 服务端完成，无需修改 OpenList 本体或重新部署 Worker，也不改变整片压缩参数、模型和提示词。MP4/MOV 时长优先通过有限的 Range 请求读取容器时钟和视频轨道头，跳过媒体包和大型样本索引；容器头读取最多 256 KiB、256 个 atom，并检查范围、文件总长和强 ETag。不能取得有效视频时长时，在原有总超时内回退到 FFprobe。此步骤只获取时长，模型输入仍为完整视频压缩后的内容。

对于需要频繁跳转的较小文件，可以通过 `MEDIA_SMALL_CACHE_BYTES` 设置有上限的临时源文件缓存，完成后自动删除。

通过已验证 Range 的媒体通道解码时，插件会显式启用 FFmpeg HTTP 跳转，以便访问末尾的 MP4 容器头；不支持该通道的来源仍保持原有读取方式。慢速大文件可在模型与扫描配置中将 `media_timeout` 提高到最多 43,200 秒（12 小时），默认值不变。请根据源文件大小和实测吞吐设置：2.59 GiB 在 100 KiB/s 下仅下载一遍就约需 7.5 小时。提高时间上限不会提高下载速度，也不会放宽并发、临时磁盘、总读取流量及 23 MiB 模型输入上限。建议小文件先恢复，大文件放在后续批次。

`/media-fetch` 的上游 403 响应会通过 `X-Media-Error-Kind` 返回固定分类：`expired`、`clock`、`signature`、`referer`、`credential`、`callback`、`access`、`concurrency`、`html-denial` 或 `denied`。这有助于区分直链过期、签名/权限校验和 CDN 拒绝；仅容量限制保留固定的重试标记，原始错误正文、签名链接和凭据不会回传。分类是排查依据，不表示该失败一定可以重试恢复。需要升级 Worker 才能取得这些分类。
