"""Shark 2.0 AI 策略引擎 v6 — 动态模型池 + 三模型委员会"""
import json
import os
import re
import time
import aiohttp

# ── API endpoints ──
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
QWEN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
QWEN_KEY = os.environ.get("QWEN_KEY", "")
VOLC_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
VOLC_KEY = os.environ.get("VOLC_KEY", "")

# ── Qwen 动态模型池（用完自动轮换）──
_QWEN_POOL = [
    "qwen-max", "qwen-plus", "qwen-turbo",
    "qwen-max-2025-01-25", "qwen-plus-2025-01-25", "qwen-turbo-2025-01-25",
    "qwen2.5-72b-instruct", "qwen2.5-32b-instruct", "qwen2.5-14b-instruct",
    "qwen2.5-7b-instruct", "qwen2.5-3b-instruct", "qwen2.5-1.5b-instruct",
    "qwen-max-latest", "qwen-plus-latest", "qwen-turbo-latest",
    "qwen2.5-72b-instruct-1m", "qwen2.5-14b-instruct-1m", "qwen2.5-7b-instruct-1m",
    "qwen3-235b-a22b", "qwen3-32b", "qwen3-30b-a3b", "qwen3-14b",
    "qwen3-8b", "qwen3-4b", "qwen3-1.7b", "qwen3-0.6b",
    "qvq-max", "qvq-plus",
]
_qwen_idx = 0
_qwen_exhausted = set()
_last_slack_alert = 0.0

FULL_MODE = os.environ.get("AI_COMMITTEE_FULL", "0") == "1"


def _ai_committee_verbose() -> bool:
    return os.environ.get("AI_COMMITTEE_VERBOSE", "").strip().lower() in ("1", "true", "yes")


TOK_DEEPSEEK = 1536 if FULL_MODE else 900
TOK_QWEN = 800 if FULL_MODE else 380
TOK_DOUBAO = 600 if FULL_MODE else 280

SYSTEM_ANALYST = "你是量化交易分析师。输出JSON：{\"direction\":\"LONG/SHORT/HOLD\",\"confidence\":0-100,\"entry_price\":数字,\"targets\":[{\"price\":数字,\"action\":\"take_profit/add_position\",\"ratio\":0-1}],\"stop_loss\":数字,\"supports\":[],\"resistances\":[],\"risk_reward\":数字,\"leverage\":数字,\"position_size_pct\":0.01-1.0}"
SYSTEM_REVIEW = "你是交易复核员。审查计划是否合理。输出JSON：{\"approved\":true/false,\"direction\":\"LONG/SHORT/HOLD\",\"reason\":\"简短原因\"}"
SYSTEM_SENTIMENT = "你是市场情绪分析师。输出JSON：{\"sentiment\":\"BULLISH/BEARISH/NEUTRAL\",\"strength\":0-100,\"key_factors\":[]}"

def _safe_json_parse(content):
    if not content: return {}
    content = content.strip()
    for pattern in [r'\{[\s\S]*\}', r'\{[^}]*\}']:
        m = re.search(pattern, content)
        if m:
            try: return json.loads(m.group())
            except: pass
    return {}

def _local_fallback_plan(symbol, price, change_24h, funding_rate):
        direction, confidence = "HOLD", 25
        if funding_rate > 0.0005 and change_24h > 1: direction, confidence = "SHORT", 35
        elif funding_rate < -0.0005 and change_24h < -1: direction, confidence = "LONG", 35
        elif change_24h > 3: direction, confidence = "LONG", 35
        elif change_24h < -3: direction, confidence = "SHORT", 35
        return {"direction":direction,"confidence":confidence,"entry_price":price,"targets":[],"stop_loss":price*0.97,"supports":[],"resistances":[],"risk_reward":1.5}

