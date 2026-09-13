"""
core/ws_client.py
抖音 WebSocket + Protobuf 消息发送客户端
绕过 UI 自动化，直接通过 WebSocket 协议发送私信
"""

import hashlib
import json
import random
import time
import uuid
import struct
import logging
import websocket

logger = logging.getLogger(__name__)

# ============================================================
# Protobuf 手动编码器（无需 protoc 编译 .proto 文件）
# ============================================================

class ProtoEncoder:
    """手动构造 protobuf 二进制消息"""

    @staticmethod
    def _varint(value: int) -> bytes:
        """编码无符号 varint"""
        if value < 0:
            # 负数转为无符号 64 位
            value = value & 0xFFFFFFFFFFFFFFFF
        result = b""
        if value == 0:
            return b"\x00"
        while value > 0:
            byte = value & 0x7F
            value >>= 7
            if value > 0:
                byte |= 0x80
            result += bytes([byte])
        return result

    @staticmethod
    def _tag(field_number: int, wire_type: int) -> bytes:
        return ProtoEncoder._varint((field_number << 3) | wire_type)

    @staticmethod
    def string(field_number: int, value: str) -> bytes:
        """编码 string 字段 (wire type 2)，proto3 默认值不编码"""
        if not value:
            return b""
        data = value.encode("utf-8")
        return ProtoEncoder._tag(field_number, 2) + ProtoEncoder._varint(len(data)) + data

    @staticmethod
    def bytes_field(field_number: int, data: bytes) -> bytes:
        """编码 bytes 字段 (wire type 2)"""
        if not data:
            return b""
        return ProtoEncoder._tag(field_number, 2) + ProtoEncoder._varint(len(data)) + data

    @staticmethod
    def varint_field(field_number: int, value: int) -> bytes:
        """编码 varint 字段 (wire type 0)，proto3 默认值(0)不编码"""
        if value == 0:
            return b""
        return ProtoEncoder._tag(field_number, 0) + ProtoEncoder._varint(value)

    @staticmethod
    def message(field_number: int, data: bytes) -> bytes:
        """编码嵌套 message 字段 (wire type 2)"""
        if not data:
            return b""
        return ProtoEncoder._tag(field_number, 2) + ProtoEncoder._varint(len(data)) + data

    @staticmethod
    def repeated_message(field_number: int, items: list) -> bytes:
        """编码 repeated message 字段，每个元素单独编码"""
        result = b""
        for item in items:
            result += ProtoEncoder.message(field_number, item)
        return result


def build_ext_value(key: str, value: str) -> bytes:
    """构造 ExtValue message {
        string key = 1;
        string value = 2;
    }"""
    return (
        ProtoEncoder.string(1, key) +
        ProtoEncoder.string(2, value)
    )


def build_send_message_body(conversation_id: str, content: str,
                            client_message_id: str,
                            conversation_short_id: int = 0) -> bytes:
    """构造 SendMessageRequestBody message (field 100 of RequestBody)"""
    # ExtValue 列表 (repeated, field 5)
    ext_items = [
        build_ext_value("s:mentioned_users", ""),
        build_ext_value("s:client_message_id", client_message_id),
    ]

    body = b""
    body += ProtoEncoder.string(1, conversation_id)             # conversation_id
    body += ProtoEncoder.varint_field(2, 1)                     # conversation_type = 1
    body += ProtoEncoder.varint_field(3, conversation_short_id) # conversation_short_id
    body += ProtoEncoder.string(4, content)                      # content (JSON)
    body += ProtoEncoder.repeated_message(5, ext_items)          # ext (repeated ExtValue)
    body += ProtoEncoder.varint_field(6, 7)                      # message_type = 7 (文本)
    body += ProtoEncoder.string(7, "deprecated")                 # ticket
    body += ProtoEncoder.string(8, client_message_id)           # client_message_id
    return body


