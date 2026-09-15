import traceback
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from utils import norm
from core.msg_builder import build_message, build_message_with_openai
from core.browser import get_browser
from core.ws_client import DouyinWSClient
from playwright.sync_api import Response
import time
import json
import os

config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))

# 好友信息字典: remark_name -> {uid, short_id, unique_id, sec_uid, nickname, remark_name}
userIDDict = {}
# 当前用户信息: {uid, device_id}
myUserInfo = {}
# 捕获的 API URL 列表（用于调试）
captured_api_urls = []
# 捕获的页面自身 WebSocket 连接 URL（包含正确的 device_id/token/access_key）
captured_ws_url = None

CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper"
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper"
CHAT_EDITOR_SELECTOR = ".messageEditorimChatEditorContainer"

CONVERSATION_LIST_SELECTORS = [
    ".conversationConversationListwrapper",
    "[class*='conversationList']",
    "[class*='conversation-list']",
    "[class*='chat-list']",
    "[class*='session-list']",
    "[data-e2e*='chat-list']",
    "[data-e2e*='conversation']",
    "nav[class*='sidebar']",
    "aside[class*='sidebar']",
    "[class*='im-chat']",
]


def handle_websocket(ws):
    """
    监听页面自身的 WebSocket 连接，捕获 IM 连接 URL
    （其中包含服务端认可的 device_id / token / access_key）
    """
    global captured_ws_url, myUserInfo
    try:
        url = ws.url
        if ("frontier" in url or "im" in url.lower()) and url.startswith("wss://"):
            captured_ws_url = url
            logger.info(f"捕获到页面 IM WebSocket 连接: {url[:180]}")
            # 从 WebSocket URL 中提取 device_id
            try:
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(url)
                qs = parse_qs(parsed.query)
                ws_device_id = qs.get("device_id", [""])[0]
                if ws_device_id and ws_device_id != "0":
                    myUserInfo["device_id"] = ws_device_id
                    logger.debug(f"从 WebSocket URL 提取到 device_id: {ws_device_id}")
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"捕获 WebSocket 连接失败: {e}")


def handle_response(response: Response):
    """
    监听接口响应，收集好友完整信息和当前用户信息
    """
    global userIDDict, myUserInfo, captured_api_urls

    url = response.url

    # 记录所有 awene/im 相关的 API 调用（用于调试）
    if ("aweme" in url or "/im/" in url) and "static" not in url:
        captured_api_urls.append(url)
        logger.debug(f"API 响应: {url[:120]}")

    # 会话列表接口（主要好友信息来源）
    if "aweme/v1/web/im/conversation/list" in url or "conversation/list" in url:
        try:
            json_data = response.json()
            if not isinstance(json_data, dict):
                return
            # 尝试多种可能的响应结构
            items = (
                json_data.get("data", []) or
                json_data.get("conversations", []) or
                json_data.get("conversation_list", []) or
                []
            )
            logger.debug(f"会话列表 API 返回 {len(items)} 条会话")
            for item in items:
                _parse_conversation_item(item)
        except Exception as e:
            logger.warning(f"解析会话列表响应失败: {e}")

    # 好友信息接口
    elif "aweme/v1/web/im/user/info" in url:
        try:
            json_data = response.json()
            if not isinstance(json_data, dict):
                return
            data_list = json_data.get("data", [])
            if data_list is None:
                logger.debug(f"用户信息 API 返回空 data 字段")
                return
            for item in data_list:
                _parse_user_info_item(item)
        except Exception as e:
            logger.warning(f"解析好友信息响应失败: {e}")

    # 搜索接口（通用搜索和 IM 搜索）
    elif "/search/" in url and "aweme" in url:
        try:
            json_data = response.json()
            if not isinstance(json_data, dict):
                return
            # 通用搜索: data 数组中 type=1 的是用户，user_list 里是用户信息
            data_list = json_data.get("data", []) or []
            user_count = 0
            for block in data_list:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == 1 or "user_list" in block:
                    for ul in block.get("user_list", []) or []:
                        user_info = ul.get("user_info", {}) if isinstance(ul, dict) else {}
                        if user_info:
                            _parse_user_info_item(user_info)
                            user_count += 1
            # IM 搜索: 可能直接返回用户列表
            for key in ("user_list", "users", "friend_list"):
                for ul in json_data.get(key, []) or []:
                    user_info = ul.get("user_info", ul) if isinstance(ul, dict) else {}
                    if isinstance(user_info, dict) and user_info.get("uid"):
                        _parse_user_info_item(user_info)
                        user_count += 1
            if user_count:
                logger.debug(f"搜索 API 解析到 {user_count} 个用户")
        except Exception as e:
            logger.debug(f"解析搜索响应失败: {e}")

    # 当前用户信息接口（多个可能的端点）
    elif "aweme/v1/web/query/user" in url or "aweme/v1/web/im/user/me" in url:
        try:
            json_data = response.json()
            if not isinstance(json_data, dict):
                return
            logger.debug(f"用户信息 API 响应 keys: {list(json_data.keys())}")
            status_code = json_data.get("status_code", None)
            # user_uid 是用户 UID；id 可能是设备 ID（未登录时也会返回）
            uid = (
                json_data.get("user_uid", "") or
                json_data.get("uid", "") or
                json_data.get("user", {}).get("uid", "") or
                json_data.get("user", {}).get("id", "") or
                json_data.get("id", "")
            )
            if uid and str(uid) != "0":
                myUserInfo["uid"] = str(uid)
                logger.debug(f"捕获到当前用户 uid: {uid} (status_code={status_code})")
            # device_id 从专门字段获取
            device_id = (
                json_data.get("device_id", "") or
                json_data.get("user", {}).get("device_id", "")
            )
            if device_id:
                myUserInfo["device_id"] = str(device_id)
                logger.debug(f"捕获到 device_id: {device_id}")
        except Exception as e:
            logger.warning(f"解析当前用户信息响应失败: {e}")


