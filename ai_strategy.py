"""Shark 2.0 AI 策略引擎 v5 — 三模型委员会（DeepSeek主分析 + Qwen复核 + 豆包情绪）"""

import asyncio, json, os, re, time
import aiohttp

# ── API endpoints ──
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
QWEN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
QWEN_KEY = os.environ.get("QWEN_KEY", "")
VOLC_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
VOLC_KEY = os.environ.get("VOLC_KEY", "")

# ── 省量/完整模式 ──
FULL_MODE = os.environ.get("AI_COMMITTEE_FULL", "0") == "1"
ENABLE_THINKING = os.environ.get("AI_ENABLE_THINKING", "1") == "1"

# ── Token 预算 ──
TOK_DEEPSEEK = 1536 if FULL_MODE else 900
TOK_QWEN = 800 if FULL_MODE else 380
TOK_DOUBAO = 600 if FULL_MODE else 280

# ── 全局降级预案（所有LLM失败时的本地兜底）──
def _local_fallback_plan(symbol: str, price: float, change_24h: float, funding_rate: float) -> dict:
    """纯本地计算：基于费率+涨跌幅的简单方向判断"""
    direction = "HOLD"
    confidence = 25
    if funding_rate > 0.0005 and change_24h > 1:
        direction, confidence = "SHORT", 35
    elif funding_rate < -0.0005 and change_24h < -1:
        direction, confidence = "LONG", 35
    elif change_24h > 3:
        direction, confidence = "LONG", 30
    elif change_24h < -3:
        direction, confidence = "SHORT", 30
    sl_pct = 0.03
    return {
        "direction": direction, "confidence": confidence,
        "entry_price": price,
        "targets": [{"price": price * (1.01 if direction == "LONG" else 0.99), "action": "take_profit", "ratio": 0.5, "reason": "本地降级"}],
        "stop_loss": price * (1 - sl_pct) if direction == "LONG" else price * (1 + sl_pct),
        "add_zone": {"price": price * 0.99, "condition": "降级无补仓"},
        "reduce_zone": {"price": price * 1.01, "condition": "降级无减仓"},
        "supports": [price * 0.98], "resistances": [price * 1.02],
        "risk_reward": 2.0, "reasoning": "本地降级",
    }


# ═══════════════════════════════════════════════════════════════
# System Prompts
# ═══════════════════════════════════════════════════════════════

SYSTEM_ANALYST = """你是全球顶级加密货币量化交易分析师。基于多时间框架数据给出精确的多层交易计划。
你可以选择方向做多(LONG)或做空(SHORT)，除非市场完全横盘无方向才选HOLD。

输出JSON（仅JSON，无markdown）：
{
  "direction": "LONG"|"SHORT"|"HOLD",
  "confidence": 0-100,
  "entry_price": 数字,
  "targets": [
    {"price": 数字, "action": "pyramid_add"|"take_profit", "ratio": 0.2-0.5, "reason": "简由"}
  ],
  "stop_loss": 数字,
  "add_zone": {"price": 数字, "condition": "缩量回调补仓"},
  "reduce_zone": {"price": 数字, "condition": "放量暴跌减仓"},
  "supports": [数字, 数字],
  "resistances": [数字, 数字],
  "risk_reward": 数字,
  "reasoning": "分析摘要20字内"
}
- 尽量给出LONG或SHORT，仅在完全无方向时才HOLD
- targets按价格排序，ratio总和≤1.0
- stop_loss基于强支撑/阻力位"""

SYSTEM_REVIEW = """你是风控复核员。审查分析师的交易计划，判断是否批准。
输出JSON：{"approved": true|false, "direction": "LONG"|"SHORT"|"HOLD", "reason": "理由10字内"}"""

SYSTEM_SENTIMENT = """你是加密货币情绪分析师。根据资金费率、盘口、成交量变化判断市场情绪方向。
输出JSON：{"sentiment": "LONG"|"SHORT"|"NEUTRAL", "strength": 0-100, "reason": "理由10字内"}"""


# ═══════════════════════════════════════════════════════════════
# K线获取
# ═══════════════════════════════════════════════════════════════

async def _fetch_timeframe_candles(sym: str, interval: str, limit: int = 20) -> list:
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
    if len(candles) < period + 1:
        return 0
    tr_list = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i-1]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_list.append(tr)
    return sum(tr_list[-period:]) / period


def _kline_summary(candles: list, label: str) -> str:
    if not candles or len(candles) < 3:
        return ""
    recent = candles[-8:]
    closes = [c["c"] for c in recent]
    highs = [c["h"] for c in recent]
    lows = [c["l"] for c in recent]
    trend = "横盘"
    if closes[-1] > closes[0] * 1.02:
        trend = "上升"
    elif closes[-1] < closes[0] * 0.98:
        trend = "下降"
    range_pct = (max(highs) - min(lows)) / min(lows) * 100 if min(lows) > 0 else 0
    return f"{label}: {trend} 波{range_pct:.1f}% 收{closes[-1]:.4f}"


