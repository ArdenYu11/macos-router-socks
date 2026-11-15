# macOS Router SOCKS

让公司内网流量通过本地 SOCKS5 使用路由器 DNS 和路由器 VPN，同时保留电脑代理客户端的 TUN 与其他网站分流。

```text
公司内网 → 代理客户端 TUN → 本地 SOCKS → 物理网卡 → 路由器 VPN → 公司内网
其他网站 → 代理客户端原有节点 → 网站
```

客户端配置脚本把公司域名和网段的规则置顶，选择本地 SOCKS 节点。无需额外设置链式代理。本地服务使用 macOS 的 `IP_BOUND_IF` / `IPV6_BOUND_IF` 约束出站网卡，让 DNS 查询和 TCP 连接通过物理网卡，避免再次进入 TUN。

## 文件

| 文件 | 用途 |
| --- | --- |
| `router_socks.py` | 仅监听 `127.0.0.1` 的 SOCKS5 TCP CONNECT 服务 |
| `client-script.js` | 添加本地 SOCKS 节点和内网规则的 `main(config)` 配置脚本 |
| `service_control.py` | 安装、管理当前用户的 macOS LaunchAgent |
| `start.command` | 启动服务；首次运行自动安装自启和进程守护 |
| `stop.command` | 暂停服务，下次登录仍可自启 |
| `enable-autostart.command` | 安装或更新登录自启与退出重启 |
| `disable-autostart.command` | 停止服务并移除登录自启 |
| `status.command` | 查看服务状态、PID、配置与日志路径 |
| `test.command` | 通过 SOCKS 测试指定 HTTP / HTTPS 地址 |

## 环境要求

- macOS、Python 3.9 或更高版本；Python 服务仅使用标准库。
- 路由器已配置公司 VPN，其 DNS 能解析内网域名。
- 代理客户端支持 SOCKS5 节点和 `main(config)` 配置脚本，使用 TUN + 智能分流 / 规则模式。

## 1. 启动 SOCKS 服务

默认自动检测物理网卡，路由器 DNS 为 `192.168.1.1`，端口为 `10808`：

```bash
./start.command
```

首次运行会安装当前用户的 LaunchAgent，立即启动服务，并启用登录自启与进程退出后重启。无需管理员权限。也可以双击 `start.command`。

自动检测优先选择通往路由器的物理网卡，排除 TUN、回环和虚拟网卡；检测不到物理网卡时保持监听，等待新连接时重试。默认每隔至少 5 秒在新连接时重新检测，Wi-Fi / 有线切换后新连接会使用重新选中的网卡。已经建立的 TCP 连接不会自动迁移。

可明确指定 `auto`、路由器 DNS 和端口：

```bash
./enable-autostart.command auto 192.168.50.1 10808
```

也可以固定物理网卡，例如：

```bash
./enable-autostart.command en0 192.168.50.1 10808
```

传入新参数会更新安装的服务配置并重启。自动检测只选择网卡，路由器 DNS 地址仍需配置。多个物理网卡无法确定出口时，服务会提示指定网卡，避免把流量送回 TUN。

### 自启与进程守护

- 自启在当前用户登录 macOS 时触发。
- `launchd` 使用 `KeepAlive` 在进程退出或被结束后重新启动；失败重启使用 5 秒节流，避免快速重复启动。
- PID 锁避免同一服务重复运行；SIGTERM 会释放监听和 PID。
- `stop.command` 通过卸载当前运行的 LaunchAgent 暂停服务，避免守护进程立刻把它拉起。下次登录仍会启动。
- `disable-autostart.command` 停止服务并移除登录自启配置，之后可以通过 `start.command` 重新安装。

```bash
./status.command
./status.command --json
./stop.command
./start.command
./disable-autostart.command
```

服务代码安装到 `~/Library/Application Support/macos-router-socks/`，自启配置位于 `~/Library/LaunchAgents/io.github.ardenyu11.router-socks.plist`。安装后服务使用独立副本，移动仓库不会影响已安装的运行文件。更新代码后再次运行 `./enable-autostart.command`，会重新复制代码并重启。

