# moviepilot-plugin-jackett

MoviePilot v3 的 Jackett / Torznab 索引器插件。在官方版本基础上补上 `get_module()` 方法，让 v3 的 PluginModuleCatalog dispatcher 能正确发现本插件的 `search_torrents` / `async_search_torrents` 钩子。

## 为什么需要这个 fork

MP v3.0.10 引入了 ModuleMethodContract 注册机制——dispatcher 只调用 `plugin.get_module()` 返回的 mapping 里的方法。官方 jackett 插件没实现 `get_module()`，所以即使 `search_torrents` / `async_search_torrents` 都定义了，搜索链路里也会被静默跳过，日志看不到 `[plugins/jackett]` 前缀。

本仓库的修复：补一个 `get_module()`，把它声明的两个钩子交给 dispatcher。

## 安装

**从 Git 安装**（MP Web UI）：
1. 进 MP 后台 → 插件 → 从 Git 安装
3. 仓库 URL 填：`https://github.com/lyj324/moviepilot-plugin-jackett`
3. 装完在"已安装插件"里能看到 Jackett 1.1.0

## 配置

在插件表单里填：
- Jackett 地址（如 `http://192.168.50.55:9117`）
- API Key（Jackett 管理界面右上角）
- 管理密码（如果 Jackett 设了 Admin password 才需填）
- 索引器列表：留空 = Jackett 中所有 configured indexer

保存后点右上角"刷新插件"让索引器列表生效。

## 与官方 jackett 插件的关系

- 同样的 `plugin_id: "jackett"`，会**替换** `/app/app/plugins/jackett/` 的内容
- `plugin_config_prefix: "jackett_"` 与官方一致 → 你已经配过的 Jackett host/api_key 不会被丢
- 版本号 1.1.0 > 官方版，确保 MP 升级时我们的版本优先

## 实现细节

- 用 `app.adapters.network.http.AsyncRequestUtils` 走 v3 自带的异步 HTTP 客户端（自动代理配置）
- 现代 Jackett (>= 0.22) 即使 AdminPassword 为空也要求 session cookie → 插件会 POST `/UI/Dashboard` 拿 cookie 再带 cookie 调 `/api/v2.0/indexers`，避免 302 → /UI/Login
- 响应是 HTML / 空 / DTD 都做了安全过滤；Torznab XML 用 stdlib `xml.etree.ElementTree`，5 MB 响应上限防恶意载荷