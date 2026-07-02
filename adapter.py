import asyncio
import os
import json
import uuid
import time
import logging
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from typing import Any, Dict, Optional

try:
    import aiohttp as _aiohttp
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 2000
CHUNK_TEXT_LIMIT = MAX_MESSAGE_LENGTH - len("[part 99/99] ".encode("utf-8"))
CHUNK_PREFIX_TEMPLATE = "[part {i}/{total}] "

def check_synology_chat_requirements() -> bool:
    return AIOHTTP_AVAILABLE

def _safe_timestamp(ts: int) -> datetime:
    try:
        if ts > 1e11: ts = ts // 1000
        return datetime.fromtimestamp(ts) if ts > 0 else datetime.now()
    except Exception:
        return datetime.now()

class SynologyChatAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig):
        if hasattr(Platform, 'SYNOLOGY_CHAT'):
            platform_identity = Platform.SYNOLOGY_CHAT
        else:
            MockEnum = type("MockEnum", (object,), {"value": "synology_chat", "name": "SYNOLOGY_CHAT"})
            platform_identity = MockEnum()

        super().__init__(config, platform_identity)
        
        # 🔔 核心对齐：从 config.extra 字典或环境变量中提取参数
        extra = getattr(config, "extra", {}) or {}
        
        # 提取 API 终点
        raw_endpoint = extra.get("api_endpoint") or os.getenv("SYNOLOGY_CHAT_API_ENDPOINT", "")
        self._api_endpoint = str(raw_endpoint).strip().strip('"').strip("'")
        
        # 提取 Token 并清洗引号
        raw_token = extra.get("token") or config.token or os.getenv("SYNOLOGY_CHAT_TOKEN", "")
        self._token = str(raw_token).strip().strip('"').strip("'")
        
        # 提取 SSL 验证
        self._ssl_verify = str(extra.get("ssl_verify", "false")).lower() in ("true", "1", "yes") or \
                           os.getenv("SYNOLOGY_CHAT_SSL_VERIFY", "false").lower() in ("true", "1", "yes")
        
        # 提取监听端口
        self._webhook_port = int(extra.get("port") or os.getenv("SYNOLOGY_CHAT_WEBHOOK_PORT", "8086"))
        
        self._http_session: Optional["_aiohttp.ClientSession"] = None
        self._site: Optional[web.TCPSite] = None
        self._runner: Optional[web.AppRunner] = None
        
        logger.info(f"[synology_chat] Config parsed. Endpoint: {self._api_endpoint[:30]}... Port: {self._webhook_port}")

    async def connect(self, is_reconnect: bool = False, **kwargs) -> bool:
        if is_reconnect:
            await self.disconnect()
        
        connector = _aiohttp.TCPConnector(ssl=self._ssl_verify)
        self._http_session = _aiohttp.ClientSession(connector=connector)
        
        try:
            app = web.Application()
            app.router.add_post("/synology-chat/webhook", self._handle_webhook_inbound)
            
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            
            self._site = web.TCPSite(self._runner, "0.0.0.0", self._webhook_port)
            await self._site.start()
            
            logger.info(f"[synology_chat] 🔥 SUCCESS: Webhook server listening on http://0.0.0.0:{self._webhook_port}/synology-chat/webhook")
        except Exception as e:
            logger.error(f"[synology_chat] Failed to bind independent port {self._webhook_port}: {e}", exc_info=True)
            return False

        self._mark_connected()
        return True

    async def _handle_webhook_inbound(self, request: "web.Request") -> "web.Response":
        try:
            data = await request.post()
            if not data:
                data = await request.json()
            
            user_id = str(data.get("user_id", "syno_user"))
            username = data.get("username", "User")
            text = str(data.get("text", "")).strip()
            
            if text:
                logger.info(f"[synology_chat] 🎯 WEBHOOK CAPTURED: '{text}' from {username}")
                
                timestamp = int(time.time())
                msg_id = f"wh_{int(time.time() * 1000)}"
                chat_id = f"synology_chat:{user_id}"
                
                source = self.build_source(
                    chat_id=chat_id, 
                    chat_name=f"Synology Chat ({username})", 
                    chat_type="dm", 
                    user_id=user_id, 
                    user_name=username
                )
                event = MessageEvent(
                    text=text, 
                    message_type=MessageType.TEXT, 
                    source=source, 
                    raw_message=dict(data), 
                    message_id=msg_id, 
                    timestamp=_safe_timestamp(timestamp)
                )
                
                task = asyncio.create_task(self.handle_message(event))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

            return web.json_response({"success": True})
            
        except Exception as e:
            logger.error(f"[synology_chat] Error processing request: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        user_id = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        return await _execute_synology_send(session=self._http_session, api_endpoint=self._api_endpoint, token=self._token, user_id=user_id, content=content, ssl_verify=self._ssl_verify)

    async def send_typing(self, chat_id: str, metadata=None) -> None: pass
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        user_id = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        return {"name": f"Synology Chat ({user_id})", "type": "dm", "user_id": user_id}

    async def disconnect(self) -> None:
        if self._site:
            try: await self._site.stop()
            except: pass
        if self._runner:
            try: await self._runner.cleanup()
            except: pass
        if self._http_session and not self._http_session.closed: 
            await self._http_session.close()
        self._mark_connected()

async def _execute_synology_send(session: Optional["_aiohttp.ClientSession"], api_endpoint: str, token: str, user_id: str, content: str, ssl_verify: bool = False) -> SendResult:
    if not api_endpoint: return SendResult(success=False, error="API missing")
    chunks = [content[i:i+CHUNK_TEXT_LIMIT] for i in range(0, len(content), CHUNK_TEXT_LIMIT)]
    last_result = SendResult(success=False)
    local_session = session if session else _aiohttp.ClientSession(connector=_aiohttp.TCPConnector(ssl=ssl_verify))
    parsed_url = urlparse(api_endpoint)
    has_query_routing = "api" in parse_qs(parsed_url.query)

    try:
        for i, chunk in enumerate(chunks):
            prefix = CHUNK_PREFIX_TEMPLATE.format(i=i+1, total=len(chunks)) if len(chunks) > 1 else ""
            try: uids = [int(user_id)]
            except: uids = [user_id]
            payload = {"text": prefix + chunk, "user_ids": uids}
            post_data = {"payload": json.dumps(payload)} if has_query_routing else {
                "api": "SYNO.Chat.External", "method": "chatbot", "version": "2", "token": token, "payload": json.dumps(payload),
            }
            async with local_session.post(api_endpoint, data=post_data, timeout=15) as resp:
                if resp.status < 300:
                    rj = await resp.json()
                    if rj.get("success"): last_result = SendResult(success=True, message_id=uuid.uuid4().hex[:12])
                    else: last_result = SendResult(success=False, error=str(rj.get("error")))
                else: last_result = SendResult(success=False, error=f"HTTP {resp.status}")
    except Exception as e: last_result = SendResult(success=False, error=str(e))
    finally:
        if not session: await local_session.close()
    return last_result

def _apply_yaml_config(yaml_cfg: dict, platform_cfg: Any) -> Optional[dict]: return {}
def _is_connected(adapter_instance: Any) -> bool: return True

def register(ctx: Any) -> None:
    ctx.register_platform(
        name="synology_chat", label="Synology Chat Hub", adapter_factory=SynologyChatAdapter,
        check_fn=check_synology_chat_requirements, is_connected=_is_connected, required_env=[],
        apply_yaml_config_fn=_apply_yaml_config, allow_all_env="SYNOLOGY_CHAT_ALLOW_ALL_USERS",
        allowed_users_env="SYNOLOGY_CHAT_ALLOWED_USERS", cron_deliver_env_var="SYNOLOGY_CHAT_HOME_CHANNEL",
        max_message_length=MAX_MESSAGE_LENGTH, emoji="💬", allow_update_command=True,
    )
