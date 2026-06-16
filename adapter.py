import os
import hmac
import json
import uuid
import time
import logging
import asyncio
import hashlib
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from typing import Any, Dict, Optional

try:
    from aiohttp import web
    import aiohttp as _aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

# 常量定义
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8086
DEFAULT_WEBHOOK_PATH = "/synology-chat/webhook"
MAX_MESSAGE_LENGTH = 2000
CHUNK_TEXT_LIMIT = MAX_MESSAGE_LENGTH - len("[part 99/99] ".encode("utf-8"))
CHUNK_PREFIX_TEMPLATE = "[part {i}/{total}] "


def check_synology_chat_requirements() -> bool:
    """检查平台所需的底层依赖是否存在。"""
    return AIOHTTP_AVAILABLE


def _safe_timestamp(ts: int) -> datetime:
    try:
        if ts <= 0:
            return datetime.now()
        return datetime.fromtimestamp(ts)
    except (OSError, ValueError, OverflowError):
        return datetime.now()


class SynologyChatAdapter(BasePlatformAdapter):
    """修复了 'str' object has no attribute 'value' 报错的 Synology Chat 适配器。"""

    def __init__(self, config: PlatformConfig):
        # 核心修复：如果核心没有内置该枚举，动态打造一个具有 .value 属性的伪枚举对象，彻底欺骗基类
        if hasattr(Platform, 'SYNOLOGY_CHAT'):
            platform_identity = Platform.SYNOLOGY_CHAT
        else:
            # 创建一个动态类，模拟 Enum 行为
            MockEnum = type("MockEnum", (object,), {"value": "synology_chat", "name": "SYNOLOGY_CHAT"})
            platform_identity = MockEnum()  # 实例化

        super().__init__(config, platform_identity)
        
        extra = config.extra or {}
        # 优先读取 YAML 配置，其次通过环境变量兜底
        self._host: str = extra.get("host", os.getenv("SYNOLOGY_CHAT_HOST", DEFAULT_HOST))
        self._port: int = int(extra.get("port", os.getenv("SYNOLOGY_CHAT_PORT", DEFAULT_PORT)))
        self._webhook_path: str = extra.get("webhook_path", DEFAULT_WEBHOOK_PATH)
        self._api_endpoint: str = extra.get("api_endpoint", os.getenv("SYNOLOGY_CHAT_API_ENDPOINT", ""))
        
        ssl_val = extra.get("ssl_verify", os.getenv("SYNOLOGY_CHAT_SSL_VERIFY", "false"))
        self._ssl_verify: bool = ssl_val in (True, "true", "1", "yes", "True")
        
        # 根部 token 映射
        self._token: str = config.token or os.getenv("SYNOLOGY_CHAT_TOKEN", "")

        self._runner = None
        self._http_session: Optional["_aiohttp.ClientSession"] = None
        self._user_map: Dict[str, str] = {}  
        self._seen_messages: Dict[str, float] = {}

    async def connect(self) -> bool:
        """核心生命周期：启动 Webhook 入站服务。"""
        if not AIOHTTP_AVAILABLE:
            logger.error("[synology_chat] aiohttp is required for this plugin.")
            return False

        if not self._token or not self._api_endpoint:
            logger.error("[synology_chat] Missing token or api_endpoint configurations.")
            return False

        connector = _aiohttp.TCPConnector(ssl=self._ssl_verify)
        self._http_session = _aiohttp.ClientSession(connector=connector)

        app = web.Application()
        app.router.add_post(self._webhook_path, self._handle_webhook)
        
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        
        self._mark_connected()
        logger.info(f"[synology_chat] Plugin running on {self._host}:{self._port}{self._webhook_path}")
        return True

    async def disconnect(self) -> None:
        """核心生命周期：停止服务。"""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None
        self._mark_disconnected()
        logger.info("[synology_chat] Plugin stopped.")

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        try:
            form_data = await request.post()
        except Exception as e:
            return web.json_response({"error": f"Bad request: {e}"}, status=400)

        # 验证入站 Token（群晖 Webhook 传过来的 Token 校验）
        incoming_token = form_data.get("token", "")
        if not hmac.compare_digest(incoming_token, self._token):
            return web.json_response({"error": "Unauthorized"}, status=401)

        user_id = str(form_data.get("user_id", ""))
        username = form_data.get("username", "unknown")
        text = str(form_data.get("text", ""))
        timestamp = int(form_data.get("timestamp", "0"))
        if timestamp > 1e12:
            timestamp = timestamp // 1000

        if not user_id or not text:
            return web.json_response({"status": "ignored"}, status=200)

        # 幂等去重
        msg_hash = hashlib.md5(f"{user_id}:{timestamp}:{text}".encode()).hexdigest()
        now = time.time()
        self._seen_messages = {k: v for k, v in self._seen_messages.items() if now - v < 300}
        if msg_hash in self._seen_messages:
            return web.json_response({"status": "duplicate"}, status=200)
        self._seen_messages[msg_hash] = now

        chat_id = f"synology_chat:{user_id}"
        self._user_map[chat_id] = user_id

        source = self.build_source(
            chat_id=chat_id,
            chat_name=f"Synology Chat ({username})",
            chat_type="dm",
            user_id=user_id,          
            user_name=username,
        )
        
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=dict(form_data),
            message_id=msg_hash,
            timestamp=_safe_timestamp(timestamp),
        )

        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return web.json_response({"status": "ok"}, status=200)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        user_id = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        return await _execute_synology_send(
            session=self._http_session,
            api_endpoint=self._api_endpoint,
            token=self._token,
            user_id=user_id,
            content=content,
            ssl_verify=self._ssl_verify  # 透传 SSL 开关
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        user_id = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        return {"name": f"Synology Chat ({user_id})", "type": "dm", "user_id": user_id}


# ------------------------------------------------------------------
# 智能出站发送核心
# ------------------------------------------------------------------

async def _execute_synology_send(
    session: Optional["_aiohttp.ClientSession"],
    api_endpoint: str,
    token: str,
    user_id: str,
    content: str,
    ssl_verify: bool = False  # 添加组件自带的验证开关
) -> SendResult:
    if not api_endpoint:
        return SendResult(success=False, error="API configuration missing")

    chunks = []
    for paragraph in content.split("\n"):
        para_bytes = paragraph.encode("utf-8")
        if len(para_bytes) <= CHUNK_TEXT_LIMIT:
            if chunks:
                merged = chunks[-1] + "\n" + paragraph
                if len(merged.encode("utf-8")) <= CHUNK_TEXT_LIMIT:
                    chunks[-1] = merged
                    continue
            chunks.append(paragraph)
        else:
            start = 0
            while start < len(para_bytes):
                end = min(start + CHUNK_TEXT_LIMIT, len(para_bytes))
                if end < len(para_bytes):
                    while end > start and (para_bytes[end] & 0xC0) == 0x80:
                        end -= 1
                chunks.append(para_bytes[start:end].decode("utf-8"))
                start = end

    last_result = SendResult(success=False, error="No chunks compiled")
    
    # 如果外部传了已经初始化的 session 则复用，否则新建一个安全带有特定 SSL 配置的连接池
    if session:
        local_session = session
    else:
        connector = _aiohttp.TCPConnector(ssl=ssl_verify)
        local_session = _aiohttp.ClientSession(connector=connector)

    parsed_url = urlparse(api_endpoint)
    query_params = parse_qs(parsed_url.query)
    has_query_routing = "api" in query_params and "method" in query_params

    try:
        for i, chunk in enumerate(chunks):
            prefix = CHUNK_PREFIX_TEMPLATE.format(i=i+1, total=len(chunks)) if len(chunks) > 1 else ""
            text_payload = prefix + chunk

            try:
                user_ids = [int(user_id)]
            except (ValueError, TypeError):
                user_ids = [user_id]

            payload_data = {"text": text_payload, "user_ids": user_ids}

            if has_query_routing:
                post_data = {"payload": json.dumps(payload_data)}
            else:
                post_data = {
                    "api": "SYNO.Chat.External",
                    "method": "chatbot",
                    "version": "2",
                    "token": token,
                    "payload": json.dumps(payload_data),
                }

            async with local_session.post(
                api_endpoint, data=post_data, timeout=_aiohttp.ClientTimeout(total=20)
            ) as resp:
                if resp.status < 300:
                    res_json = await resp.json()
                    if res_json.get("success"):
                        last_result = SendResult(success=True, message_id=uuid.uuid4().hex[:12])
                    else:
                        last_result = SendResult(success=False, error=str(res_json.get("error")))
                else:
                    last_result = SendResult(success=False, error=f"HTTP Error {resp.status}")

            if i < len(chunks) - 1:
                await asyncio.sleep(0.5)
    except Exception as e:
        last_result = SendResult(success=False, error=str(e))
    finally:
        if not session:  # 只有非复用的临时连接才需要显式关闭
            await local_session.close()

    return last_result


# ------------------------------------------------------------------
# YAML 配置桥梁钩子
# ------------------------------------------------------------------

def _apply_yaml_config(yaml_cfg: dict, platform_cfg: Any) -> Optional[dict]:
    sc_cfg = yaml_cfg.get("platforms", {}).get("synology_chat", {})
    if not sc_cfg:
        return None
        
    if "token" in sc_cfg and hasattr(platform_cfg, "token"):
        if not platform_cfg.token:
            platform_cfg.token = sc_cfg["token"]

    extra_data = sc_cfg.get("extra", {})
    return {
        "host": extra_data.get("host", DEFAULT_HOST),
        "port": extra_data.get("port", DEFAULT_PORT),
        "api_endpoint": extra_data.get("api_endpoint", ""),
        "ssl_verify": str(extra_data.get("ssl_verify", "false")).lower() in ("true", "1", "yes", "True"),
        "webhook_path": extra_data.get("webhook_path", DEFAULT_WEBHOOK_PATH),
    }


def _is_connected(adapter_instance: Any) -> bool:
    if adapter_instance and hasattr(adapter_instance, "is_connected"):
        return adapter_instance.is_connected()
    return False


# ------------------------------------------------------------------
# 全新动态平台注册器入口
# ------------------------------------------------------------------

def register(ctx: Any) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="synology_chat",
        label="Synology Chat Hub",
        adapter_factory=SynologyChatAdapter,
        check_fn=check_synology_chat_requirements,
        is_connected=_is_connected,
        required_env=[],
        
        # 绑定 YAML 转换桥梁钩子
        apply_yaml_config_fn=_apply_yaml_config,
        
        # 将全局允许变量和特定允许列表绑定到内核鉴权机制
        allow_all_env="SYNOLOGY_CHAT_ALLOW_ALL_USERS",
        allowed_users_env="SYNOLOGY_CHAT_ALLOWED_USERS",  
        
        # Cron 定时投递的专属默认环境变量
        cron_deliver_env_var="SYNOLOGY_CHAT_HOME_CHANNEL",
        
        # 基础限制与展示定义
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="💬",
        allow_update_command=True,
    )
