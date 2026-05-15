import redis
import json

r = redis.Redis(host='localhost', port=6379, decode_responses=True)
state_str = r.get("shark:state")
if state_str:
    state = json.loads(state_str)
    for k, v in state.items():
        print(f"{k}: {type(v)}")
else:
    print("No state in redis")
