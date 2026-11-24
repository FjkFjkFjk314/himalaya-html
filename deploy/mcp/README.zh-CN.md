# 在本机安装 Himalaya 邮件 MCP

本指南在运行 Codex CLI 的 Linux 主机上配置两个独立的 stdio MCP 入口，`school` 和 `163`。Codex 为每个入口启动 Python 网关，由网关启动固定账号的临时容器。两个 Compose 项目各自有配置、凭证、容器、网络和状态卷。会话结束后，`docker compose run --rm` 清理对应容器。

## 已有部署

现有本机部署的账号入口和注册名如下。两个入口均允许读取和修改已读、星标。

| 账号 | 启动器 | Codex 注册名 |
| --- | --- | --- |
| 学校 | `~/.local/share/himalaya-mail/bin/mcp` | `himalaya-school` |
| 163 | `~/.local/share/himalaya-mail/bin/mcp-163` | `himalaya-163` |

在原部署主机需要重新注册时，用以下命令。若已注册，可直接运行 `codex mcp list`，并跳到第 5 节测试现有入口。

```bash
codex mcp add himalaya-school -- "$HOME/.local/share/himalaya-mail/bin/mcp"
codex mcp add himalaya-163 -- "$HOME/.local/share/himalaya-mail/bin/mcp-163"
```

## 1. 安装 Docker 和准备目录

