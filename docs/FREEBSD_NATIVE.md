# FreeBSD 原生安装与旧伴随插件迁移

不需要 Docker 或本地 GPU。运行环境是 Java 21、Python 3.12（3.11 也可）、nginx，模型继续使用在线 API。OpenList 本体保持独立运行。

## 布局与路由

完整应用目录例如 `$HOME/apps/ostrm-ai`：

```text
openlisttostrm.jar       Java 后端
public/                 Nuxt 静态构建
.venv/                  AI 服务 Python 环境
.env                    仅服务器可读的配置
deploy/freebsd/          启动器与进程守护脚本
data/db/ostrm.sqlite3    OStrm 来源、任务配置
data/config/            OStrm 管理员资料
data/ai/                AI 数据、密钥、字幕、证据和封面缓存
strm/                   STRM / NFO / 图片等导出
runtime/                PID、运行日志与生成的 nginx 配置
```

默认网关仅监听 `127.0.0.1:43647`，由已有 HTTPS 代理转发。同域名 `/` 保持 OpenList；`/ostrm/` 是完整 OStrm 应用；`/ostrm/api/` 进入 Java；`/ai/`、`/media/` 进入 AI。Java 与 AI 分别仅监听本地 43648、43649。端口可通过 `NATIVE_*_PORT` 配置，不能与现有服务冲突。

Serv00 需先用 `devil port list` 检查配额并预留端口。三个端口配额下，若 OpenList 和网关已占两个，可将 Java 绑定 `127.0.0.1:43648`、AI 绑定 `127.0.0.2:43648`（`NATIVE_AI_HOST=127.0.0.2`、`NATIVE_AI_PORT=43648`）。必须先验证该主机配置了对应的 loopback 地址；不能假设任意 127.* 地址均可用。临时网关可使用已验证的 `[::1]:43648`；正式网关继续原地址，因此不会额外开放外网端口。

## 构建和安装

在开发机或 CI 构建，避免在低配主机编译。Java 21 / Node 22 / Python 环境下，在仓库根目录执行：

```sh
cd backend
./gradlew test bootJar
cd ../frontend
npm ci
NUXT_APP_BASE_URL=/ostrm/ NUXT_PUBLIC_API_BASE=/ostrm/api npm run generate
cd ../ai-service
python -m pip wheel --no-deps . -w ../dist
```

将 `backend/build/libs/openlisttostrm.jar`、`frontend/.output/public/`、`deploy/freebsd/`、生成的 wheel 上传到服务器应用目录。不要上传开发机 `.env`、测试数据或 `node_modules`。

```sh
cd "$HOME/apps/ostrm-ai"
python3.12 -m venv .venv
.venv/bin/python -m pip install ./openlist_ai_tags-*.whl
```

从 `ai-service/.env.example` 创建 `.env`，补入 `deploy/freebsd/native.env.example` 的设置。填写 OpenList 本地服务地址、在线模型地址/规格/密钥、独立随机的 JWT_SECRET 和 AI_BRIDGE_KEY。不要把网关自身填写为 OpenList 后端，以免代理循环。配置公开域名 `PUBLIC_OPENLIST_URL` 与 `MEDIA_PUBLIC_BASE_URL`；均不能使用 127.0.0.1。限制权限：

```sh
chmod 700 "$HOME/apps/ostrm-ai"
chmod 600 .env
/bin/sh deploy/freebsd/service.sh start
/bin/sh deploy/freebsd/service.sh status
```

服务脚本使用 FreeBSD `daemon` 守护三个进程。日志在 `runtime/ai.log`、`runtime/java.log`、`runtime/gateway.log`，nginx 错误日志为 `runtime/nginx-error.log`。外层 HTTPS 代理必须支持 GET/POST/PUT、HEAD、Range、PROPFIND，并保留 Authorization。

**首次开放公网前初始化管理员。** 可仅通过 SSH 隧道访问本地 Java 的 `/api/auth/sign-up` 创建账号；用户名与 OpenList 管理员一致时，应用支持交换已有 OpenList 管理员会话。旧 Token/普通用户不能通过此桥接提升权限。初始化完再启用公网入口；普通密码登录仍可使用。