日志和 PID 也位于该 Application Support 目录，`socks.log` 每份 2 MiB、保留 3 份轮转备份。运行 `status.command` 可查看具体路径。配置保存在该目录的 `settings.json`。关闭自启会保留配置和日志。

如已有旧版后台服务占用了端口，先使用旧版的停止脚本退出，再安装新版自启。新安装不会终止占用端口的其他进程。

也可以仅在终端前台运行服务，不安装自启：

```bash
python3 router_socks.py --interface auto --router-dns 192.168.1.1 --port 10808
```

前台服务按 Ctrl+C 停止，PID 写在脚本旁边。可用 `PYTHON_BIN` 为管理脚本指定 Python 解释器。

## 2. 配置客户端脚本

编辑 `client-script.js` 中的 `localRules`，替换两个占位符：

| 占位符 | 填写方式 |
| --- | --- |
| `<内网域名后缀>` | 仅填域名后缀，如 `corp.example.com`，无需 `*.` 或 `+.` |
| `<内网网段>` | 填 CIDR 网段，如 `10.20.0.0/16` |

多个域名或网段可以增加多条规则；不用的规则可以删除。原样保留占位符会提示错误，避免向客户端提交无效规则。若修改 SOCKS 端口，也要同步修改脚本中的 `localProxy.port`。

将修改后的完整脚本粘贴到客户端的配置脚本功能，启用、保存并重新连接。脚本会：

- 添加名为 `本地路由器 SOCKS` 的节点，连接 `127.0.0.1:10808`。
- 把内网规则放到其他规则前面，选择该本地节点。
- 合并已有节点和规则，重复运行不重复添加同名节点或相同规则。
- 保留客户端原有的 DNS 设置。

保持 SOCKS 服务运行。配置脚本不会启动服务。DNS 覆写可以保持关闭；客户端应把域名还原后传给 SOCKS，服务才会向路由器解析。

## 3. 测试

传入自己的测试地址：

```bash
./test.command 'https://<内网网站>/路径'
```

自定义端口时：

```bash
./test.command 'https://<内网网站>/路径' 10808
```

收到 HTTP 状态表示服务器已响应，例如 `200` 或登录跳转的 `302`；是否有权限访问还取决于网站登录。这个命令直接测试本地 SOCKS。客户端脚本启用后，再用浏览器访问内网站点，验证 TUN 分流的完整路线。

日志应出现：

```text
Physical interface selected: <物理网卡> (<索引>), router DNS=<路由器地址>
DNS <内网域名> via <路由器地址> on <物理网卡> -> <真实内网 IP>
CONNECT <内网域名>:443 -> <真实内网 IP>:443 via <物理网卡>
```

停止服务：

```bash
./stop.command
```

## 功能范围

- 支持 SOCKS5 TCP CONNECT，包括 HTTPS；不支持 UDP ASSOCIATE。
- 域名查询使用路由器 DNS 的 IPv4 A 记录，UDP DNS 响应截断时改用 TCP。直接收到 IPv6 地址时可以通过绑定的物理网卡连接。
- 收到真实 IP 时直接连接。收到 `198.18.0.0/15` 的 Fake-IP 时返回明确错误，因为本地服务不能独立恢复客户端的 Fake-IP 映射；客户端需要发送原域名或真实地址。
- 绑定物理网卡绕过 TUN 已在实际环境验证。换网络或客户端改变 TUN 实现后，需要重新测试。

已验证 TUN 开启时自动检测物理网卡、路由器 DNS 解析、内网 HTTPS 和公网 HTTPS，以及强制结束进程后由 `launchd` 拉起并恢复访问。客户端脚本接入后的浏览器内网访问已在先前版本验证。

测试网卡选择、网络中断与切换、PID 锁和 LaunchAgent 配置：

```bash
python3 -m unittest discover -s tests -v
```

`launchd` 守护处理进程退出后的重启；不会终止一个仍在运行但无响应的进程。
