# 协议、运维与兼容性

## 数据流与模块

```mermaid
flowchart LR
  O[OpenList 挂载] --> S[OStrm 扫描 / STRM 流水线]
  S --> B[Java JWT 管理桥接]
  B --> Q[Python 持久化识别队列]
  Q --> C[受限下载与完整时间轴压缩 / 字幕证据缓存]
  C --> M[在线 MiniCPM-V 4.7]
  M --> D[AI 结果 + 人工标签 / SQLite 或 TiDB]
  D --> P[受限目录订阅与资料发布]
  P --> F[STRM NFO 海报 / M3U RSS CMS]
  P --> J[Jellyfin 直放 API / 只读 WebDAV]
  F --> E[外部媒体服务器或播放器]
  J --> E
  E --> R[实时解析 OpenList 地址后重定向]
```

模型提示词位于 `ai-service/aitags/model.py`；视频压缩在 `media.py`；字幕提取在 `subtitles.py`。不是数据库中的写死标签。电影关系识别可能有误，应在批量识别前检查结果；已识别的正式片名/演职员资料与画面观察是不同来源。

`ai-service/aitags/protocols/` 是协议模块：store 保存订阅和观看状态，catalog 统一授权/ID/资料视图，formats 生成格式，publisher 管理本地资料和导出队列，router/Jellyfin/WebDAV 提供接口。Java `AiMetadataHandler` 在生成 STRM 后登记共享输出路径；AI 故障跳过该步骤，不破坏原 STRM 任务。

## 资源与性能

管理端的「实时处理状态」显示实际活跃的视频和目录、阶段耗时、源站读取字节、FFmpeg 输出时轴/帧数/速率及最近实际进展。目录页显示当前页已检查的条目。时轴百分比只针对当前编码阶段，不是整项 AI 识别的完成率；模型等待阶段不显示虚构百分比。

任务线程反馈与实际进展分开记录：线程仍在轮询不代表读到了新数据。长时间没有新数据时显示等待情况，不自行断言进程已卡死。模型阶段显示调用/尝试次数及读取超时配置；最近成功时间从持久化历史读取，近期处理摘要在服务内保留最多五条。详细帧/字节监测只在内存更新，不逐帧写 TiDB；FFmpeg 监测流通常每秒采样一次，每次最多读取 4 KiB。服务重启后详细阶段监测重新开始，原任务仍按队列恢复规则续跑。

进度接口沿用 `/ai/api/overview` 的管理员权限；不返回模型密钥、原始媒体地址、网关临时地址或完整进程命令行。

- 默认并发 1，最多 4。工作线程限制独立于播放器，协议播放只重定向，不运行播放转码。
- 单视频读取受 max_video_bytes、max_network_bytes、media_timeout 和本地工作空间上限约束。完整时间轴压缩仍然需要读取源视频；不承诺任意大文件零流量。
- 完整压缩视频目标约 23 MiB，模型服务的 HTTP 限额、视频帧采样和上下文限制仍取决于供应商；补充证据帧不是完整视频的替代品。
- Evidence cache 默认 64 MiB/24 小时，可通过 EVIDENCE_CACHE_MAX_BYTES / EVIDENCE_CACHE_TTL 调整。
- 协议目录只包含完成识别且在订阅范围内的视频，默认最多 5000，最高 50000；只读结果缓存 10 秒，撤销凭据每次都实时验证。
- MEDIA_EXPORT_MAX_BYTES 默认 256 MiB，限制单次资料同步和 ZIP；文件是资料，不包含原视频。不同订阅会分别占用输出目录，定期清理不再使用的订阅目录。
- 自动同步约每分钟检查一次。写入使用原子替换和哈希清单，遇到人工改动报告 conflicts；不会删除用户文件，也不会自动回收旧订阅文件。
- 撤销只阻止继续向服务请求。已经下载的 NFO/简介不会被远程删除，已经领取的网盘直链可能持续有效到源站过期。

## TiDB 与备份

AI_DB_BACKEND=tidb 时配置 AI_TIDB_HOST、PORT、USER、PASSWORD、DATABASE（默认 openlist_ai）。可复用 OpenList 的 TiDB 实例，但使用独立数据库。TLS 验证保持启用，不修改 OpenList 自身的表。TiDB 账号需要该数据库的建表权限；首次创建数据库可执行：

```bash
python -m aitags.migrate_tidb --init-only
```

SQLite 迁入 TiDB：先让当前识别完成，停止服务，备份整个 AI_DATA_DIR；设置 TiDB 环境变量后执行：

```bash
python -m aitags.migrate_tidb --source /原数据目录/aitags.sqlite3
```

