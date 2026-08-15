import { NextResponse } from "next/server";

// 使用服务端变量，不会暴露到浏览器
const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8500";

// 删除会话
export async function DELETE(
  request: Request,
  { params }: { params: Promise<{ sessionId: string }> }
) {
  try {
    const { sessionId } = await params;
    const res = await fetch(
      `${BACKEND_URL}/chat/sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
      }
    );
    if (res.status === 204 || res.status === 200) {
      return new NextResponse(null, { status: 204 });
    }
    const data = await res.json().catch(() => ({}));
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    console.error("Delete session API error:", error);
    return NextResponse.json({ error: "Delete session failed" }, { status: 500 });
  }
}