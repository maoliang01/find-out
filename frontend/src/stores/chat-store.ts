import { create } from "zustand";
import { subscribeWithSelector } from "zustand/middleware";
import type { ChatSession, Message, ModelConfigAPI } from "@/types";
import { sendChat, streamChat, getSessions, createSession, deleteSession, getSessionMessages } from "@/lib/api";
import { useSettingsStore } from "./settings-store";

// 模型配置类型（内部使用，camelCase）
interface ModelConfig {
  id: string;
  name: string;
  type: string;
  baseUrl: string;
  apiKey?: string;
  modelName?: string;
}

const API_BASE = "/api";  // 使用同源 API 代理

interface ModelOption {
  id: string;
  name: string;
  provider?: string;
  isFavorite?: boolean;
}

interface ChatStore {
  sessions: ChatSession[];
  currentSessionId: string | null;
  models: ModelOption[];
  selectedModel: string;
  isStreaming: boolean;
  isLoading: boolean;
  error: string | null;
  abortController: AbortController | null;
  kgEnhanced: boolean;
  isInitialized: boolean;

  // Actions
  setCurrentSession: (id: string | null) => void;
  addSession: () => void;
  deleteSession: (id: string) => void;
  addMessage: (sessionId: string, message: Message) => void;
  updateLastMessage: (sessionId: string, content: string) => void;
  setStreaming: (streaming: boolean) => void;
  setKgEnhanced: (v: boolean) => void;
  setModel: (modelId: string) => void;
  updateSessionTitle: (id: string, title: string) => void;
  toggleRag: (sessionId: string) => void;
  clearSession: (sessionId: string) => void;
  setError: (error: string | null) => void;
  stopStreaming: () => void;
  isAborted: () => boolean;

  // 异步操作
  sendMessage: (sessionId: string, content: string) => Promise<void>;
  loadModels: () => Promise<void>;
  loadSessions: () => Promise<void>;
  loadMessages: (sessionId: string) => Promise<void>;
}

