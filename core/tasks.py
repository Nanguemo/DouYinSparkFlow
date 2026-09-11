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

CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper"
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper"
CHAT_EDITOR_SELECTOR = ".messageEditorimChatEditorContainer"


def handle_response(response: Response):
    """
    监听接口响应，收集好友完整信息和当前用户信息
    """
    global userIDDict, myUserInfo

    url = response.url

    # 好友信息接口
    if "aweme/v1/web/im/user/info" in url:
        try:
            json_data = response.json()
            for item in json_data.get("data", []):
                uid = item.get("uid", "")  # 用户 UID（数字 ID，用于 WebSocket）
                short_id = item.get("short_id", "")  # short_id
                unique_id = item.get("unique_id", "")  # unique_id
                sec_uid = item.get("sec_uid", "")  # sec_uid
                nickname = norm(item.get("nickname"))  # 昵称
                remark_name = norm(item.get("remark_name", nickname))  # 备注名
                userIDDict[remark_name] = {
                    "uid": uid,
                    "short_id": short_id,
                    "unique_id": unique_id,
                    "sec_uid": sec_uid,
                    "nickname": nickname,
                    "remark_name": remark_name,
                }
        except Exception as e:
            logger.warning(f"解析好友信息响应失败: {e}")

    # 当前用户信息接口（多个可能的端点）
    elif "aweme/v1/web/query/user" in url or "aweme/v1/web/im/user/me" in url:
        try:
            json_data = response.json()
            # 提取 uid
            uid = json_data.get("uid", "") or json_data.get("user", {}).get("uid", "")
            if uid:
                myUserInfo["uid"] = str(uid)
                logger.debug(f"捕获到当前用户 uid: {uid}")
            # 提取 device_id
            device_id = json_data.get("id", "") or json_data.get("device_id", "")
            if device_id:
                myUserInfo["device_id"] = str(device_id)
                logger.debug(f"捕获到 device_id: {device_id}")
        except Exception as e:
            logger.warning(f"解析当前用户信息响应失败: {e}")


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

    scrollable_friends_selector = CONVERSATION_LIST_SELECTOR
    empty_scroll_count = 0
    MAX_EMPTY_SCROLLS = 10

    for _ in range(max_scrolls):
        scrollable_element = page.locator(scrollable_friends_selector).element_handle()
        if not scrollable_element:
            logger.warning(f"账号 {username} 未找到滚动容器")
            break

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


def extract_user_info_from_page(page):
    """从页面 JS 上下文提取当前用户的 uid 和 device_id"""
    global myUserInfo

    # 方法1: 尝试从页面全局变量获取
    try:
        result = page.evaluate("""
            () => {
                const info = {uid: '', deviceId: ''};
                // 尝试从 __INITIAL_STATE__ 获取
                if (window.__INITIAL_STATE__) {
                    const state = window.__INITIAL_STATE__;
                    if (state.user) {
                        info.uid = state.user.uid || state.user.userId || '';
                    }
                    if (state.deviceId) info.deviceId = state.deviceId;
                }
                // 尝试从 localStorage 获取
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i);
                    try {
                        const val = localStorage.getItem(key);
                        if (val && val.includes('"uid"')) {
                            const parsed = JSON.parse(val);
                            if (parsed.uid) info.uid = String(parsed.uid);
                            if (parsed.device_id) info.deviceId = parsed.device_id;
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
                    try {
                        const resp = await fetch('/aweme/v1/web/query/user', {
                            credentials: 'include',
                            headers: {'Accept': 'application/json'}
                        });
                        if (resp.ok) {
                            return await resp.json();
                        }
                    } catch (e) {}
                    return null;
                }
            """)
            if result:
                uid = result.get("uid", "")
                device_id = result.get("id", "")
                if uid:
                    myUserInfo["uid"] = str(uid)
                if device_id:
                    myUserInfo["device_id"] = str(device_id)
                logger.debug(f"从 API 获取到 uid={uid}, device_id={device_id}")
        except Exception as e:
            logger.debug(f"通过 API 获取用户信息失败: {e}")


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
    context = browser.new_context()
    context.set_default_navigation_timeout(config["browserTimeout"])
    context.set_default_timeout(config["browserTimeout"])

    page = context.new_page()
    page.on("response", handle_response)

    # 注入 Cookie
    context.add_cookies(cookies)

    # 打开聊天页面
    retry_operation(
        "打开抖音网页聊天页面",
        page.goto,
        retries=config["taskRetryTimes"],
        delay=5,
        url="https://www.douyin.com/chat",
    )

    logger.info(f"账号 {username} 聊天页面已打开，等待加载...")
    time.sleep(5)

    # 滚动会话列表收集好友信息
    scroll_conversation_list(page, username)
    logger.info(f"账号 {username} 共收集到 {len(userIDDict)} 个好友信息")

    # 提取当前用户信息
    extract_user_info_from_page(page)

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
