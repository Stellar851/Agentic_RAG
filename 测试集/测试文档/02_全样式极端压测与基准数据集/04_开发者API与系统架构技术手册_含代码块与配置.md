# 星网智云企业级微服务网关与分布式缓存开发手册

## 一、 系统架构概览

本手册规范了在生产环境中基于 Python FastAPI 框架、Pydantic V2 契约校验以及 Redis 7.x 哨兵集群搭建高性能异步 AI 服务的标准化步骤。

### 1.1 技术栈选型
- **Web 核心框架**：`FastAPI >= 0.110.0`，搭配 `uvicorn[standard] >= 0.28.0`（基于 uvloop 事件循环）；
- **数据序列化**：`Pydantic >= 2.6.0`（Rust 编写核心，解析性能较 V1 提升 5~10 倍）；
- **缓存与状态机**：`redis.asyncio`，采用连接池模式（ConnectionPool）；
- **通信协议**：支持原生 REST HTTP 与 SSE（Server-Sent Events）长连接双向通道。

---

## 二、 核心代码实现：异步微服务与流式响应网关

下述 Python 代码演示了标准生产环境中的流式 SSE 问答接口实现，包含连接池优雅关闭与异常重试逻辑：

```python
import asyncio
import json
from typing import AsyncGenerator
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

app = FastAPI(title="StarNet AI Gateway", version="2.5.0")

# Redis 连接池全局单例
redis_pool: aioredis.ConnectionPool = None

class ChatRequest(BaseModel):
    session_id: str = Field(..., description="用户唯一会话ID", min_length=8)
    query: str = Field(..., description="用户提问内容", max_length=1000)
    top_k: int = Field(default=5, ge=1, le=20, description="召回知识切片上限")
    temperature: float = Field(default=0.3, ge=0.0, le=1.0)

@app.on_event("startup")
async def startup_event():
    global redis_pool
    redis_pool = aioredis.ConnectionPool.from_url(
        "redis://:StrongPassword123@127.0.0.1:6379/0",
        max_connections=50,
        decode_responses=True
    )
    print("SUCCESS: Redis connection pool initialized.")

@app.on_event("shutdown")
async def shutdown_event():
    global redis_pool
    if redis_pool:
        await redis_pool.disconnect()
        print("INFO: Redis connection pool disconnected.")

async def stream_ai_generator(session_id: str, query: str) -> AsyncGenerator[str, None]:
    """模拟生成器：基于 SSE 规范逐字推送数据"""
    try:
        # 发送建立连接心跳
        yield f"event: connect\ndata: {json.dumps({'status': 'ready'})}\n\n"
        
        simulated_tokens = ["您好，", "已接收到", "您的业务", "咨询请求：", f"[{query}]。", "系统正在为您检索最新知识库..."]
        for token in simulated_tokens:
            await asyncio.sleep(0.05)
            payload = json.dumps({"token": token, "session_id": session_id}, ensure_ascii=False)
            yield f"event: message\ndata: {payload}\n\n"
            
        # 发送结束标记
        yield f"event: finish\ndata: {json.dumps({'status': 'completed'})}\n\n"
    except asyncio.CancelledError:
        print(f"WARNING: Client disconnected prematurely: {session_id}")
    except Exception as e:
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

@app.post("/api/v1/chat/stream", summary="流式长连接智能会话接口")
async def chat_stream_endpoint(req: ChatRequest, request: Request):
    if not req.query.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Query cannot be empty")
    return StreamingResponse(
        stream_ai_generator(req.session_id, req.query),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no" # 禁用 Nginx 缓冲
        }
    )
```

---

## 三、 Redis 分布式会话与限流配置

生产环境中必须在 `redis.conf` 中开启持久化并配置最大内存淘汰机制：

```yaml
# redis.conf 生产推荐核心配置参数
maxmemory 4gb
maxmemory-policy allkeys-lru
save 900 1
save 300 10
save 60 10000
appendonly yes
appendfsync everysec
timeout 300
tcp-keepalive 60
```

### 3.1 常用维护命令
- 查看当前客户端连接情况：
  ```bash
  redis-cli -a StrongPassword123 info clients
  ```
- 检查大 Key 与内存占用：
  ```bash
  redis-cli -a StrongPassword123 --bigkeys
  ```