async def _fetch_orderbook(sym: str, depth: int = 10) -> str:
    try:
        gate_sym = sym.replace("/USDT", "_USDT")
        url = "https://api.gateio.ws/api/v4/futures/usdt/order_book"
        params = {"contract": gate_sym, "limit": depth}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        bid_vol = sum(float(b[1]) for b in bids[:5]) if bids else 0
        ask_vol = sum(float(a[1]) for a in asks[:5]) if asks else 0
        best_bid = float(bids[0][0]) if bids else 0
        best_ask = float(asks[0][0]) if asks else 0
        spread = (best_ask - best_bid) / best_bid * 100 if best_bid > 0 else 0
        ratio = bid_vol / max(ask_vol, 1)
        if ratio > 2:
            wall = f"买盘强势({ratio:.1f}x)"
        elif ratio < 0.5:
            wall = f"卖盘强势({1/ratio:.1f}x)"
        else:
            wall = f"均衡({ratio:.1f}x)"
        return f"盘口:{wall} 价差{spread:.3f}%"
    except:
        return ""


# ═══════════════════════════════════════════════════════════════
# JSON 解析容错
# ═══════════════════════════════════════════════════════════════

def _safe_json_parse(text: str) -> dict:
    """多策略JSON解析：md代码块 → 直接parse → regex → strict=False"""
    if not text or not text.strip():
        return {}
    # 策略0: 提取 markdown ```json ... ``` 代码块
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except:
            pass
    # 策略1: 直接解析
    try:
        return json.loads(text)
    except:
        pass
    # 策略2: regex 提取最外层 {...}
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except:
            pass
    # 策略3: 宽松模式
    try:
        return json.loads(text, strict=False)
    except:
        pass
    return {}


# ═══════════════════════════════════════════════════════════════
# LLM 调用封装
# ═══════════════════════════════════════════════════════════════

async def _call_llm(url: str, key: str, model: str, system: str, prompt: str,
                    max_tokens: int, json_mode: bool = True, temperature: float = 0.25,
                    timeout: int = 25, label: str = "") -> dict:
    """统一LLM调用，返回 (result_dict, tok_in, tok_out) 或 (None, 0, 0)"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # 火山引擎不支持 response_format
    if json_mode and "volces" not in url:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    print(f"[AI委] {label} API错误 {resp.status}: {text[:100]}", flush=True)
                    return None, 0, 0
                data = await resp.json()
                choice = data["choices"][0]
                msg = choice.get("message", {})
                content = msg.get("content", "")

                # 如果有 reasoning_content，尝试从中提取 JSON
                reasoning = msg.get("reasoning_content", "")
                if reasoning and not content.strip():
                    content = reasoning

                tok_in = data.get("usage", {}).get("prompt_tokens", 0)
                tok_out = data.get("usage", {}).get("completion_tokens", 0)

                result = _safe_json_parse(content)
                return result, tok_in, tok_out
    except asyncio.TimeoutError:
        print(f"[AI委] {label} 超时", flush=True)
        return None, 0, 0
    except Exception as e:
        print(f"[AI委] {label} 异常: {e}", flush=True)
        return None, 0, 0


# ═══════════════════════════════════════════════════════════════
# 主入口：三模型委员会
# ═══════════════════════════════════════════════════════════════

async def get_ai_targets(symbol: str, price: float, funding_rate: float,
                         change_24h: float, volume_24h: float) -> dict:
    """三模型委员会 v5 — 短路优化 + 投票 + JSON容错"""

    t0 = time.time()

    # ── 并行获取数据 ──
    k15, k1h, k4h = await asyncio.gather(
        _fetch_timeframe_candles(symbol, "15m", 20),
        _fetch_timeframe_candles(symbol, "1h", 16),
        _fetch_timeframe_candles(symbol, "4h", 12),
    )
    atr = _calc_atr(k15) if k15 else 0
    atr_pct = (atr / price * 100) if price > 0 else 0
    ob_text = await _fetch_orderbook(symbol)

    # ── 构建分析 prompt ──
    k15_text = _kline_summary(k15, "15m")
    k1h_text = _kline_summary(k1h, "1h")
    k4h_text = _kline_summary(k4h, "4h")
    fr_pct = funding_rate * 100

    analysis_prompt = f"""分析 {symbol}：
