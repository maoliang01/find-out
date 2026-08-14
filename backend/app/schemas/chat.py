from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from datetime import datetime


class Message(BaseModel):
    """对话消息（聊天请求/响应共用的消息结构）"""
    role: str  # user, assistant, system
    content: str


class StoredMessage(BaseModel):
    """持久化到本机数据库的对话消息。"""
    id: str
    session_id: str
    role: str
    content: str
    model: Optional[str] = None
    created_at: datetime


class ChatSessionCreate(BaseModel):
    """创建会话请求。"""
    title: Optional[str] = "新对话"
    model: Optional[str] = ""


class ChatSessionOut(BaseModel):
    """会话信息（不含消息，消息单列接口获取）。"""
    id: str
    title: str
    model: str
    use_rag: bool
    created_at: datetime
    updated_at: datetime


class ChatSessionList(BaseModel):
    """会话列表响应。"""
    sessions: List[ChatSessionOut]


class ChatRequest(BaseModel):
    """聊天请求"""
    model_id: Optional[str] = None
    messages: List[Dict[str, str]]
    stream: bool = True
    temperature: float = 0.7
    max_tokens: Optional[int] = None

    class Config:
        extra = "allow"  # 允许额外字段


class ChatResponse(BaseModel):
    """聊天响应"""
    content: str
    model: Optional[str] = None
    usage: Optional[Dict] = None


class ModelInfo(BaseModel):
    """模型信息"""
    id: str
    name: str
    type: str
    base_url: str
    api_key: Optional[str] = None
    model_name: Optional[str] = None
    is_connected: bool = False
    latency: Optional[int] = None
    last_tested_at: Optional[str] = None


class ModelConfigRequest(BaseModel):
    """模型配置请求"""
    name: str
    type: str  # llm, embedding, multimodal
    base_url: str
    api_key: Optional[str] = None
    model_name: Optional[str] = None


class TestResult(BaseModel):
    """测试结果"""
    success: bool
    latency: Optional[int] = None
    error: Optional[str] = None
    model: Optional[str] = None