在 OStrm 来源页添加已有 OpenList，填写服务器使用的 Token。Token 保存后不返回浏览器；编辑留空保留，修改来源地址必须重新填写。进入「AI 识别与协议」配置扫描、查看队列、管理外部协议。页面不包含播放器。

## 从旧 Python 伴随插件迁移

1. 记录视频各状态数量、任务状态、出版订阅数量。等待 running 任务结束，避免两个 worker 同时领取任务。
2. 备份旧 `.env`、数据库、所有数据目录、网关配置和 crontab。备份目录只允许当前用户读取。
3. **TiDB：继续连接同一 `openlist_ai` 库，不重建表、不清空队列。** SQLite：停止旧服务后使用 SQLite backup API 生成一致快照。
4. 复制 `media-secret.key`（必须逐字节一致，否则已有订阅失效）、`model-secret.json`、字幕、证据缓存。对封面 SQLite 缓存使用 backup API，不直接复制正在写入的文件。旧 `data/exports` 移至新 `strm`，同步修改设置中的绝对输出路径。
5. 新 `.env` 的 `AI_DATA_DIR` 指向新 `data/ai`，`MEDIA_EXPORT_DIR` 指向 `strm`。模型服务、TiDB 证书等路径也必须检查。
6. 先用不同网关端口、`AI_BACKGROUND_ENABLED=false` 启动新版。检查 Java、AI、静态资源、SSO、来源配置、元数据和旧订阅链接。
7. 停止并禁用旧伴随服务的自动拉起。停止新版临时进程，设置正式网关端口和 `AI_BACKGROUND_ENABLED=true`，重新启动新版。切换期间保留 `/media/` 原路径与公开域名。
8. 复核数量和原队列，真实识别一个短视频，再验证原生浏览和已有播放器配置。检查重启后数据和任务恢复。
9. 移除旧插件专属 cron、自启和旧管理入口。旧目录作为私有回滚归档，不再运行；验证稳定后可按自己的备份保留策略清理。**不卸载 OpenList，不删除其挂载或数据库。**

自愈 cron 可每五分钟执行（仅保留一条，使用实际绝对目录）：

```cron
*/5 * * * * /bin/sh /home/YOUR_USER/apps/ostrm-ai/deploy/freebsd/service.sh start >/dev/null 2>&1
```

临时维护时先创建 `runtime/app.disabled` 再 `service.sh stop`；恢复时移除该文件再 start，避免 cron 把维护中的程序拉起。不要覆盖与本项目无关的 cron。

切换阶段的环境变量不能继续留在正式进程里；特别检查 `AI_BACKGROUND_ENABLED=true`。`/ai/health` 的 `worker_enabled`、`worker_running` 均应为 true，并用一个真实任务验证已被领取。单独更新 AI 时用 `service.sh restart-ai`；脚本等待原进程退出后再启动，若仍在停止会返回错误，避免误报已完成重启。

## 资源与故障排查

Java 启动器默认限制 192 MiB 堆、160 MiB Metaspace、64 MiB 代码缓存、2 个活动 CPU；堆外与线程内存还需额外空间。识别并发建议 1。保留一套 worker；不在切换前同时开启新旧后台队列。若修改 JVM 预算，请在 `launch.py` 调整并重新验证真实主机的内存/进程限制。

`502` 先检查三个服务状态和对应日志；Java SQLite JNI 必须支持实际 FreeBSD 架构；静态资源 404 要确认构建时的 `/ostrm/` 与运行时前缀一致。登录失败检查两端独立的 AI_BRIDGE_KEY 是否一致、OpenList 管理员用户名是否与 OStrm 一致。模型 401 检查密钥所属区域，连接超时先验证服务器到在线 API 的网络。

nginx 配置由 `launch.py prepare` 从服务器 `.env` 生成，不要手改生成文件。更换前缀必须重新构建 Nuxt，并修改 `NATIVE_APP_PREFIX`。默认根路径 Docker 安装仍使用原 Compose，不受这一原生同域名方案影响。
