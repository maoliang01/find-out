import { NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8500";

async function forward(request: Request, method: "PUT" | "POST") {
  try {
    const body = await request.json();
    const suffix = method === "POST" ? "/classify" : "";
    const response = await fetch(`${BACKEND_URL}/scrape/routes${suffix}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "站点路由请求失败" },
      { status: 500 },
    );
  }
}

export async function GET() {
  try {
    const response = await fetch(`${BACKEND_URL}/scrape/routes`, { cache: "no-store" });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "读取站点路由失败" },
      { status: 500 },
    );
  }
}

export async function PUT(request: Request) {
  return forward(request, "PUT");
}

export async function POST(request: Request) {
  return forward(request, "POST");
}
