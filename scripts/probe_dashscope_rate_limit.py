"""
阿里云百炼 (DashScope) 模型最大并发数与限流阶梯探测脚本
支持自动探测 MaaS 工作空间端点与官方标准端点，极简负载省 Token，遇到 429/403 自动安全早停。
"""

import os
import sys
import time
import math
import json
import asyncio
import argparse
from typing import Dict, Any, List, Optional, Tuple

import httpx

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DEFAULT_MODEL = "qwen-plus-2025-07-14"
WORKSPACE_BASE_URL = "https://ws-kcf6twfk4vfy5l1e.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
OFFICIAL_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _extract_limit_headers(headers: httpx.Headers) -> Dict[str, str]:
    interesting = {}
    for k, v in headers.items():
        lk = k.lower()
        if any(tok in lk for tok in ("limit", "quota", "retry", "dashscope", "ratelimit")):
            interesting[k] = v
    return interesting


def _classify_error(status_code: Optional[int], body_text: str) -> str:
    low = (body_text or "").lower()
    if status_code == 403 or any(m in low for m in ("quota", "arrearage", "insufficient", "allocationquota", "free tier")):
        return "quota_err"
    if status_code == 429 or any(m in low for m in ("429", "rate limit", "too many requests", "burst", "throttl")):
        return "rate_limited"
    return "other_err"


async def _single_call(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    model: str,
) -> Dict[str, Any]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "只回复1个字：好"}],
        "max_tokens": 4,
        "temperature": 0,
    }
    t0 = time.perf_counter()
    try:
        resp = await client.post(url, headers=headers, json=payload)
        latency = time.perf_counter() - t0
        limit_headers = _extract_limit_headers(resp.headers)
        if resp.status_code == 200:
            data = resp.json()
            usage = data.get("usage") or {}
            total_tokens = int(usage.get("total_tokens") or 0)
            return {
                "status": "ok",
                "http_status": 200,
                "latency": latency,
                "total_tokens": total_tokens,
                "limit_headers": limit_headers,
                "error_msg": None,
            }
        body_brief = " ".join((resp.text or "").split())[:220]
        kind = _classify_error(resp.status_code, body_brief)
        return {
            "status": kind,
            "http_status": resp.status_code,
            "latency": latency,
            "total_tokens": 0,
            "limit_headers": limit_headers,
            "error_msg": f"HTTP {resp.status_code}: {body_brief}",
        }
    except Exception as exc:
        latency = time.perf_counter() - t0
        err_text = f"{type(exc).__name__}: {str(exc)}"
        return {
            "status": _classify_error(None, err_text),
            "http_status": None,
            "latency": latency,
            "total_tokens": 0,
            "limit_headers": {},
            "error_msg": err_text[:220],
        }


async def _probe_endpoint(
    client: httpx.AsyncClient,
    candidates: List[str],
    api_key: str,
    model: str,
) -> Tuple[Optional[str], Dict[str, str], Optional[Dict[str, Any]]]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    last_result = None
    for base in candidates:
        chat_url = base.rstrip("/") + "/chat/completions"
        res = await _single_call(client, chat_url, headers, model)
        last_result = res
        if res["status"] == "ok":
            return base, res["limit_headers"], res
        print(f"⚠️ 端点探测未通过 [{base}] -> {res['error_msg']}")
    return None, {}, last_result


def _percentile_95(values: List[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))
    return ordered[idx]


