"""Jackett indexer plugin for MoviePilot v3.

Implements the async_search_torrents hook so Jackett's Torznab indexers
(27+ configured: 1337x, RARBG, nyaa, etc.) participate in v3's search chain.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlsplit

import defusedxml.ElementTree as ET

from app.domain.context import MediaType, TorrentInfo
from app.plugins import _PluginBase
from app.runtime.log import logger


class Jackett(_PluginBase):
    """Jackett / Torznab indexer bridge.

    Configure your Jackett URL + API key in the plugin form, optionally pick
    which Jackett indexers to use (empty = all), then MoviePilot's search chain
    will fan out Torznab queries to Jackett for each keyword.
    """

    plugin_name = "Jackett"
    plugin_desc = "通过 Jackett (Torznab) 接入第三方索引器（1337x/RARBG/nyaa 等）。"
    plugin_icon = "https://raw.githubusercontent.com/Jackett/Jackett/master/src/Jackett.Common/Content/favicon.ico"
    plugin_version = "1.1.0"
    plugin_author = "lyj324"
    author_url = "https://github.com/lyj324"
    plugin_config_prefix = "jackett_"
    plugin_order = 21
    auth_level = 2

    _enabled = False
    _host = ""
    _api_key = ""
    _password = ""
    _indexers: List[str] = []
    _session_cookies: Dict[str, str] = {}

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._host = (config.get("host") or "").rstrip("/")
        self._api_key = config.get("api_key") or ""
        self._password = config.get("password") or ""
        raw_indexers = config.get("indexers") or []
        if isinstance(raw_indexers, str):
            self._indexers = [x.strip() for x in raw_indexers.split(",") if x.strip()]
        elif isinstance(raw_indexers, list):
            self._indexers = [str(x).strip() for x in raw_indexers if str(x).strip()]
        else:
            self._indexers = []

    def get_state(self) -> bool:
        return self._enabled and bool(self._host) and bool(self._api_key)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        host_default = "http://192.168.50.55:9117"
        return (
            [
                {
                    "component": "VForm",
                    "content": [
                        {
                            "component": "VSwitch",
                            "props": {"model": "enabled", "label": "启用插件"},
                        },
                        {
                            "component": "VTextField",
                            "props": {
                                "model": "host",
                                "label": "Jackett 地址",
                                "placeholder": host_default,
                            },
                        },
                        {
                            "component": "VTextField",
                            "props": {
                                "model": "api_key",
                                "label": "API Key",
                                "type": "password",
                                "placeholder": "Jackett 管理界面右上角",
                            },
                        },
                        {
                            "component": "VTextField",
                            "props": {
                                "model": "password",
                                "label": "管理密码",
                                "type": "password",
                                "placeholder": "若 Jackett 设置了 Admin password 才需填写",
                            },
                        },
                        {
                            "component": "VAlert",
                            "props": {
                                "type": "info",
                                "variant": "tonal",
                                "text": "索引器列表留空表示使用 Jackett 中所有 configured indexer。修改后请保存配置后点击右上角「刷新插件」让索引器列表生效。",
                            },
                        },
                    ],
                }
            ],
            {
                "enabled": False,
                "host": host_default,
                "api_key": "",
                "password": "",
                "indexers": "",
            },
        )

    def get_page(self) -> List[dict]:
        cfg = self.get_config() or {}
        status = "已启用" if self.get_state() else "未启用"
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": (
                        f"状态：{status}\n"
                        f"Jackett 地址：{self._host or '(未配置)'}\n"
                        f"指定索引器：{'全部 ' + str(len(self._indexers)) if self._indexers else '全部（自动获取）'}"
                    ),
                },
            }
        ]

    def stop_service(self) -> None:
        self._enabled = False

    def get_module(self) -> dict[str, Any]:
        """向 v3 模块调度器声明本插件实现的钩子（dispatcher 通过这里发现）。

        不返回的话，``search_torrents`` / ``async_search_torrents`` 方法存在也不会被
        搜索链路调用——v3 的 ``PluginProjection.modules()`` 只读取 ``get_module()``。
        """
        return {
            "search_torrents": self.search_torrents,
            "async_search_torrents": self.async_search_torrents,
        }

    # ── Hook into v3 search chain ────────────────────────────────────────
    def search_torrents(
        self,
        site: Optional[Any] = None,
        keyword: Optional[str] = None,
        mtype: Optional[MediaType] = None,
        page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        return asyncio.run(self._async_search_impl(keyword, mtype, page))

    async def async_search_torrents(
        self,
        site: Optional[Any] = None,
        keyword: Optional[str] = None,
        mtype: Optional[MediaType] = None,
        page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        return await self._async_search_impl(keyword, mtype, page)

    async def _async_search_impl(
        self,
        keyword: Optional[str],
        mtype: Optional[MediaType],
        page: Optional[int],
    ) -> List[TorrentInfo]:
        if not self.get_state():
            logger.debug(f"Jackett: 插件未启用，跳过 '{keyword}'")
            return []
        if not keyword:
            return []

        logger.info(f"Jackett: 开始搜索 keyword='{keyword}' mtype={mtype}")
        indexers = await self._resolve_indexers()
        if not indexers:
            logger.warn("Jackett: 没有可用索引器（configured=true 返回空？）")
            return []

        tasks = [
            self._search_single_indexer(indexer, keyword, mtype, page)
            for indexer in indexers
        ]
        results_per_indexer = await asyncio.gather(*tasks, return_exceptions=True)

        merged: List[TorrentInfo] = []
        for indexer_id, result in zip(indexers, results_per_indexer):
            if isinstance(result, Exception):
                logger.warn(
                    f"Jackett 索引器 {indexer_id} 搜索失败：{result}"
                )
                continue
            merged.extend(result)
        return merged

    async def _resolve_indexers(self) -> List[str]:
        if self._indexers:
            return self._indexers
        all_indexers = await self._fetch_indexers()
        return [ix["id"] for ix in all_indexers if ix.get("id")]

    async def _fetch_indexers(self) -> List[dict]:
        """List configured Jackett indexers."""
        if not await self._ensure_authenticated():
            return []
        url = f"{self._host}/api/v2.0/indexers?configured=true"
        try:
            text = await self._http_get_text(url)
        except Exception as err:
            logger.error(f"Jackett 获取索引器列表失败：{err}")
            return []
        head = text[:200] if text else "(empty)"
        logger.info(f"Jackett API 响应状态码={getattr(self, '_last_status', '?')}, 长度={len(text)}, 头={head!r}")
        if head.lstrip().lower().startswith("<") or "ui/login" in head.lower():
            logger.error(f"Jackett API 响应像是 HTML/登录页，cookie 可能未生效")
            return []
        try:
            result = _parse_json_safe(text)
            logger.info(f"Jackett: 解析到 {len(result)} 个索引器")
            return result
        except Exception as err:
            logger.error(f"Jackett JSON 解析失败：{err}; 响应头：{head!r}")
            return []

    async def _ensure_authenticated(self) -> bool:
        """Modern Jackett (>=0.22?) requires a session cookie even when AdminPassword
        is null: API endpoints (incl. /api/v2.0/indexers) return 302 → /UI/Login if
        no session cookie is present. We POST /UI/Dashboard with the configured
        password (or empty when none) to obtain a session cookie, then reuse it
        on every subsequent request.
        """
        if self._session_cookies:
            return True
        url = f"{self._host}/UI/Dashboard"
        try:
            from app.adapters.network.http import AsyncRequestUtils
            utils = AsyncRequestUtils(timeout=15)
            # httpx 参数是 follow_redirects 而不是 allow_redirects
            response = await utils.post(
                url,
                data={"password": self._password},
                follow_redirects=False,
            )
            if response is None:
                logger.warn("Jackett: 登录 POST 返回 None")
                return True
            cookies = {}
            resp_cookies = getattr(response, "cookies", None)
            if resp_cookies is not None:
                try:
                    cookies = {k: v for k, v in resp_cookies.items()}
                except Exception:
                    try:
                        cookies = dict(resp_cookies)
                    except Exception:
                        cookies = {}
            status = getattr(response, "status_code", None)
            logger.info(
                f"Jackett: 登录 POST 收到 status={status}, 拿到 {len(cookies)} 个 cookie, "
                f"keys={[c for c in cookies.keys()][:3]}"
            )
            if cookies:
                self._session_cookies = cookies
            return True
        except Exception as err:
            logger.warn(f"Jackett 登录失败（继续尝试无 cookie 模式）：{err}")
        return True  # 不阻塞；很多 Jackett 老版本不需要 session

    async def _search_single_indexer(
        self,
        indexer_id: str,
        keyword: str,
        mtype: Optional[MediaType],
        page: Optional[int],
    ) -> List[TorrentInfo]:
        cat = self._torznab_category_for(mtype)
        params = {
            "apikey": self._api_key,
            "t": "search",
            "q": keyword,
            "limit": 100,
        }
        if cat:
            params["cat"] = cat
        url = f"{self._host}/api/v2.0/indexers/{indexer_id}/results/torznab/api?{urlencode(params)}"
        # 日志路径用 path-only，不带 apikey query string，避免凭证泄漏到日志
        url_path = urlsplit(url).path
        try:
            xml_text = await self._http_get_text(url)
        except Exception as err:
            logger.warn(f"Jackett [{indexer_id}] 请求失败：{err}")
            return []
        return _parse_torznab(xml_text, indexer_id)

    async def _http_get_text(self, url: str) -> str:
        """Use get_res to get the full Response object so we can see status,
        headers, and cookies (get() returns just the body string)."""
        from app.adapters.network.http import AsyncRequestUtils
        utils = AsyncRequestUtils(
            cookies=self._session_cookies or None,
            timeout=20,
        )
        response = await utils.get_res(
            url,
            allow_redirects=False,  # 关键：不要跟随 302 到登录页
        )
        if response is None:
            raise RuntimeError("no response")
        status = response.status_code
        self._last_status = status
        text = response.text or ""
        if status == 302:
            location = ""
            try:
                location = response.headers.get("Location", "")
            except Exception:
                pass
            # 只记 path 和 Location 头，绝不带 apikey query string
            logger.warn(
                f"Jackett: GET {url_path} 返回 302, Location={location!r}, "
                f"当前 cookie keys={list(self._session_cookies.keys()) if self._session_cookies else []}"
            )
            if "/UI/Login" in location:
                self._session_cookies = {}
                await self._ensure_authenticated()
                utils2 = AsyncRequestUtils(
                    cookies=self._session_cookies or None,
                    timeout=20,
                )
                response2 = await utils2.get_res(url, allow_redirects=False)
                if response2 is not None:
                    text = response2.text or ""
                    self._last_status = response2.status_code
        return text

    @staticmethod
    def _torznab_category_for(mtype: Optional[MediaType]) -> Optional[str]:
        if mtype == MediaType.MOVIE:
            return "2000"  # Movies
        if mtype == MediaType.TV:
            return "5000"  # TV
        return None


# ── Module-level helpers (no self needed) ──────────────────────────────
def _parse_json_safe(text: str) -> list:
    import json
    try:
        return json.loads(text) if text else []
    except Exception:
        return []


_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|K|M|G)?\s*$", re.IGNORECASE)


def _parse_size(size_str: str) -> float:
    if not size_str:
        return 0.0
    try:
        return float(size_str)
    except (TypeError, ValueError):
        pass
    match = _SIZE_RE.match(str(size_str))
    if not match:
        return 0.0
    value = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    multipliers = {
        "B": 1, "K": 1024, "KB": 1024, "M": 1024**2, "MB": 1024**2,
        "G": 1024**3, "GB": 1024**3, "T": 1024**4, "TB": 1024**4,
    }
    return value * multipliers.get(unit, 1)


def _parse_torznab(xml_text: str, indexer_id: str) -> List[TorrentInfo]:
    if not xml_text:
        return []
    # Defensive: defusedxml raises on DTD/entity declarations, and Python's
    # stdlib ET (3.7.1+) already disables external entity resolution by
    # default — belt-and-suspenders. We still cap the size and reject raw
    # "<!doctype" / "<!entity" markers as an extra guard against encoded
    # payloads that try to sneak past defusedxml's parser.
    if len(xml_text) > 5_000_000:  # 5 MB sanity cap
        logger.warn(f"Jackett [{indexer_id}] 响应过大（{len(xml_text)}B），跳过")
        return []
    lowered = xml_text[:4096].lower()
    if "<!doctype" in lowered or "<!entity" in lowered or "<![cdata[" in lowered and "]]><!entity" in lowered:
        logger.warn(f"Jackett [{indexer_id}] 响应包含 DTD/ENTITY，跳过以保安全")
        return []
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError) as err:
        # defusedxml raises ValueError on DTD/entity; stdlib raises ParseError
        logger.warn(f"Jackett [{indexer_id}] XML 解析失败：{err}")
        return []
    if root.tag != "rss":
        return []

    channel = root.find("channel")
    if channel is None:
        return []

    def _text_of(el) -> str:
        """Return element text as str, never None."""
        if el is None:
            return ""
        t = el.text
        return t if isinstance(t, str) else ""

    out: List[TorrentInfo] = []
    for item in channel.findall("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        guid_el = item.find("guid")
        size_el = item.find("size")
        pub_el = item.find("pubDate")
        desc_el = item.find("description")

        title = _text_of(title_el).strip()
        link = _text_of(link_el).strip()
        guid = _text_of(guid_el).strip() or link

        if not title or not link:
            continue

        seeders = 0
        peers = 0
        grabs = 0
        upload_factor = None
        download_factor = None
        hit_and_run = False
        freedate = ""
        labels: list[str] = []
        for attr in item.findall("{http://torznab.com/schemas/2015/feed}attr"):
            name = attr.get("name")
            value = attr.get("value")
            if not name:
                continue
            lname = name.lower()
            try:
                if lname == "seeders":
                    seeders = int(value or 0)
                elif lname == "peers":
                    peers = int(value or 0)
                elif lname == "grabs":
                    grabs = int(value or 0)
                elif lname == "downloadvolumefactor":
                    download_factor = float(value)
                elif lname == "uploadvolumefactor":
                    upload_factor = float(value)
                elif lname in ("hitandrun", "hit_and_run"):
                    hit_and_run = str(value).lower() in ("1", "true", "yes")
                elif lname in ("freedate",):
                    freedate = str(value or "")
                elif lname == "category":
                    # 数值类目（100000 拆分）按需可入 labels
                    labels.append(f"cat:{value}")
                elif lname in ("imdb", "tvdb", "tmdb"):
                    labels.append(f"{lname}:{value}")
            except (TypeError, ValueError):
                pass

        description = _text_of(desc_el).strip()

        torrent = TorrentInfo(
            site=hash(indexer_id) & 0x7FFFFFFF,  # synthetic numeric id
            site_name=f"[Jackett] {indexer_id}",
            site_cookie=None,
            site_ua=None,
            site_proxy=False,
            site_order=0,
            site_downloader=None,
            title=title,
            description=description,
            enclosure=link,
            page_url=guid or link,
            size=_parse_size(_text_of(size_el)),
            seeders=seeders,
            peers=peers,
            grabs=grabs,
            pubdate=_text_of(pub_el),
            freedate=freedate or None,
            uploadvolumefactor=upload_factor,
            downloadvolumefactor=download_factor,
            hit_and_run=hit_and_run,
            labels=labels,
        )
        out.append(torrent)
    return out