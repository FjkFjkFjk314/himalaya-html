---
cairn: change
id: docker-mail-mcp-web
status: active
created: 2026-11-24
---

# 本机 Docker 邮件服务、私有 MCP 与公网只读网页

## 目标

在本机 Docker Compose 中运行 Himalaya。AI 从本机或私有网络读取、管理和发送多个邮箱的邮件。用户从公网域名登录浏览器阅读邮件。指定账号允许 AI 直接发信，其余启用发信的账号需要用户确认。真实凭证、邮件和生产配置留在部署环境，公开仓库只提交占位示例和合成测试数据。

## 仓库现状

- `src/lib.rs:26` 的统一邮件接口由各协议适配器实现，已有多账号 TOML、能力查询及结构化输出。复用这些能力，保留现有 CLI 和原生 stdio MCP 用法。
- `cairn/spec/mcp.md:9` 的 MCP 每个进程固定一个账号。`src/mcp.rs:150` 的开关控制工具分类，但开启后也会授予与本方案无关的动作。部署入口需要独立的账号、工具和参数授权。
- `src/unified/message/read.rs:142` 已提供正文与 MIME 部件元数据。HTML 字段是解码后的邮件内容，网页需要单独净化和隔离。当前分支没有 Docker、HTTP MCP 或浏览器阅读器。
- `cairn/spec/testing/` 已有 Gmail、Graph、QQ、163 的上游实测报告，可用于设计用例。此次 Docker、MCP 和 Web 的端到端验收需要重新执行。

## 公网入口与本机服务

浏览器访问 `mail.example.com` → DNS-only 指向公网服务器 → 公网 TCP 443 透传 → 私有链路 → 本机 HTTPS 网关 → 本机身份服务与只读邮件网页。

- 建议用 Tailscale Grants 限定公网服务器节点只能连接本机指定的 Web TLS 端口，用 HAProxy `mode tcp`（或现有等效转发器）透传。公网上仅发布 Web 入口。公网服务器持有其自身的网络身份，邮件域名的 TLS 私钥、二步验证状态、邮件数据和邮箱凭证由本机持有。公网服务器能观察连接元数据，本机离线时公网网页不可用。
- 在本机签发、续期并加载域名证书。建议用 DNS-01，DNS 凭证注入本机 ACME 组件，验证续期后自动加载。可将同级登录子域名经同一个 TCP 端口透传，由本机按域名选择路由。DNS 保持直连模式。
- 建议用本机反向代理集成 Authelia，以密码加 TOTP 作为首版二步验证。只允许配置的用户访问，保护页面、API、附件和直接访问的地址。会话设置过期时间、退出和安全 Cookie，应用端不可通过 Docker 容器端口或伪造转发头绕开鉴权。
- Web 使用 Rust 服务端渲染和少量前端代码。提供账号切换、文件夹、分页、搜索、正文和附件下载。所有读接口按账号、文件夹、消息 ID 定位，保留已读状态。纯文本转义，HTML 净化后放入隔离的 sandbox iframe，默认阻止远程资源和外部跟踪。下载强制使用附件响应和 `nosniff`，邮件响应禁止缓存。

## MCP 与发送策略

- 复用现有统一 API 和工具定义，通过固定账号的 stdio 子进程执行。计划增加受控的本机 stdio 入口和私有网络 Streamable HTTP 入口，两者共用账号注册表与授权。远程客户端用单独凭证接入 HTTPS，每个请求都检查凭证、账号和操作权限，撤销后立即拒绝。公网转发节点的网络授权只覆盖 Web。
- 首版工具范围为账号和能力查询、文件夹与邮件的列出和搜索、阅读和取附件、标记、归档或同账号移动、移到垃圾箱、拟稿及发送、回复、转发。工具和参数在发现和调用两个阶段都走允许列表。原始协议、任意文件路径、永久删除、自动回复和跨账号移动不开放。支持能力由各供应商决定，工具错误保留后端的结构化信息。
- 发信策略按账号设为直接发送、先确认或禁用。新接入且允许发信的账号默认先确认。网关统一收口邮件提交，过滤现有 compose/reply/forward 等工具附带的发送参数，避免旁路。来自邮件的内容始终作为数据处理，账号策略只能由本机管理员修改。
- 需要确认时，在本机持久化待发快照。用户在独立的本机审批页面预览发件身份、全部收件人（含 Bcc）、主题、正文和附件并批准。批准绑定该版本且只能使用一次。发送状态区分待确认、发送中、已发送、已拒绝和结果未知。并发执行只能认领一次。服务重启后对结果未知的发送先核对，避免自动重发。直接发送使用同一执行层，并区分送达前的供应商受理、Sent 副本及最终投递。

## 部署与验收边界

- 本机 Compose 提供可复建镜像、独立内部服务、持久卷与最小挂载。凭证通过仓库外的受保护文件和按服务分配的 Compose secrets 注入，兼容现有 secret command。运行时 OAuth 状态只保存在本机。日志只留必要操作元数据，不记录邮件正文、附件、凭证和认证头。镜像、公共 CI、Git 和示例禁止包含真实数据或凭证。
- 首版实测矩阵包括通用 IMAP/SMTP、Gmail API、Microsoft 365/Outlook 的 Microsoft Graph、QQ IMAP/SMTP、163 IMAP/SMTP。Gmail 和 Graph 验证 OAuth 更新及撤销。QQ 和 163 验证服务商授权码、SMTP 和文件夹差异。每项标明成功、后端明确不支持或待实测。JMAP 等现有后端保留为后续实测对象。
- Web 首版面向单用户并保持只读。发送审批页面是本机操作入口。依赖实际域名、可用私有链路、可续期证书、测试邮箱及明确的测试收件地址才能完成部署验收。

## 参考

- [Tailscale Grants](https://tailscale.com/docs/features/access-control/grants)
- [Let's Encrypt ACME 验证类型](https://letsencrypt.org/docs/challenge-types/)
- [Authelia 双因素访问策略](https://www.authelia.com/configuration/security/access-control/)
- [MCP 2025-06-18 传输规范](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
- [Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/)
