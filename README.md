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
| `start.command` | 后台启动服务，记录日志和 PID |
| `stop.command` | 停止本目录启动的服务 |
| `test.command` | 通过 SOCKS 测试指定 HTTP / HTTPS 地址 |

## 环境要求

- macOS、Python 3.9 或更高版本；Python 服务仅使用标准库。
- 路由器已配置公司 VPN，其 DNS 能解析内网域名。
- 代理客户端支持 SOCKS5 节点和 `main(config)` 配置脚本，使用 TUN + 智能分流 / 规则模式。

## 1. 启动 SOCKS 服务

默认参数为物理网卡 `en1`、路由器 DNS `192.168.1.1`、端口 `10808`：

```bash
./start.command
```

也可以按自己的网络传入参数：

```bash
./start.command en0 192.168.50.1 10808
```

双击 `start.command` 时使用脚本中的默认值。查看网卡可以运行 `networksetup -listallhardwareports`，也可以运行 `route -n get <路由器地址>` 查看到路由器的物理接口。更换网卡、路由器或端口前，先运行 `./stop.command`。

前台运行可使用：

```bash
python3 router_socks.py --interface en1 --router-dns 192.168.1.1 --port 10808
```

前台服务按 Ctrl+C 停止。后台服务写入 `socks.log`、`socks.pid`，这两个文件由 Git 忽略。可用 `PYTHON_BIN` 指定 Python 解释器。

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
DNS <内网域名> via <路由器地址> on physical interface -> <真实内网 IP>
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

已验证 TUN 开启时路由器 DNS 解析、内网 HTTPS、真实 IP CONNECT 和公网 HTTPS；客户端脚本接入后，浏览器访问公司内网也已通过。
