import os, sys
from enum import Enum
import json
import logging
from utils.logger import setup_logger
from utils import norm

logger = setup_logger(level=logging.DEBUG)

"""
是否启用调试模式
更详细的日志打印，浏览器操作可视化等
"""
DEBUG = True
config = None
userData = None


def load_target_uids_map():
    """
    加载仓库内硬编码的好友 uid 映射文件（utils/target_uids.json）。
    结构: {"用户名": {好友名: "数字uid", ...}}
    由于从 US IP 访问会话列表 API 会被风控（404 Janus），WebSocket
    发送依赖该映射获取目标的数字 uid，无需再拉取会话列表。
    """
    path = os.path.join(os.path.dirname(__file__), "target_uids.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        logger.warning("加载 target_uids.json 失败", exc_info=True)
    return {}


class Environment(Enum):
    GITHUBACTION = "GITHUB_ACTION"  # GitHub Action 运行
    LOCAL = "LOCAL"  # 本地代码运行
    PACKED = "PACKED"  # PyInstaller 打包运行

    def __str__(self):
        return self.value


def get_environment():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Environment.PACKED
    elif os.getenv("GITHUB_ACTIONS") == "true":
        return Environment.GITHUBACTION
    else:
        return Environment.LOCAL


def get_config():
    """
    获取配置信息
    :return: 配置字典
    """
    global config

    if config:
        return config

    config = {
        "proxyAddress": os.getenv("PROXY_ADDRESS", ""),
        "messageTemplate": os.getenv(
            "MESSAGE_TEMPLATE",
            "[盖瑞]今日火花[加一]\\n—— [右边] 每日一言 [左边] ——\\n[API]",
        ),
        "hitokotoTypes": json.loads(
            os.getenv("HITOKOTO_TYPES", '["文学","影视","诗词","哲学"]')
        ),
        "browserTimeout": int(
            os.getenv("BROWSER_TIMEOUT", "120000")
        ),  # 浏览器操作超时时间，单位毫秒
        "friendListTimeout": int(
            os.getenv("FRIEND_LIST_WAIT_TIME", "2000")
        ),  # 好友列表加载超时时间，单位毫秒
        "taskRetryTimes": int(os.getenv("TASK_RETRY_TIMES", "3")),  # 任务重试次数
        "logLevel": os.getenv("LOG_LEVEL", "DEBUG"),  # 日志级别
    }

    return config


def sanitize_cookies(cookies):
    for cookie in cookies:
        if "sameSite" in cookie:
            cookie.pop("sameSite")  # 移除 sameSite 字段，Playwright 可能不支持该字段
    return cookies


def get_userData():
    """
    获取用户数据目录
    :return: 用户数据目录路径
    """
    global userData

    if userData:
        return userData

    tasks = json.loads(os.getenv("TASKS", "[]"))

    userData = []

    file_uids = load_target_uids_map()  # 仓库内硬编码 uid 映射（用户名 -> 好友名 -> uid）

    for task in tasks:
        username = task.get("username", "未知用户")
        unique_id = task.get("unique_id")
        if not unique_id:
            logger.warning(f"{username} 的任务  缺少 unique_id 字段，已跳过")
            continue
        cookies_key = f"cookies_{unique_id}".upper()
        cookies_str = (
            os.getenv(cookies_key, "").encode("utf-8").decode("unicode_escape")
        )
        if not cookies_str:
            logger.warning(f"{username} 的任务 缺少 {cookies_key} 环境变量，已跳过")
            continue
        try:
            cookies = json.loads(cookies_str)
        except json.JSONDecodeError:
            logger.warning(f"{username} 的任务 {cookies_key} 格式不正确，已跳过")
            continue

        # 合并 uid：TASKS 内 task.target_uids 优先，其次仓库 utils/target_uids.json 里的映射
        merged_uids = dict(file_uids.get(username, {}) or {})
        merged_uids.update(task.get("target_uids", {}) or {})

        userData.append(
            {
                "unique_id": unique_id,
                "username": username,
                "cookies": sanitize_cookies(cookies),
                "targets": [norm(t) for t in task.get("targets", [])], # 标准化目标列表
                # 硬编码的好友名 -> 数字 uid 映射（绕过从 US IP 被风控的会话列表 API）。
                "target_uids": merged_uids,
            }
        )

    return userData
