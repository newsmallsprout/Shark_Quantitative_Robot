"""Shark 2.0 AI 策略引擎 v2 — 多重信号采集 + 精准目标价"""

import asyncio, json, os, time
import aiohttp

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "sk-122b190c357440a997c28197b12f221d")

SYSTEM_PROMPT = """你是全球顶级加密货币量化交易AI。分析市场给出精确的多层交易计划。

输出JSON：
{
  "direction": "LONG"|"SHORT",
  "confidence": 0-100,
  "entry_price": 数字,
  "targets": [
    {"price": 数字, "action": "pyramid_add", "ratio": 0.2, "reason": "突破加仓"},
    {"price": 数字, "action": "take_profit", "ratio": 0.5, "reason": "阻力区"},
    {"price": 数字, "action": "take_profit", "ratio": 0.5, "reason": "终极目标"}
  ],
  "stop_loss": 数字,
  "add_zone": {"price": 数字, "condition": "缩量回调补仓"},
  "reduce_zone": {"price": 数字, "condition": "放量暴跌减仓"},
  "supports": [数字, 数字],
  "resistances": [数字, 数字],
  "risk_reward": 数字,
  "reasoning": "分析摘要"
}

- targets按价格排序，action=pyramid_add或take_profit
- ratio是该层仓位比例，总和≤1.0
- stop_loss基于强支撑位（做多时<entry_price, 做空时>entry_price）
- supports/resistances基于筹码分布和均线
- confidence<50不交易"""


async def _fetch_timeframe_candles(sym: str, interval: str, limit: int = 20) -> list:
    """获取指定时间框架的K线"""
    try:
        gate_sym = sym.replace("/USDT", "_USDT")
        url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
        params = {"contract": gate_sym, "interval": interval, "limit": limit}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
        if not data or not isinstance(data, list):
            return []
        return [{"o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]),
                 "c": float(r["c"]), "v": float(r.get("sum", 0))} for r in data]
    except:
        return []


def _calc_atr(candles: list, period: int = 14) -> float:
    """计算ATR(14)"""
    if len(candles) < period + 1:
        return 0
    tr_list = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i-1]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_list.append(tr)
    return sum(tr_list[-period:]) / period


def _kline_summary(candles: list, label: str) -> str:
    """K线形态文字摘要"""
    if not candles or len(candles) < 3:
        return ""
    recent = candles[-5:]
    closes = [c["c"] for c in recent]
    highs = [c["h"] for c in recent]
    lows = [c["l"] for c in recent]
    
    # 趋势判断
    trend = "横盘"
    if closes[-1] > closes[0] * 1.02:
        trend = "上升"
    elif closes[-1] < closes[0] * 0.98:
        trend = "下降"
    
    # 波动范围
    range_pct = (max(highs) - min(lows)) / min(lows) * 100 if min(lows) > 0 else 0
    
    return f"{label}: {trend}趋势 波动{range_pct:.1f}% 收盘{closes[-1]:.4f} 高{max(highs):.4f} 低{min(lows):.4f}"


async def _fetch_orderbook(sym: str, depth: int = 10) -> str:
    """获取盘口深度摘要"""
    try:
        gate_sym = sym.replace("/USDT", "_USDT")
        url = f"https://api.gateio.ws/api/v4/futures/usdt/order_book"
        params = {"contract": gate_sym, "limit": depth}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
        
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        
        # 计算买卖墙强度
        bid_vol = sum(float(b[1]) for b in bids[:5]) if bids else 0
        ask_vol = sum(float(a[1]) for a in asks[:5]) if asks else 0
        
        best_bid = float(bids[0][0]) if bids else 0
        best_ask = float(asks[0][0]) if asks else 0
        spread = (best_ask - best_bid) / best_bid * 100 if best_bid > 0 else 0
        
        ratio = bid_vol / max(ask_vol, 1)
        if ratio > 2:
            wall = f"买盘强势({ratio:.1f}x) 支撑{best_bid:.2f}"
        elif ratio < 0.5:
            wall = f"卖盘强势({1/ratio:.1f}x) 阻力{best_ask:.2f}"
        else:
            wall = f"买卖均衡({ratio:.1f}x)"
        
        return f"盘口: {wall} 价差{spread:.3f}% 买一{best_bid:.2f} 卖一{best_ask:.2f}"
    except:
        return ""


async def get_ai_targets(symbol: str, price: float, funding_rate: float,
                         change_24h: float, volume_24h: float) -> dict:
    """增强版AI目标价——多时间框架 + ATR + 盘口"""
    
    # 并行获取三个时间框架
    k15, k1h, k4h = await asyncio.gather(
        _fetch_timeframe_candles(symbol, "15m", 30),
        _fetch_timeframe_candles(symbol, "1h", 24),
        _fetch_timeframe_candles(symbol, "4h", 24),
    )
    
    # 计算ATR
    atr_15m = _calc_atr(k15) if k15 else 0
    atr_1h = _calc_atr(k1h) if k1h else 0
    atr_pct = (atr_15m / price * 100) if price > 0 else 0
    
    # K线摘要
    k15_text = _kline_summary(k15, "15m")
    k1h_text = _kline_summary(k1h, "1h")
    k4h_text = _kline_summary(k4h, "4h")
    
    # 盘口
    ob_text = await _fetch_orderbook(symbol)
    
    # 构建增强版prompt
    prompt = f"""分析 {symbol} 给出精确多层交易计划：

## 实时数据
价格: {price:.6f}
24h涨跌: {change_24h:+.2f}%  24h成交量: {volume_24h:,.0f} USDT
资金费率: {funding_rate*100:+.4f}%

## 波动率
ATR(15m): {atr_15m:.6f} ({atr_pct:.2f}% of price)
ATR(1h): {atr_1h:.6f}

## 多级别K线形态
{k15_text}
{k1h_text}
{k4h_text}

## 盘口深度
{ob_text}

请综合以上所有数据，给出最优交易计划JSON。"""

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.25,
        "max_tokens": 1000,
    }
    
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "Content-Type": "application/json",
    }
    
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(DEEPSEEK_URL, json=payload, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    print(f"[AI] API错误 {resp.status}: {text[:120]}")
                    return None
                data = await resp.json()
                content = data["choices"][0]["message"]["content"]
                result = json.loads(content)
                
                if result.get("confidence", 0) < 45:
                    return None
                if not result.get("targets"):
                    return None
                    
                return result
    except Exception as e:
        print(f"[AI] 异常: {e}")
        return None


def apply_ai_targets(pos: dict, px: float, targets: list, sym: str, runner) -> list:
    """根据AI目标价检查是否需要执行操作"""
    actions = []
    side = pos["side"]
    
    for t in targets:
        target_price = float(t["price"])
        action = t.get("action", "take_profit")
        ratio = float(t.get("ratio", 0.3))
        
        triggered = False
        if side == "long" and px >= target_price:
            triggered = True
        elif side == "short" and px <= target_price:
            triggered = True
            
        if triggered:
            actions.append({
                "type": action,
                "price": target_price,
                "ratio": ratio,
                "current_px": px,
            })
    
    return actions
