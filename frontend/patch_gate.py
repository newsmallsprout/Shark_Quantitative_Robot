import json
from src.exchange.gate_gateway import GateFuturesGateway

async def fetch_balance(self):
    await self.start_rest_session()
    url = self.REST_URL + "/futures/usdt/accounts"
    headers = self._generate_signature("GET", "/futures/usdt/accounts", "")
    try:
        async with self.session.get(url, headers=headers) as response:
            if response.status in [200, 201]:
                data = await response.json()
                total = float(data.get("total", 0.0))
                return {'total': {'USDT': total}, 'free': {'USDT': float(data.get("available", 0.0))}}
            else:
                return {'total': {'USDT': 0.0}, 'free': {'USDT': 0.0}}
    except Exception as e:
        return {'total': {'USDT': 0.0}, 'free': {'USDT': 0.0}}

async def fetch_positions(self):
    await self.start_rest_session()
    url = self.REST_URL + "/futuresimport json
from src.exchange.gate_gateway import atfrom src.e "
async def fetch_balance(self):
    await self.start_rey:
    await self.start_rest_seson    url = self.REST_URL + "/futureon    headers = self._generate_signature("GET", "/f
     try:
        async with self.session.get(url, headers=headers) as resp
                      if response.status in [200, 201]:
                data = !=                data = await response.json()({                total = float(data.get("tott(                return {'tota'/'),
                   else:
                return {'0 else short,