目标库必须为空，工具逐表比较所有行，输出数量；包含队列游标、协议 ID、订阅、观看状态和导出任务。保留原 `media-secret.key`，否则旧订阅凭据无法重建。`media_export_jobs` 中运行中的导出在单 Worker 启动后重新排队；原 AI 队列按既有恢复规则继续。

备份途径：管理员 `POST /ai/api/backup` 返回 SQLite 快照（包括协议表）；`POST /ai/api/exports/backup` 返回数据库 + media-secret.key 的 ZIP。后者含可恢复订阅的秘密，应作为私密备份保存。当前 OStrm Java 桥接只开放 JSON 管理接口；二进制备份使用 Python 服务回环地址和原 OpenList 管理认证。模型密钥单独保存在 model-secret.json 或环境变量，不在数据库备份中。

完整灾备还需备份 AI_DATA_DIR（TiDB 封面 JPEG 在本地 cover-cache.sqlite3）、输出目录、OStrm config/db 和私密 .env。恢复时停止服务，校验备份，替换同一版本文件，设置权限 0600，再启动。TiDB 从 SQLite 灾备恢复同样通过 migrate_tidb 导入空库。

升级：备份后在 dev 拉取，重新构建并 `docker compose -f compose.ai.yml up -d --build`。只更换服务代码，不重建数据卷。卸载用 `docker compose -f compose.ai.yml down`，不要加 `-v`；确认不再需要后自行删除插件数据和生成资料。OpenList 视频和自身数据库不受影响。

## 安全边界

- 管理界面是现有 OStrm 单管理员账户，不提供新的多租户权限体系。
- 伴随模式复用 OpenList 管理账户；普通文件列表资料接口仍执行原文件权限检查。
- 管理员创建的订阅是主动授权其目录内容。每个订阅使用独立可撤销只读凭据，客户端无需管理员账户；订阅链接本身应保密。
- 所有协议使用相同根目录边界和内容过滤。Jellyfin 登录会话只访问其所属订阅；撤销/轮换后立即失效。
- 播放请求检查原文件指纹，修改后返回 409，要求重新识别。外部媒体主机须在 MEDIA_ALLOWED_HOSTS 中，或与 OpenList 同源。允许主机不是开放代理许可，私网跳转仍受限制。
- 服务单进程队列锁避免同一数据目录的重复 Worker。不要多副本共享单数据库运行后台队列。
- 默认 Caddy 日志移除请求 URI 和认证头，避免订阅密钥被记录；额外 nginx/Cloudflare/第三方客户端日志也应自行配置脱敏。

## 排错

### 历史失败的分批恢复

修复网盘连接、允许的媒体主机和资源配置后，可在 AI 服务的工作目录执行以下命令。原生安装先激活 Python 环境；Docker 使用 `docker compose -f compose.ai.yml exec ai python -m aitags.recovery ...`。

```sh
python -m aitags.recovery start --batch-size 20 --directory-batch-size 3 --rounds 2 --interval 30
python -m aitags.recovery status
python -m aitags.recovery pause
python -m aitags.recovery resume
python -m aitags.recovery stop
```

某个挂载的下载网络或授权尚未恢复时，可在创建计划时加 `--exclude-root /网盘/暂不可用的挂载`（可重复传入）。该范围的视频和目录只延后，不删除失败记录，也不会改变 OpenList 挂载配置；恢复摘要显示延后数量。修复外部条件后，停止已结束/旧计划并重新创建不带该排除项的计划即可重试。

恢复计划保存在现有 AI 数据库中，重启后继续，只有单个后台 Worker 投放批次。上例每批重新入队最多 20 个已有失败视频和 3 个失败目录，实际数量取决于配置。本批视频及扫描新发现的视频任务处理完后，再投放下一批视频；正在运行的目录扫描不会阻塞失败视频恢复，但会阻止叠加新的目录批次。目录保留原分页游标，存在活动目录时不会提前结束恢复计划；正常增量扫描继续跳过未变化的成功视频。旧任务失败、但视频已在后续识别成功的情况，也不会重复入队。暂停或取消的扫描不会被恢复计划擅自重新开启。

默认最多两轮，第二轮至少间隔五分钟，已确定为损坏、已删除、认证/下载权限或大小预算不足的文件不在第二轮反复尝试。`pause`/`stop` 停止投放后续批次，已入队任务可在原任务管理中暂停/取消。`/ai/api/overview` 的 `recovery` 返回恢复摘要，完整 ID 游标不会混入模型设置。任务历史记录读取、整片压缩和调用模型的阶段，便于区分慢任务与空队列。

背景 OpenList 读请求使用 `OPENLIST_SCAN_TIMEOUT`（默认 60 秒），临时错误最多追加两次重试；四秒目录可用性探测不会被延长。模型仅对明确的 408/429/5xx 临时拒绝做有限重试，不自动重复认证错误、HTTP 409 或结果不明的超时请求。媒体下载错误保留源站 HTTP 状态、预算或网络原因，不再一律报 FFmpeg 解码失败。

