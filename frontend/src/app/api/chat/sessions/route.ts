import { NextResponse } from "next/server";

// 使用服务端变量，不会暴露到浏览器
const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8500";

// 获取对话会话列表
export async function GET() {
  try {
    const res = await fetch(`${BACKEND_URL}/chat/sessions`, {
      method: "GET",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });
    const data = await res.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error("Sessions API error:", error);
    return NextResponse.json({ error: "Sessions failed" }, { status: 500 });
  }
}

// 创建对话会话
export async function POST(request: Request) {
  try {
    const body = await request.json();
    const res = await fetch(`${BACKEND_URL}/chat/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    console.error("Create session API error:", error);
    return NextResponse.json({ error: "Create session failed" }, { status: 500 });
  }
}