def _parse_conversation_item(item: dict):
    """解析会话列表项，提取好友信息"""
    global userIDDict
    if not isinstance(item, dict):
        return

    uid = (
        item.get("to_uid", "") or
        item.get("uid", "") or
        item.get("user_id", "") or
        ""
    )
    # 从 ext 字段提取好友信息
    ext = item.get("ext", {}) or {}
    if isinstance(ext, str):
        try:
            ext = json.loads(ext)
        except:
            ext = {}

    nickname = norm(ext.get("nickname", "") or item.get("nickname", ""))
    remark_name = norm(ext.get("remark_name", "") or item.get("remark_name", nickname))
    short_id = ext.get("short_id", "") or item.get("short_id", "")
    unique_id = ext.get("unique_id", "") or item.get("unique_id", "")
    sec_uid = ext.get("sec_uid", "") or item.get("sec_uid", "")

    # 如果没有 uid，尝试从 conversation_id 解析
    if not uid:
        conv_id = item.get("conversation_id", "") or item.get("conversation_short_id", "")
        if conv_id and ":" in str(conv_id):
            parts = str(conv_id).split(":")
            if len(parts) >= 3:
                uid = parts[2]

    if remark_name and uid:
        userIDDict[remark_name] = {
            "uid": uid,
            "short_id": short_id,
            "unique_id": unique_id,
            "sec_uid": sec_uid,
            "nickname": nickname,
            "remark_name": remark_name,
        }


def _parse_user_info_item(item: dict):
    """解析用户信息项，提取好友信息"""
    global userIDDict
    if not isinstance(item, dict):
        return

    uid = item.get("uid", "") or item.get("id", "")
    short_id = item.get("short_id", "")
    unique_id = item.get("unique_id", "")
    sec_uid = item.get("sec_uid", "")
    nickname = norm(item.get("nickname", ""))
    remark_name = norm(item.get("remark_name", nickname))

    if remark_name and uid:
        userIDDict[remark_name] = {
            "uid": uid,
            "short_id": short_id,
            "unique_id": unique_id,
            "sec_uid": sec_uid,
            "nickname": nickname,
            "remark_name": remark_name,
        }


def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    """
    通用的重试逻辑
    """
    for attempt in range(retries):
        try:
            return operation(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"{name} 失败，正在重试第 {attempt + 1} 次，错误：{e}")
                time.sleep(delay)
            else:
                logger.error(f"{name} 失败，已达到最大重试次数，错误：{e}")
                raise


