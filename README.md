# moviepilot-plugin-jackett

MoviePilot v3 的 Jackett / Torznab 索引器插件。在官方版本基础上补上 `get_module()` 方法，让 v3 的 PluginModuleCatalog dispatcher 能正确发现本插件的 `search_torrents` / `async_search_torrents` 钩子。

## 为什么需要这个 fork

MP v3.0.10 引入了 ModuleMethodContract 注册机制——dispatcher 只调用 `plugin.get_module()` 返回的 mapping 里的方法。官方 jackett 插件没实现 `get_module()`，所以即使 `search_torrents` / `async_search_torrents` 都定义了，搜索链路里也会被静默跳过，日志看不到 `[plugins/jackett]` 前缀。

本仓库的修复：补一个 `get_module()`，把它声明的两个钩子交给 dispatcher。

## 仓库结构（对齐 MP 官方插件仓库格式）

```
moviepilot-plugin-jackett/
├── package.v3.json             ← MP 解析这个 JSON 来枚举插件
├── plugins.v3/                 ← 所有 v3 插件目录
│   └── jackett/                ← plugin_id = jackett（小写）
│       ├── __init__.py         ← 插件代码
│       └── requirements.txt      ← 依赖（defusedxml）
├── README.md
├── .gitattributes
└── .gitignore
```

`package.v3.json` 是关键——MP v3 通过它发现哪些插件可用，没有它仓库会被识别成"未绑定"。

## 安装

**插件仓库**（MP Web UI）：

1. MP 后台 → 插件 → **插件仓库** → 添加 `https://github.com/lyj324/moviepilot-plugin-jackett`
2. 同步仓库列表
3. 在列表里找到 Jackett 1.1.0，点安装
4. 已安装插件里：版本 `v1.1.0`、作者 `lyj324`、**来源**不再是"未绑定"

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
- 响应是 HTML / 空 / DTD 都做了安全过滤；Torznab XML 用 `defusedxml.ElementTree`（防 XXE / billion-laughs），5 MB 响应上限防恶意载荷
- 关键日志里只打 URL path，避免 apikey 泄露到日志文件