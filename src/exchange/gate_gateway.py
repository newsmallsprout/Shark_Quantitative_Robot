import asyncio
import aiohttp
import hmac
import hashlib
import os
import time
import json
from typing import Dict, Any, Callable, List, Optional, Tuple
from src.utils.logger import log
from src.core.paper_engine import paper_engine

# USDT perpetual public feed and REST are always on Gate mainnet (paper trading uses live quotes).
GATE_MAINNET_REST = "https://api.gateio.ws/api/v4"
GATE_MAINNET_WS = "wss://fx-ws.gateio.ws/v4/ws/usdt"

class GateFuturesGateway:
    """
    High-Frequency Trading Gateway for Gate.io USDT Futures.
    Combines WebSocket for ultra-low latency market data and REST for reliable order execution.
    """
    
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        on_tick: Callable,
        on_orderbook: Callable,
        testnet: bool = False,
        use_paper_trading: bool = True,
        on_trade: Optional[Callable] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.use_paper_trading = use_paper_trading
        
        # Market data (WS + public REST) always uses mainnet — no testnet quote feed.
        self.REST_URL = GATE_MAINNET_REST
        self.WS_URL = GATE_MAINNET_WS
        if use_paper_trading:
            log.info("Paper trading: live mainnet USDT futures quotes + local matching engine.")
            
        # Callbacks for event-driven architecture
        self.on_tick = on_tick
        self.on_orderbook = on_orderbook
        self.on_trade = on_trade
        
        self.session = None
        self.ws = None
        self.running = False
        self.subscribed_symbols = set()
        self.reconnect_count = 0
        self.contract_specs_cache: Dict[str, Dict] = {}
        self.risk_limit_tiers_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._quanto_warmed_symbols: set = set()
        # Last futures.tickers payload per symbol (for UI / WS broadcast without strategy coupling)
        self.latest_tick_by_symbol: Dict[str, Dict[str, Any]] = {}
        # Best bid/ask from futures.order_book (real spread, not a % of last)
        self.latest_book_top: Dict[str, Dict[str, float]] = {}
        # get_symbol_limits 结果缓存（秒级时间戳 + payload），减轻 REST 压力
        self._symbol_limits_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        
    def _generate_signature(self, method: str, url: str, query_string: str = "", payload_str: str = "") -> Dict[str, str]:
        """
        Gate.io v4 API Signature Generator.
        Required for Private REST Endpoints.
        """
        t = int(time.time())
        m = hashlib.sha512()
        m.update(payload_str.encode('utf-8'))
        hashed_payload = m.hexdigest()
        
        s = f"{method}\n{url}\n{query_string}\n{hashed_payload}\n{t}"
        sign = hmac.new(self.api_secret.encode('utf-8'), s.encode('utf-8'), hashlib.sha512).hexdigest()
        
        return {
            'KEY': self.api_key,
            'Timestamp': str(t),
            'SIGN': sign,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

    # ==========================================
    # REST API: Order Execution
    # ==========================================
    
    async def start_rest_session(self):
        if not self.session:
            # Verified TLS (requires CA bundle — install ca-certificates in Docker)
            connector = aiohttp.TCPConnector(ssl=True)
            self.session = aiohttp.ClientSession(connector=connector)
            
    async def close_rest_session(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def create_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float = None,
        reduce_only: bool = False,
        leverage: int = 10,
        margin_mode: str = "cross",
        berserker: bool = False,
        post_only: bool = False,
        entry_context: Optional[Dict[str, Any]] = None,
        exit_reason: Optional[str] = None,
        order_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute an order via REST API.
        Uses POST /futures/usdt/orders
        """
        ect = dict(entry_context or {})
        # Final leverage clamp: always respect exchange max leverage if available.
        try:
            limits = ect.get("symbol_limits") or {}
            ex_max_lev = int(limits.get("max_leverage") or 0)
            if ex_max_lev > 0:
                leverage = int(max(1, min(int(leverage), ex_max_lev)))
                ect["effective_leverage"] = leverage
        except Exception:
            pass
        oid = order_text or ect.get("client_oid") or ect.get("text")
        if self.use_paper_trading:
            return paper_engine.execute_order(
                symbol,
                side,
                amount,
                price,
                reduce_only,
                leverage=leverage,
                margin_mode=margin_mode,
                berserker=berserker,
                post_only=post_only,
                entry_context=ect,
                exit_reason=exit_reason,
                order_text=str(oid)[:28] if oid else None,
            )
            
        await self.start_rest_session()
        
        endpoint = "/futures/usdt/orders"
        url = self.REST_URL + endpoint
        
        # Gate.io Futures uses 'size' (positive for long, negative for short)
        # But 'size' can also be positive if 'close' or 'reduce_only' is specified properly.
        # Standardizing: size is contract amount. Negative for short.
        # But for reduce_only, it depends on current position. 
        # Here we map standard 'buy'/'sell' to Gate.io size logic.
        size = amount if side == 'buy' else -amount
        
        if post_only and price:
            tif = "poc"
        elif price:
            tif = "gtc"
        else:
            tif = "ioc"
        payload = {
            "contract": symbol.replace('/', '_'),  # Gate.io uses BTC_USDT
            "size": int(size),
            "price": str(price) if price else "0",
            "tif": tif,
            "reduce_only": reduce_only,
        }
        # 禁止自动追加保证金（部分合约/账户支持；若下单被拒可设环境变量 SKIP_GATE_AUTO_MARGIN_FALSE=1）
        if os.environ.get("SKIP_GATE_AUTO_MARGIN_FALSE", "").strip().lower() not in (
            "1",
            "true",
            "yes",
        ):
            payload["auto_margin"] = False
        if oid:
            payload["text"] = str(oid)[:28]

        payload_str = json.dumps(payload)
        headers = self._generate_signature("POST", endpoint, "", payload_str)
        
        try:
            async with self.session.post(url, data=payload_str, headers=headers) as response:
                result = await response.json()
                if response.status in [200, 201]:
                    log.info(f"Gate Order Success: {result.get('id')} - {side} {amount} {symbol}")
                    return result
                else:
                    err_msg = result.get('message', 'Unknown Error')
                    err_label = result.get('label', 'UNKNOWN_LABEL')
                    log.error(f"Gate Order Failed [{response.status}]: {err_label} - {err_msg}")
                    return None
        except Exception as e:
            log.error(f"Network error executing order: {e}")
            return None

    async def create_orders_concurrently(self, orders: list[Dict[str, Any]]) -> list[Any]:
        coros = []
        for order in orders:
            coros.append(
                self.create_order(
                    symbol=str(order.get("symbol")),
                    side=str(order.get("side")),
                    amount=float(order.get("amount")),
                    price=order.get("price"),
                    reduce_only=bool(order.get("reduce_only", False)),
                    leverage=int(order.get("leverage", 1)),
                    margin_mode=str(order.get("margin_mode", "cross")),
                    berserker=bool(order.get("berserker", False)),
                    post_only=bool(order.get("post_only", False)),
                    entry_context=dict(order.get("entry_context") or {}),
                    exit_reason=order.get("exit_reason"),
                    order_text=order.get("order_text"),
                )
            )
        return await asyncio.gather(*coros, return_exceptions=True)

    async def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> Dict[str, Any]:
        """
        撤销单笔合约挂单。纸面：释放影子队列 / maker 队列；实盘：DELETE /futures/usdt/orders/{id}
        """
        oid = str(order_id or "").strip()
        if not oid:
            return {"status": "rejected", "reason": "missing order id"}

        if self.use_paper_trading:
            return paper_engine.cancel_local_order(oid)

        await self.start_rest_session()
        endpoint = f"/futures/usdt/orders/{oid}"
        qs = ""
        if symbol:
            qs = f"contract={symbol.replace('/', '_')}"
        url = self.REST_URL + endpoint + ("?" + qs if qs else "")
        payload_str = ""
        headers = self._generate_signature("DELETE", endpoint, qs, payload_str)
        try:
            async with self.session.delete(url, headers=headers) as response:
                txt = await response.text()
                try:
                    result = json.loads(txt) if txt else {}
                except json.JSONDecodeError:
                    result = {"raw": txt}
                if response.status in (200, 201):
                    log.info(f"Gate cancel ok: {oid} {symbol or ''}")
                    return {"status": "canceled", "id": oid, "raw": result}
                err_msg = result.get("message", txt) if isinstance(result, dict) else txt
                log.error(f"Gate cancel failed [{response.status}]: {err_msg}")
                return {"status": "rejected", "reason": str(err_msg)[:500]}
        except Exception as e:
            log.error(f"cancel_order network: {e}")
            return {"status": "rejected", "reason": str(e)}

    async def close_all_positions(self):
        """
        Kill Switch: paper = 一次性平仓（无延迟）；实盘 = 分批 TWAP 降低滑点。
        fetch_positions 的 size 恒为正值，必须用 position['side'] 判断平仓方向。
        """
        try:
            positions = await self.fetch_positions()
            if not positions:
                return

            log.warning("Kill Switch: closing all positions...")

            if self.use_paper_trading:
                # 禁止再用 last±2% 限价：会把浮盈仓「砍」成巨额亏损。走市价逻辑（price=None）
                # 用缓存 orderbook 做 VWAP，与平时吃单一致。
                for pos in positions:
                    symbol = pos.get("symbol")
                    qty = float(pos.get("size", 0))
                    if qty <= 0 or not symbol:
                        continue
                    pos_side = str(pos.get("side", "long")).lower()
                    close_side = "sell" if pos_side in ("long", "buy") else "buy"
                    paper_engine.execute_order(
                        symbol,
                        close_side,
                        qty,
                        None,
                        reduce_only=True,
                        exit_reason="kill_switch_flat",
                    )
                    log.info(f"Kill Switch [Paper] market-flat {symbol} {close_side} {qty} (orderbook VWAP)")
                return

            for pos in positions:
                qty = float(pos.get("size", 0))
                if qty <= 0:
                    continue
                symbol = pos.get("symbol")
                pos_side = str(pos.get("side", "long")).lower()
                close_side = "sell" if pos_side in ("long", "buy") else "buy"
                chunks = 5
                chunk_size = max(1.0, qty / chunks)
                remaining_size = qty

                while remaining_size > 0:
                    current_chunk = min(chunk_size, remaining_size)
                    ticker = await self.fetch_ticker(symbol)
                    if ticker:
                        last_price = float(ticker["last"] or 0)
                        # IOC 限价略劣于最新价以促成交，避免旧版 ±2% 对账户的毁灭性滑点
                        slip = 0.002
                        safe_price = (
                            last_price * (1.0 - slip) if close_side == "sell" else last_price * (1.0 + slip)
                        )
                    else:
                        safe_price = 0

                    endpoint = "/futures/usdt/orders"
                    url = self.REST_URL + endpoint
                    payload = {
                        "contract": symbol.replace("/", "_"),
                        "size": int(-current_chunk) if close_side == "sell" else int(current_chunk),
                        "price": str(safe_price) if safe_price else "0",
                        "tif": "ioc",
                        "reduce_only": True,
                    }
                    if os.environ.get("SKIP_GATE_AUTO_MARGIN_FALSE", "").strip().lower() not in (
                        "1",
                        "true",
                        "yes",
                    ):
                        payload["auto_margin"] = False
                    payload_str = json.dumps(payload)
                    headers = self._generate_signature("POST", endpoint, "", payload_str)

                    await self.start_rest_session()
                    async with self.session.post(url, data=payload_str, headers=headers) as order_res:
                        res = await order_res.json()
                        if order_res.status in [200, 201]:
                            log.info(f"Kill Switch Chunk: {close_side} {current_chunk} {symbol} @ {safe_price}")
                        else:
                            log.error(f"Kill Switch Chunk Failed {symbol}: {res}")

                    remaining_size -= current_chunk
                    if remaining_size > 0:
                        await asyncio.sleep(0.5)

        except Exception as e:
            log.error(f"Error in close_all_positions: {e}")

    # ==========================================
    # WebSocket: Market Data Stream
    # ==========================================
    
    async def subscribe_market_data(self, symbols: List[str]):
        """Add symbols to the subscription list."""
        formatted_symbols = [s.replace('/', '_') for s in symbols]
        self.subscribed_symbols.update(formatted_symbols)

        for s in symbols:
            if s in self._quanto_warmed_symbols:
                continue
            self._quanto_warmed_symbols.add(s)
            asyncio.create_task(self._ensure_quanto_multiplier(s))
        
        # If WS is already running, send subscribe command immediately
        if self.ws and not self.ws.closed:
            await self._send_subscription(formatted_symbols)

    async def _ensure_quanto_multiplier(self, symbol: str) -> None:
        """REST 拉取 quanto_multiplier，供 paper_engine 名义价值/手续费/PnL 对齐 Gate。"""
        try:
            await self.fetch_contract_specs(symbol)
        except Exception as e:
            log.debug(f"[Gate] quanto warm {symbol}: {e}")

    async def _send_subscription(self, symbols: List[str]):
        """Send WS subscribe for tickers + per-contract order book (Gate payload: [contract, depth, interval])."""
        t = int(time.time())

        # 每个合约单独订阅：["BTC_USDT", "20", "0"]，不能写成 ["20","0", ...合约]
        for sym in symbols:
            ob_req = {
                "time": t,
                "channel": "futures.order_book",
                "event": "subscribe",
                "payload": [sym, "20", "0"],
            }
            await self.ws.send_json(ob_req)

        ticker_req = {
            "time": t,
            "channel": "futures.tickers",
            "event": "subscribe",
            "payload": symbols,
        }
        await self.ws.send_json(ticker_req)

        for sym in symbols:
            tr_req = {
                "time": t,
                "channel": "futures.trades",
                "event": "subscribe",
                "payload": [sym],
            }
            await self.ws.send_json(tr_req)

        log.info(
            f"Subscribed futures.order_book + tickers + trades for {len(symbols)} contracts"
        )

    async def start_ws(self):
        """
        Start the WebSocket connection with Auto-Reconnect.
        """
        self.running = True
        await self.start_rest_session()
        
        retry_delay = 1
        
        while self.running:
            try:
                log.info(f"Connecting to Gate.io WS: {self.WS_URL}")
                async with self.session.ws_connect(self.WS_URL, heartbeat=30, ssl=True) as ws:
                    self.ws = ws
                    log.info("Gate.io WS Connected.")
                    retry_delay = 1 # Reset backoff
                    
                    if self.subscribed_symbols:
                        await self._send_subscription(list(self.subscribed_symbols))
                        
                    # Message Loop
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            await self._handle_ws_message(data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                            
            except asyncio.CancelledError:
                log.info("WS Connection Cancelled.")
                break
            except Exception as e:
                log.error(f"Gate.io WS Error: {e}")
                
            if self.running:
                log.warning(f"WS Disconnected. Reconnecting in {retry_delay}s...")
                self.reconnect_count += 1
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30) # Max 30s backoff

    async def stop_ws(self):
        self.running = False
        if self.ws:
            await self.ws.close()
        await self.close_rest_session()
        log.info("GateGateway stopped.")

    async def fetch_balance(self):
        """Fetch balance snapshot with wallet/equity split when available."""
        if self.use_paper_trading:
            return paper_engine.get_balance()
            
        await self.start_rest_session()
        url = self.REST_URL + "/futures/usdt/accounts"
        headers = self._generate_signature("GET", "/futures/usdt/accounts", "")
        try:
            async with self.session.get(url, headers=headers) as response:
                if response.status in [200, 201]:
                    data = await response.json()
                    total = float(data.get("total", 0.0))
                    available = float(data.get("available", 0.0))
                    unrealized = float(
                        data.get("unrealised_pnl", 0.0)
                        or data.get("unrealized_pnl", 0.0)
                        or 0.0
                    )
                    wallet = total - unrealized
                    return {
                        'total': {'USDT': total},
                        'free': {'USDT': available},
                        'wallet_balance': {'USDT': wallet},
                        'unrealized_pnl': {'USDT': unrealized},
                        'accumulated_fee_paid': {'USDT': 0.0},
                        'accumulated_funding_fee': {'USDT': 0.0},
                    }
                else:
                    return {
                        'total': {'USDT': 0.0},
                        'free': {'USDT': 0.0},
                        'wallet_balance': {'USDT': 0.0},
                        'unrealized_pnl': {'USDT': 0.0},
                        'accumulated_fee_paid': {'USDT': 0.0},
                        'accumulated_funding_fee': {'USDT': 0.0},
                    }
        except Exception as e:
            log.error(f"Error fetching balance: {e}")
            return {
                'total': {'USDT': 0.0},
                'free': {'USDT': 0.0},
                'wallet_balance': {'USDT': 0.0},
                'unrealized_pnl': {'USDT': 0.0},
                'accumulated_fee_paid': {'USDT': 0.0},
                'accumulated_funding_fee': {'USDT': 0.0},
            }

    async def fetch_positions(self):
        if self.use_paper_trading:
            return paper_engine.get_positions()
            
        await self.start_rest_session()
        url = self.REST_URL + "/futures/usdt/positions"
        headers = self._generate_signature("GET", "/futures/usdt/positions", "")
        positions = []
        try:
            async with self.session.get(url, headers=headers) as response:
                if response.status in [200, 201]:
                    data = await response.json()
                    for p in data:
                        size = float(p.get("size", 0))
                        if size != 0:
                            positions.append({
                                "symbol": p.get("contract").replace('_', '/'),
                                "side": "long" if size > 0 else "short",
                                "size": abs(size),
                                "entryPrice": float(p.get("entry_price", 0)),
                                "unrealizedPnl": float(p.get("unrealised_pnl", 0)),
                                "leverage": float(p.get("leverage", 1))
                            })
        except Exception as e:
            log.error(f"Error fetching positions: {e}")
        return positions

    async def fetch_open_orders(self, symbol: Optional[str] = None):
        if self.use_paper_trading:
            return paper_engine.list_open_orders_for_gateway(symbol)

        await self.start_rest_session()
        url = self.REST_URL + "/futures/usdt/orders"
        headers = self._generate_signature("GET", "/futures/usdt/orders", "status=open")
        orders = []
        try:
            async with self.session.get(url + "?status=open", headers=headers) as response:
                if response.status in [200, 201]:
                    data = await response.json()
                    for o in data:
                        cid = o.get("contract") or ""
                        sym = str(cid).replace("_", "/")
                        if symbol and sym != symbol:
                            continue
                        sz = float(o.get("size", 0) or 0)
                        left = o.get("left")
                        try:
                            left_f = float(left) if left is not None else abs(sz)
                        except (TypeError, ValueError):
                            left_f = abs(sz)
                        filled = max(abs(sz) - left_f, 0.0) if sz else 0.0
                        st = str(o.get("status") or "open")
                        orders.append({
                            "id": str(o.get("id") or o.get("order_id") or ""),
                            "symbol": sym,
                            "side": "buy" if sz > 0 else "sell",
                            "type": "limit" if o.get("tif") == "gtc" else "market",
                            "price": float(o.get("price", 0)),
                            "amount": abs(sz),
                            "filled": filled,
                            "remaining": left_f,
                            "status": st,
                        })
        except Exception as e:
            log.error(f"Error fetching open orders: {e}")
        return orders

    async def fetch_tickers(self, symbols: List[str]):
        await self.start_rest_session()
        url = self.REST_URL + "/futures/usdt/tickers"
        tickers = {}
        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    for t in data:
                        sym = t.get("contract").replace('_', '/')
                        if sym in symbols:
                            tickers[sym] = {
                                "symbol": sym,
                                "last": float(t.get("last", 0)),
                                "percentage": float(t.get("change_percentage", 0)),
                                "quoteVolume": float(t.get("volume_24h_quote", 0))
                            }
        except Exception as e:
            log.error(f"Error fetching tickers: {e}")
        return tickers

    async def fetch_ticker(self, symbol: str):
        tickers = await self.fetch_tickers([symbol])
        return tickers.get(symbol)
        
    async def fetch_contract_specs(self, symbol: str):
        """Fetch funding, mark/index, quanto_multiplier (contract size in base per 1 contract)."""
        await self.start_rest_session()
        url = self.REST_URL + f"/futures/usdt/contracts/{symbol.replace('/', '_')}"
        specs: Dict[str, Any] = {}
        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    qm = float(data.get("quanto_multiplier") or 0.0)
                    if qm <= 0:
                        qm = 1.0
                    specs = {
                        "symbol": symbol,
                        "funding_rate": float(data.get("funding_rate", 0)),
                        "mark_price": float(data.get("mark_price", 0)),
                        "index_price": float(data.get("index_price", 0)),
                        "next_funding_time": data.get("funding_next_apply", 0),
                        "quanto_multiplier": qm,
                        "order_size_min": float(data.get("order_size_min") or 0),
                        "order_size_max": float(data.get("order_size_max") or 0),
                        "market_order_size_max": float(data.get("market_order_size_max") or 0),
                        "leverage_min": float(data.get("leverage_min") or 1),
                        "leverage_max": float(data.get("leverage_max") or 0),
                        "enable_decimal": bool(data.get("enable_decimal", False)),
                    }
                    self.contract_specs_cache.setdefault(symbol, {}).update(specs)
        except Exception as e:
            log.error(f"Error fetching contract specs: {e}")
        return specs

    async def get_symbol_limits(
        self, symbol: str, ref_price: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        币对交易所约束：最大杠杆、名义上下界（USDT 口径，用于战术裁剪）。
        结果缓存约 1 小时；优先用已同步的 contract_specs_cache / risk_limit_tiers_cache。
        """
        now = time.time()
        ttl_sec = 3600.0
        ck = symbol
        hit = self._symbol_limits_cache.get(ck)
        if hit and (now - hit[0]) < ttl_sec:
            return dict(hit[1])

        await self.start_rest_session()
        specs = await self.fetch_contract_specs(symbol)
        if not specs:
            specs = dict(self.contract_specs_cache.get(symbol) or {})

        qm = float(specs.get("quanto_multiplier") or 0.0)
        if qm <= 0:
            qm = 1.0
        px = float(ref_price or 0.0)
        if px <= 0:
            px = float(specs.get("mark_price") or specs.get("index_price") or 0.0)
        if px <= 0:
            t = self.latest_tick_by_symbol.get(symbol) or {}
            px = float(t.get("last") or t.get("mark_price") or 0.0)
        if px <= 0:
            px = 1.0

        omin = float(specs.get("order_size_min") or 0.0)
        omax = float(specs.get("order_size_max") or 0.0)
        lev_max = float(specs.get("leverage_max") or 125.0)
        lev_min = float(specs.get("leverage_min") or 1.0)

        min_notional = max(0.0, omin * qm * px)
        max_notional = float("inf")
        if omax > 0:
            max_notional = omax * qm * px

        tiers = self.risk_limit_tiers_cache.get(symbol)
        if tiers:
            ordered = sorted(tiers, key=lambda t: float(t.get("risk_limit") or 0.0))
            last = ordered[-1] if ordered else {}
            rl = float(last.get("risk_limit") or 0.0)
            if rl > 0:
                max_notional = min(max_notional, rl) if max_notional < float("inf") else rl

        out: Dict[str, Any] = {
            "symbol": symbol,
            "max_leverage": int(max(1, min(lev_max, 1000))),
            "min_leverage": int(max(1, min(lev_min, int(lev_max) or 125))),
            "min_notional_usdt": min_notional,
            "max_notional_usdt": max_notional,
            "quanto_multiplier": qm,
            "order_size_min": omin,
            "order_size_max": omax,
        }
        self._symbol_limits_cache[ck] = (now, out)
        return dict(out)

    async def sync_usdt_futures_physics_matrix(self) -> None:
        """
        镜像 Gate USDT 永续：全量合约规格 + 风控阶梯表（公开 REST，无需签名）。
        写入 contract_specs_cache / risk_limit_tiers_cache，供 paper_engine 预检。
        """
        await self.start_rest_session()
        if not self.session:
            return
        contracts_url = f"{self.REST_URL}/futures/usdt/contracts"
        tiers_url = f"{self.REST_URL}/futures/usdt/risk_limit_tiers"
        try:
            async with self.session.get(contracts_url) as response:
                if response.status != 200:
                    log.error(f"[Gate] sync contracts HTTP {response.status}")
                    return
                rows = await response.json()
            if not isinstance(rows, list):
                return
            n = 0
            for data in rows:
                if not isinstance(data, dict):
                    continue
                name = str(data.get("name") or "")
                if not name:
                    continue
                sym = name.replace("_", "/")
                qm = float(data.get("quanto_multiplier") or 0.0)
                if qm <= 0:
                    qm = 1.0
                patch = {
                    "symbol": sym,
                    "funding_rate": float(data.get("funding_rate", 0)),
                    "mark_price": float(data.get("mark_price", 0)),
                    "index_price": float(data.get("index_price", 0)),
                    "next_funding_time": data.get("funding_next_apply", 0),
                    "quanto_multiplier": qm,
                    "order_size_min": float(data.get("order_size_min") or 0),
                    "order_size_max": float(data.get("order_size_max") or 0),
                    "market_order_size_max": float(data.get("market_order_size_max") or 0),
                    "leverage_min": float(data.get("leverage_min") or 1),
                    "leverage_max": float(data.get("leverage_max") or 0),
                    "enable_decimal": bool(data.get("enable_decimal", False)),
                }
                self.contract_specs_cache.setdefault(sym, {}).update(patch)
                n += 1
            log.info(f"[Gate] synced {n} USDT futures contract specs (physics matrix)")
        except Exception as e:
            log.error(f"[Gate] sync_usdt_futures_physics_matrix contracts: {e}")
            return

        try:
            async with self.session.get(tiers_url) as response:
                if response.status != 200:
                    log.error(f"[Gate] sync risk_limit_tiers HTTP {response.status}")
                    return
                raw = await response.json()
        except Exception as e:
            log.error(f"[Gate] sync_usdt_futures_physics_matrix tiers: {e}")
            return

        if not isinstance(raw, list):
            return
        by_contract: Dict[str, List[Dict[str, Any]]] = {}
        for row in raw:
            if not isinstance(row, dict):
                continue
            c = str(row.get("contract") or "")
            if not c:
                continue
            sym = c.replace("_", "/")
            by_contract.setdefault(sym, []).append(row)
        for sym, lst in by_contract.items():
            lst.sort(key=lambda t: float(t.get("risk_limit") or 0.0))
            self.risk_limit_tiers_cache[sym] = lst
        log.info(f"[Gate] synced risk_limit_tiers for {len(by_contract)} contracts")

    async def fetch_candlesticks(self, symbol: str, interval: str = "1m", limit: int = 200) -> List[Dict[str, Any]]:
        """Public REST: OHLCV for chart bootstrap (mainnet)."""
        await self.start_rest_session()
        contract = symbol.replace("/", "_")
        url = f"{self.REST_URL}/futures/usdt/candlesticks"
        params = {"contract": contract, "interval": interval, "limit": min(limit, 2000)}
        out: List[Dict[str, Any]] = []
        try:
            async with self.session.get(url, params=params) as response:
                if response.status != 200:
                    return out
                raw = await response.json()
                if not isinstance(raw, list):
                    return out
                for row in raw:
                    if isinstance(row, dict):
                        t = int(row.get("t", 0))
                        o = float(row.get("o", 0) or 0)
                        h = float(row.get("h", 0) or 0)
                        l = float(row.get("l", 0) or 0)
                        c = float(row.get("c", 0) or 0)
                        v = float(row.get("v", row.get("volume", 0)) or 0)
                    elif isinstance(row, (list, tuple)) and len(row) >= 5:
                        t = int(row[0])
                        o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
                        v = float(row[5]) if len(row) >= 6 else 0.0
                    else:
                        continue
                    if t <= 0:
                        continue
                    if v <= 0:
                        v = max(h - l, 1e-12) * 1.0
                    out.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v})
        except Exception as e:
            log.error(f"Error fetching candlesticks {symbol}: {e}")
        return out

    async def reload_config(self):
        from src.core.config_manager import config_manager
        import asyncio
        config = config_manager.get_config().exchange
        self.api_key = config.api_key
        self.api_secret = config.api_secret
        self.testnet = config.sandbox_mode
        self.REST_URL = GATE_MAINNET_REST
        self.WS_URL = GATE_MAINNET_WS

        if self.running:
            await self.stop_ws()
            asyncio.create_task(self.start_ws())

    async def _handle_ws_message(self, data: Dict[str, Any]):
        """
        Parse raw WS messages and route to callbacks.
        """
        channel = data.get("channel")
        event = data.get("event")
        result = data.get("result")

        # Gate may send full snapshot as event "all" on some channels; "update" for deltas.
        if event not in ("update", "all") or not result:
            return
            
        if channel == "futures.tickers":
            # Standardize Ticker Data
            # result is a list of ticker dicts
            for raw_ticker in result:
                symbol = raw_ticker.get("contract").replace('_', '/')
                price = float(raw_ticker.get("last", 0))
                
                # Cache contract specs for frontend
                if symbol not in self.contract_specs_cache:
                    self.contract_specs_cache[symbol] = {}
                    
                vol_q = float(raw_ticker.get("volume_24h_quote", 0))
                self.contract_specs_cache[symbol].update({
                    "symbol": symbol,
                    "funding_rate": float(raw_ticker.get("funding_rate", 0)),
                    "mark_price": float(raw_ticker.get("mark_price", 0)),
                    "index_price": float(raw_ticker.get("index_price", 0)),
                    "change_24h_pct": float(raw_ticker.get("change_percentage", 0) or 0),
                    "24h_volume": vol_q,
                    "volume_24h": vol_q,
                })

                ts_ms = data.get("time_ms") or int(time.time() * 1000)
                ts_sec = float(ts_ms) / 1000.0

                if price > 0:
                    from src.core import l1_fast_loop

                    l1_fast_loop.on_ticker_price(symbol, ts_sec, price)

                # Pass to paper engine for PnL update
                if self.use_paper_trading and price > 0:
                    paper_engine.update_price(symbol, price)
                    from src.core.position_exit_monitor import position_exit_monitor

                    await position_exit_monitor.on_ticker(self, symbol, price)
                standard_ticker = {
                    "symbol": symbol,
                    "last": price,
                    "mark_price": float(raw_ticker.get("mark_price", 0)),
                    "volume": vol_q,
                    "timestamp": ts_ms,
                }
                self.latest_tick_by_symbol[symbol] = standard_ticker
                # Dispatch
                if self.on_tick:
                    await self.on_tick(symbol, standard_ticker)

        elif channel == "futures.trades":
            from src.core import l1_fast_loop

            rows = result if isinstance(result, list) else [result]
            rows = [x for x in rows if isinstance(x, dict)]
            if not rows:
                return
            sym = str(rows[0].get("contract", "")).replace("_", "/")
            if not sym:
                return
            l1_fast_loop.ingest_trades(sym, rows)
            if self.use_paper_trading:
                vol = 0.0
                for r in rows:
                    try:
                        vol += abs(float(r.get("size", 0) or 0))
                    except (TypeError, ValueError):
                        continue
                if vol > 0:
                    paper_engine.note_trade_volume(sym, vol)
            if self.on_trade:
                await self.on_trade(sym, rows)

        elif channel == "futures.order_book":
            # Gate 推送档位为 {"p","s"} 对象，也可能是 [price, size] 元组
            def _parse_levels(raw):
                out = []
                for x in raw or []:
                    if isinstance(x, dict):
                        pv, sv = x.get("p"), x.get("s")
                        if pv is not None and sv is not None:
                            try:
                                out.append([float(pv), float(sv)])
                            except (TypeError, ValueError):
                                continue
                    elif isinstance(x, (list, tuple)) and len(x) >= 2:
                        try:
                            out.append([float(x[0]), float(x[1])])
                        except (TypeError, ValueError):
                            continue
                return out

            symbol = str(result.get("contract", "")).replace("_", "/")
            if not symbol:
                return
            asks = _parse_levels(result.get("asks", []))
            bids = _parse_levels(result.get("bids", []))

            # 增量 update 常只带单侧变化；无完整买卖盘时不覆盖，避免把本地簿清空
            if not bids or not asks:
                return

            # Pass to paper engine for slippage calculation
            if self.use_paper_trading:
                paper_engine.update_orderbook(
                    symbol=symbol,
                    bids=bids,
                    asks=asks
                )
                from src.core.position_exit_monitor import calc_obi, position_exit_monitor

                await position_exit_monitor.on_orderbook(self, symbol, calc_obi(bids, asks))

            if bids and asks:
                bb, ba = float(bids[0][0]), float(asks[0][0])
                spr_abs = max(ba - bb, 0.0)
                mid = (bb + ba) / 2.0 if bb > 0 and ba > 0 else 0.0
                # 相对价差（mid 分数），供前端 ×10000 = bps；勿存绝对美元差以免显示成千级 bps
                spr_frac = (spr_abs / mid) if mid > 0 else 0.0
                self.latest_book_top[symbol] = {
                    "best_bid": bb,
                    "best_ask": ba,
                    "spread": spr_frac,
                    "spread_abs": spr_abs,
                }

            standard_ob = {
                "symbol": symbol,
                "asks": asks, # Sorted ascending by price usually
                "bids": bids, # Sorted descending by price usually
                "timestamp": data.get("time_ms", 0)
            }
            # Dispatch
            if self.on_orderbook:
                await self.on_orderbook(symbol, standard_ob)