def scroll_conversation_list(page, username, max_scrolls=15):
    """滚动会话列表，触发 API 调用以收集好友信息"""
    logger.debug(f"账号 {username} 开始滚动会话列表收集好友信息")

    empty_scroll_count = 0
    MAX_EMPTY_SCROLLS = 10

    # 尝试多个选择器找到会话列表容器
    scrollable_element = None
    matched_selector = None
    for selector in CONVERSATION_LIST_SELECTORS:
        try:
            scrollable_element = page.query_selector(selector)
            if scrollable_element:
                matched_selector = selector
                logger.debug(f"找到会话列表容器: {selector}")
                break
        except Exception:
            continue

    if not scrollable_element:
        # 尝试检测是否存在任何可滚动的列表容器（evaluate 返回布尔值，DOM 元素无法序列化）
        try:
            has_scrollable = page.evaluate("""
                () => {
                    const candidates = document.querySelectorAll('div[class*="list"], div[class*="container"], nav, aside');
                    for (const el of candidates) {
                        if (el.scrollHeight > el.clientHeight && el.clientHeight > 100) {
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if has_scrollable:
                logger.debug("找到可滚动容器（通过 scrollHeight 检测）")
                matched_selector = "auto-detected"
                scrollable_element = True  # 哨兵值，实际滚动用 evaluate 内联查找
        except Exception:
            pass

    if not scrollable_element:
        logger.warning(f"账号 {username} 未找到会话列表容器，可能页面未完全加载")
        # 尝试记录页面上所有可能的会话相关元素
        try:
            elem_info = page.evaluate("""
                () => {
                    const results = [];
                    const selectors = ['[class*="conversation"]', '[class*="chat"]', '[class*="session"]', '[data-e2e*="chat"]', '[class*="im-"]'];
                    for (const sel of selectors) {
                        const els = document.querySelectorAll(sel);
                        if (els.length > 0) {
                            results.push({selector: sel, count: els.length, firstClass: els[0].className.substring(0, 100)});
                        }
                    }
                    return results;
                }
            """)
            if elem_info:
                for info in elem_info:
                    logger.debug(f"  找到元素: {info.get('selector')} ({info.get('count')}个) class={info.get('firstClass')}")
        except Exception:
            pass
        return

    for _ in range(max_scrolls):
        try:
            if matched_selector == "auto-detected":
                scroll_top_before = page.evaluate("""
                    () => {
                        const candidates = document.querySelectorAll('div[class*="list"], div[class*="container"], nav, aside');
                        for (const el of candidates) {
                            if (el.scrollHeight > el.clientHeight && el.clientHeight > 100) {
                                return el.scrollTop;
                            }
                        }
                        return -1;
                    }
                """)
                page.evaluate("""
                    () => {
                        const candidates = document.querySelectorAll('div[class*="list"], div[class*="container"], nav, aside');
                        for (const el of candidates) {
                            if (el.scrollHeight > el.clientHeight && el.clientHeight > 100) {
                                el.scrollTop += 800;
                                break;
                            }
                        }
                    }
                """)
                time.sleep(0.3)
                scroll_top_after = page.evaluate("""
                    () => {
                        const candidates = document.querySelectorAll('div[class*="list"], div[class*="container"], nav, aside');
                        for (const el of candidates) {
                            if (el.scrollHeight > el.clientHeight && el.clientHeight > 100) {
                                return el.scrollTop;
                            }
                        }
                        return -1;
                    }
                """)
            else:
                scroll_top_before = page.evaluate(
                    "(element) => element.scrollTop", scrollable_element
                )
                page.evaluate("(element) => element.scrollTop += 800", scrollable_element)
                time.sleep(0.3)
                scroll_top_after = page.evaluate(
                    "(element) => element.scrollTop", scrollable_element
                )

            if scroll_top_before == scroll_top_after:
                empty_scroll_count += 2
            else:
                empty_scroll_count = 0
                logger.debug(f"账号 {username} 滚动好友列表 (scrollTop: {scroll_top_before} -> {scroll_top_after})")

            if empty_scroll_count >= MAX_EMPTY_SCROLLS:
                logger.debug(f"账号 {username} 滚动完成，共收集到 {len(userIDDict)} 个好友")
                break

            time.sleep(1.5)
        except Exception as e:
            logger.warning(f"账号 {username} 滚动过程出错: {e}")
            break


def fetch_friends_via_api(page):
    """通过 API 获取好友列表（fallback 方案）"""
    global userIDDict
    logger.info("尝试通过 API 获取好友信息")

    # 尝试调用会话列表 API（带更多参数）
    result = page.evaluate("""
        async () => {
            const results = [];
            const baseParams = 'device_platform=web&aid=6383&channel=channel_pc_web&pc_client_version=1.0.0&cookie_enabled=true&browser_language=zh-CN';
            const apis = [
                {
                    url: '/aweme/v1/web/im/conversation/list',
                    params: 'inbox_type=0&cursor=0&limit=50&' + baseParams,
                },
                {
                    url: '/aweme/v1/web/im/conversation/list',
                    params: 'inbox_type=1&cursor=0&limit=50&' + baseParams,
                },
                {
                    url: '/aweme/v1/web/im/conversation/list',
                    params: 'inbox_type=0&cursor=0&limit=100&' + baseParams,
                },
            ];
            for (const api of apis) {
                try {
                    const resp = await fetch(api.url + '?' + api.params, {
                        credentials: 'include',
                        headers: {'Accept': 'application/json'}
                    });
                    if (resp.ok) {
                        const text = await resp.text();
                        if (text && text.trim()) {
                            try {
                                const data = JSON.parse(text);
                                results.push({url: api.url, params: api.params, data: data});
                            } catch (e) {
                                results.push({url: api.url, error: 'JSON parse failed', body: text.substring(0, 200)});
                            }
                        } else {
                            results.push({url: api.url, error: 'Empty response', status: resp.status});
                        }
                    } else {
                        results.push({url: api.url, error: 'HTTP ' + resp.status, status: resp.status});
                    }
                } catch (e) {
                    results.push({url: api.url, error: e.message});
                }
            }
            return results.length > 0 ? results : null;
        }
    """)

    if not result:
        logger.warning("API 获取好友信息失败：所有端点均返回空")
        return

    for api_result in result:
        api_url = api_result.get("url", "")

        # 处理错误响应
        if "error" in api_result:
            logger.warning(f"API {api_url} 错误: {api_result.get('error')}")
            if "body" in api_result:
                logger.debug(f"  响应体: {api_result.get('body', '')[:200]}")
            continue

        data = api_result.get("data", {})

        # 记录响应结构帮助调试
        if isinstance(data, dict):
            logger.debug(f"API {api_url} 返回 keys: {list(data.keys())}")

        # 尝试多种可能的响应结构
        items = (
            data.get("data", []) or
            data.get("conversations", []) or
            data.get("conversation_list", []) or
            []
        )

        if not items:
            logger.debug(f"API {api_url} 未返回会话列表数据")
            continue

        logger.info(f"API {api_url} 返回 {len(items)} 条会话数据")

        for item in items:
            # 会话项结构: {conversation_id, to_uid, ext: {nickname, remark_name, ...}}
            uid = (
                item.get("to_uid", "") or
                item.get("uid", "") or
                item.get("user_id", "") or
                ""
            )
            # 从 ext 字段提取好友信息
            ext = item.get("ext", {}) or {}
            if isinstance(ext, str):
                try:
                    ext = json.loads(ext)
                except:
                    ext = {}

            nickname = norm(ext.get("nickname", "") or item.get("nickname", ""))
            remark_name = norm(ext.get("remark_name", "") or item.get("remark_name", nickname))

            short_id = ext.get("short_id", "") or item.get("short_id", "")
            unique_id = ext.get("unique_id", "") or item.get("unique_id", "")
            sec_uid = ext.get("sec_uid", "") or item.get("sec_uid", "")

            # 如果没有 uid，尝试从 conversation_id 解析
            if not uid:
                conv_id = item.get("conversation_id", "") or item.get("conversation_short_id", "")
                if conv_id and ":" in str(conv_id):
                    parts = str(conv_id).split(":")
                    if len(parts) >= 3:
                        uid = parts[2]  # "0:1:{toid}:{myid}" 中的 toid

            if remark_name and uid:
                userIDDict[remark_name] = {
                    "uid": uid,
                    "short_id": short_id,
                    "unique_id": unique_id,
                    "sec_uid": sec_uid,
                    "nickname": nickname,
                    "remark_name": remark_name,
                }

    logger.info(f"通过 API 收集到 {len(userIDDict)} 个好友信息")


def extract_friends_from_dom(page):
    """从页面 DOM 和 JS 状态提取好友信息（最后 fallback）"""
    global userIDDict
    found = 0

    # 方法1: 从 __INITIAL_STATE__ 提取会话数据
    try:
        result = page.evaluate("""
            () => {
                const conversations = [];
                if (window.__INITIAL_STATE__) {
                    const findConv = (obj, path = '', depth = 0) => {
                        if (depth > 6 || !obj || typeof obj !== 'object') return;
                        for (const key of Object.keys(obj)) {
                            const val = obj[key];
                            if (Array.isArray(val) && val.length > 0 && val.length < 200) {
                                if (key.match(/conversation|chat|session|friend/i)) {
                                    conversations.push({key: key, path: path + '.' + key, sample: JSON.stringify(val[0]).substring(0, 500)});
                                }
                            }
                            findConv(val, path + '.' + key, depth + 1);
                        }
                    };
                    findConv(window.__INITIAL_STATE__);
                }
                return conversations;
            }
        """)
        if result:
            logger.debug(f"从 __INITIAL_STATE__ 找到 {len(result)} 个可能的会话数组")
            for conv in result:
                logger.debug(f"  key={conv.get('key')}, path={conv.get('path')}")
                logger.debug(f"  sample={conv.get('sample', '')[:200]}")
    except Exception as e:
        logger.debug(f"从 __INITIAL_STATE__ 提取失败: {e}")

    # 方法2: 从 DOM 元素提取好友名称
    try:
        items = page.evaluate("""
            () => {
                const results = [];
                const selectors = [
                    '[class*="conversation"] [class*="title"]',
                    '[class*="conversation"] [class*="name"]',
                    '[class*="chat-item"] [class*="name"]',
                    '[class*="session"] [class*="title"]',
                    '[data-e2e*="chat"] [class*="name"]',
                    '[class*="im-chat"] [class*="name"]',
                    '[class*="conv"] [class*="name"]',
                ];
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        const text = (el.innerText || '').trim();
                        if (text && text.length < 50) {
                            const parent = el.closest('[class*="conversation"], [class*="chat"], [class*="session"], [data-e2e*="chat"]');
                            const dataUid = parent ? (parent.getAttribute('data-uid') || parent.getAttribute('data-id') || '') : '';
                            results.push({name: text, uid: dataUid, selector: sel});
                        }
                    }
                }
                return results;
            }
        """)
        if items:
            logger.debug(f"从 DOM 提取到 {len(items)} 个可能的好友名称")
            for item in items:
                name = norm(item.get("name", ""))
                uid = item.get("uid", "")
                if name and name not in userIDDict:
                    if uid:
                        userIDDict[name] = {
                            "uid": uid, "short_id": "", "unique_id": "",
                            "sec_uid": "", "nickname": name, "remark_name": name,
                        }
                        found += 1
                    else:
                        # 即使没有 uid，也记录名称（后续可能通过搜索找到 uid）
                        userIDDict[name] = {
                            "uid": "", "short_id": "", "unique_id": "",
                            "sec_uid": "", "nickname": name, "remark_name": name,
                        }
                        found += 1
                        logger.debug(f"  DOM 好友(无uid): {name}")
    except Exception as e:
        logger.debug(f"从 DOM 提取好友信息失败: {e}")

    if found > 0:
        logger.info(f"从 DOM/JS 状态提取到 {found} 个好友信息")
    return found


def extract_user_info_from_page(page):
    """从页面 JS 上下文提取当前用户的 uid 和 device_id"""
    global myUserInfo

    # 方法1: 尝试从页面全局变量和 localStorage 获取
    try:
        result = page.evaluate("""
            () => {
                const info = {uid: '', deviceId: ''};
                // 尝试从 __INITIAL_STATE__ 获取
                if (window.__INITIAL_STATE__) {
                    const state = window.__INITIAL_STATE__;
                    if (state.user) {
                        info.uid = state.user.uid || state.user.userId || state.user.id || '';
                    }
                    if (state.deviceId) info.deviceId = state.deviceId;
                }
                // 尝试从 localStorage 获取 uid 和 device_id
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i);
                    try {
                        const val = localStorage.getItem(key);
                        if (!val) continue;
                        // 直接匹配 device_id 键
                        if (key === 'device_id' || key === 'deviceId') {
                            info.deviceId = val;
                        }
                        // 解析 JSON 值
                        if (val.includes('"uid"') || val.includes('"device_id"')) {
                            const parsed = JSON.parse(val);
                            if (parsed.uid) info.uid = String(parsed.uid);
                            if (parsed.device_id) info.deviceId = String(parsed.device_id);
                            if (parsed.user && parsed.user.uid) info.uid = String(parsed.user.uid);
                        }
                    } catch {}
                }
                return info;
            }
        """)
        if result.get("uid") and str(result["uid"]) != "0":
            myUserInfo["uid"] = str(result["uid"])
            logger.debug(f"从页面 JS 提取到 uid: {result['uid']}")
        if result.get("deviceId") and str(result["deviceId"]) != "0":
            myUserInfo["device_id"] = str(result["deviceId"])
            logger.debug(f"从页面 JS 提取到 device_id: {result['deviceId']}")
    except Exception as e:
        logger.debug(f"从页面 JS 提取用户信息失败: {e}")

    # 方法2: 尝试通过 fetch 调用 API 获取
    if not myUserInfo.get("uid") or not myUserInfo.get("device_id"):
        try:
            result = page.evaluate("""
                async () => {
                    const urls = [
                        '/aweme/v1/web/query/user',
                        '/aweme/v1/web/im/user/me',
                    ];
                    for (const url of urls) {
                        try {
                            const resp = await fetch(url, {
                                credentials: 'include',
                                headers: {'Accept': 'application/json'}
                            });
                            if (resp.ok) {
                                const data = await resp.json();
                                return {url: url, data: data};
                            }
                        } catch (e) {}
                    }
                    return null;
                }
            """)
            if result:
                api_url = result.get("url", "")
                data = result.get("data", {})
                logger.debug(f"API {api_url} 返回数据 keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")

                # id 字段是用户 UID（不是 device_id）
                uid = (
                    data.get("user_uid", "") or
                    data.get("uid", "") or
                    data.get("user", {}).get("uid", "") or
                    data.get("user", {}).get("id", "") or
                    data.get("id", "")
                )
                if uid and str(uid) != "0":
                    myUserInfo["uid"] = str(uid)
                    logger.debug(f"从 API 获取到 uid: {uid}")

                # device_id 需要从其他字段获取
                device_id = (
                    data.get("device_id", "") or
                    data.get("user", {}).get("device_id", "")
                )
                if device_id:
                    myUserInfo["device_id"] = str(device_id)
                    logger.debug(f"从 API 获取到 device_id: {device_id}")
        except Exception as e:
            logger.debug(f"通过 API 获取用户信息失败: {e}")

    # 方法3: 如果仍然没有 device_id，从 localStorage 单独提取
    if not myUserInfo.get("device_id"):
        try:
            device_id = page.evaluate("""
                () => {
                    // 常见的 device_id 存储 key
                    const keys = ['device_id', 'deviceId', 'tt_device_id', 'fpid'];
                    for (const key of keys) {
                        const val = localStorage.getItem(key);
                        if (val) return val;
                    }
                    // 从 cookie 中提取 ttwid 作为 fallback
                    const match = document.cookie.match(/ttwid=([^;]+)/);
                    return match ? match[1] : '';
                }
            """)
            if device_id:
                myUserInfo["device_id"] = str(device_id)
                logger.debug(f"从 localStorage 提取到 device_id: {device_id}")
        except Exception as e:
            logger.debug(f"从 localStorage 提取 device_id 失败: {e}")

    # 方法4: 如果有 uid 但没有 device_id，用 uid 作为 device_id（部分场景可用）
    if myUserInfo.get("uid") and not myUserInfo.get("device_id"):
        myUserInfo["device_id"] = myUserInfo["uid"]
        logger.debug(f"使用 uid 作为 device_id fallback: {myUserInfo['uid']}")


def find_target_uid(target, cookies):
    """根据目标名称查找好友的 uid"""
    # 先在已收集的好友信息中查找
    for remark_name, info in userIDDict.items():
        # 匹配: 备注名、昵称、unique_id、short_id
        if target in [info["remark_name"], info["nickname"],
                       info["unique_id"], info["short_id"]]:
            uid = info.get("uid", "")
            if uid:
                return uid, info
            # uid 为空时用 short_id 作为 fallback
            if info.get("short_id"):
                return info["short_id"], info

    logger.warning(f"未在会话列表中找到目标 {target}，可能不是近期聊天好友")
    return None, None


def search_friend_by_name(page, target_name):
    """通过抖音主站搜索页查找好友 uid（跳过聊天页搜索，避免 IP 风控超时）"""
    global userIDDict
    logger.info(f"尝试搜索好友: {target_name}")

    # ---- 直接使用主站搜索页（页面自带 X-Bogus 签名）----
    # 跳过聊天页搜索框（在 GitHub Actions US IP 上会超时）
    try:
        from urllib.parse import quote
        search_url = f"https://www.douyin.com/search/{quote(target_name)}?type=user"
        before_count = len([v for v in userIDDict.values() if v.get("uid")])

        page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        time.sleep(2)  # 等待搜索结果渲染和 API 响应

        # handle_response 会解析搜索 API 响应并填充 userIDDict
        # 搜索页是 SSR 渲染，结果直接嵌在 __INITIAL_STATE__ 中，也提取一份
        try:
            ssr_users = page.evaluate("""
                () => {
                    const state = window.__INITIAL_STATE__;
                    if (!state) return null;
                    const users = [];
                    const seen = new Set();
                    // 深度遍历 state，收集所有带 uid+nickname 的用户对象
                    const scan = (obj, depth) => {
                        if (depth > 8 || !obj || typeof obj !== 'object') return;
                        if (Array.isArray(obj)) {
                            for (const item of obj) scan(item, depth + 1);
                            return;
                        }
                        const uid = obj.uid || obj.user_id || '';
                        const nickname = obj.nickname || '';
                        if (uid && nickname && !seen.has(String(uid))) {
                            seen.add(String(uid));
                            users.push({
                                uid: String(uid),
                                nickname: nickname,
                                sec_uid: obj.sec_uid || '',
                                short_id: obj.short_id || '',
                                unique_id: obj.unique_id || '',
                                remark_name: obj.remark_name || nickname,
                            });
                        }
                        for (const key of Object.keys(obj)) {
                            scan(obj[key], depth + 1);
                        }
                    };
                    scan(state, 0);
                    return users.slice(0, 50);
                }
            """)
            if ssr_users:
                logger.debug(f"从搜索页 __INITIAL_STATE__ 提取到 {len(ssr_users)} 个用户")
                for u in ssr_users:
                    if isinstance(u, dict) and u.get("uid"):
                        _parse_user_info_item(u)
        except Exception as e:
            logger.debug(f"从搜索页 __INITIAL_STATE__ 提取失败: {e}")

        # 检查新增的条目中是否有精确匹配目标的
        matched = None
        for remark_name, info in userIDDict.items():
            if target_name in [info["remark_name"], info["nickname"]]:
                uid = info.get("uid", "")
                if uid:
                    matched = (uid, info)
                    break

        if matched:
            logger.info(f"主站搜索找到好友 {target_name} (uid={matched[0]})")
            return matched

        new_count = len([v for v in userIDDict.values() if v.get("uid")])
        logger.debug(f"主站搜索后新增 {new_count - before_count} 个用户，但无精确匹配 {target_name}")
    except Exception as e:
        logger.debug(f"主站搜索 {target_name} 失败: {e}")

    return None, None


def search_friends_via_api(page, targets):
    """通过 API 搜索好友（需要 X-Bogus 签名，可能在页面内 fetch 不需要）"""
    global userIDDict
    found_count = 0

    for target in targets:
        if target in [info.get("remark_name") for info in userIDDict.values()]:
            continue  # 已找到

        try:
            # 使用页面内 fetch 调用搜索 API
            result = page.evaluate(f"""
                async () => {{
                    try {{
                        const resp = await fetch(
                            '/aweme/v1/web/general/search/single/?keyword=' + 
                            encodeURIComponent('{target}') + 
                            '&count=10&search_source=normal&is_full_text=1',
                            {{credentials: 'include'}}
                        );
                        if (resp.ok) return await resp.json();
                    }} catch (e) {{}}
                    return null;
                }}
            """)

            if not result or not isinstance(result, dict):
                continue

            # 解析搜索结果
            data = result.get("data", [])
            for item in data:
                user = item.get("user", {}) or item
                uid = user.get("uid", "") or user.get("id", "")
                nickname = norm(user.get("nickname", ""))
                short_id = user.get("short_id", "")
                unique_id = user.get("unique_id", "")
                sec_uid = user.get("sec_uid", "")
                remark_name = norm(user.get("remark_name", nickname))

                if target in [nickname, remark_name, unique_id, short_id] and uid:
                    userIDDict[remark_name] = {
                        "uid": uid,
                        "short_id": short_id,
                        "unique_id": unique_id,
                        "sec_uid": sec_uid,
                        "nickname": nickname,
                        "remark_name": remark_name,
                    }
                    found_count += 1
                    logger.info(f"API 搜索找到好友 {target} (uid={uid})")
                    break

            time.sleep(1)  # 搜索间隔

        except Exception as e:
            logger.debug(f"API 搜索 {target} 失败: {e}")

    return found_count


def try_send_via_ws(page, context, cookies, username, targets):
    """
    尝试通过 WebSocket 发送消息（绕过聊天页 IP 风控）
    返回 True 表示成功发送，False 表示发送失败，None 表示无法尝试（需要 fallback）
    """
    global userIDDict, myUserInfo, captured_ws_url

    message = build_message()
    logger.info(f"账号 {username} WebSocket 消息内容: {message}")

    # 1. 尝试通过 API 获取会话列表（可能因缺少 X-Bogus 签名而失败）
    fetch_friends_via_api(page)
    logger.info(f"API + handle_response 获取到 {len(userIDDict)} 个好友信息")

    # 2. 搜索未找到的好友
    remaining = [t for t in targets if not find_target_uid(t, cookies)[0]]
    if remaining:
        logger.info(f"需要搜索的好友: {remaining} (共 {len(remaining)} 个)")
        for target in remaining:
            search_friend_by_name(page, target)
            time.sleep(0.5)

    # 3. 检查目标好友的 uid
    target_uids = {}
    for target in targets:
        uid, info = find_target_uid(target, cookies)
        if uid:
            target_uids[target] = uid
        else:
            logger.warning(f"未能找到好友 {target} 的 uid")

    if not target_uids:
        logger.error("未能找到任何目标好友的 uid，WebSocket 发送无法进行")
        return None

    logger.info(f"找到 {len(target_uids)}/{len(targets)} 个目标好友的 uid: {target_uids}")

    # 4. 连接 WebSocket 并发送消息
    try:
        ws_client = DouyinWSClient(
            cookies=cookies,
            device_id=myUserInfo.get("device_id", ""),
            myid=myUserInfo["uid"],
            captured_ws_url=captured_ws_url,
        )
        ws_client.connect(timeout=15)

        sent_count = 0
        for target, uid in target_uids.items():
            try:
                success = ws_client.send_message(uid, message)
                if success:
                    sent_count += 1
                    logger.info(f"✅ WebSocket 发送成功: {target} (uid={uid})")
                else:
                    logger.warning(f"⚠️ WebSocket 发送状态异常: {target} (uid={uid})")
                time.sleep(2)
            except Exception as e:
                logger.error(f"发送给 {target} 失败: {e}")

        ws_client.close()

        if sent_count > 0:
            logger.info(f"账号 {username} WebSocket 发送完成: 成功 {sent_count}/{len(target_uids)}")
            return True
        else:
            logger.error(f"账号 {username} WebSocket 发送全部失败")
            return False
    except Exception as e:
        logger.error(f"WebSocket 连接/发送失败: {e}")
        traceback.print_exc()
        return None


def do_user_task(browser, username, cookies, targets):
    """
    发送续火花消息（WebSocket 优先，UI 作为 fallback）：
    1. 用浏览器打开抖音主页（www.douyin.com），激活 Cookie，捕获 uid/device_id/WebSocket URL
    2. 通过 API 获取会话列表，搜索未找到的好友
    3. 通过 WebSocket + Protobuf 直接发送私信（绕过聊天页 IP 风控）
    4. 若 WebSocket 失败，fallback 到 UI 方式（导航聊天页 + 键盘输入）
    5. 提取刷新后的 Cookie 保存到文件（供自动续期）
    """
    global userIDDict, myUserInfo, captured_ws_url, captured_api_urls
    userIDDict = {}
    myUserInfo = {}
    captured_ws_url = None
    captured_api_urls = []

    # ========== 阶段1: 浏览器设置 ==========
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
    )
    context.set_default_navigation_timeout(config["browserTimeout"])
    context.set_default_timeout(config["browserTimeout"])

    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = window.chrome || {runtime: {}};
        Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    """)

    page = context.new_page()
    page.on("response", handle_response)
    page.on("websocket", handle_websocket)

    # 注入 Cookie
    context.add_cookies(cookies)

    cookie_names = sorted(c.get("name", "") for c in cookies)
    logger.info(f"已加载 {len(cookies)} 个 Cookie: {', '.join(cookie_names)}")
    critical_cookies = ["sessionid", "sessionid_ss"]
    missing_critical = [c for c in critical_cookies if c not in cookie_names]
    if missing_critical:
        logger.error(
            f"❌ 缺少关键登录 Cookie: {missing_critical}！"
            f"请重新导出完整 Cookie 并更新 GitHub Secret"
        )

    # ========== 阶段2: 打开抖音主页（激活 Cookie + 捕获认证信息）==========
    retry_operation(
        "打开抖音主页",
        page.goto,
        retries=config["taskRetryTimes"],
        delay=5,
        url="https://www.douyin.com/",
        wait_until="domcontentloaded",
    )
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    time.sleep(3)  # 等待 API 响应和 WebSocket 连接被捕获

    # 从页面提取当前用户信息（uid, device_id）
    # 仅在 handle_response 未捕获到有效 uid 时才从页面提取
    if not myUserInfo.get("uid") or str(myUserInfo.get("uid")) == "0":
        extract_user_info_from_page(page)
    logger.info(f"捕获到当前用户: uid={myUserInfo.get('uid')}, device_id={myUserInfo.get('device_id')}")
    if captured_ws_url:
        logger.info(f"捕获到页面 WebSocket: {captured_ws_url[:120]}")
    logger.info(f"API 响应捕获: {len(captured_api_urls)} 个 API 调用")

    # 检查登录状态（主页能捕获 uid 即说明 Cookie 有效）
    if not myUserInfo.get("uid"):
        logger.error(f"❌ 账号 {username} 未能捕获用户 uid，Cookie 可能已失效")
        try:
            os.makedirs("logs", exist_ok=True)
            page.screenshot(path="logs/cookie_expired.png", full_page=False, timeout=10000)
        except Exception:
            pass
        context.close()
        return False

    if not myUserInfo.get("device_id"):
        myUserInfo["device_id"] = myUserInfo["uid"]
        logger.debug(f"使用 uid 作为 device_id: {myUserInfo['uid']}")

    # ========== Cookie 自动续期：提取刷新后的 Cookie ==========
    try:
        refreshed_cookies = context.cookies()
        if refreshed_cookies and len(refreshed_cookies) >= len(cookies):
            os.makedirs("logs", exist_ok=True)
            with open("logs/refreshed_cookies.json", "w", encoding="utf-8") as f:
                json.dump(refreshed_cookies, f, ensure_ascii=False)
            logger.info(f"已保存 {len(refreshed_cookies)} 个刷新后的 Cookie 到 logs/refreshed_cookies.json")
    except Exception as e:
        logger.warning(f"提取刷新后的 Cookie 失败: {e}")

    # ========== 阶段3: WebSocket 方式发送消息（绕过聊天页 IP 风控）==========
    ws_result = try_send_via_ws(page, context, cookies, username, targets)
    if ws_result is not None:
        context.close()
        return ws_result

    # ========== 阶段4: UI 方式发送消息（fallback）==========
    logger.info("WebSocket 方式未能发送消息，尝试 UI 方式（聊天页）...")
    retry_operation(
        "打开抖音聊天页面",
        page.goto,
        retries=config["taskRetryTimes"],
        delay=5,
        url="https://www.douyin.com/chat",
        wait_until="domcontentloaded",
    )
    logger.info(f"账号 {username} 聊天页面已打开，等待加载...")
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        logger.warning("等待 networkidle 超时，继续执行")
    time.sleep(10)

    # 检测登录弹窗
    is_logged_in = True
    try:
        current_url = page.url
        body_text = page.evaluate("() => document.body ? document.body.innerText.substring(0, 1000) : ''")
        if "passport" in current_url or "/login" in current_url.lower():
            is_logged_in = False
        elif "扫码登录" in body_text or "验证码登录" in body_text:
            page.keyboard.press("Escape")
            time.sleep(1)
            page.evaluate("""() => {
                const overlays = document.querySelectorAll('[class*="modal"], [class*="overlay"], [class*="mask"], [class*="dialog"]');
                overlays.forEach(el => { if (el.style) el.style.display = 'none'; });
            }""")
            time.sleep(2)
            body_text2 = page.evaluate("() => document.body ? document.body.innerText.substring(0, 500) : ''")
            if "扫码登录" in body_text2 or "验证码登录" in body_text2:
                conv_count = page.locator(CONVERSATION_ITEM_SELECTOR).count()
                if conv_count == 0:
                    is_logged_in = False
    except Exception:
        pass

    if not is_logged_in:
        logger.error(f"❌ 账号 {username} 聊天页 IP 风控，WebSocket 和 UI 均失败")
        try:
            os.makedirs("logs", exist_ok=True)
            page.screenshot(path="logs/chat_ip_blocked.png", full_page=False, timeout=10000)
        except Exception:
            pass
        context.close()
        return False

    # ========== 截图调试 ==========
    try:
        import os
        os.makedirs("logs", exist_ok=True)
        page.screenshot(path="logs/chat_page_debug.png", full_page=False, timeout=10000)
    except Exception:
        pass

    # ========== 等待会话列表加载 ==========
    try:
        page.wait_for_selector(CONVERSATION_ITEM_SELECTOR, timeout=30000)
        item_count = page.locator(CONVERSATION_ITEM_SELECTOR).count()
        logger.info(f"会话列表已加载，当前可见 {item_count} 个会话")
    except Exception:
        logger.warning("会话列表条目未出现，可能页面未完全加载或 Cookie 失效")

    # ========== 阶段4: 查找并点击好友，发送消息 ==========
    message = build_message()
    logger.info(f"账号 {username} 消息内容: {message}")

    sent_targets = []
    failed_targets = []

    try:
        for target_name in scroll_and_select_user(page, username, targets):
            # 等待聊天输入框出现
            chat_input = None
            for sel in [
                '[contenteditable="true"]',
                '.chat-input-dccKiL',
                CHAT_EDITOR_SELECTOR,
                '[class*="chat-input"]',
                '[class*="editor"] [contenteditable]',
            ]:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0:
                        chat_input = loc.first
                        chat_input.click()
                        time.sleep(1)
                        logger.debug(f"找到聊天输入框: {sel}")
                        break
                except Exception:
                    continue

            if chat_input is None:
                logger.warning(f"账号 {username} 未找到聊天输入框，跳过 {target_name}")
                try:
                    page.screenshot(path=f"logs/no_input_{target_name}.png", full_page=False, timeout=10000)
                except Exception:
                    pass
                continue

            # 用 keyboard 级别输入（对 emoji 更可靠）
            lines = message.replace("\r\n", "\n").replace("\\n", "\n").split("\n")
            for i, line in enumerate(lines):
                if line:
                    page.keyboard.type(line, delay=50)
                if i < len(lines) - 1:
                    page.keyboard.press("Shift+Enter")
                    time.sleep(0.3)

            time.sleep(0.5)

            # 发送消息
            page.keyboard.press("Enter")
            time.sleep(2)

            # 验证消息是否出现在聊天区域
            verified = False
            try:
                page.wait_for_timeout(500)
                chat_area_text = page.evaluate("""
                    () => {
                        const msgs = document.querySelectorAll('[class*="message-content"], [class*="msg-content"], [class*="bubble"]');
                        return Array.from(msgs).slice(-5).map(m => m.innerText || m.textContent || '').join(' | ');
                    }
                """)
                if message.strip() in chat_area_text or any(message.strip() in m for m in chat_area_text.split(' | ')):
                    verified = True
                    logger.info(f"✅ 账号 {username} 已发送并验证消息给 {target_name}")
                else:
                    logger.debug(f"聊天区域文本（末5条）: {chat_area_text[:300]}")
            except Exception:
                pass

            if not verified:
                logger.info(f"✅ 账号 {username} 已发送消息给 {target_name}（未验证）")

            sent_targets.append(target_name)
    except Exception as e:
        logger.error(f"❌ 账号 {username} 发送阶段出错: {e}")
        traceback.print_exc()
        try:
            page.screenshot(path="logs/send_error.png", full_page=False, timeout=10000)
        except Exception:
            pass

    # 发送结果统计
    sent_normalized = {norm(s) for s in sent_targets}
    missing = [t for t in targets if norm(t) not in sent_normalized]
    if missing:
        logger.warning(f"账号 {username} 未找到 {len(missing)} 个目标的会话: {missing}")
    logger.info(f"账号 {username} 发送完成: 成功 {len(sent_targets)}/{len(targets)}")

    # 保存最终截图
    try:
        import os
        os.makedirs("logs", exist_ok=True)
        page.screenshot(path="logs/chat_after_send.png", full_page=False, timeout=10000)
    except Exception:
        pass

    context.close()
    logger.info(f"账号 {username} 任务完成")
    return True


