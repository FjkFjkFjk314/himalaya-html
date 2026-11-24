---
cairn: delta
change: docker-mail-mcp-web
---

## ADDED Requirements

### Requirement: 本机容器部署与凭证边界

`packaging` SHALL 提供可复建的本机 Docker Compose 部署。邮箱连接、邮件状态和应用持久数据 SHALL 留在本机。所有真实邮件与凭证 SHALL 存放在 Git 之外，并仅挂载给使用它们的组件。在透传部署中，公网服务器 SHALL 仅转发 Web 的加密 TCP 流量，域名私钥和 Web 登录状态留在本机。在首尔托管前端的部署中，独立站点 SHALL 在云端验证浏览器会话，通过受限私有链路访问本机只读邮件接口。邮箱凭证和 AI 写入入口 SHALL 留在本机。

### Requirement: 私有多账号 MCP

`mcp` SHALL 为本机 stdio 和授权私有网络 HTTPS 入口提供账号级能力查询和受控的邮件读写与发送。网关 SHALL 对账号、工具和参数实施同一服务端策略。网络入口及审批入口 SHALL 无法经公网 Web 端点访问。原生单账号 stdio 的协议和调用方式 SHALL 保持兼容。

### Requirement: 按账号执行发送策略

`mcp` SHALL 为每账号执行直接发送、人工确认或禁用策略。需要确认的发送、回复和转发 SHALL 在本机由人批准固定快照后执行，批准只能认领一次。拒绝、篡改和过期的请求 SHALL 不触发投递，并发重复调用 SHALL 不触发额外投递。发送结果未知时 SHALL 不自动重发。

### Requirement: 双因素保护的只读邮件网页

新 capability `mail-web` SHALL 提供账号切换、文件夹、列表、搜索、正文和附件读取。页面、API 及附件 SHALL 由前端站点验证二步登录和账号权限。读取 SHALL 保持现有邮件状态，并隔离不可信 HTML 与附件。Web 的公开入口 SHALL 仅开放阅读功能。

### Requirement: 记住此浏览器 30 天

`mail-web` SHALL 提供默认关闭的「记住此浏览器 30 天」选项。在用户主动选择并通过密码和 TOTP 后，站点身份服务 SHALL 保留仅用于只读邮件网页的持久会话。同一浏览器配置文件在完整登录后的 30 天内可凭有效会话访问页面、API 和附件，并继续接受账号权限检查。重复访问 SHALL 不延长有效期。退出和服务端撤销 SHALL 使会话失效。到期、清除 Cookie 或换用没有有效会话的浏览器后，用户 SHALL 重新完成密码和 TOTP 验证。审批和 MCP SHALL 继续使用独立授权。

### Requirement: 五类邮箱的部署验收

`packaging` SHALL 提供通用 IMAP/SMTP、Gmail API、Microsoft 365/Outlook 的 Microsoft Graph、QQ 和 163 的脱敏配置说明和实测矩阵。验收 SHALL 区分真实账号已测、后端不支持和待实测。公开自动化 SHALL 使用合成邮件与账号。