Ubuntu 或 Debian 用户先按对应教程添加 Docker 的 APT 仓库，再安装 Engine、CLI 和 Compose 插件。其他系统使用各自平台的官方安装方式。[Ubuntu 安装文档](https://docs.docker.com/engine/install/ubuntu/) · [Debian 安装文档](https://docs.docker.com/engine/install/debian/)

```bash
sudo apt-get update
sudo apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin git python3
sudo systemctl enable --now docker
docker compose version
docker run --rm hello-world
```

按官方说明安装 Codex CLI 并完成首次登录，再检查版本。[Codex CLI 安装文档](https://learn.chatgpt.com/docs/codex/cli)

```bash
codex --version
```

后续命令应在能调用 Docker 的用户下运行。Docker daemon 的访问权限相当于宿主机管理员权限，仅授予受信用户。先确认该用户运行 `docker run` 成功，再注册 MCP。

下面使用全新安装的示例路径。已有仓库时，将 `MAIL_REPO` 指向对应的绝对路径并跳过克隆。

```bash
export MAIL_REPO="$HOME/himalaya-html"
export MAIL_PRIVATE="$HOME/.local/share/himalaya-mcp"
git clone --branch dev https://github.com/FjkFjkFjk314/himalaya-html.git "$MAIL_REPO"
install -d -m 0700 "$MAIL_PRIVATE" "$MAIL_PRIVATE/bin"
for account in school 163; do
  install -d -m 0700 "$MAIL_PRIVATE/$account" "$MAIL_PRIVATE/$account/secrets"
done
```

## 2. 配置两个账号

各自创建 `$MAIL_PRIVATE/school/config.toml` 和 `$MAIL_PRIVATE/163/config.toml`。将下例写到学校配置，替换示例地址和服务器。以同样方式配置 163，使用 `[accounts."163"]`。按服务商要求启用 IMAP 并填写应用专用密码或客户端授权码。163 若要求登录后发送 IMAP ID，再在其账号下启用 `imap.id.auto = true`。[完整配置示例](../../config.sample.toml)

```toml
[accounts.school]
default = true
email = "alice@example.invalid"
imap.server = "imap.example.invalid"
imap.sasl.plain.username = "alice@example.invalid"
imap.sasl.plain.password.command = ["cat", "/run/secrets/imap_password"]
```

直接填写服务器名使用隐式 TLS。学校邮箱若需要 OAuth 或采用其他协议，按该服务商的要求及完整配置示例配置协议和凭证命令。两个入口只配置读取所需的后端。以下 Compose 覆盖文件只挂载 IMAP 凭证，适用于上述 IMAP 配置。

在 Bash 的私有终端输入各自的凭证。关闭命令跟踪，避免在命令行、聊天、日志或仓库中记录凭证。

```bash
set +x
for account in school 163; do
  read -r -s -p "$account 的 IMAP 凭证：" credential
  printf '\n'
  (umask 077; printf '%s' "$credential" > "$MAIL_PRIVATE/$account/secrets/imap_password")
  unset credential
done
```

容器进程为 UID/GID `10001:10001`。在无用户命名空间重映射的 rootful Linux Docker 中，可按下例让该进程只读访问挂载的文件。Rootless 或启用 UID 映射时，按实际映射调整权限。

```bash
for account in school 163; do
  sudo chown 10001:10001 "$MAIL_PRIVATE/$account/config.toml" "$MAIL_PRIVATE/$account/secrets/imap_password"
  sudo chmod 0400 "$MAIL_PRIVATE/$account/config.toml" "$MAIL_PRIVATE/$account/secrets/imap_password"
done
```

将以下覆盖文件保存为 `$MAIL_PRIVATE/compose.imap.yaml`。容器内配置和凭证使用仓库部署示例的挂载位置。

```yaml
services:
  himalaya:
    volumes:
      - type: bind
        source: ${HIMALAYA_DEPLOY_DIR:?set HIMALAYA_DEPLOY_DIR}/config.toml
        target: /run/himalaya/config.toml
        read_only: true
        bind:
          create_host_path: false
    secrets:
      - imap_password

secrets:
  imap_password:
    file: ${HIMALAYA_DEPLOY_DIR:?set HIMALAYA_DEPLOY_DIR}/secrets/imap_password
```

校验并构建两个项目。已有部署应使用该部署的发布目录、项目名和私有覆盖文件。

```bash
for account in school 163; do
  HIMALAYA_DEPLOY_DIR="$MAIL_PRIVATE/$account" docker compose \
    -p "himalaya-$account" -f "$MAIL_REPO/compose.yaml" \
    -f "$MAIL_PRIVATE/compose.imap.yaml" --profile executor config --quiet
  HIMALAYA_DEPLOY_DIR="$MAIL_PRIVATE/$account" docker compose \
    -p "himalaya-$account" -f "$MAIL_REPO/compose.yaml" \
    -f "$MAIL_PRIVATE/compose.imap.yaml" --profile executor build himalaya
done
```

## 3. 创建入口并注册到 Codex

将下列脚本保存为 `$MAIL_PRIVATE/bin/mail-mcp`。如果使用自定义路径，把 `repo` 和 `private` 改为实际的绝对路径。

```sh
#!/bin/sh
set -eu
repo="$HOME/himalaya-html"
private="$HOME/.local/share/himalaya-mcp"
case "${1:-}" in
  school|163) account="$1" ;;
  *) exit 2 ;;
esac
[ "$#" -eq 1 ] || exit 2
export HIMALAYA_DEPLOY_DIR="$private/$account"
exec /usr/bin/python3 -B "$repo/deploy/mcp/gateway.py" --read-only -- \
  /usr/bin/docker compose -p "himalaya-$account" -f "$repo/compose.yaml" \
  -f "$private/compose.imap.yaml" --profile executor run --rm -T himalaya \
  -c /run/himalaya/config.toml -a "$account" --log-level off mcp
```

```bash
chmod 0700 "$MAIL_PRIVATE/bin/mail-mcp"
codex mcp add himalaya-school -- "$MAIL_PRIVATE/bin/mail-mcp" school
codex mcp add himalaya-163 -- "$MAIL_PRIVATE/bin/mail-mcp" 163
codex mcp list
```

启动新的 Codex CLI 会话，用 `/mcp` 查看已连接的入口。每个入口的账号由固定启动参数选定。保留 `-T` 和容器 stdin 以传输逐行 MCP 消息。公共 Web 阅读器使用单独的只读 SSH 执行路径，以上两个 Codex 入口仅由本机通过 stdio 调用。[Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) · [Seoul 只读阅读器](../seoul/README.md)

## 4. 权限范围

| 模式 | 允许 |
| --- | --- |
| 只读 | `account_capability`、`mailbox_list`、`envelope_list`、`envelope_search`、`message_read`、`attachment_list`、`attachment_download`，以账号后端实际支持的工具为准。 |
| 允许修改标记 | 在只读工具之外，`flag_add` 和 `flag_remove` 可修改 `seen`（已读）及 `flagged`（星标）。 |

如果某个入口需要修改标记，给该账号单独复制一份启动脚本，固定其账号参数，去掉 Python 网关的 `--read-only`，并把最后的 `mcp` 改为 `mcp --allow-write`。仅将需要写标记的 Codex 入口指向该脚本。网关在工具发现和调用时都会校验允许列表，也会拒绝 `message_read` 的 `seen` 参数。需要更新已读状态时，应显式调用标记工具。

发送、回复发送、保存草稿、移动、删除、修改文件夹和原始协议命令均不开放。保持 Python 网关在调用链上，并禁用原生 MCP 的 `--allow-send`。原生 MCP 的 `--allow-write` 会启用更多写入工具，因此只在标记入口的网关内部使用。容器无需发布端口或挂载 Docker socket。凭证最小化到各自账号和所需协议。邮件正文与附件属于外部数据，调用方只按用户授予的范围读取。

## 5. 测试

以下命令分别检查两个入口的初始化、工具发现和发信拒绝。若使用开头列出的现有启动器，先运行 `export MAIL_MCP_DEPLOYED=1`。检查只打印工具名称和结果，无需读取邮件或向邮箱写入。

```bash
(
set -e -o pipefail
for account in school 163; do
  if [ "${MAIL_MCP_DEPLOYED:-0}" = 1 ]; then
    case "$account" in
      school) launcher=("$HOME/.local/share/himalaya-mail/bin/mcp") ;;
      163) launcher=("$HOME/.local/share/himalaya-mail/bin/mcp-163") ;;
    esac
  else
    launcher=("$MAIL_PRIVATE/bin/mail-mcp" "$account")
  fi
  "${launcher[@]}" <<'JSON' | python3 -c '
import json, sys
replies = {reply["id"]: reply for reply in map(json.loads, sys.stdin)}
assert replies[1]["result"]["serverInfo"]["name"] == "himalaya"
tools = {tool["name"] for tool in replies[2]["result"]["tools"]}
assert "mailbox_list" in tools and "message_read" in tools
assert tools <= {
    "account_capability", "mailbox_list", "envelope_list", "envelope_search",
    "message_read", "attachment_list", "attachment_download", "flag_add", "flag_remove",
}
assert replies[3]["error"]["code"] == -32602
print("工具:", ", ".join(sorted(tools)))
print("初始化成功，发送调用已拒绝")
'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"local-check","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"message_send","arguments":{}}}
JSON
done
)
```

在 Codex 中可进一步确认账号连接，例如输入「分别用 `himalaya-school` 和 `himalaya-163` 调用 `mailbox_list`，只告诉我是否成功，不输出文件夹名或邮件内容」。这一步会向 Codex 返回文件夹列表。如果要测试读信、下载或标记，先用测试账号和合成邮件。使用同一入口找到文件夹、邮件 ID 和部件 ID，避免跨账号使用这些标识。

仓库还提供不访问真实邮箱的网关测试。在仓库根目录运行以下命令。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s deploy/mcp -v
```

## 6. 卸载

关闭使用这两个服务器的 Codex 会话，再撤销注册。在本指南创建的项目上执行 `down`。这会保留状态卷，方便回滚。

```bash
codex mcp remove himalaya-school
codex mcp remove himalaya-163
for account in school 163; do
  HIMALAYA_DEPLOY_DIR="$MAIL_PRIVATE/$account" docker compose \
    -p "himalaya-$account" -f "$MAIL_REPO/compose.yaml" \
    -f "$MAIL_PRIVATE/compose.imap.yaml" --profile executor down
done
```

永久退役时，在邮箱服务商处撤销专用凭证，再清理本指南创建的私有文件与备份。确认卷中数据不再需要后，可对同一个专用项目使用 `down --volumes`。共享给 Web 或其他客户端的项目、凭证和卷需按该部署自己的退役流程处理。