def build_request(myid: str, toid: str, message: str, device_id: str,
                  conversation_short_id: int = 0) -> bytes:
    """构造完整的 Request protobuf (cmd=100)"""
    client_message_id = str(uuid.uuid4())
    seq_id = random.randint(10100, 10300)

    # 消息内容 JSON
    content = json.dumps({"aweType": 0, "text": message}, ensure_ascii=False)

    # conversation_id 格式: "0:1:{toid}:{myid}"
    conversation_id = f"0:1:{toid}:{myid}"

    # 构造 SendMessageRequestBody
    send_msg_body = build_send_message_body(
        conversation_id, content, client_message_id, conversation_short_id
    )

    # 构造 RequestBody (send_message_body 在 field 100)
    request_body = ProtoEncoder.message(100, send_msg_body)

    # 构造 headers (repeated ExtValue, field 15)
    headers = [
        build_ext_value("aid", "6383"),
        build_ext_value("app_name", "douyin_pc"),
        build_ext_value("channel", "web"),
        build_ext_value("device_platform", "douyin_pc"),
        build_ext_value("os", "windows"),
        build_ext_value("referer", "https://www.douyin.com/chat"),
        build_ext_value("cookie_enabled", "true"),
        build_ext_value("browser_language", "zh-CN"),
        build_ext_value("browser_platform", "Win32"),
        build_ext_value("browser_name", "Mozilla"),
        build_ext_value("browser_online", "true"),
        build_ext_value("user_is_login", "true"),
        build_ext_value("app_language", "zh-Hans"),
        build_ext_value("tz_name", "Asia/Shanghai"),
        build_ext_value("is_page_visible", "true"),
        build_ext_value("focus_state", "false"),
        build_ext_value("history_len", "9"),
        build_ext_value("data_collection_enabled", "true"),
    ]

    # 构造 Request
    request = b""
    request += ProtoEncoder.varint_field(1, 100)               # cmd = 100
    request += ProtoEncoder.varint_field(2, seq_id)             # sequence_id
    request += ProtoEncoder.string(3, "1.2.3")                   # sdk_version
    request += ProtoEncoder.string(4, "")                         # token
    request += ProtoEncoder.varint_field(5, 3)                    # refer
    request += ProtoEncoder.varint_field(6, 0)                    # inbox_type
    request += ProtoEncoder.string(7, "831c301:master")          # build_number
    request += ProtoEncoder.message(8, request_body)             # body (RequestBody)
    request += ProtoEncoder.string(9, device_id)                 # device_id
    request += ProtoEncoder.string(11, "douyin_pc")              # device_platform
    request += ProtoEncoder.repeated_message(15, headers)       # headers (repeated)
    request += ProtoEncoder.varint_field(18, 1)                  # auth_type = 1 (简化认证)

    return request, seq_id


def build_frame(payload: bytes, seq_id: int) -> bytes:
    """构造 Frame {
        uint64 seqid = 1;
        uint64 logid = 2;
        int32 service = 3;
        int32 method = 4;
        repeated ExtValue headers = 5;
        string payload_encoding = 6;
        string payload_type = 7;
        bytes payload = 8;
    }"""
    frame = b""
    frame += ProtoEncoder.varint_field(1, seq_id)                # seqid
    frame += ProtoEncoder.varint_field(2, int(time.time() * 1000))  # logid
    frame += ProtoEncoder.varint_field(3, 5)                     # service = 5
    frame += ProtoEncoder.varint_field(4, 1)                     # method = 1
    frame += ProtoEncoder.string(7, "pb")                        # payload_type
    frame += ProtoEncoder.bytes_field(8, payload)               # payload
    return frame


# ============================================================
# Protobuf 解码器（用于解析响应）
# ============================================================

class ProtoDecoder:
    """简单 protobuf 解码器"""

    @staticmethod
    def decode(data: bytes) -> list:
        """解码 protobuf 字节流为 (field_number, wire_type, value) 列表"""
        fields = []
        i = 0
        while i < len(data):
            # 读取 tag
            tag, i = ProtoDecoder._read_varint(data, i)
            field_number = tag >> 3
            wire_type = tag & 0x07

            if wire_type == 0:  # varint
                value, i = ProtoDecoder._read_varint(data, i)
                fields.append((field_number, wire_type, value))
            elif wire_type == 2:  # length-delimited
                length, i = ProtoDecoder._read_varint(data, i)
                value = data[i:i + length]
                i += length
                fields.append((field_number, wire_type, value))
            elif wire_type == 5:  # 32-bit
                value = struct.unpack("<I", data[i:i + 4])[0]
                i += 4
                fields.append((field_number, wire_type, value))
            elif wire_type == 1:  # 64-bit
                value = struct.unpack("<Q", data[i:i + 8])[0]
                i += 8
                fields.append((field_number, wire_type, value))
            else:
                break
        return fields

    @staticmethod
    def _read_varint(data: bytes, offset: int) -> tuple:
        result = 0
        shift = 0
        while offset < len(data):
            byte = data[offset]
            result |= (byte & 0x7F) << shift
            offset += 1
            if not (byte & 0x80):
                break
            shift += 7
        return result, offset


# ============================================================
# WebSocket 客户端
# ============================================================

APP_KEY = "e1bd35ec9db7b8d846de66ed140b1ad9"
FP_ID = "9"
SALT = "f8a69f1719916z"
WS_URL = "wss://frontier-im.douyin.com/ws/v2"


def calculate_access_key(device_id: str) -> str:
    """计算 access_key = MD5(fpId + appKey + device_id + salt)"""
    raw = f"{FP_ID}{APP_KEY}{device_id}{SALT}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def cookies_to_string(cookies: list) -> str:
    """将 cookie 列表转为 cookie 字符串"""
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def get_cookie_value(cookies: list, name: str) -> str:
    """从 cookie 列表中获取指定 cookie 的值"""
    for c in cookies:
        if c.get("name") == name:
            return c.get("value", "")
    return ""


