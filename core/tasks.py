import traceback
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from utils import norm
from core.msg_builder import build_message, build_message_with_openai
from core.browser import get_browser
from playwright.sync_api import Response
import time
import json

config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))

# 好友信息字典: remark_name -> {uid, short_id, unique_id, sec_uid, nickname, remark_name}
userIDDict = {}
# 当前用户信息: {uid, device_id}
myUserInfo = {}
# 捕获的 API URL 列表（用于调试）
captured_api_urls = []

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
                json_data.get("uid", "") or
                json_data.get("user_uid", "") or
                json_data.get("user", {}).get("uid", "") or
                json_data.get("user", {}).get("id", "") or
                json_data.get("id", "")
            )
            if uid:
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
                    url: '/aweme/v1/web/im/conversation/list/',
                    params: 'inbox_type=0&cursor=0&limit=50&' + baseParams,
                },
                {
                    url: '/aweme/v1/web/im/conversation/list/',
                    params: 'inbox_type=1&cursor=0&limit=50&' + baseParams,
                },
                {
                    url: '/aweme/v1/web/im/conversation/list/',
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
        if result.get("uid"):
            myUserInfo["uid"] = str(result["uid"])
            logger.debug(f"从页面 JS 提取到 uid: {result['uid']}")
        if result.get("deviceId"):
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
                    data.get("uid", "") or
                    data.get("user", {}).get("uid", "") or
                    data.get("id", "") or
                    data.get("user", {}).get("id", "")
                )
                if uid:
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
    """通过抖音搜索查找好友 uid：先试聊天页搜索框，失败则用主站搜索页"""
    global userIDDict
    logger.info(f"尝试搜索好友: {target_name}")

    # ---- 方式1: 聊天页面搜索框 ----
    try:
        search_selectors = [
            'input[placeholder*="搜索"]',
            'input[placeholder*="search"]',
            'input[placeholder*="查找"]',
            '[class*="search"] input',
        ]

        search_input = None
        for selector in search_selectors:
            try:
                search_input = page.query_selector(selector)
                if search_input:
                    logger.debug(f"找到搜索输入框: {selector}")
                    break
            except Exception:
                continue

        if search_input:
            search_input.click()
            search_input.fill("")
            search_input.type(target_name)
            time.sleep(3)
            time.sleep(2)  # 等待搜索 API 响应被 handle_response 捕获

            for remark_name, info in userIDDict.items():
                if target_name in [info["remark_name"], info["nickname"]]:
                    uid = info.get("uid", "")
                    if uid:
                        logger.info(f"聊天页搜索找到好友 {target_name} (uid={uid})")
                        return uid, info

            # 清空搜索框
            try:
                search_input.fill("")
                time.sleep(1)
            except Exception:
                pass
        else:
            logger.debug("聊天页未找到搜索输入框")
    except Exception as e:
        logger.debug(f"聊天页搜索 {target_name} 失败: {e}")

    # ---- 方式2: 抖音主站搜索页（页面自带 X-Bogus 签名）----
    try:
        from urllib.parse import quote
        search_url = f"https://www.douyin.com/search/{quote(target_name)}?type=user"
        before_count = len([v for v in userIDDict.values() if v.get("uid")])

        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(5)  # 等待搜索结果渲染和 API 响应

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

        # 回到聊天页，保持后续流程的页面状态
        try:
            page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
        except Exception:
            pass
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