白名单必须按实际可信存储配置，例如百度 `*.baidupcs.com`、PikPak `*.mypikpak.net`；不要使用任意主机 `*`。每次重定向仍检查白名单和公网地址。百度媒体请求使用其驱动要求的 `User-Agent: pan.baidu.com`。文件真的损坏、删除、网盘授权过期或服务商无法访问时，恢复计划保留失败原因，不会伪造成功。

| 现象 | 检查 |
|---|---|
| AI 中心 503 | 两个服务的 AI_BRIDGE_KEY 一致；AI_SERVICE_URL 从 Java 可达 |
| 模型 401 | 国内/海外密钥区域和端点是否一致，不只看模型名称 |
| 模型 413 | 服务商请求体限额；完整 MP4 的 base64 会额外放大约三分之一 |
| 扫描完成但没有结果 | 识别任务失败原因、目录权限、扩展名和排除目录；API 只导出 done |
| 播放 409 | 源文件大小/修改时间/哈希已变化，重新扫描识别 |
| 播放 502 | OpenList 直链是否可用，主机白名单、HTTPS 和外部客户端网络 |
| 无封面 | 在模型与扫描设置启用补齐封面；检查原视频、FFmpeg 和媒体主机白名单；源目录不可写时仍保留私有缓存。已生成图片不必重新识别，后台会从持久化任务恢复 |
| 资料输出 conflicts | 生成文件被手工改过，或外部 NFO 格式错误；保护现有文件后人工处理 |
| Jellyfin 客户端要求转码 | 使用真实 Jellyfin 服务端导入 STRM/NFO；兼容层只支持直放 |
| LibreTV 提示私网源 | 配置外部可访问的 HTTPS 服务地址，其默认策略限制 LAN 源 |
| 重启后订阅失效 | media-secret.key 未与数据库一起恢复、已轮换/撤销、公开地址变化 |
| Caddy 配置校验失败 | 使用 Caddy 2；保留 /media 不经过 SPA rewrite；检查挂载文件是否正确 |

## 验证范围

最新交付结果与真实 TiDB/在线模型数据见 [VALIDATION.md](VALIDATION.md)。

本地验证包含 Python API/扫描/媒体/权限/协议测试、Java 测试和 Spotless、Nuxt 生产生成、真实 Java 登录桥接与 Python Worker、实际 FFmpeg 压缩与解码、HTTP Range、官方 @jellyfin/sdk 0.13、桌面和 390px 移动端页面操作及溢出检查。集成模型使用固定响应，不向 CI 注入任何真实密钥。

CI 工作流 `.github/workflows/ai-protocols.yml` 在 dev push 时执行，另检查 AI Docker 镜像、Caddy 和真实 Jellyfin 10.11 NFO/海报导入。**具体最新通过状态以 GitHub Actions 为准**，不能将定义了测试等同于已经通过。物理 Infuse/Kodi/TVBox/Apple 客户端和各种网盘视频编码组合未穷尽验证。

## 协议依据