class DouyinWSClient:
    """抖音 WebSocket 消息客户端"""

    def __init__(self, cookies: list, device_id: str, myid: str):
        self.cookies = cookies
        self.cookie_str = cookies_to_string(cookies)
        self.device_id = device_id
        self.myid = myid
        self.access_key = calculate_access_key(device_id)
        self.sessionid = get_cookie_value(cookies, "sessionid") or get_cookie_value(cookies, "sessionid_ss")
        if not self.sessionid:
            logger.warning("Cookie 中缺少 sessionid/sessionid_ss，WebSocket 认证 token 为空，消息大概率无法发送")
        self.ws = None
        self._connected = False
        self._response_received = False
        self._response_status = None

    @property
    def ws_url(self) -> str:
        params = (
            f"aid=6383"
            f"&device_platform=douyin_pc"
            f"&fpid={FP_ID}"
            f"&device_id={self.device_id}"
            f"&token={self.sessionid}"
            f"&access_key={self.access_key}"
        )
        return f"{WS_URL}?{params}"

    def _on_open(self, ws):
        logger.debug("WebSocket 连接已建立")
        self._connected = True

    def _on_message(self, ws, message):
        """处理 WebSocket 消息"""
        try:
            if isinstance(message, str):
                logger.debug(f"收到文本消息: {message[:200]}")
                return

            # 解析 PushFrame
            fields = ProtoDecoder.decode(message)
            payload = None
            payload_type = None
            for fn, wt, val in fields:
                if fn == 7:  # payload_type
                    payload_type = val.decode("utf-8") if isinstance(val, bytes) else str(val)
                elif fn == 8:  # payload
                    payload = val

            if payload and payload_type == "pb":
                # 解析 Response
                resp_fields = ProtoDecoder.decode(payload)
                for fn, wt, val in resp_fields:
                    if fn == 1:  # cmd
                        cmd = val
                        logger.debug(f"收到响应 cmd={cmd}")
                    elif fn == 3:  # status_code (TikTok) / error_desc (Douyin)
                        if isinstance(val, bytes):
                            try:
                                desc = val.decode("utf-8")
                                logger.debug(f"响应描述: {desc}")
                            except:
                                pass
                        else:
                            logger.debug(f"状态码: {val}")
                    elif fn == 6:  # body (ResponseBody)
                        body_fields = ProtoDecoder.decode(val)
                        for bfn, bwt, bval in body_fields:
                            if bfn == 100:  # send_message_body (response)
                                self._response_received = True
                                # 解析 SendMessageResponseBody
                                send_resp_fields = ProtoDecoder.decode(bval)
                                for sfn, swt, sval in send_resp_fields:
                                    if sfn == 3:  # status
                                        status = sval if isinstance(sval, int) else 0
                                        self._response_status = status
                                        if status == 0:
                                            logger.info("✅ 消息发送成功（服务器确认）")
                                        else:
                                            logger.warning(f"⚠️ 消息发送状态: {status}")
        except Exception as e:
            logger.error(f"解析 WebSocket 响应失败: {e}")

    def _on_error(self, ws, error):
        logger.error(f"WebSocket 错误: {error}")
        self._connected = False

    def _on_close(self, ws, close_status, close_msg):
        logger.debug(f"WebSocket 连接关闭: {close_status} {close_msg}")
        self._connected = False

    def connect(self, timeout: int = 10):
        """建立 WebSocket 连接"""
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            header={
                "Pragma": "no-cache",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "Cache-Control": "no-cache",
                "Sec-WebSocket-Protocol": "binary, base64, pbbp2",
                "Sec-WebSocket-Extensions": "permessage-deflate; client_max_window_bits",
            },
            cookie=self.cookie_str,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

        import threading
        self._thread = threading.Thread(
            target=self.ws.run_forever,
            kwargs={"origin": "https://www.douyin.com"},
            daemon=True,
        )
        self._thread.start()

        # 等待连接建立
        start = time.time()
        while not self._connected and time.time() - start < timeout:
            time.sleep(0.1)

        if not self._connected:
            raise ConnectionError("WebSocket 连接超时")
        logger.info("WebSocket 连接成功")

    def send_message(self, toid: str, message: str,
                     conversation_short_id: int = 0,
                     timeout: int = 10) -> bool:
        """通过 WebSocket 发送消息"""
        if not self._connected:
            raise ConnectionError("WebSocket 未连接")

        self._response_received = False
        self._response_status = None

        # 构造 protobuf 消息
        request_data, seq_id = build_request(
            self.myid, toid, message, self.device_id, conversation_short_id
        )
        frame_data = build_frame(request_data, seq_id)

        # 发送二进制帧
        self.ws.send(frame_data, opcode=0x2)  # 0x2 = binary frame
        logger.debug(f"已发送 WebSocket 二进制帧 (seq={seq_id}, {len(frame_data)} bytes)")

        # 等待响应
        start = time.time()
        while not self._response_received and time.time() - start < timeout:
            time.sleep(0.1)

        if not self._response_received:
            logger.warning("未收到服务器响应（可能已发送但未确认）")
            return True  # 超时不一定代表失败

        return self._response_status == 0 if self._response_status is not None else True

    def send_heartbeat(self):
        """发送心跳"""
        if self._connected:
            self.ws.send("hi", opcode=0x1)
            logger.debug("已发送心跳")

    def close(self):
        """关闭连接"""
        if self.ws:
            self.ws.close()
        self._connected = False
        logger.debug("WebSocket 客户端已关闭")
