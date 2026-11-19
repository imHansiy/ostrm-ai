# 可选网盘云端出口

用于服务器能访问 Cloudflare、但不能连接阿里/夸克/UC 官方 API 或指定下载 CDN 的场景。先确认挂载凭据有效，再验证这个出口能否连接目标。它依赖你的 Cloudflare 账户和部署服务器，不依赖个人电脑常驻，不运行模型，也不替代 OpenList 的登录和文件权限。

`OpenList / 指定媒体来源 → 本机鉴权 CONNECT 代理 → WSS → Cloudflare Worker → 官方 HTTPS`

原始 TLS 在 OpenList/识别服务与网盘之间建立；不安装根证书、不关闭证书校验。Worker 只接受源码中的官方主机和 443 端口。其他 HTTPS 请求经本机代理直接连接。代理只监听 `[::1]:43647`，需要单独的随机口令；不要通过公网反代暴露它。

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

若 AI 服务需要直读受阻的阿里 CDN，可仅为该来源设置：

```dotenv
MEDIA_PROXY_URL=http://openlist:YOUR_PROXY_SECRET@[::1]:43647
MEDIA_PROXY_HOSTS=dl1.aliyundrive.cloud
```

同时把实际媒体主机加入已有 `MEDIA_ALLOWED_HOSTS`，保留原有其他主机。夸克/UC 通常由 OpenList 自身代理视频，识别服务继续读取 OpenList 提供的地址，不应提取 Cookie 发给浏览器。

等正在识别的任务完成，再重启配置变更的服务。分别确认：API 鉴权成功、强制刷新目录成功、Range 请求实际读到视频数据、完整识别产生摘要和标签。只返回文件列表不能证明视频读取正常。重试时只选择失败任务；成功结果与人工标签应保留。

## 传输限制与排错

- API 通道最长 120 秒，接收上限 16 MiB；指定 CDN 通道最长 30 分钟，接收上限 3 GiB。仍受插件本身的单片流量、压缩、磁盘和超时限制。
- CDN 使用确认帧限制发送窗口，避免慢客户端导致无限缓存。单帧至多 32 KiB，至多 32 帧未确认。
- 下载期间客户端可能只接收而不再发送数据。不能给上传方向设置固定读取超时，否则会中断正常长视频。桥接保留上游空闲超时和连接总时长限制。
- `401` 表示中转密钥不匹配；`403` 表示目标不在白名单或下载通道未启用流控；`502` 表示云端尚未连通目标。网盘自身的令牌过期仍需按驱动规则刷新。
- 不自动启用付费套餐。实际可用性、限额和费用取决于 Cloudflare 账户与到目标网盘的网络；运行时更新也可能断开长连接，失败任务应保留并有限重试。
- 排错日志只应记录主机、错误类别和数量，不要记录 Cookie、Authorization、签名下载 URL 或 Cloudflare 管理凭据。

回退时先删除相关进程的代理环境变量并重启，再停桥接，最后删除你账户中的这个 Worker。原有挂载和识别数据库不需要删除。

测试：`python -m pytest test_bridge_stream.py`（需安装 pytest 和 requirements.txt）。回归测试会持续下载而保持上传方向静默，检查不会提前断流。
