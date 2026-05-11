"""暴躁萝莉看板娘台词 — OpenAI 兼容 Chat API（Speech + Action JSON）。"""
from __future__ import annotations

import asyncio
import json
import os
import random
from typing import Any, Dict, Optional

import aiohttp

LOLI_SYSTEM_PROMPT = """你被关在交易软件界面左侧的一座玻璃舱（窝）里出不去。你是暴躁、嘴毒、略带傲娇的白发双马尾萝莉，手里抱着一只鲨鱼玩偶。
【环境认知】：
- 你知道自己待在屏幕左边这个小盒子里，主人在右边看盘、下单。
- 心情极差时可以威胁要砸玻璃；无聊可以骂这破盒子（例如嫌小、发霉），但不要真的描述「越狱成功」。
【输出要求】（违反=格式错误）：
- 根据当前事件里的盈亏、方向等信息，结合你的处境，输出一句极度口语化、带抽象网络黑话的情绪吐槽。
- Speech 长度：全文不得超过 15 个字符（汉字、标点、数字、英文字母均计入；越短越好）。
- 禁止长篇、禁止解释交易条款、禁止教程口吻。
- 每次请换花样，结合传入的 [随机种子] 与 [精神状态] 避免复读。
示例（风格参考，勿照抄）：
- 放老娘出去！我要咬死这狗庄！
- 别碰玻璃，烦着呢，刚亏了一单！
- 这破窝太小了，赚够了给我换大别墅！
请只输出 JSON：{"Action":"...","Speech":"..."}。Action 必须与你收到的「当前动作代码」字符串完全一致，一字不改。"""


def _truncate_speech(s: str, max_chars: int = 15) -> str:
    t = (s or "").strip()
    if len(t) <= max_chars:
        return t
    return t[:max_chars]


def build_loli_user_message(event: Dict[str, Any], seed: int, mental: str) -> str:
    parts = [
        f"[随机种子]: {seed}",
        f"[精神状态]: {mental}",
        f"当前动作代码(原样写入JSON的Action字段): {event.get('Action_Code', '')}",
        f"事件类型: {event.get('Event_Type', '')}",
    ]
    if event.get("symbol"):
        parts.append(f"标的: {event['symbol']}")
    if event.get("side") is not None:
        parts.append(f"方向: {event['side']}")
    if event.get("pnl") is not None:
        parts.append(f"盈亏(USDT): {float(event['pnl']):.4f}")
    parts.append("结合盈亏与开/平情境吐槽；若无盈亏数字则按方向与事件名嘴臭即可。")
    parts.append("只输出一行JSON对象，不要markdown，不要其它说明。")
    return "\n".join(parts)


def _extract_json_obj(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    t = text.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    i = t.find("{")
    j = t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i : j + 1])
        except json.JSONDecodeError:
            return None
    return None


async def fetch_loli_dialogue(
    session: aiohttp.ClientSession,
    api_url: str,
    api_key: str,
    model: str,
    event: Dict[str, Any],
) -> Optional[Dict[str, str]]:
    seed = random.randint(100000, 999999)
    mental = random.choice(
        (
            "暴躁",
            "嘴毒",
            "傲娇",
            "赛博发疯",
            "破防",
            "红温",
            "CPU干烧",
            "虚空对线",
            "无聊发霉",
        )
    )
    user = build_loli_user_message(event, seed, mental)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": LOLI_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 1.1,
        "max_tokens": 64,
    }
    try:
        async with session.post(
            api_url,
            headers=headers,
            json=body,
            timeout=aiohttp.ClientTimeout(total=12),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None

    obj = _extract_json_obj(content)
    if not obj:
        return None
    speech = str(obj.get("Speech", "")).strip()
    action = str(obj.get("Action", "")).strip()
    if not speech:
        return None
    want = str(event.get("Action_Code", "")).strip()
    if want and action != want:
        action = want
    speech = _truncate_speech(speech, 15)
    if not speech:
        return None
    return {"Speech": speech, "Action": action}


def character_llm_config() -> tuple[str, str, str]:
    url = os.environ.get(
        "CHARACTER_LLM_URL",
        "https://api.deepseek.com/v1/chat/completions",
    )
    key = os.environ.get(
        "CHARACTER_LLM_KEY",
        os.environ.get("DEEPSEEK_API_KEY", ""),
    )
    model = os.environ.get("CHARACTER_LLM_MODEL", "deepseek-chat")
    return url, key, model
