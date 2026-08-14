"use client";

import { useState, useRef, useEffect } from "react";
import { useChatStore } from "@/stores/chat-store";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Send,
  Plus,
  Trash2,
  Bot,
  User,
  Copy,
  RotateCcw,
  ThumbsUp,
  BookOpen,
  Star,
  AlertCircle,
  Square,
  Brain,
  Sparkles,
  Eraser,
  ChevronDown,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";
import type { Message } from "@/types";
import { qaAnswer } from "@/lib/api-kg";
import MiniGraph from "@/components/kg/MiniGraph";
import EntitySourcePopover from "@/components/kg/EntitySourcePopover";

export default function ChatPage() {
  const {
    sessions,
    currentSessionId,
    setCurrentSession,
    addSession,
    deleteSession,
    clearSession,
    addMessage,
    models,
    selectedModel,
    setModel,
    isStreaming,
    sendMessage,
    loadModels,
    loadSessions,
    loadMessages,
    error,
    setError,
    stopStreaming,
    kgEnhanced,
    setKgEnhanced,
  } = useChatStore();

  const [popoverEntity, setPopoverEntity] = useState<string | null>(null);
  // 控制是否显示欢迎页面（刷新后默认显示欢迎页，不自动进入历史对话）
  const [showWelcome, setShowWelcome] = useState(true);

  // 初始化时加载模型列表（不加载会话，刷新后显示欢迎页）
  useEffect(() => {
    loadModels();
    // 不调用 loadSessions()，刷新后始终显示欢迎页
    // 如果需要查看历史对话，可以点击"查看历史对话"按钮

    // 检查是否有从提示词模板页面传递过来的提示词
    const pendingPrompt = localStorage.getItem('pending_prompt')
    if (pendingPrompt) {
      setInput(pendingPrompt)
      localStorage.removeItem('pending_prompt')
      setShowWelcome(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const [input, setInput] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const currentSession = sessions.find((s) => s.id === currentSessionId);

  // 在文章片段中内联高亮实体名
  function highlightSnippet(snippet: string, entityName: string) {
    if (!snippet || !entityName) return snippet;
    const parts = snippet.split(new RegExp(`(${entityName})`, "gi"));
    return parts.map((p, i) =>
      p.toLowerCase() === entityName.toLowerCase() ? (
        <mark key={i} className="bg-yellow-200 px-0.5 rounded">
          {p}
        </mark>
      ) : (
        <span key={i}>{p}</span>
      ),
    );
  }

  // 自动滚动到底部
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [currentSession?.messages]);

  // 自动调整文本框高度
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 200)}px`;
    }
  }, [input]);

  // 处理发送消息
  const handleSend = async () => {
    if (!input.trim() || isStreaming) return;

    // 确保有会话存在
    let sessionId = currentSessionId;
    if (!sessionId) {
      await addSession();
      sessionId = useChatStore.getState().currentSessionId;
      if (!sessionId) return;
    }

    setError(null);
    const messageContent = input.trim();
    setInput("");

    if (kgEnhanced) {
      // 走 KG QA 增强分支
      const now = new Date();
      const userMsg: Message = {
        id: crypto.randomUUID(),
        role: "user",
        content: messageContent,
        createdAt: now,
      };
      const tmpMsg: Message = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: "",
        createdAt: now,
      };
      addMessage(sessionId, userMsg);
      addMessage(sessionId, tmpMsg);
      try {
        const data = await qaAnswer(messageContent, selectedModel);
        useChatStore.setState((state) => ({
          sessions: state.sessions.map((s) => {
            if (s.id !== sessionId) return s;
            const messages = s.messages.map((m) =>
              m.id === tmpMsg.id ? { ...m, content: data.answer, kg: data } : m,
            );
            return { ...s, messages };
          }),
        }));
      } catch (e: any) {
        useChatStore.setState((state) => ({
          sessions: state.sessions.map((s) => {
            if (s.id !== sessionId) return s;
            const messages = s.messages.map((m) =>
              m.id === tmpMsg.id ? { ...m, content: `错误: ${e?.message || e}` } : m,
            );
            return { ...s, messages };
          }),
        }));
      }
      return;
    }

    // 发送消息后退出欢迎页模式
    setShowWelcome(false);
    await sendMessage(sessionId, messageContent);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const currentModel = models.find((m) => m.id === selectedModel);

  // 显示后端配置的所有模型
  const availableModels = models;

  const isEmpty = !currentSession || currentSession.messages.length === 0 || showWelcome;

  // 输入区（空态居中 / 有消息时贴底，同一套 UI）
  const composer = (
    <div className="mx-auto w-full max-w-3xl px-5">
      <div className="mb-2 flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <Select value={selectedModel} onValueChange={(value) => value && setModel(value)}>
            <SelectTrigger className="h-7 text-xs gap-1 text-muted-foreground hover:text-foreground border-0 bg-muted/50 rounded-lg">
              <Bot className="h-3.5 w-3.5" />
              <SelectValue placeholder="选择模型" />
              <ChevronDown className="h-3 w-3" />
            </SelectTrigger>
            <SelectContent>
              {availableModels.length === 0 ? (
                <div className="p-2 text-sm text-muted-foreground">
                  暂无可用模型
                </div>
              ) : (
                availableModels.map((model) => (
                  <SelectItem key={model.id} value={model.id}>
                    <div className="flex flex-col">
                      <span>{model.name}</span>
                      {model.provider && (
                        <span className="text-xs text-muted-foreground">{model.provider}</span>
                      )}
                    </div>
                  </SelectItem>
                ))
              )}
            </SelectContent>
          </Select>

          <div className="flex items-center gap-1.5 pl-2 border-l border-border">
            <button
              onClick={() =>
                currentSessionId &&
                useChatStore.getState().toggleRag(currentSessionId)
              }
              className={cn(
                "flex items-center gap-1 text-xs rounded-md px-2 py-1 transition-colors",
                currentSession?.useRag
                  ? "bg-blue-500/10 text-blue-600"
                  : "text-muted-foreground hover:bg-muted"
              )}
            >
              <BookOpen className="h-3.5 w-3.5" />
              RAG
            </button>
            <button
              onClick={() => setKgEnhanced(!kgEnhanced)}
              className={cn(
                "flex items-center gap-1 text-xs rounded-md px-2 py-1 transition-colors",
                kgEnhanced
                  ? "bg-violet-500/10 text-violet-600"
                  : "text-muted-foreground hover:bg-muted"
              )}
            >
              <Brain className="h-3.5 w-3.5" />
              知识图谱
            </button>
          </div>
        </div>

        <span className="text-xs text-muted-foreground">
          当前模型：{currentModel?.name || "未选择"}
        </span>
      </div>

      <Card
        className="border-border/60 shadow-sm bg-card py-0 cursor-text"
        onClick={() => textareaRef.current?.focus()}
      >
        <div className="flex items-end gap-2 p-3">
          <Textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="输入消息，Shift+Enter 换行..."
            className="flex-1 min-h-[60px] max-h-[200px] resize-none border-0 p-0 shadow-none focus-visible:ring-0 focus-visible:ring-offset-0 text-[15px]"
            rows={2}
            disabled={isStreaming}
          />
          {isStreaming ? (
            <Button
              size="icon"
              onClick={(e) => {
                e.stopPropagation();
                stopStreaming();
              }}
              className="h-9 w-9 shrink-0 rounded-full bg-destructive hover:bg-destructive/90"
            >
              <Square className="h-4 w-4" />
            </Button>
          ) : (
            <Button
              size="icon"
              onClick={(e) => {
                e.stopPropagation();
                handleSend();
              }}
              disabled={!input.trim() || isStreaming}
              className="h-9 w-9 shrink-0 rounded-full"
            >
              <Send className="h-4 w-4" />
            </Button>
          )}
        </div>
      </Card>
      <div className="mt-2 text-center text-xs text-muted-foreground">
        Enter 发送 · Shift+Enter 换行
      </div>
    </div>
  );

  return (
    <div className="flex h-full flex-col">
      {/* 顶部工具栏 */}
      <div className="h-14 shrink-0 border-b border-border flex items-center justify-between px-5">
        <div className="flex items-center gap-2 min-w-0">
          <Sparkles className="h-4 w-4 text-primary shrink-0" />
          <h2 className="text-sm font-medium truncate">
            {currentSession?.title || "新对话"}
          </h2>
          {currentSession?.useRag && (
            <Badge variant="secondary" className="text-xs shrink-0">
              <BookOpen className="h-3 w-3 mr-1" />
              RAG
            </Badge>
          )}
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={addSession}
            title="新建对话"
          >
            <Plus className="h-4 w-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={() => currentSessionId && clearSession(currentSessionId)}
            disabled={isEmpty}
            title="清空对话"
          >
            <Eraser className="h-4 w-4" />
          </Button>
          <Separator orientation="vertical" className="h-5 mx-1" />
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8 hover:text-destructive"
            onClick={() => currentSessionId && deleteSession(currentSessionId)}
            title="删除对话"
          >
            <Trash2 className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* 错误提示 */}
      {error && (
        <div className="px-5 py-2.5 bg-destructive/10 text-destructive text-sm flex items-center gap-2 shrink-0">
          <AlertCircle className="h-4 w-4" />
          <span className="truncate">{error}</span>
          <Button
            variant="ghost"
            size="sm"
            className="ml-auto h-6 shrink-0"
            onClick={() => setError(null)}
          >
            关闭
          </Button>
        </div>
      )}

      {isEmpty ? (
        /* 空态：欢迎语 + 输入框整体垂直居中（经典 AI Studio） */
        <div className="flex-1 min-h-0 flex flex-col items-center justify-center px-4 pb-8">
          <div className="w-full max-w-3xl flex flex-col items-center">
            <div className="flex flex-col items-center text-center mb-8">
              <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10 mb-4">
                <Bot className="h-7 w-7 text-primary" />
              </div>
              <h2 className="text-2xl font-semibold tracking-tight">
                有什么我能帮你的吗？
              </h2>
              <p className="text-muted-foreground mt-2 max-w-md text-sm leading-relaxed">
                选择模型后直接输入问题，开始对话
              </p>
            </div>
            <div className="w-full" onClick={() => setShowWelcome(false)}>
              {composer}
            </div>
            {sessions.length > 0 && (
              <div className="mt-6 text-center">
                <Button
                  variant="outline"
                  onClick={(e) => {
                    e.stopPropagation();
                    setShowWelcome(false);
                    setCurrentSession(sessions[0].id);
                    void loadMessages(sessions[0].id);
                  }}
                  className="text-sm"
                >
                  <BookOpen className="h-4 w-4 mr-2" />
                  查看历史对话 ({sessions.length})
                </Button>
              </div>
            )}
          </div>
        </div>
      ) : (
        <>
          {/* 有消息：消息区滚动 + 输入框贴底 */}
          <ScrollArea className="flex-1 min-h-0">
            <div className="mx-auto w-full max-w-3xl space-y-6 px-5 py-6">
              {currentSession!.messages.map((message) => (
                <div
                  key={message.id}
                  className={cn(
                    "flex gap-3 animate-message",
                    message.role === "user" && "flex-row-reverse"
                  )}
                >
                  <div
                    className={cn(
                      "flex h-8 w-8 shrink-0 items-center justify-center rounded-full shadow-sm",
                      message.role === "user" ? "bg-primary" : "bg-accent"
                    )}
                  >
                    {message.role === "user" ? (
                      <User className="h-4 w-4 text-primary-foreground" />
                    ) : (
                      <Bot className="h-4 w-4" />
                    )}
                  </div>

                  <div
                    className={cn(
                      "flex-1 max-w-[85%]",
                      message.role === "user" && "flex flex-col items-end"
                    )}
                  >
                    <Card
                      className={cn(
                        "border-0 shadow-sm",
                        message.role === "user"
                          ? "bg-primary text-primary-foreground rounded-2xl rounded-br-md"
                          : "bg-transparent border-0 shadow-none"
                      )}
                    >
                      <div
                        className={cn(
                          "px-4 py-3",
                          message.role === "user"
                            ? ""
                            : "prose prose-sm dark:prose-invert max-w-none"
                        )}
                      >
                        {message.role === "assistant" ? (
                          <>
                            <ReactMarkdown
                              remarkPlugins={[remarkGfm]}
                              components={{
                                code: ({ node, className, children, ...props }) => {
                                  const match = /language-(\w+)/.exec(className || "");
                                  const isInline = !match;
                                  return isInline ? (
                                    <code className={className} {...props}>
                                      {children}
                                    </code>
                                  ) : (
                                    <div className="relative group">
                                      <pre className="!mt-0 !mb-0">
                                        <code className={className} {...props}>
                                          {children}
                                        </code>
                                      </pre>
                                      <Button
                                        size="sm"
                                        variant="ghost"
                                        className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 h-8"
                                        onClick={() => {
                                          navigator.clipboard.writeText(String(children));
                                        }}
                                      >
                                        <Copy className="h-4 w-4" />
                                      </Button>
                                    </div>
                                  );
                                },
                              }}
                            >
                              {message.content}
                            </ReactMarkdown>

                            {message.kg?.subgraph && message.kg.subgraph.nodes.length > 0 && (
                              <div className="mt-3 border border-slate-200 rounded-md bg-slate-50 overflow-hidden">
                                <div className="text-xs text-slate-500 px-3 py-1.5 border-b bg-white flex items-center gap-1.5">
                                  <Brain className="h-3.5 w-3.5 text-violet-600" />
                                  图谱子图 ({message.kg.subgraph.nodes.length} 节点 / {message.kg.subgraph.edges.length} 关系)
                                </div>
                                <MiniGraph
                                  nodes={message.kg.subgraph.nodes}
                                  edges={message.kg.subgraph.edges}
                                  onNodeClick={(n) => setPopoverEntity(n.name)}
                                />
                              </div>
                            )}

                            {message.kg?.cited_entities && message.kg.cited_entities.length > 0 && (
                              <div className="mt-2 text-xs text-slate-500">
                                引用实体: {message.kg.cited_entities.join("、")}
                              </div>
                            )}

                            {message.kg?.sources && message.kg.sources.length > 0 && (
                              <div className="mt-3 border border-slate-200 rounded-md bg-white overflow-hidden">
                                <div className="text-xs text-slate-500 px-3 py-1.5 border-b bg-slate-50 flex items-center gap-1.5">
                                  <BookOpen className="h-3.5 w-3.5 text-blue-600" />
                                  引用文章 ({message.kg.sources.length})
                                </div>
                                <div className="divide-y divide-slate-100">
                                  {message.kg.sources.map((s, i) => (
                                    <a
                                      key={s.article_id}
                                      href={`/articles?highlight=${encodeURIComponent(
                                        s.entity_name || message.kg!.cited_entities[0] || "",
                                      )}&article=${s.article_id}`}
                                      target="_blank"
                                      rel="noopener noreferrer"
                                      className="block px-3 py-2 hover:bg-blue-50 transition group"
                                    >
                                      <div className="flex items-start justify-between gap-2">
                                        <span className="text-sm font-medium text-slate-800 group-hover:text-blue-700 line-clamp-1">
                                          [{i + 1}] {s.title}
                                        </span>
                                        {s.entity_name && (
                                          <Badge
                                            variant="outline"
                                            className="text-[10px] px-1.5 py-0 shrink-0 border-violet-200 text-violet-700 bg-violet-50"
                                          >
                                            via {s.entity_name}
                                          </Badge>
                                        )}
                                      </div>
                                      {s.snippet && (
                                        <p className="mt-1 text-xs text-slate-500 leading-relaxed line-clamp-2">
                                          …{highlightSnippet(s.snippet, s.entity_name || "")}…
                                        </p>
                                      )}
                                    </a>
                                  ))}
                                </div>
                              </div>
                            )}
                          </>
                        ) : (
                          <p className="whitespace-pre-wrap text-sm">{message.content}</p>
                        )}
                      </div>
                    </Card>

                    <div
                      className={cn(
                        "flex items-center gap-1 mt-1 text-xs text-muted-foreground",
                        message.role === "user" && "justify-end"
                      )}
                    >
                      <span>
                        {new Date(message.createdAt).toLocaleTimeString("zh-CN", {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </span>
                      {message.role === "assistant" && (
                        <>
                          <Separator orientation="vertical" className="h-3" />
                          <button className="p-1 hover:text-foreground">
                            <ThumbsUp className="h-3 w-3" />
                          </button>
                          <button
                            className="p-1 hover:text-foreground"
                            onClick={() => {
                              navigator.clipboard.writeText(message.content);
                            }}
                          >
                            <Copy className="h-3 w-3" />
                          </button>
                          <button className="p-1 hover:text-foreground">
                            <RotateCcw className="h-3 w-3" />
                          </button>
                          <button className="p-1 hover:text-foreground">
                            <Star className="h-3 w-3" />
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                </div>
              ))}

              {isStreaming && (
                <div className="flex gap-3 animate-message">
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent">
                    <Bot className="h-4 w-4" />
                  </div>
                  <div className="flex items-center gap-2 text-muted-foreground text-sm">
                    <Bot className="h-4 w-4 animate-pulse" />
                    <span>生成中...</span>
                  </div>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>
          </ScrollArea>

          <div className="shrink-0 border-t border-border py-4">
            {composer}
          </div>
        </>
      )}

      {popoverEntity && (
        <EntitySourcePopover
          entityName={popoverEntity}
          onClose={() => setPopoverEntity(null)}
          onJumpToArticle={(id) => {
            const name = popoverEntity;
            setPopoverEntity(null);
            window.location.href = `/articles?highlight=${encodeURIComponent(name)}&article=${id}`;
          }}
        />
      )}
    </div>
  );
}