export const useChatStore = create<ChatStore>()(
  subscribeWithSelector((set, get) => ({
    sessions: [],
    currentSessionId: null,
    models: [],
    selectedModel: "",
    isStreaming: false,
    isLoading: false,
    error: null,
    abortController: null,
    kgEnhanced: false,
    isInitialized: false,

    setCurrentSession: (id) => set({ currentSessionId: id }),

    addSession: async () => {
      const state = get();
      let newSession: ChatSession;
      try {
        const created = await createSession({
          model: state.selectedModel || "",
        });
        newSession = {
          id: created.id,
          title: created.title,
          model: created.model,
          messages: [],
          createdAt: new Date(created.created_at),
          updatedAt: new Date(created.updated_at),
          useRag: created.use_rag,
        };
      } catch (e) {
        console.error("创建会话失败，回退到本地临时会话:", e);
        // 后端不可用时回退到本地临时会话，避免页面无法使用
        newSession = {
          id: `local-${Date.now().toString()}`,
          title: "新对话",
          model: state.selectedModel || "",
          messages: [],
          createdAt: new Date(),
          updatedAt: new Date(),
          useRag: false,
        };
      }
      set((state) => ({
        sessions: [newSession, ...state.sessions],
        currentSessionId: newSession.id,
      }));
    },

    deleteSession: async (id) => {
      // 本地先移除，后端删除失败仅告警，不阻塞交互
      const newSessions = get().sessions.filter((s) => s.id !== id);
      set((state) => ({
        sessions: newSessions,
        currentSessionId:
          state.currentSessionId === id
            ? newSessions[0]?.id || null
            : state.currentSessionId,
      }));
      if (!id.startsWith("local-")) {
        try {
          await deleteSession(id);
        } catch (e) {
          console.error("删除后端会话失败:", e);
        }
      }
    },

    addMessage: (sessionId, message) =>
      set((state) => ({
        sessions: state.sessions.map((s) =>
          s.id === sessionId
            ? {
                ...s,
                messages: [...s.messages, message],
                updatedAt: new Date(),
                title:
                  s.messages.length === 0 && message.role === "user"
                    ? message.content.slice(0, 30) + (message.content.length > 30 ? "..." : "")
                    : s.title,
              }
            : s
        ),
      })),

    updateLastMessage: (sessionId, content) =>
      set((state) => ({
        sessions: state.sessions.map((s) =>
          s.id === sessionId
            ? {
                ...s,
                messages: s.messages.map((m, i) =>
                  i === s.messages.length - 1 ? { ...m, content } : m
                ),
                updatedAt: new Date(),
              }
            : s
        ),
      })),

    setStreaming: (streaming) => set({ isStreaming: streaming }),
    setKgEnhanced: (v) => set({ kgEnhanced: v }),

    setModel: (modelId) => set({ selectedModel: modelId }),

    updateSessionTitle: (id, title) =>
      set((state) => ({
        sessions: state.sessions.map((s) =>
          s.id === id ? { ...s, title, updatedAt: new Date() } : s
        ),
      })),

    toggleRag: (sessionId) =>
      set((state) => ({
        sessions: state.sessions.map((s) =>
          s.id === sessionId ? { ...s, useRag: !s.useRag } : s
        ),
      })),

    clearSession: (sessionId) =>
      set((state) => ({
        sessions: state.sessions.map((s) =>
          s.id === sessionId
            ? {
                ...s,
                messages: [],
                title: "新对话",
                updatedAt: new Date(),
              }
            : s
        ),
      })),

    setError: (error) => set({ error }),

    stopStreaming: () => {
      const { abortController } = get();
      if (abortController) {
        abortController.abort();
        set({ abortController: null, isStreaming: false });
      }
    },

    isAborted: () => {
      const { abortController } = get();
      return abortController ? abortController.signal.aborted : false;
    },

    /**
     * 发送消息并获取 AI 响应
     */
    sendMessage: async (sessionId, content) => {
      const state = get();
      const session = state.sessions.find((s) => s.id === sessionId);
      if (!session) return;

      // 如果模型未加载，先加载
      const settingsStore = useSettingsStore.getState();
      if (!settingsStore.models.length) {
        console.log("模型未加载，等待同步...");
        await settingsStore.syncModelsFromBackend();
      }

      // 创建 AbortController 用于取消请求
      const abortController = new AbortController();
      set({ abortController, error: null });

      // 从 settings store 获取所选模型的完整配置
      const settingsModels = useSettingsStore.getState().models;
      console.log("=== 发送消息 ===");
      console.log("selectedModel (store):", state.selectedModel);
      console.log("settingsModels:", settingsModels.map(m => ({ id: m.id, name: m.name })));

      const selectedModelConfig = settingsModels.find(
        (m: ModelConfig) => m.id === state.selectedModel
      );
      console.log("找到的模型配置:", selectedModelConfig);

      // 添加用户消息
      const userMessage: Message = {
        id: Date.now().toString(),
        role: "user",
        content,
        createdAt: new Date(),
      };
      get().addMessage(sessionId, userMessage);

      set({ isStreaming: true });

      try {
        // 构建消息历史 (用于上下文)
        const messages = session.messages.map((m) => ({
          role: m.role,
          content: m.content,
        }));
        messages.push({ role: "user", content });

        // 添加空的 AI 消息占位
        const assistantMessageId = (Date.now() + 1).toString();
        get().addMessage(sessionId, {
          id: assistantMessageId,
          role: "assistant",
          content: "",
          createdAt: new Date(),
          model: state.selectedModel,
        });

        console.log("=== 发送调试信息 ===");
        console.log("selectedModel:", state.selectedModel);
        console.log("settingsModels:", settingsModels.map(m => ({ id: m.id, name: m.name })));
        console.log("selectedModelConfig:", selectedModelConfig);

        // 构造模型配置
        const modelConfig = selectedModelConfig
          ? {
              name: selectedModelConfig.name,
              type: selectedModelConfig.type,
              base_url: selectedModelConfig.baseUrl,
              api_key: selectedModelConfig.apiKey || "",
              model_name: selectedModelConfig.modelName || "",
            }
          : undefined;

        let fullContent = "";

        try {
          // 非流式请求
          const requestData = {
            model_id: state.selectedModel,
            messages,
            stream: false,
            model_config: modelConfig,
            // 服务端据此将消息写入本机数据库（KG 增强分支不携带，故不落库）
            session_id: sessionId.startsWith("local-") ? undefined : sessionId,
          };

          const response = await sendChat(requestData, abortController.signal);
          fullContent = response.content || "";
          get().updateLastMessage(sessionId, fullContent);
        } catch (chatErr) {
          if (abortController.signal.aborted) {
            console.log("请求已被用户取消");
            get().updateLastMessage(sessionId, fullContent + "\n\n[已停止生成]");
            return;
          }
          console.error("请求失败:", chatErr);
          // 备用：尝试流式请求
          try {
            for await (const chunk of streamChat({
              model_id: state.selectedModel,
              messages,
              stream: true,
              model_config: modelConfig,
              session_id: sessionId.startsWith("local-") ? undefined : sessionId,
            }, abortController.signal)) {
              if (abortController.signal.aborted) {
                console.log("流式请求已被用户取消");
                get().updateLastMessage(sessionId, fullContent + "\n\n[已停止生成]");
                return;
              }
              fullContent += chunk;
              get().updateLastMessage(sessionId, fullContent);
            }
          } catch {
            if (abortController.signal.aborted) {
              get().updateLastMessage(sessionId, fullContent + "\n\n[已停止生成]");
              return;
            }
            throw chatErr;
          }
        }
      } catch (err) {
        const errorMessage = err instanceof Error ? err.message : "发送消息失败";
        set({ error: errorMessage });
        get().updateLastMessage(sessionId, `[错误] ${errorMessage}`);
      } finally {
        set({ isStreaming: false, abortController: null });
      }
    },

    /**
     * 从设置页面加载模型列表
     */
    loadModels: async () => {
      const state = get();

      // 先从后端同步模型配置
      await useSettingsStore.getState().syncModelsFromBackend();

      // 从 settings store 获取 LLM 和多模态类型的模型
      const settingsModels = useSettingsStore.getState().models;
      const llmModels = settingsModels.filter((m: ModelConfig) => m.type === "llm" || m.type === "multimodal");

      console.log("=== 加载模型 ===");
      console.log("从 settingsStore 获取到模型:", llmModels.map(m => ({ id: m.id, name: m.name })));

      if (llmModels.length === 0) {
        console.warn("未找到可用的 LLM 模型，请在设置中添加");
        return;
      }

      // 同步模型到后端
      try {
        await fetch(`${API_BASE}/models/sync`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(llmModels.map((m: ModelConfig) => ({
            name: m.name,
            type: m.type,
            base_url: m.baseUrl,
            api_key: m.apiKey || "",
            model_name: m.modelName || "",
          }))),
        });
      } catch (e) {
        console.error("同步模型到后端失败:", e);
      }

      // 构建模型选项，使用后端返回的 id
      const modelOptions: ModelOption[] = llmModels.map((m: ModelConfig) => ({
        id: m.id,
        name: m.name,
        provider: m.modelName || "",
        isFavorite: true,
      }));

      // 保留已选中的模型（如果存在），否则选择第一个
      const validSelected = state.selectedModel &&
        modelOptions.some(m => m.id === state.selectedModel);
      const newSelectedModel = validSelected
        ? state.selectedModel
        : modelOptions[0]?.id || "";

      console.log("模型选项:", modelOptions);
      console.log("选中的模型:", newSelectedModel);

      set({
        models: modelOptions,
        selectedModel: newSelectedModel,
        isInitialized: true,
      });
    },

    /**
     * 从后端本机数据库加载会话列表（不含消息，消息按需懒加载）。
     */
    loadSessions: async () => {
      try {
        const remote = await getSessions();
        const sessions: ChatSession[] = remote.map((s) => ({
          id: s.id,
          title: s.title,
          model: s.model,
          messages: [],
          createdAt: new Date(s.created_at),
          updatedAt: new Date(s.updated_at),
          useRag: s.use_rag,
        }));
        // 清空本地会话，以服务端为权威；若当前有会话尚未落库则保留
        const current = get().currentSessionId;
        const nextCurrent =
          current && sessions.some((s) => s.id === current)
            ? current
            : sessions[0]?.id ?? null;
        set({
          sessions,
          currentSessionId: nextCurrent,
        });
        // 无任何会话时自动创建首个会话，保证打开页面即可开聊
        if (get().sessions.length === 0) {
          await get().addSession();
        }
      } catch (e) {
        console.error("加载会话列表失败:", e);
      }
    },

    /**
     * 懒加载某会话的历史消息（从后端本机数据库读取）。
     */
    loadMessages: async (sessionId) => {
      if (sessionId.startsWith("local-")) return;
      try {
        const remote = await getSessionMessages(sessionId);
        const messages: Message[] = remote
          .filter((m) => m.role === "user" || m.role === "assistant" || m.role === "system")
          .map((m) => ({
            id: m.id,
            role: m.role as Message["role"],
            content: m.content,
            createdAt: new Date(m.created_at),
            model: m.model || undefined,
          }));
        // 若该会话消息已加载过（含本地新增），避免覆盖正在编辑的本地消息
        set((state) => ({
          sessions: state.sessions.map((s) =>
            s.id === sessionId && s.messages.length === 0
              ? { ...s, messages }
              : s
          ),
        }));
      } catch (e) {
        console.error(`加载会话 ${sessionId} 消息失败:`, e);
      }
    },
  }))
);

// 监听 settingsStore.models 的变化，自动更新 chatStore
useSettingsStore.subscribe(
  (state) => state.models,
  (models: ModelConfig[]) => {
    const chatStore = useChatStore.getState();
    if (chatStore.isInitialized && models.length > 0) {
      // 从 settingsStore 的模型列表中找出对应的模型，更新 chatStore
      const llmModels = models.filter((m) => m.type === "llm");
      const modelOptions: ModelOption[] = llmModels.map((m) => ({
        id: m.id,
        name: m.name,
        provider: m.modelName || "",
        isFavorite: true,
      }));

      const validSelected = chatStore.selectedModel &&
        modelOptions.some(m => m.id === chatStore.selectedModel);
      const newSelectedModel = validSelected
        ? chatStore.selectedModel
        : modelOptions[0]?.id || "";

      console.log("=== 模型从 settingsStore 更新 ===");
      console.log("模型列表:", modelOptions);
      console.log("选中:", newSelectedModel);

      useChatStore.setState({
        models: modelOptions,
        selectedModel: newSelectedModel,
      });
    }
  }
);