def do_user_task_ws(browser, username, cookies, targets):
    """
    WebSocket 方式发送消息：
    1. 用浏览器打开聊天页面（仅用于认证和收集信息）
    2. 滚动列表触发 API 调用，收集好友 uid
    3. 提取当前用户 uid 和 device_id
    4. 关闭浏览器
    5. 通过 WebSocket + Protobuf 发送消息
    """
    global userIDDict, myUserInfo
    # 每个用户重置状态
    userIDDict = {}
    myUserInfo = {}

    # ========== 阶段1: 浏览器提取认证信息 ==========
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

    # 反无头检测：移除 navigator.webdriver 标记
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = window.chrome || {runtime: {}};
        Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    """)

    page = context.new_page()
    page.on("response", handle_response)

    # 注入 Cookie
    context.add_cookies(cookies)

    # Cookie 诊断：检查关键登录 Cookie 是否存在
    cookie_names = sorted(c.get("name", "") for c in cookies)
    logger.info(f"已加载 {len(cookies)} 个 Cookie: {', '.join(cookie_names)}")
    critical_cookies = ["sessionid", "sessionid_ss"]
    missing_critical = [c for c in critical_cookies if c not in cookie_names]
    if missing_critical:
        logger.error(
            f"❌ 缺少关键登录 Cookie: {missing_critical}！"
            f"聊天功能需要 sessionid，请在已登录抖音的浏览器中重新导出完整 Cookie 并更新 GitHub Secret"
        )

    # 先打开抖音主页（激活 Cookie 并触发风控初始化），再进聊天页
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
    time.sleep(5)

    # 打开聊天页面（用 domcontentloaded 避免 SPA load 超时）
    retry_operation(
        "打开抖音网页聊天页面",
        page.goto,
        retries=config["taskRetryTimes"],
        delay=5,
        url="https://www.douyin.com/chat",
        wait_until="domcontentloaded",
    )

    logger.info(f"账号 {username} 聊天页面已打开，等待加载...")
    # 等待网络空闲和 JS 渲染
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        logger.warning("等待 networkidle 超时，继续执行")
    time.sleep(15)  # 给 JS 足够时间渲染会话列表

    # ========== 页面调试信息 ==========
    global captured_api_urls
    captured_api_urls = []
    try:
        logger.info(f"页面 URL: {page.url}")
        logger.info(f"页面标题: {page.title()}")
    except Exception:
        pass
    # 检测登录状态（URL 跳转到 passport 说明 Cookie 失效）
    try:
        current_url = page.url
        if "passport" in current_url or "login" in current_url:
            logger.error(f"页面跳转到登录页: {current_url}，Cookie 可能已失效！")
        login_state = page.evaluate("""
            () => {
                const state = window.__INITIAL_STATE__ || {};
                const user = state.user || {};
                return {
                    hasInitialState: !!window.__INITIAL_STATE__,
                    isLoggedIn: !!(user.uid || user.userId || user.id),
                    uid: String(user.uid || user.userId || user.id || ''),
                };
            }
        """)
        logger.info(f"页面登录状态: {login_state}")
    except Exception as e:
        logger.debug(f"检测登录状态失败: {e}")
    try:
        import os
        os.makedirs("logs", exist_ok=True)
        page.screenshot(path="logs/chat_page_debug.png", full_page=False)
        logger.info("已保存页面截图到 logs/chat_page_debug.png")
    except Exception as e:
        logger.debug(f"截图失败: {e}")
    try:
        body_text = page.evaluate("() => document.body ? document.body.innerText.substring(0, 500) : ''")
        logger.debug(f"页面文本预览: {body_text[:200]}")
        # 检测登录弹窗
        if "扫码登录" in body_text or "验证码登录" in body_text:
            logger.error(
                "❌ 聊天页面显示登录弹窗，Cookie 已失效或不完整！"
                "请重新导出完整 Cookie（必须包含 sessionid）并更新 GitHub Secret"
            )
    except Exception:
        pass

    # 滚动会话列表收集好友信息
    scroll_conversation_list(page, username)

    # 记录捕获到的 API 调用
    if captured_api_urls:
        logger.info(f"页面共调用了 {len(captured_api_urls)} 个 API")
        for api_url in captured_api_urls[:20]:
            logger.debug(f"  API: {api_url[:150]}")
    else:
        logger.warning("页面未调用任何 aweme/im API，可能页面未完全加载或 Cookie 已过期")
    captured_api_urls = []
    logger.info(f"账号 {username} 共收集到 {len(userIDDict)} 个好友信息")

    # 提取当前用户信息
    extract_user_info_from_page(page)

    # 如果没有收集到好友信息，尝试通过 API 获取
    if not userIDDict:
        logger.warning(f"账号 {username} 未通过滚动收集到好友信息，尝试通过 API 获取")
        try:
            fetch_friends_via_api(page)
        except Exception as e:
            logger.warning(f"通过 API 获取好友信息失败: {e}")

    # 如果 API 也失败，尝试从 DOM/JS 状态提取
    if not userIDDict:
        logger.warning(f"账号 {username} API 获取也失败，尝试从 DOM/JS 状态提取")
        try:
            extract_friends_from_dom(page)
        except Exception as e:
            logger.warning(f"从 DOM 提取好友信息失败: {e}")

    # 检查缺失的好友，尝试搜索（只有拥有 uid 的才算真正找到）
    found_names = {info.get("remark_name") for info in userIDDict.values() if info.get("uid")}
    missing_targets = [t for t in targets if t not in found_names]
    if missing_targets:
        logger.info(f"账号 {username} 有 {len(missing_targets)} 个好友未在会话列表中，尝试搜索")
        # 先尝试 API 搜索
        try:
            found = search_friends_via_api(page, missing_targets)
            if found > 0:
                logger.info(f"通过 API 搜索找到 {found} 个好友")
                found_names = {info.get("remark_name") for info in userIDDict.values() if info.get("uid")}
                missing_targets = [t for t in missing_targets if t not in found_names]
        except Exception as e:
            logger.debug(f"API 搜索失败: {e}")

        # 再尝试页面搜索框
        if missing_targets:
            for target in missing_targets[:5]:  # 限制搜索数量避免超时
                search_friend_by_name(page, target)
                time.sleep(1)

    logger.info(f"账号 {username} 最终收集到 {len(userIDDict)} 个好友信息")

    # 关闭浏览器（不再需要 UI）
    context.close()
    logger.info(f"账号 {username} 浏览器阶段完成，已关闭")

    # ========== 阶段2: WebSocket 发送消息 ==========
    uid = myUserInfo.get("uid")
    device_id = myUserInfo.get("device_id")

    if not uid:
        logger.error(f"账号 {username} 未能获取当前用户 uid，无法发送消息")
        return

    if not device_id:
        logger.error(f"账号 {username} 未能获取 device_id，无法建立 WebSocket 连接")
        return

    logger.info(f"账号 {username} 当前用户 uid={uid}, device_id={device_id}")

    # 导入 WebSocket 客户端
    from core.ws_client import DouyinWSClient

    # 创建 WebSocket 客户端
    ws_client = DouyinWSClient(cookies, device_id, uid)

    try:
        # 建立 WebSocket 连接
        ws_client.connect(timeout=15)
        time.sleep(2)  # 等待连接稳定

        # 向每个目标好友发送消息
        message = build_message()
        logger.debug(f"消息内容: {message}")

        for target in targets:
            toid, friend_info = find_target_uid(target, cookies)

            if not toid:
                logger.warning(f"账号 {username} 未找到好友 {target} 的 uid，跳过")
                continue

            logger.info(f"账号 {username} 开始发送消息给 {target} (uid={toid})")

            try:
                success = ws_client.send_message(toid, message, timeout=15)
                if success:
                    logger.info(f"✅ 账号 {username} 已发送消息给 {target}")
                else:
                    logger.warning(f"⚠️ 账号 {username} 发送消息给 {target} 可能失败")
            except Exception as e:
                logger.error(f"账号 {username} 发送消息给 {target} 失败: {e}")
                traceback.print_exc()

            time.sleep(2)  # 消息间隔

    finally:
        ws_client.close()
        logger.info(f"账号 {username} WebSocket 连接已关闭")


# ========== 以下为旧的 UI 自动化代码（保留作为 fallback）==========

def checkTargetName(targetName, targets):
    """检查targetName是否为目标"""
    targetSymbol = None
    targetName = norm(targetName)

    if targetName in userIDDict:
        info = userIDDict[targetName]
        for v in [info.get("uid"), info.get("short_id"), info.get("unique_id")]:
            if v and v in targets:
                targetSymbol = v
                break
    else:
        if targetName in targets:
            targetSymbol = targetName
    return targetSymbol


def scroll_and_select_user(page, username, targets):
    """尝试滚动并查找用户名（UI 方式，已弃用）"""
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


def do_user_task(browser, username, cookies, targets):
    """UI 自动化方式发送消息（旧方案，保留作为 fallback）"""
    context = browser.new_context()
    context.set_default_navigation_timeout(config["browserTimeout"])
    context.set_default_timeout(config["browserTimeout"])

    page = context.new_page()
    page.on("response", handle_response)
    context.add_cookies(cookies)

    retry_operation(
        "打开抖音网页聊天页面",
        page.goto,
        retries=config["taskRetryTimes"],
        delay=5,
        url="https://www.douyin.com/chat",
    )

    time.sleep(5)

    for username in scroll_and_select_user(page, username, targets):
        chat_input_selector = CHAT_EDITOR_SELECTOR
        page.wait_for_selector(chat_input_selector, timeout=config["browserTimeout"])
        chat_input = page.locator(chat_input_selector)

        message = build_message()
        for line in message.split("\\n"):
            chat_input.type(line)
            if line != message.split("\\n")[-1]:
                chat_input.press("Shift+Enter")

        chat_input.press("Enter")
        time.sleep(2)

    context.close()


def runTasks():
    playwright, browser = get_browser()
    try:
        logger.info("开始执行任务（WebSocket 模式）")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")
        for user in userData:
            logger.debug(f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}")

        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
            do_user_task_ws(browser, username, cookies, targets)
            logger.info(f"账号 {username} 任务完成")
    finally:
        browser.close()
        playwright.stop()