async def _call_llm(url, key, model, system, prompt, max_tokens, json_mode=True, temperature=0.25, timeout=25, label=""):
    global _qwen_idx, _qwen_exhausted, _last_slack_alert
    actual_model = model
    is_qwen = "dashscope" in url
    if is_qwen:
        tried = 0
        while tried < len(_QWEN_POOL):
            candidate = _QWEN_POOL[_qwen_idx % len(_QWEN_POOL)]
            _qwen_idx += 1
            if candidate not in _qwen_exhausted:
                actual_model = candidate; break
            tried += 1
        if tried >= len(_QWEN_POOL):
            now = time.time()
            if now - _last_slack_alert > 600:
                _last_slack_alert = now
                try:
                    wh = os.environ.get("SLACK_WEBHOOK","")
                    if wh:
                        async with aiohttp.ClientSession() as s:
                            await s.post(wh, json={"text":"Qwen全部模型额度耗尽"})
                except: pass
            if _ai_committee_verbose():
                print(f"[AI委] {label} Qwen全部耗尽")
            return None, 0, 0

    payload = {"model":actual_model,"messages":[{"role":"system","content":system},{"role":"user","content":prompt}],"temperature":temperature,"max_tokens":max_tokens}
    if json_mode and "volces" not in url: payload["response_format"] = {"type":"json_object"}
    headers = {"Authorization":f"Bearer {key}","Content-Type":"application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status in (429,402):
                    if is_qwen:
                        _qwen_exhausted.add(actual_model)
                        return await _call_llm(url, key, model, system, prompt, max_tokens, json_mode, temperature, timeout, label)
                    return None, 0, 0
                if resp.status != 200:
                    return None, 0, 0
                data = await resp.json()
                choice = data["choices"][0]
                msg = choice.get("message",{})
                content = msg.get("content","") or msg.get("reasoning_content","")
                return _safe_json_parse(content), data.get("usage",{}).get("prompt_tokens",0), data.get("usage",{}).get("completion_tokens",0)
    except: return None, 0, 0

async def get_ai_targets(symbol, price, change_24h, volume_24h, funding_rate, ob_text=""):
    """三模型委员会：DeepSeek主分析 → Qwen复核 → 豆包情绪"""
    if not DEEPSEEK_KEY:
        return _local_fallback_plan(symbol, price, change_24h, funding_rate), 0, "local", {}
    
    from core.live import get_contract_spec
    spec = get_contract_spec(symbol)
    if spec:
        max_lev = spec.get('leverage_max', '20')
        max_size = spec.get('order_size_max', 0)
        min_size = spec.get('order_size_min', 0)
        risk_limit = spec.get('risk_limit_base', 0)
        quanto = float(spec.get('quanto_multiplier', 1))
        
        # 计算成 USDT 金额给 AI 更直观
        max_usdt = float(max_size) * quanto * price if max_size else "未知"
        # 实际最大开仓金额不仅受 max_size 限制，还受风险限额 risk_limit_base 限制
        if risk_limit and isinstance(max_usdt, float):
            max_usdt = min(max_usdt, float(risk_limit))
        elif risk_limit:
            max_usdt = float(risk_limit)

        min_usdt = float(min_size) * quanto * price if min_size else "未知"
        if isinstance(max_usdt, float): max_usdt = f"{max_usdt:,.0f}U"
        if isinstance(min_usdt, float): min_usdt = f"{min_usdt:,.1f}U"
        
        limits_text = f"交易所限制(务必遵守): 最大杠杆{max_lev}x, 最小开仓金额{min_usdt}, 最大开仓金额{max_usdt}。结合限制合理分配 leverage 和 position_size_pct。"
        ob_text = f"{ob_text} {limits_text}".strip()

    analysis_prompt = f"{symbol} 现价{price} 24h{change_24h:+.2f}% 量{volume_24h:,.0f} 费率{funding_rate*100:+.4f}% {ob_text}"
    
    ds_result, ds_in, ds_out = await _call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-chat", SYSTEM_ANALYST, analysis_prompt, TOK_DEEPSEEK, label=f"{symbol} DS")
    if not ds_result:
        return _local_fallback_plan(symbol, price, change_24h, funding_rate), 0, "local", {}
    
    ds_dir = (ds_result.get("direction") or "").upper()
    ds_conf = ds_result.get("confidence", 0)
    if _ai_committee_verbose():
        print(f"[AI委] {symbol} DeepSeek dir={ds_dir} conf={ds_conf}")
    
    if ds_dir == "HOLD" or ds_conf < 35:
        return None, ds_in+ds_out, "hold", {}
    
    vote_score = 1.0
    if QWEN_KEY:
        qw_result, qw_in, qw_out = await _call_llm(QWEN_URL, QWEN_KEY, "qwen-max", SYSTEM_REVIEW, f"{symbol} {analysis_prompt}\n计划:{json.dumps(ds_result,ensure_ascii=False)}", TOK_QWEN, label=f"{symbol} QW", timeout=20)
        if qw_result and qw_result.get("approved"):
            vote_score += 1.0
            if _ai_committee_verbose():
                print(f"[AI委] {symbol} Qwen approved")
    
    if VOLC_KEY:
        db_result, db_in, db_out = await _call_llm(VOLC_URL, VOLC_KEY, "doubao-1-5-lite-32k-250115", SYSTEM_SENTIMENT, f"{symbol} {analysis_prompt}", TOK_DOUBAO, json_mode=False, label=f"{symbol} DB", timeout=15)
        if db_result:
            if _ai_committee_verbose():
                print(f"[AI委] {symbol} 豆包 sentiment={db_result.get('sentiment')}")
    
    return ds_result, ds_in+ds_out, "ai_committee", {"vote": vote_score, "conf": ds_conf}

def apply_ai_targets(pos, px, ai_targets, sym, runner):
    """
    检查AI目标价，生成对应的交易动作 (take_profit, add_position 等)。
    """
    actions = []
    if not isinstance(ai_targets, list):
        return actions
        
    side = pos.get("side")
    entry = float(pos.get("entry", 0))
    if entry <= 0: return actions
    
    for t in ai_targets:
        if not isinstance(t, dict): continue
        t_px = float(t.get("price", 0))
        t_action = t.get("action", "")
        if t_px <= 0 or not t_action: continue
        
        # 简单判断是否触及目标价
        if side == "long" and px >= t_px and t_action == "take_profit":
            actions.append({"type": "take_profit", "price": t_px})
        elif side == "short" and px <= t_px and t_action == "take_profit":
            actions.append({"type": "take_profit", "price": t_px})
            
    return actions