async def run_probe(
    model: str,
    levels: List[int],
    cooldown: float,
    base_url: Optional[str] = None,
) -> int:
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        print("❌ 未检测到环境变量 DASHSCOPE_API_KEY，无法执行探测。")
        return 1

    candidates = [base_url] if base_url else [WORKSPACE_BASE_URL, OFFICIAL_BASE_URL]

    limits = httpx.Limits(max_connections=100, max_keepalive_connections=100)
    async with httpx.AsyncClient(limits=limits, timeout=45.0) as client:
        print("=" * 78)
        print(f"🔎 [百炼并发与限流探测] 目标模型: {model} | 压测阶梯: {levels}")
        print("=" * 78)

        active_base, init_headers, first_res = await _probe_endpoint(client, candidates, api_key, model)
        if not active_base:
            print(f"❌ 端点探测全部失败，最后错误: {(first_res or {}).get('error_msg')}")
            return 2

        total_tokens_consumed = int((first_res or {}).get("total_tokens") or 0)
        print(f"✅ 命中可用端点: {active_base}")
        if init_headers:
            print(f"📋 响应限流头: {json.dumps(init_headers, ensure_ascii=False)}")
        else:
            print("📋 响应限流头: (服务端未返回显式 X-RateLimit-* 头部)")

        chat_url = active_base.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        max_zero_rl_level = 0
        first_rl_level: Optional[int] = None
        aborted_by_quota = False
        summary_rows: List[Dict[str, Any]] = []

        print("\n并发N | 成功 | 429限流 | 403配额 | 其他错 | 均值延迟 | P95延迟 | 本轮耗时 | 吞吐(req/s)")
        print("-" * 78)

        for level in levels:
            if cooldown > 0:
                await asyncio.sleep(cooldown)

            round_t0 = time.perf_counter()
            tasks = [
                asyncio.create_task(_single_call(client, chat_url, headers, model))
                for _ in range(level)
            ]
            results = await asyncio.gather(*tasks)
            round_elapsed = max(0.001, time.perf_counter() - round_t0)

            ok_cnt = sum(1 for r in results if r["status"] == "ok")
            rl_cnt = sum(1 for r in results if r["status"] == "rate_limited")
            q_cnt = sum(1 for r in results if r["status"] == "quota_err")
            other_cnt = sum(1 for r in results if r["status"] == "other_err")
            latencies = [r["latency"] for r in results if r["status"] == "ok"]
            avg_lat = (sum(latencies) / len(latencies)) if latencies else 0.0
            p95_lat = _percentile_95(latencies)
            rps = ok_cnt / round_elapsed
            round_tokens = sum(int(r.get("total_tokens") or 0) for r in results)
            total_tokens_consumed += round_tokens

            first_err = next((r["error_msg"] for r in results if r["error_msg"]), None)
            extra_headers = next((r["limit_headers"] for r in results if r.get("limit_headers")), {})

            print(
                f"{level:5d} | {ok_cnt:4d} | {rl_cnt:7d} | {q_cnt:7d} | {other_cnt:6d} | "
                f"{avg_lat:6.2f}s | {p95_lat:5.2f}s | {round_elapsed:6.2f}s | {rps:9.2f}"
            )
            if first_err:
                print(f"      └─ 首条异常: {first_err}")
            if extra_headers and extra_headers != init_headers:
                print(f"      └─ 新限流头: {json.dumps(extra_headers, ensure_ascii=False)}")

            summary_rows.append({
                "concurrency": level,
                "ok": ok_cnt,
                "rate_limited": rl_cnt,
                "quota_err": q_cnt,
                "other_err": other_cnt,
                "avg_latency": round(avg_lat, 3),
                "p95_latency": round(p95_lat, 3),
                "round_elapsed": round(round_elapsed, 3),
                "rps": round(rps, 2),
                "first_err": first_err,
            })

            if q_cnt > 0:
                aborted_by_quota = True
                print("🛑 检测到配额耗尽 (403/quota)，停止继续加压。")
                break

            if rl_cnt > 0:
                first_rl_level = level
                print(f"⚠️ 在并发 {level} 首次触发 429 限流，触发早停保护。")
                break

            if other_cnt == 0:
                max_zero_rl_level = level

        recommended = max(1, int(max_zero_rl_level * 0.75)) if max_zero_rl_level > 0 else 2
        print("\n" + "=" * 78)
        print("📊 [探测总结]")
        print(f" - 探测模型          : {model}")
        print(f" - 使用端点          : {active_base}")
        print(f" - 最大零限流并发    : {max_zero_rl_level}")
        if first_rl_level is not None:
            print(f" - 首次触发限流并发  : {first_rl_level}")
        elif aborted_by_quota:
            print(" - 首次触发限流并发  : 因配额耗尽提前终止")
        else:
            print(f" - 首次触发限流并发  : 最高测至 {levels[-1]} 并发仍未触发 429")
        print(f" - 建议评测安全并发  : {recommended}（按最大稳定档位的约 75% 预留余量）")
        print(f" - 累计消耗 Token    : {total_tokens_consumed}")
        print("=" * 78)
        return 0


def main():
    parser = argparse.ArgumentParser(description="阿里云百炼模型并发与限流阶梯探测脚本")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"目标模型名（默认 {DEFAULT_MODEL}）")
    parser.add_argument("--levels", default="4,8,12,16,20,24,32,40,48", help="逗号分隔的并发阶梯")
    parser.add_argument("--cooldown", type=float, default=2.0, help="每轮间冷却秒数（默认 2.0s）")
    parser.add_argument("--base-url", default=None, help="可选自定义 compatible-mode base_url")
    args = parser.parse_args()

    levels = [int(x.strip()) for x in args.levels.split(",") if x.strip()]
    sys.exit(asyncio.run(run_probe(model=args.model, levels=levels, cooldown=args.cooldown, base_url=args.base_url)))


if __name__ == "__main__":
    main()
