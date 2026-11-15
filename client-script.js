// 粘贴到支持 main(config) 的 Clash / Mihomo 客户端配置脚本中。
// 先将 localRules 中的占位符替换为自己的域名后缀和 CIDR 网段。
// 域名由本地 SOCKS 使用路由器 DNS 解析，此脚本不修改客户端 DNS。
function main(config) {
  const localName = "本地路由器 SOCKS";
  const localProxy = {
    name: localName,
    type: "socks5",
    server: "127.0.0.1",
    port: 10808,
    udp: false
  };

  const localRules = [
    `DOMAIN-SUFFIX,<内网域名后缀>,${localName}`,
    `IP-CIDR,<内网网段>,${localName},no-resolve`
  ];

  if (localRules.some(rule => /<[^>]+>/.test(rule))) {
    throw new Error("请先替换 localRules 中的 <内网域名后缀> 和 <内网网段>。");
  }

  const proxies = Array.isArray(config.proxies) ? config.proxies : [];
  config.proxies = [
    localProxy,
    ...proxies.filter(proxy => proxy.name !== localName)
  ];

  const rules = Array.isArray(config.rules) ? config.rules : [];
  config.rules = [
    ...localRules,
    ...rules.filter(rule => !localRules.includes(rule))
  ];

  return config;
}