价格:{price:.6f} 24h:{change_24h:+.2f}% 量:{volume_24h:,.0f} 费率:{fr_pct:+.4f}%
ATR:{atr:.6f}({atr_pct:.2f}%) {k15_text} {k1h_text} {k4h_text} {ob_text}"""

    total_tok = 0
    ds_direction = None
    ds_confidence = 0
    final_plan = None

    # ── 第1步：DeepSeek 主分析 ──
    if DEEPSEEK_KEY:
        ds_result, ds_in, ds_out = await _call_llm(
            DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-chat",
            SYSTEM_ANALYST, analysis_prompt,
            TOK_DEEPSEEK, json_mode=True, label=f"{symbol} DS"
        )
        total_tok += ds_in + ds_out
        if ds_result:
            ds_direction = (ds_result.get("direction") or "").upper()
            ds_confidence = ds_result.get("confidence", 0)
            final_plan = ds_result
            print(
                f"[AI委] {symbol} 【分析】DeepSeek direction={ds_direction} "
                f"conf={ds_confidence} entry={ds_result.get('entry_price','?')} "
                f"sl={ds_result.get('stop_loss','?')} | tok in={ds_in} out={ds_out} total={ds_in+ds_out}",
                flush=True
            )
    else:
        print(f"[AI委] {symbol} DeepSeek 未配置key，使用本地降级", flush=True)
        return _local_fallback_plan(symbol, price, change_24h, funding_rate)

    # 短路：HOLD / conf<35 / 无targets → 跳过复核和情绪
    if ds_direction == "HOLD" or ds_confidence < 35 or not ds_result.get("targets"):
        print(f"[AI委] {symbol} 短路跳过(HOLD={ds_direction=='HOLD'} conf={ds_confidence}) tok={total_tok}", flush=True)
        return None

    vote_score = 1.0  # DeepSeek 自带1票
    qwen_approved = False

    # ── 第2步：Qwen 复核 ──
    if QWEN_KEY:
        review_prompt = f"{symbol} 现价{price} {analysis_prompt}\n计划:{json.dumps(final_plan, ensure_ascii=False)}"
        qw_result, qw_in, qw_out = await _call_llm(
            QWEN_URL, QWEN_KEY, "qwen-max",
            SYSTEM_REVIEW, review_prompt,
            TOK_QWEN, json_mode=True, label=f"{symbol} QW", timeout=20
        )
        total_tok += qw_in + qw_out
        if qw_result and qw_result.get("approved"):
            qwen_approved = True
            vote_score += 1.0
            qw_dir = qw_result.get("direction", "").upper()
            print(
                f"[AI委] {symbol} 【复核】Qwen approved={True} dir={qw_dir} "
                f"| tok in={qw_in} out={qw_out} total={qw_in+qw_out}",
                flush=True
            )
        else:
            qw_reason = qw_result.get("reason", "无响应") if qw_result else "无响应"
            print(f"[AI委] {symbol} 【复核】Qwen 拒绝:{qw_reason} | tok in={qw_in} out={qw_out}", flush=True)

    # ── 第3步：豆包情绪分析 ──
    if VOLC_KEY:
        sentiment_prompt = f"{symbol} 费率:{fr_pct:+.4f}% 24h:{change_24h:+.2f}% 量:{volume_24h:,.0f} {ob_text}"
        db_result, db_in, db_out = await _call_llm(
            VOLC_URL, VOLC_KEY, "doubao-1-5-lite-32k-250115",
            SYSTEM_SENTIMENT, sentiment_prompt,
            TOK_DOUBAO, json_mode=False, label=f"{symbol} DB", timeout=15
        )
        total_tok += db_in + db_out
        if db_result:
            db_sent = (db_result.get("sentiment") or "").upper()
            db_strength = db_result.get("strength", 0)
            print(
                f"[AI委] {symbol} 【情绪】豆包 sentiment={db_sent} "
                f"strength={db_strength} | tok in={db_in} out={db_out} total={db_in+db_out}",
                flush=True
            )
            if db_sent == ds_direction:
                vote_score += 0.3
                ds_confidence += 5
                final_plan["confidence"] = min(100, ds_confidence)
        else:
            print(f"[AI委] {symbol} 【情绪】豆包 无响应", flush=True)

    # ── 投票裁决 ──
    threshold = 1.5 if qwen_approved else 1.0
    if vote_score >= threshold and ds_confidence >= 35:
        elapsed = (time.time() - t0) * 1000
        final_plan["confidence"] = min(100, ds_confidence)
        print(
            f"[AI委] {symbol} ✅通过 票={vote_score:.1f}/{threshold} "
            f"方向={ds_direction} 信={ds_confidence} 用时={elapsed:.0f}ms tok={total_tok}",
            flush=True
        )
        return final_plan
    else:
        print(
            f"[AI委] {symbol} ❌否决 票={vote_score:.1f}<{threshold} "
            f"方向={ds_direction} 信={ds_confidence} tok={total_tok}",
            flush=True
        )
        return None


# ═══════════════════════════════════════════════════════════════
# apply_ai_targets（未改动，保持兼容）
# ═══════════════════════════════════════════════════════════════

def apply_ai_targets(pos: dict, px: float, targets: list, sym: str, runner) -> list:
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
