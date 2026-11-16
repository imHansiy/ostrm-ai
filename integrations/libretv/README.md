# 可选：独立 LibreTV 网盘影院

AI 项目继续负责识别、资料和协议。此集成使用独立的 LibreTV 前端与 Node 进程负责观影，保留原版首页、推荐区、导航与设置，通过原版源管理接入两个 CMS 源。无需在 AI 插件里重新维护播放器，也不运行本地推理或服务端转码。

## 源码与许可

- 上游：<https://github.com/LibreSpark/LibreTV>
- 固定提交：`92461b5e458ba45233e1cba30b09ce0bbc0fa911`（2.23.2）。
- 改造补丁：[onedrive-cinema.patch](onedrive-cinema.patch)，包含 CMS 地址参数、原文件格式、登录凭据隔离及子路径适配、独立网盘目录页，以及测试与原生部署脚本；原版首页与页脚保持固定上游版本原样。
- 遵守上游 **AGPL-3.0-or-later**。部署产物应附带本次构建的完整源码，通过 `/cinema/source.zip` 提供下载；源码包不得包含 `.env`、私钥、真实订阅凭据、测试追踪或运行数据。

```sh
git clone https://github.com/LibreSpark/LibreTV.git libretv
cd libretv
git checkout 92461b5e458ba45233e1cba30b09ce0bbc0fa911
git apply /你的ostrm-ai绝对目录/integrations/libretv/onedrive-cinema.patch
npm ci
npm test
NEXT_PUBLIC_BASE_PATH=/cinema DOCKER_BUILD=1 npm run build
```

`DOCKER_BUILD=1` 仅启用上游 standalone 输出选项；原生安装不需要 Docker。详细步骤在应用补丁后的 `deploy/freebsd/README.md`。

## 与现有原生安装结合

应用放在 OStrm 目录的同级 `libretv`，例如 `~/apps/ostrm-ai` 与 `~/apps/libretv`。OStrm `.env` 设置 `CINEMA_ENABLED=true` 后，网关将 `/cinema` 交给影院，原生启动器将其加入已有自愈启动流程。默认 Node 使用 `[::1]:43648`，与 IPv4 回环上的 Java/AI 分开；先验证目标主机可用此地址和端口，不增加新的端口预留。

单独配置影院 `.env` 的访问密码、独立会话密钥及成人/非成人 CMS 订阅。两组订阅使用不同的范围授权；不要把 OpenList 管理 Token 或模型密钥填入来源地址。未登录不返回来源配置和媒体数据。

使用原版搜索、详情和播放流程，顶部新增“我的网盘”入口，进入 `/cinema/library` 分页海报页，支持成人／非成人、类型、年份、关键词及排序；首页推荐和播放器界面保持原版。两个源在“设置 → 源管理”中选择；原版搜索中的成人源默认受原版过滤控制，需要在“偏好设置 → 首页与内容”关闭“成人内容过滤”后启用；独立网盘页直接用其范围按钮切换。MP4/WebM 等原文件走原生 video，HLS 继续走 LibreTV 的 HLS 播放链路。容器或编码不受浏览器支持时，仍需外部播放器。来源中的新增识别结果会随 CMS 接口更新，可通过原版搜索和网盘目录找到；目录有短期缓存，详见部署说明。

测试分别覆盖源码单元测试、生产构建、桌面/手机布局、真实源隔离、全库筛选排序、分页、搜索、详情及真实视频解码播放。通用回归不要使用成人画面截图作为报告或文档素材。

## 网盘页加载加速

网盘页增加登录保护的服务端首屏直出、私有目录持久缓存、按需后台更新与同源封面缓存。浏览器使用 `private`/Cookie 隔离和 ETag，缓存不进入公共目录、Git 或镜像；封面下载限制为同时 2 个、单图 2 MiB，磁盘约 128 MiB/2000 张。重启影院可继续使用缓存，原视频同目录封面和 TiDB 中的业务资料保持原有存储位置。首次部署会后台预热少量首屏封面；未生成或原源不存在的封面仍显示占位。详见补丁中的 `deploy/freebsd/README.md`。

[加载性能实测与测试条件](PERFORMANCE.md)。
