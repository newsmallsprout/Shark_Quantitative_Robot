"""补全 boring + stoploss 各500条"""
import asyncio, json, os, sys, random
sys.path.insert(0, "/app")
import aiohttp
from persistence.dialogue_store import DialogueStore, resolve_sync_psycopg_url

TARGET = 500
BATCH = 40
CATS = {"boring": "横盘无聊", "stoploss": "止损割肉"}

PROMPT = """你是币圈段子手。给量化机器人写{count}条【{cat}】类台词。

铁律：每条≤15汉字，短平快。每条主题/句式/用词完全不同，不能改一两个字当新的！
用币圈粗口+抽象梗。可打破第四面墙（破盒子、显卡、网线、CPU）。

不许和这些重复：
{existing}

只输出JSON数组：["台词1","台词2",...]"""

def similar(a, b):
    if abs(len(a)-len(b)) > 3: return False
    sa, sb = set(a), set(b)
    return len(sa & sb) / max(len(sa), len(sb), 1) > 0.7 if sa else False

async def fill_cat(store, cat, cat_cn, api_key):
    # 读已有
    existing = set()
    if store._session_factory:
        from persistence.models import DialogueLine
        from sqlalchemy import select
        with store._session_factory() as s:
            rows = s.scalars(select(DialogueLine.line).where(DialogueLine.category == cat)).all()
            existing = set(rows)
    
    while len(existing) < TARGET:
        need = min(BATCH, TARGET - len(existing))
        sample = random.sample(list(existing), min(20, len(existing))) if existing else ["（尚无）"]
        
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    json={"model": "deepseek-chat", "temperature": 1.3, "max_tokens": 4096,
                          "messages": [{"role": "user", "content": PROMPT.format(count=need, cat=cat_cn, existing="\n".join(sample))}]},
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    data = await resp.json()
                    content = data["choices"][0]["message"]["content"]
            
            start = content.find("["); end = content.rfind("]")+1
            if start < 0: continue
            candidates = json.loads(content[start:end])
            
            added = 0; skipped = 0
            for line in candidates:
                line = line.strip()
                if not line or len(line) > 16 or line in existing: continue
                # 相似度检查（只比对最近100条）
                dup = False
                for e in list(existing)[-100:]:
                    if similar(line, e): dup = True; break
                if dup: skipped += 1; continue
                existing.add(line)
                store.insert_unique(cat, line)
                added += 1
            
            print(f"  +{added} (跳{skipped}) 累计{len(existing)}/{TARGET}")
        except Exception as e:
            print(f"  失败: {e}")
            await asyncio.sleep(3)
    
    print(f"  {cat} 完成: {len(existing)}条")

async def main():
    store = DialogueStore(resolve_sync_psycopg_url())
    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("QWEN_KEY")
    for cat, cn in CATS.items():
        await fill_cat(store, cat, cn, api_key)
    print("全部完成!")

asyncio.run(main())