- [Kodi STRM](https://kodi.wiki/view/Strm)、[Kodi Movie NFO](https://kodi.wiki/view/NFO_files/Movies)
- [RSS 2.0](https://www.rssboard.org/rss-specification)、[Media RSS](https://www.rssboard.org/media-rss)
- [Apple Podcasts requirements](https://podcasters.apple.com/support/823-podcast-requirements)
- [LibreTV 数据源格式](https://libretv.is-an.org/wiki/Data-Sources.html)
- [Infuse 网络共享](https://support.firecore.com/hc/en-us/articles/215090977-Streaming-From-a-Mac-PC-or-NAS)
- [Jellyfin SDK](https://typescript-sdk.jellyfin.org/index.html)、[Jellyfin NFO](https://jellyfin.org/docs/general/server/metadata/nfo/)
- [MiniCPM-V 官方项目](https://github.com/OpenBMB/MiniCPM-V)


## 分阶段识别与后台资料发布

识别沿用完整时间线压缩、原有模型及提示词，准备阶段同一时间只运行一个视频。默认识别并发为 1 时，最多领取两个视频：一个准备视频证据，另一个等待模型或保存结果；只有模型槽位空闲才会发送下一次识别请求。识别并发配置限制模型阶段并发，活动视频总数最多为 `min(4, 并发 + 1)`。等待模型槽位会在实时状态中单独显示，不虚构进度。

任务仍使用原 `tasks` 表，领取后的准备/等待/识别均属于 running。暂停停止新领取，已领取的视频继续完成；取消在进入模型前和提交结果前检查。意外重启后 running 重新入队，完整压缩证据按原有缓存策略尽量复用，不会将只准备好的视频标记为成功。每个成功任务额外记录一条 performance 历史，区分证据准备、模型排队、模型及保存耗时。

识别结果与 `postprocess_jobs` 后台任务在同一事务中保存。封面继续复用压缩时生成的画面，优先写私有封面缓存，再上传视频同目录；协议资料发布独立于识别槽位。上传失败不会阻止协议发布，也不会把成功识别改为失败。后台失败按 10 秒至 5 分钟退避重试；重启、SQLite/TiDB 迁移和完整备份保留未完成任务。重复执行采用同路径覆盖/版本核对，过期视频版本的任务会丢弃。

此优化提高连续任务的吞吐量，不保证单部电影更快。首次处理长视频仍需读取和解码原片；外部模型、网盘网络及共享主机资源会影响实际速度。后台封面和导出文件可能比识别结果稍后可见。

### 缺失封面的恢复

补图只处理已识别成功的视频，并跳过当前后处理任务及已有本地图片。空记录和被清理的私有图片会在至少十分钟的冷却后重新检查，按文件顺序轮转，避免单个离线文件挡住后续补图。补图成功后先保存持久化后处理任务和私有图片，上传同目录及协议发布交给后台队列，慢上传不会阻塞下一张封面的取帧。已有后处理任务中的图片每分钟分批恢复到缓存；旧上传失败不会阻塞新任务。

优先复用 OpenList 提供的 JPEG 视频缩略图。其域名也受 `MEDIA_ALLOWED_HOSTS` 和公网地址检查约束，例如 OneDrive 可能使用 `westus31-mediap.svc.ms`。只有确认该域名属于自己的存储服务且服务器可达时才追加到白名单；读取失败会回退到视频抽帧。

不超过 16 MiB 的视频只下载一次到私有临时目录，允许最多 75 秒，在本地探测并取帧，结束后删除原始缓存。较大视频保持有界远程取帧：主流程上限 32 MiB/60 秒，失败后可用额外最多 16 MiB/40 秒顺序读取首帧，绕过部分分段 MP4 的索引预扫描。封面代理单独限制发送缓冲区，减少 FFmpeg 跳转读取位置时丢弃的预读流量；完整视频识别继续使用原传输参数。探测最多 20 秒，单次解码最多 30 秒。回退帧可能位于片头，目的是补齐可用封面，不改变整片识别证据或提示词。视频损坏、已移动、返回 404 或超出读取限制时仍可能无法生成图片，不会用其他视频的图片冒充。


## 总览的统计口径

总览先说明当前运行或等待原因，再显示识别结果与三个后台阶段。默认“当前处理范围”排除恢复计划 `exclude_roots` 中的目录；可切换“全库”。暂缓范围单列，不与当前范围的失败混在一起。统计来自实际视频记录，不使用恢复游标推算识别完成数量。

“仍需处理”包含当前范围内所有未成功视频；“视频队列”只统计已入队且可领取的任务及处理中任务。目录检查与后台封面/协议发布分别计数。空视频队列不会显示为全库完成；如果目录检查阻挡下一批，会说明等待目录的原因。数据库标记为检查中与实时活动上报分开表述，页面超过20秒没有取得新数据会显示更新中断。

成功占比仅针对已经发现的视频，不保证所有挂载目录已扫描完。最近成功时间不充当服务心跳。统计接口使用只读连接，前端自动刷新不会持有识别队列的写锁。状态判断测试可运行 `cd frontend && npm run test:overview`，并已接入 AI 协议 CI 工作流。


## 数据库短暂断连与后台健康检查

后台扫描、封面、识别调度和协议发布的异常处理不会依赖历史表写入成功。TiDB 暂时不可达时，历史记录降级为仅包含异常类型的服务日志，循环退避后继续；扫描延迟使用本轮已读取配置，不会在异常保护之外再次查询数据库。已经提交的成功结果也不会因补写历史失败而回退为失败。

`GET /ai/health` 在启用后台处理时要求所有 Worker 线程及协议发布线程存活。正常返回 HTTP 200、`status: ok`；关键线程已退出时返回 HTTP 503、`status: degraded`，并分别给出 `worker_running`、`publisher_running`。这些存活指标不等同于每个任务都在推进，具体阶段仍查看总览。关闭后台处理的测试/预览实例不按线程缺失判故障。

FreeBSD 部署时，原 OpenList 与本项目是独立服务。仅用 `daemon -r` 可以处理进程退出，主机重启后还需要为原 OpenList 的幂等启动脚本配置开机启动；可另加定时启动检查，路径须按自己的安装位置调整。应保留原 crontab，并验证重复调用不会创建第二个实例。OpenList 初始化多个不可达网盘时，API 可能在挂载初始化完成前等待；应先确认原服务登录及目录 API 可用，再恢复大量识别任务，避免消耗失败重试轮次。
