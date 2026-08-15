import { NextResponse } from "next/server";

// 使用服务端变量，不会暴露到浏览器
const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8500";

// 获取某会话的历史消息
export async function GET(
  request: Request,
  { params }: { params: Promise<{ sessionId: string }> }
) {
  try {
    const { sessionId } = await params;
    const res = await fetch(
      `${BACKEND_URL}/chat/sessions/${encodeURIComponent(sessionId)}/messages`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        cache: "no-store",
      }
    );
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    console.error("Session messages API error:", error);
    return NextResponse.json(
      { error: "Session messages failed" },
      { status: 500 }
    );
  }
}