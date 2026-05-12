"""补 stoploss 到500条"""
import asyncio, json, os, sys
sys.path.insert(0, "/app")
import aiohttp
from persistence.dialogue_store import DialogueStore, resolve_sync_psycopg_url

async def main():
    store = DialogueStore(resolve_sync_psycopg_url())
    key = os.environ.get("DEEPSEEK_API_KEY")
    
    from persistence.models import DialogueLine
    from sqlalchemy import select
    existing = set()
    with store._session_factory() as s:
        rows = s.scalars(select(DialogueLine.line).where(DialogueLine.category == "stoploss")).all()
        existing = set(rows)
    print(f"已有 {len(existing)} 条 stoploss")
    
    while len(existing) < 500:
        need = min(50, 500 - len(existing))
        sample = list(existing)[-25:] if existing else ["尚无"]
        
        prompt = f"生成{need}条【止损割肉】台词。每条≤15汉字，短平快。每条主题/句式/用词完全不一样，不能改一两个字当新的！用币圈粗口(卧槽、狗庄、淦)和抽象梗(上天台、画门、断头台)。可打破第四面墙。\n\n禁止重复:\n" + "\n".join(sample) + "\n\n只输出JSON数组：[\"台词1\",\"台词2\",...]"
        
        async with aiohttp.ClientSession() as s:
            async with s.post("https://api.deepseek.com/v1/chat/completions",
                json={"model":"deepseek-chat","temperature":1.3,"max_tokens":4096,
                      "messages":[{"role":"user","content":prompt}]},
                headers={"Authorization":f"Bearer {key}"},
                timeout=aiohttp.ClientTimeout(total=60)) as r:
                data = await r.json()
                content = data["choices"][0]["message"]["content"]
        
        start = content.find("["); end = content.rfind("]")+1
        if start < 0: continue
        candidates = json.loads(content[start:end])
        
        added = 0
        for line in candidates:
            line = line.strip()
            if not line or len(line)>16 or line in existing: continue
            dup = False
            for e in list(existing)[-50:]:
                sa, sb = set(line), set(e)
                if sa and sb and len(sa&sb)/max(len(sa),len(sb))>0.7: dup=True; break
            if dup: continue
            existing.add(line)
            store.insert_unique("stoploss", line)
            added += 1
        
        print(f"+{added} 累计{len(existing)}/500")
    
    print(f"完成: {len(existing)}条")

asyncio.run(main())
