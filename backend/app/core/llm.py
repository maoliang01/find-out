import time
import asyncio
import json
import logging
from typing import AsyncIterator, Optional, Dict, Any, List, Union
from openai import AsyncOpenAI
import httpx

from app.core.config import get_llm_config, ModelConfig

logger = logging.getLogger("ai-studio.llm")


class LLMService:
    """大模型服务"""

    def __init__(self):
        self.clients: Dict[str, AsyncOpenAI] = {}
        # 缓存前端传递的配置
        self.dynamic_configs: Dict[str, Dict[str, Any]] = {}

    def get_client(self, base_url: str, api_key: str) -> AsyncOpenAI:
        """获取或创建 OpenAI 客户端"""
        if not api_key:
            raise ValueError("API Key 不能为空")
        cache_key = f"{base_url}:{api_key[:8] if api_key else 'none'}"
        if cache_key not in self.clients:
            # 禁用代理，确保访问内部 API
            import os
            old_http = os.environ.pop('http_proxy', None)
            old_https = os.environ.pop('https_proxy', None)
            old_http_upper = os.environ.pop('HTTP_PROXY', None)
            old_https_upper = os.environ.pop('HTTPS_PROXY', None)

            try:
                print(f"[DEBUG] 创建客户端: base_url={base_url}, api_key={api_key[:8]}...")
                self.clients[cache_key] = AsyncOpenAI(
                    api_key=api_key,
                    base_url=base_url,
                    http_client=httpx.AsyncClient(
                        timeout=httpx.Timeout(timeout=180.0, connect=30.0),
                        proxy=None,
                        trust_env=False,
                    ),
                )
            finally:
                # 恢复环境变量
                if old_http: os.environ['http_proxy'] = old_http
                if old_https: os.environ['https_proxy'] = old_https
                if old_http_upper: os.environ['HTTP_PROXY'] = old_http_upper
                if old_https_upper: os.environ['HTTPS_PROXY'] = old_https_upper

        return self.clients[cache_key]

    def set_model_config(self, model_id: str, config: Dict[str, Any]) -> None:
        """设置模型配置（用于前端传递的配置）"""
        self.dynamic_configs[model_id] = config

    def get_model_config(self, model_id: str) -> Optional[Dict[str, Any]]:
        """获取模型配置（优先动态配置，其次全局配置）"""
        # 优先从动态配置获取
        if model_id in self.dynamic_configs:
            return self.dynamic_configs[model_id]
        # 回退到全局配置
        config = get_llm_config()

        # 直接按 ID 查找
        if model_id in config.models:
            return config.models[model_id].model_dump()

        # 如果 model_id 格式是 model-xxx，尝试按名称模糊匹配
        if model_id.startswith("model-"):
            for mid, model in config.models.items():
                # 匹配后8位或完整ID
                if mid.endswith(model_id[-8:]) or model.name.lower().replace("-", "").replace("_", "") in model_id.lower().replace("-", "").replace("_", ""):
                    return model.model_dump()

        # 最后尝试按名称匹配（忽略大小写和连接符）
        model_id_normalized = model_id.lower().replace("-", "").replace("_", "")
        for mid, model in config.models.items():
            if model.name.lower().replace("-", "").replace("_", "") == model_id_normalized:
                return model.model_dump()

        return None

    async def test_connection(self, model_id: str) -> Dict[str, Any]:
        """测试模型连接"""
        model_config = self.get_model_config(model_id)
        if not model_config:
            return {"success": False, "error": "模型不存在"}

        start_time = time.time()

        try:
            client = self.get_client(
                model_config.get("base_url", ""),
                model_config.get("api_key", "")
            )
            model_type = model_config.get("type", "")
            model_name = model_config.get("model_name", "gpt-4o")

            # 根据模型类型选择不同的测试方式
            if model_type == "embedding":
                # Embedding 模型使用 embeddings API
                response = await client.embeddings.create(
                    model=model_name,
                    input="Hello world",
                )
                latency = int((time.time() - start_time) * 1000)
                return {
                    "success": True,
                    "latency": latency,
                    "model": model_name,
                }
            else:
                # LLM 和多模态模型使用 chat API
                response = await client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": "Hi"}],
                    max_tokens=5,
                )
                latency = int((time.time() - start_time) * 1000)
                return {
                    "success": True,
                    "latency": latency,
                    "model": response.model,
                }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def chat(
        self,
        model_id: str,
        messages: List[Dict[str, str] | Dict[str, Any]],
        stream: bool = True,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        model_config: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[str]:
        """发送对话请求，返回流式响应"""
        # 如果前端传递了配置，直接使用它
        config = model_config
        # 兼容历史调用方传入的 "default"；真正的默认模型由配置中的
        # default_llm 决定，不能把它当成一个必须存在的模型 ID。
        if model_id in ("default", "default-llm"):
            model_id = ""
        if not config and model_id:
            config = self.get_model_config(model_id)
            if not config:
                yield f"[错误] 模型不存在: {model_id}"
                return
        if not config:
            global_config = get_llm_config()
            default_id = global_config.default_llm
            config = self.get_model_config(default_id)
            if not config:
                yield "[错误] 未配置任何模型"
                return

        try:
            client = self.get_client(
                config.get("base_url", ""),
                config.get("api_key", "")
            )

            # 处理消息格式，支持多模态内容
            processed_messages = self._process_multimodal_messages(messages)

            if stream:
                response = await client.chat.completions.create(
                    model=config.get("model_name", "gpt-4o"),
                    messages=processed_messages,
                    stream=True,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

                async for chunk in response:
                    content = ""
                    # 处理 OpenAI 格式
                    if chunk.choices and chunk.choices[0].delta.content:
                        content = chunk.choices[0].delta.content
                    # 处理自定义格式 (reasoning_content)
                    if hasattr(chunk.choices[0].delta, 'reasoning_content') and chunk.choices[0].delta.reasoning_content:
                        content = chunk.choices[0].delta.reasoning_content + "\n" + content if content else chunk.choices[0].delta.reasoning_content

                    if content:
                        yield content
            else:
                response = await client.chat.completions.create(
                    model=config.get("model_name", "gpt-4o"),
                    messages=processed_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

                # 处理响应
                content = ""
                if response.choices and response.choices[0].message.content:
                    content = response.choices[0].message.content
                # 处理 reasoning_content (如果有)
                if hasattr(response.choices[0].message, 'reasoning_content') and response.choices[0].message.reasoning_content:
                    reasoning = response.choices[0].message.reasoning_content
                    content = f"**思考过程:**\n{reasoning}\n\n**回答:**\n{content}"

                yield content

        except Exception as e:
            yield f"[错误] {str(e)}"

    def _process_multimodal_messages(self, messages: List[Dict[str, Any] | Dict[str, str]]) -> List[Dict[str, Any]]:
        """
        处理消息格式，将前端传来的多模态格式转换为 OpenAI API 格式。
        前端可能传入:
        - 普通文本消息: { role: "user", content: "文本" }
        - 多模态消息: { role: "user", content: [{ type: "text", text: "..." }, { type: "image_url", image_url: { url: "data:..." } }] }
        """
        processed = []
        for msg in messages:
            # 已经是标准格式（简单文本）
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                processed.append(msg)

            # 多模态内容数组格式
            elif isinstance(msg, dict) and isinstance(msg.get("content"), list):
                processed.append(msg)

            # 其他情况（应该不会有）
            else:
                processed.append(msg)

        return processed

    async def non_stream_chat(
        self,
        model_id: str,
        messages: List[Dict[str, str] | Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        model_config: Optional[Dict[str, Any]] = None,
        response_format: Optional[Dict[str, str]] = None,
    ) -> str:
        """发送对话请求，返回完整响应（非流式）"""
        # 优先使用前端传递的配置
        config = model_config
        # 兼容历史调用方传入的 "default"；真正的默认模型由配置中的
        # default_llm 决定，不能把它当成一个必须存在的模型 ID。
        if model_id in ("default", "default-llm"):
            model_id = ""
        if not config and model_id:
            config = self.get_model_config(model_id)
            if not config:
                return f"[错误] 模型不存在: {model_id}"
        if not config:
            global_config = get_llm_config()
            default_id = global_config.default_llm
            config = self.get_model_config(default_id)
            if not config:
                return "[错误] 未配置任何模型"

        api_key = config.get("api_key", "")
        base_url = config.get("base_url", "")
        if not api_key:
            return "[错误] API Key 未配置，请先在设置中配置模型"

        try:
            client = self.get_client(base_url, api_key)

            # 处理消息格式，支持多模态内容
            processed_messages = self._process_multimodal_messages(messages)

            request_kwargs = {
                "model": config.get("model_name", "gpt-4o"),
                "messages": processed_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if response_format:
                request_kwargs["response_format"] = response_format
            try:
                response = await client.chat.completions.create(**request_kwargs)
            except Exception as exc:
                error_text = str(exc).lower()
                status_code = getattr(exc, "status_code", None)
                unsupported_format = status_code in (400, 422) and any(
                    marker in error_text
                    for marker in ("response_format", "json_object", "structured output")
                )
                if not response_format or not unsupported_format:
                    raise
                logger.warning("模型接口不支持 response_format，自动降级为普通 JSON 提示")
                request_kwargs.pop("response_format", None)
                response = await client.chat.completions.create(**request_kwargs)

            # 处理响应
            content = ""
            if response.choices and response.choices[0].message.content:
                content = response.choices[0].message.content
            # 处理 reasoning_content (如果有)
            if (
                not content
                and hasattr(response.choices[0].message, 'reasoning_content')
                and response.choices[0].message.reasoning_content
            ):
                content = response.choices[0].message.reasoning_content

            return content

        except Exception as e:
            import traceback
            print(f"[ERROR] Chat error: {e}")
            print(f"[ERROR] Traceback: {traceback.format_exc()}")
            return f"[错误] {str(e)}"


# 全局服务实例
llm_service = LLMService()