# ========== UI 会话选择辅助 ==========

def checkTargetName(targetName, targets):
    """检查会话标题是否为目标（优先名称匹配，其次 uid/short_id/unique_id）"""
    targetSymbol = None
    targetName = norm(targetName)

    # 名称直接匹配（targets 也做规范化，返回原始目标便于后续统计）
    norm_targets = {norm(t): t for t in targets}
    if targetName in norm_targets:
        return norm_targets[targetName]

    # 通过已收集的用户信息匹配 uid/short_id/unique_id（targets 为 ID 时走这里）
    if targetName in userIDDict:
        info = userIDDict[targetName]
        for v in [info.get("uid"), info.get("short_id"), info.get("unique_id")]:
            if v and v in targets:
                targetSymbol = v
                break
    return targetSymbol


def scroll_and_select_user(page, username, targets):
    """滚动会话列表，查找并点击目标会话（生成器：每命中一个目标 yield 一次）"""
    target_selector = CONVERSATION_ITEM_SELECTOR
    scrollable_friends_selector = CONVERSATION_LIST_SELECTOR

    found_targets = set()
    remaining_targets = set(targets)
    empty_scroll_count = 0
    MAX_EMPTY_SCROLLS = 10

    while True:
        target_elements = page.locator(target_selector).all()
        prev_found_count = len(found_targets)

        for element in target_elements:
            try:
                span = element.locator(CONVERSATION_TITLE_SELECTOR)
                targetName = span.inner_text()

                if targetName in found_targets:
                    continue
                found_targets.add(targetName)

                targetSymbol = checkTargetName(targetName, targets)
                if targetSymbol:
                    element.click()
                    time.sleep(2)
                    yield targetSymbol
                    if targetSymbol in remaining_targets:
                        remaining_targets.remove(targetSymbol)
                    if len(remaining_targets) == 0:
                        return
                    break
            except Exception as e:
                traceback.print_exc()
        else:
            new_found = len(found_targets) > prev_found_count
            if new_found:
                empty_scroll_count = 0
            else:
                empty_scroll_count += 1

            if empty_scroll_count >= MAX_EMPTY_SCROLLS:
                if len(remaining_targets) > 0:
                    logger.warning(f"账号 {username} 未找到好友: {remaining_targets}")
                break

            scrollable_element = page.locator(scrollable_friends_selector).element_handle()
            if scrollable_element:
                scroll_top_before = page.evaluate("(element) => element.scrollTop", scrollable_element)
                page.evaluate("(element) => element.scrollTop += 800", scrollable_element)
                time.sleep(0.3)
                scroll_top_after = page.evaluate("(element) => element.scrollTop", scrollable_element)

                if scroll_top_before == scroll_top_after:
                    empty_scroll_count += 2
                time.sleep(1.5)
            else:
                break


def runTasks():
    playwright, browser = get_browser()
    any_success = False
    try:
        logger.info("开始执行任务（WebSocket 优先，UI fallback）")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")
        for user in userData:
            logger.debug(f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}")

        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
            result = do_user_task(browser, username, cookies, targets)
            if result:
                any_success = True
            logger.info(f"账号 {username} 任务完成")
    finally:
        browser.close()
        playwright.stop()

    if not any_success:
        logger.error("❌ 所有账号均未成功发送消息（Cookie 可能已过期），退出码 1")
        import sys
        sys.exit(1)
