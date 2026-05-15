"""生产告警：Slack 推送 + API 重试"""
import os
import time
import asyncio
import logging

_log = logging.getLogger(__name__)

# ── Slack 告警 ──

SLACK_ALERT_URL = os.environ.get("SLACK_ALERT_WEBHOOK", "").strip()
_last_alert: dict = {}  # key → timestamp, 冷却去重


async def _send_slack(text: str) -> bool:
    """发送 Slack 消息，30s 同内容冷却"""
    if not SLACK_ALERT_URL:
        return False
    key = text[:80]
    now = time.time()
    if now - _last_alert.get(key, 0) < 30:
        return False
    _last_alert[key] = now
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.post(SLACK_ALERT_URL, json={"text": text},
                              timeout=aiohttp.ClientTimeout(total=5)) as r:
                return r.status == 200
    except Exception:
        return False


def alert_sync(text: str):
    """同步发送告警（在非 async 上下文中使用）"""
    try:
        asyncio.get_event_loop().create_task(_send_slack(text))
    except Exception:
        pass


async def alert_redis_down():
    await _send_slack("🚨 [Shark] Redis 连接断开！tick 循环暂停新开仓")


async def alert_redis_recovered():
    await _send_slack("✅ [Shark] Redis 连接恢复")


async def alert_gate_api_error(method: str, path: str, error: str, retries: int = 0):
    await _send_slack(f"⚠️ [Shark] Gate.io API 错误 {method} {path} (重试{retries}次): {error[:100]}")


async def alert_gate_api_recovered():
    await _send_slack("✅ [Shark] Gate.io API 恢复")


async def alert_position_mismatch(sym: str, mem_side: str, ex_side: str):
    await _send_slack(f"🔴 [Shark] 持仓对账不一致 {sym}: 内存={mem_side} 交易所={ex_side}")


async def alert_consecutive_errors(count: int):
    await _send_slack(f"🚨 [Shark] 连续 {count} 次订单失败，实盘熔断！")


async def alert_large_loss(sym: str, pnl: float, reason: str):
    await _send_slack(f"💀 [Shark] 大额亏损 {sym} ${pnl:.2f} ({reason})")


async def alert_balance_low(balance: float, threshold: float):
    await _send_slack(f"⚠️ [Shark] 余额不足 ${balance:.2f} < ${threshold:.2f}")


async def alert_system_start():
    await _send_slack("🟢 [Shark] 系统启动 paper模式 初始资金$500")


# ── API 重试 ──

def api_retry(func, *args, max_retries: int = 3, base_delay: float = 1.0, **kwargs):
    """API 调用带指数退避重试。func 抛异常时重试，全部失败抛最后一次异常。"""
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
    raise last_err
