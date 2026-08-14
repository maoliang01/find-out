from fastapi import APIRouter, Request, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse
import asyncio
import json
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.schemas.chat import ChatResponse, StoredMessage, ChatSessionCreate, ChatSessionOut, ChatSessionList
from app.core.llm import llm_service
from app.core.config import get_llm_config
from app.core.database import get_db
from app.models.chat import ChatSession, ChatMessage

router = APIRouter(prefix="/chat", tags=["对话"])


def _write_message(db: Session, session_id: str, role: str, content: str, model: str = None) -> None:
    """将单条消息落库到本机数据库。"""
    msg = ChatMessage(
        id=uuid.uuid4().hex[:32],
        session_id=session_id,
        role=role,
        content=content or "",
        model=model,
    )
    db.add(msg)
    s = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if s:
        s.updated_at = datetime.utcnow()
        # 新会话以首条提问作为标题，方便在历史列表中识别。
        if role == "user" and s.title == "新对话":
            normalized = " ".join((content or "").split())
            if normalized:
                s.title = normalized[:30] + ("..." if len(normalized) > 30 else "")
    db.commit()


def _session_or_404(db: Session, session_id: str) -> ChatSession:
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.get("/sessions", response_model=ChatSessionList)
async def list_sessions(db: Session = Depends(get_db)):
    """获取本地保存的对话会话列表（按最近更新倒序，不含消息）。"""
    sessions = (
        db.query(ChatSession)
        .order_by(ChatSession.updated_at.desc())
        .all()
    )
    return {"sessions": [
        {
            "id": s.id,
            "title": s.title,
            "model": s.model,
            "use_rag": s.use_rag,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }
        for s in sessions
    ]}


@router.post("/sessions", response_model=ChatSessionOut)
async def create_session(payload: ChatSessionCreate, db: Session = Depends(get_db)):
    """创建新的对话会话。"""
    session = ChatSession(
        id=uuid.uuid4().hex[:32],
        title=payload.title or "新对话",
        model=payload.model or "",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return {
        "id": session.id,
        "title": session.title,
        "model": session.model,
        "use_rag": session.use_rag,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


@router.get("/sessions/{session_id}/messages", response_model=List[StoredMessage])
async def list_session_messages(session_id: str, db: Session = Depends(get_db)):
    """获取某个会话的历史消息（按创建时间正序）。"""
    _session_or_404(db, session_id)
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .all()
    )
    return [
        {
            "id": m.id,
            "session_id": m.session_id,
            "role": m.role,
            "content": m.content,
            "model": m.model,
            "created_at": m.created_at,
        }
        for m in messages
    ]


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, db: Session = Depends(get_db)):
    """删除会话及其全部消息。"""
    session = _session_or_404(db, session_id)
    db.query(ChatMessage).filter(ChatMessage.session_id == session_id).delete()
    db.delete(session)
    db.commit()


@router.post("", response_model=ChatResponse)
async def chat(request: Request, db: Session = Depends(get_db)):
    """发送对话请求（非流式），会将会话消息落库。"""
    # 直接读取原始 JSON
    body = await request.json()

    model_id = body.get("model_id") or get_llm_config().default_llm
    messages = body.get("messages", [])
    temperature = body.get("temperature", 0.7)
    max_tokens = body.get("max_tokens")
    model_config = body.get("model_config")
    session_id = body.get("session_id")

    print(f"[DEBUG] chat API 收到请求:")
    print(f"  model_id: {model_id}")
    print(f"  messages count: {len(messages)}")
    print(f"  session_id: {session_id}")
    print(f"  model_config: {model_config}")

    content = await llm_service.non_stream_chat(
        model_id=model_id,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        model_config=model_config,
    )

    # 有 session_id 时落库：user 消息在发问时写入，assistant 在回复后写入
    if session_id:
        if messages and messages[-1].get("role") == "user":
            _write_message(db, session_id, "user", messages[-1].get("content", ""), model_id)
        _write_message(db, session_id, "assistant", content, model_id)

    return ChatResponse(content=content)


async def chat_stream_generator(request: Request, body: dict, db: Session):
    """流式对话生成器，支持客户端断开检测，生成结束后落库。"""
    model_id = body.get("model_id") or get_llm_config().default_llm
    messages = body.get("messages", [])
    temperature = body.get("temperature", 0.7)
    max_tokens = body.get("max_tokens")
    model_config = body.get("model_config")
    session_id = body.get("session_id")

    # 发问时即写入 user 消息（即使中断也保留问题）
    if session_id and messages and messages[-1].get("role") == "user":
        _write_message(db, session_id, "user", messages[-1].get("content", ""), model_id)

    full_content = ""
    try:
        async for chunk in llm_service.chat(
            model_id=model_id,
            messages=messages,
            stream=True,
            temperature=temperature,
            max_tokens=max_tokens,
            model_config=model_config,
        ):
            # 检查客户端是否已断开连接
            if await request.is_disconnected():
                print("[DEBUG] 客户端已断开连接，停止生成")
                break

            full_content += chunk
            yield {
                "event": "message",
                "data": json.dumps({"content": chunk, "done": False}),
            }
            await asyncio.sleep(0)  # 允许其他任务运行
    except Exception as e:
        if "cancel" not in str(e).lower():
            yield {
                "event": "error",
                "data": json.dumps({"error": str(e)}),
            }
    finally:
        if session_id and full_content:
            _write_message(db, session_id, "assistant", full_content, model_id)
        yield {
            "event": "message",
            "data": json.dumps({"content": "", "done": True}),
        }


@router.post("/stream")
async def chat_stream(request: Request, db: Session = Depends(get_db)):
    """流式对话（SSE），支持客户端断开检测，会将会话消息落库。"""
    body = await request.json()
    return EventSourceResponse(chat_stream_generator(request, body, db))
