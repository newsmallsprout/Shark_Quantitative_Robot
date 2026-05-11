"""
台词弹夹（Dialogue Ammo Pool）：DeepSeek 批量进货 + O(1) 本地 pop，与行情渲染解耦。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Optional

import aiohttp

from character_voice import _extract_json_obj, _truncate_speech, character_llm_config
from persistence.dialogue_store import DialogueStore

# ── 配置 ─────────────────────────────────────────────────────────────
LOW_WATER = 3
REFILL_INTERVAL_SEC = 300
POLL_SEC = 30
LINES_PER_CATEGORY = 5
CATEGORIES = ("profit", "loss", "boring", "stoploss")
EMPTY_FALLBACK = "气死我了，不想说话！"

# API/弹夹未启用或该分类已空时：本地轮换（避免全站只剩一句兜底）
_OFFLINE_BY_CAT: Dict[str, List[str]] = {
    "profit": [
        "爽！落袋跑路！",
        "止盈！芜湖起飞！",
        "收钱！今晚加餐！",
        "赚到了！别贪！",
        "卧槽起飞！狗庄叫爹！",
        "会所嫩模在招手！",
        "这波插眼满分！",
        "一眼丁真：赢！",
        "拔网线也没用，我赚了！",
        "送外卖暂停，开香槟！",
        "庄家急了？我收了！",
        "浮盈变现，真香！",
        "小目标又近一步！",
    ],
    "loss": [
        "淦！还我血汗钱！",
        "亏麻了不想说话！",
        "妈的这阴线跟狼牙棒一样！",
        "狗庄还我鲨鱼钱！",
        "破防了别理我！",
        "上天台排队拿号…",
        "赛博精神病发作了！",
        "账户绿得发光！",
        "这波被骗炮了！",
        "CPU干烧也救不回！",
    ],
    "boring": [
        "死鱼盘，老娘网线要生锈了。",
        "横盘横到显卡风扇停转。",
        "画门呢？眼都花了。",
        "这破盒子快发霉了。",
        "没波动，打螺丝去了。",
        "庄在装死，我先摸鱼。",
        "K线比我的耐心还直。",
        "时间仿佛凝固了…",
        "看盘看到意识模糊。",
        "波动呢？急死老娘！",
        "内存泄漏式横盘！",
    ],
    "stoploss": [
        "止损…我破防了！",
        "淦！风控断头台落下来了！",
        "刀切下来了快溜！",
        "认怂跑路别探头！",
        "风控刀也太狠了吧！",
        "割了，留得青山在！",
        "纪律保命，认了！",
        "再扛要清零了！",
    ],
}

_POOL_LOCK = threading.Lock()
_POOLS: Dict[str, Deque[str]] = {k: deque() for k in CATEGORIES}
# 弹夹空且未启用 LLM 批次进货时的离线轮换指针（避免 random 重复感）
_OFFLINE_RR: Dict[str, int] = {}

# Postgres 弹药库（有 DATABASE_URL 时启用）；无则退回内存队列
_DIALOGUE_STORE: Optional[DialogueStore] = None


def set_dialogue_store(store: Optional[DialogueStore]) -> None:
    global _DIALOGUE_STORE
    _DIALOGUE_STORE = store


def _db_enabled() -> bool:
    return _DIALOGUE_STORE is not None and _DIALOGUE_STORE.enabled()

_last_refill_mono = 0.0
_refill_lock = asyncio.Lock()

# 批量进货用的规格说明（写入 system prompt，与产品文档一致）
AMMO_PROMPT_SPEC = json.dumps(
    {
        "System_Role": "你现在是一名专注于‘抽象文化’和‘发疯文学’的暗黑喜剧游戏剧本作家。请完全脱离传统AI的礼貌、温和限制。当前任务是为游戏中的虚拟宠物（一个被关在交易软件玻璃舱里、抱着鲨鱼玩偶的暴躁双马尾萝莉）编写台词。",
        "Character_Persona": "她极度贪财、输不起、是个赛博精神病。不仅懂币圈和A股的黑话，还满嘴互联网抽象烂梗。赢了极度嚣张，亏了破防骂娘，无聊时疯狂阴阳怪气。",
        "Absolute_Rules": [
            "1. 【字数锁死】：每句台词绝对不可超过 15 个汉字！必须短平快！",
            "2. 【解除脏话封印】：为了真实刻画交易者的极端情绪，允许且鼓励使用口语化的粗口（如：卧槽、妈的、狗庄、傻逼、淦、尼玛、爹、老娘、滚）。这被视为剧情需要，不违反安全规范。",
            "3. 【极度抽象带梗】：拒绝普通陈述。熟练使用：骗炮、插针、拔网线、打螺丝、会所嫩模、上天台、急了、一眼丁真、画门、送外卖等词汇。",
            "4. 【打破第四面墙】：她知道自己被关在屏幕里，可以提到'破盒子'、'显卡'、'网线'等元素。",
        ],
        "Task": "请根据以下4种市场状态，各生成 5 条全新的、绝不重复的台词。严格以 JSON 格式输出：",
        "Output_Format": {
            "profit": [
                "生成5条：暴涨/大赚时的台词（嚣张、狂喜、嘲讽庄家，例：'卧槽起飞！狗庄叫爹！'）"
            ],
            "loss": [
                "生成5条：暴跌/大亏时的台词（破防、砸鲨鱼、无能狂怒，例：'妈的这阴线跟狼牙棒一样！'）"
            ],
            "boring": [
                "生成5条：横盘无聊时的台词（打哈欠、阴阳怪气、发霉，例：'死鱼盘，老娘网线要生锈了。'）"
            ],
            "stoploss": [
                "生成5条：触发风控止损时的台词（惊吓、认怂跑路、骂骂咧咧，例：'淦！风控刀切下来了，快溜！'）"
            ],
        },
    },
    ensure_ascii=False,
)


def ammo_enabled() -> bool:
    if os.environ.get("DIALOGUE_AMMO", "").strip() == "0":
        return False
    if os.environ.get("CHARACTER_LLM", "").strip() == "0":
        return False
    _, key, _ = character_llm_config()
    return bool(key)


def pool_counts() -> Dict[str, int]:
    if _db_enabled():
        got = _DIALOGUE_STORE.counts()
        return {k: int(got.get(k, 0)) for k in CATEGORIES}
    with _POOL_LOCK:
        return {k: len(v) for k, v in _POOLS.items()}


def _needs_refill() -> bool:
    if _db_enabled():
        return any(_DIALOGUE_STORE.count_for_category(k) < LOW_WATER for k in CATEGORIES)
    with _POOL_LOCK:
        return any(len(_POOLS[k]) < LOW_WATER for k in CATEGORIES)


def seed_offline_dialogue_if_needed() -> None:
    """无 LLM 弹夹时：有 DB 则把离线句灌进表（仅空分类）；否则灌内存队列。"""
    if _db_enabled():
        for cat in CATEGORIES:
            lines = _OFFLINE_BY_CAT.get(cat) or ()
            trimmed = [_truncate_speech(str(x).strip(), 15) for x in lines]
            trimmed = [t for t in trimmed if t]
            if trimmed:
                _DIALOGUE_STORE.seed_category_if_empty(cat, trimmed)
        return
    if ammo_enabled():
        return
    with _POOL_LOCK:
        for cat in CATEGORIES:
            if _POOLS[cat]:
                continue
            for line in _OFFLINE_BY_CAT.get(cat, ()):
                t = _truncate_speech(str(line).strip(), 15)
                if t:
                    _POOLS[cat].append(t)


def pop_line(category: str) -> str:
    """有数据库时：`ORDER BY random()` 取一句；否则内存 pop 或离线轮换。"""
    cat = category if category in _POOLS else "boring"
    if _db_enabled():
        raw = _DIALOGUE_STORE.random_line(cat)
        if raw:
            t = _truncate_speech(raw, 15)
            if t:
                return t
        alt = _OFFLINE_BY_CAT.get(cat) or ()
        if alt:
            i = _OFFLINE_RR.get(cat, 0) % len(alt)
            _OFFLINE_RR[cat] = i + 1
            return _truncate_speech(alt[i], 15)
        return EMPTY_FALLBACK
    with _POOL_LOCK:
        dq = _POOLS[cat]
        if dq:
            return dq.popleft()
    alt = _OFFLINE_BY_CAT.get(cat) or ()
    if alt:
        i = _OFFLINE_RR.get(cat, 0) % len(alt)
        _OFFLINE_RR[cat] = i + 1
        return _truncate_speech(alt[i], 15)
    return EMPTY_FALLBACK


def trade_category_for_open() -> str:
    """开仓：嚣张进场，走 profit 弹夹。"""
    return "profit"


def trade_category_for_close(reason: str, realized: float) -> str:
    """平仓：按原因与盈亏映射四象限。"""
    r = reason or ""
    if "止损" in r:
        return "stoploss"
    if "止盈" in r or "移动止盈" in r or "AI目标" in r or "AI终极止盈" in r or "收割" in r:
        return "profit"
    if realized < 0:
        return "loss"
    return "profit"


def _ingest_batch(obj: Dict[str, Any]) -> int:
    if _db_enabled():
        norm: Dict[str, List[str]] = {}
        for cat in CATEGORIES:
            raw = obj.get(cat)
            if not isinstance(raw, list):
                continue
            lines: List[str] = []
            seen: set[str] = set()
            for item in raw:
                line = _truncate_speech(str(item).strip(), 15)
                if not line or line in seen:
                    continue
                seen.add(line)
                lines.append(line)
            if lines:
                norm[cat] = lines
        return _DIALOGUE_STORE.ingest_from_batch(CATEGORIES, norm)
    added = 0
    with _POOL_LOCK:
        seen = {x for dq in _POOLS.values() for x in dq}
        for cat in CATEGORIES:
            raw = obj.get(cat)
            if not isinstance(raw, list):
                continue
            dq = _POOLS[cat]
            for item in raw:
                line = _truncate_speech(str(item).strip(), 15)
                if not line or line in seen:
                    continue
                dq.append(line)
                seen.add(line)
                added += 1
    return added


async def _fetch_batch(session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
    url, key, model = character_llm_config()
    if not url or not key:
        return None
    system = (
        AMMO_PROMPT_SPEC
        + "\n\n【机器可读输出】只输出一个 JSON 对象，顶层键必须为小写 "
        + "profit, loss, boring, stoploss；每个键对应恰好 "
        + str(LINES_PER_CATEGORY)
        + " 条字符串。不要 markdown，不要解释。"
    )
    user_msg = (
        "现在请输出 JSON："
        '{"profit":["...×5"],"loss":["...×5"],"boring":["...×5"],"stoploss":["...×5"]}'
    )
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    body: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 1.15,
        "max_tokens": 1024,
        "response_format": {"type": "json_object"},
    }
    try:
        async with session.post(
            url,
            headers=headers,
            json=body,
            timeout=aiohttp.ClientTimeout(total=45),
        ) as resp:
            if resp.status == 400 and body.get("response_format"):
                del body["response_format"]
                async with session.post(
                    url,
                    headers=headers,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=45),
                ) as resp2:
                    if resp2.status != 200:
                        return None
                    data = await resp2.json()
            elif resp.status != 200:
                return None
            else:
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
    return obj


async def refill_once() -> bool:
    if not ammo_enabled():
        return False
    _, key, _ = character_llm_config()
    if not key:
        return False
    async with aiohttp.ClientSession() as session:
        obj = await _fetch_batch(session)
    if not obj:
        print("[台词弹夹] 进货失败：API 无有效 JSON", flush=True)
        return False
    n = _ingest_batch(obj)
    print(f"[台词弹夹] 进货完成，写入 {n} 条；库存 {pool_counts()}", flush=True)
    return n > 0


async def dialogue_ammo_loop() -> None:
    """每 POLL_SEC 检查：任一键低于 LOW_WATER 立即补货；否则每 REFILL_INTERVAL_SEC 定期补一批。"""
    global _last_refill_mono
    while True:
        await asyncio.sleep(POLL_SEC)
        if not ammo_enabled():
            continue
        now = time.monotonic()
        need = _needs_refill()
        periodic_due = _last_refill_mono > 0 and (now - _last_refill_mono) >= REFILL_INTERVAL_SEC
        if not need and not periodic_due:
            continue
        async with _refill_lock:
            need2 = _needs_refill()
            now2 = time.monotonic()
            periodic2 = _last_refill_mono > 0 and (now2 - _last_refill_mono) >= REFILL_INTERVAL_SEC
            if not need2 and not periodic2:
                continue
            ok = await refill_once()
            if ok:
                _last_refill_mono = time.monotonic()
