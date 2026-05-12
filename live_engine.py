"""
Shark Live Trading Engine v1.0
Gate.io USDT Perpetual Futures 实盘执行
通过 SHARK_MODE=live + GATE_API_KEY/SECRET 激活
"""

import os, json, time, hmac, hashlib, uuid, logging
from typing import Optional, Dict, Tuple
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)

GATE_BASE = "https://api.gateio.ws/api/v4/futures/usdt"

# ═══════════════════════════════════════════════
# Gate.io API 签名
# ═══════════════════════════════════════════════

def _gate_headers(method: str, path: str, query: str = "", body: str = "") -> dict:
    """生成 Gate.io v4 签名头"""
    key = os.environ.get("GATE_API_KEY", "")
    secret = os.environ.get("GATE_API_SECRET", "")
    if not key or not secret:
        raise RuntimeError("GATE_API_KEY/GATE_API_SECRET 未配置")

    t = str(int(time.time()))
    # Gate v4 签名: sha512(payload)
    payload = f"{method}\n{path}\n{query}\n{hashlib.sha512(body.encode()).hexdigest()}\n{t}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha512).hexdigest()
    return {
        "KEY": key, "Timestamp": t, "SIGN": sig,
        "Content-Type": "application/json", "Accept": "application/json",
    }


def _api(method: str, path: str, body: dict = None, query: str = "", timeout: int = 10) -> dict:
    """调用 Gate.io API，返回 JSON"""
    import urllib.request, urllib.error
    url = f"{GATE_BASE}{path}"
    if query:
        url += f"?{query}"
    data = json.dumps(body) if body else ""
    # Gate.io v4 签名需要完整 API 路径 + query
    full_path = f"/api/v4/futures/usdt{path}"
    headers = _gate_headers(method, full_path, query=query, body=data)

    req = urllib.request.Request(url, data=data.encode() if data else None,
                                  headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        _log.error("Gate API %s %s → %s %s", method, path, e.code, err[:200])
        raise


# ═══════════════════════════════════════════════
# Live Order Executor
# ═══════════════════════════════════════════════

@dataclass
class LivePosition:
    symbol: str
    side: str            # "long" or "short"
    size: int            # 合约张数（Gate.io 整数）
    entry_price: float
    leverage: int
    margin: float
    order_id: str        # 开仓订单 ID
    opened_at: float


class LiveEngine:
    """Gate.io 实盘执行引擎"""

    def __init__(self):
        self.positions: Dict[str, LivePosition] = {}
        self._contract_cache: Dict[str, dict] = {}
        self._order_errors = 0
        self._consecutive_errors = 0
        self._last_sync = 0.0
        self.active = False
        self._verify_credentials()  # 放到最后，不会被覆盖

    def _verify_credentials(self):
        """验证 API 密钥可用性"""
        try:
            info = _api("GET", "/contracts/BTC_USDT")
            if "name" in info:
                _log.info("✅ Gate.io API 连接成功: %s", info.get("name"))
                self.active = True
            else:
                _log.error("❌ Gate.io API 验证失败: %s", info)
        except Exception as e:
            _log.error("❌ Gate.io API 不可达: %s", e)
            self.active = False

    def _sym_to_contract(self, sym: str) -> str:
        """BTC/USDT → BTC_USDT"""
        return sym.replace("/", "_")

    def _contract_to_sym(self, contract: str) -> str:
        """BTC_USDT → BTC/USDT"""
        return contract.replace("_", "/")

    def _get_contract_spec(self, sym: str) -> dict:
        """获取合约规格（缓存）"""
        ct = self._sym_to_contract(sym)
        if ct not in self._contract_cache:
            try:
                self._contract_cache[ct] = _api("GET", f"/contracts/{ct}")
            except Exception:
                return {}
        return self._contract_cache.get(ct, {})

    # ── 杠杆设置 ──

    def set_leverage(self, sym: str, leverage: int) -> bool:
        """开仓前设置杠杆（Gate.io 逐仓模式需要）"""
        ct = self._sym_to_contract(sym)
        try:
            _api("POST", f"/positions/{ct}/leverage",
                 body={"leverage": str(leverage)})
            return True
        except Exception as e:
            _log.warning("设置杠杆失败 %s %dx: %s", sym, leverage, e)
            return False

    # ── 开仓 ──

    def open_position(self, sym: str, side: str, size: int,
                      leverage: int, px: float = 0) -> Optional[str]:
        """
        下市价单开仓，返回 order_id（失败返回 None）
        side: "long" / "short"
        size: 合约张数（整数）
        """
        ct = self._sym_to_contract(sym)
        contract_size = 0
        spec = self._get_contract_spec(sym)

        # 检查最小下单量
        min_size = spec.get("order_size_min", 1)
        if size < min_size:
            _log.warning("%s size=%d < min=%d, bump", sym, size, min_size)
            size = min_size

        # 设置杠杆
        self.set_leverage(sym, leverage)

        # 下单
        body = {
            "contract": ct,
            "size": size,
            "price": "0",           # 市价
            "tif": "ioc",           # 立即成交或取消
            "text": f"t-shark-{uuid.uuid4().hex[:6]}",
        }
        # 方向：做空用 size 负数
        if side == "short":
            body["size"] = -size

        try:
            result = _api("POST", "/orders", body=body)
            oid = str(result.get("id", ""))
            fill_price = float(result.get("fill_price", 0) or 0)
            fill_size = abs(int(result.get("size", 0) or 0))
            status = result.get("status", "")

            if status == "finished" and fill_size > 0:
                _log.info("✅ 开仓 %s %s size=%d px=%s oid=%s",
                          sym, side.upper(), fill_size, fill_price, oid)
                self._consecutive_errors = 0
                return oid
            else:
                _log.error("❌ 开仓未成交 %s status=%s result=%s", sym, status, result)
                self._order_errors += 1
                self._consecutive_errors += 1
                return None

        except Exception as e:
            _log.error("❌ 开仓异常 %s: %s", sym, e)
            self._order_errors += 1
            self._consecutive_errors += 1
            return None

    # ── 平仓 ──

    def close_position(self, sym: str, side: str, size: int) -> Tuple[bool, float]:
        """
        市价平仓，返回 (成功, 成交均价)
        """
        ct = self._sym_to_contract(sym)
        # 平仓方向：做多→卖(size), 做空→买(size)
        close_size = -size if side == "long" else size
        body = {
            "contract": ct,
            "size": -size if side == "long" else size,  # 反向平仓
            "price": "0",
            "tif": "ioc",
            "reduce_only": True,
            "text": f"t-shark-close-{uuid.uuid4().hex[:6]}",
        }

        try:
            result = _api("POST", "/orders", body=body)
            fill_price = float(result.get("fill_price", 0) or 0)
            status = result.get("status", "")
            if status == "finished" and fill_price > 0:
                _log.info("✅ 平仓 %s %s @ %s", sym, side.upper(), fill_price)
                self._consecutive_errors = 0
                return True, fill_price
            else:
                _log.error("❌ 平仓未成交 %s status=%s", sym, status)
                self._consecutive_errors += 1
                return False, 0

        except Exception as e:
            _log.error("❌ 平仓异常 %s: %s", sym, e)
            self._consecutive_errors += 1
            return False, 0

    # ── 持仓同步 ──

    def sync_positions(self) -> Dict[str, dict]:
        """
        从交易所拉取实际持仓，返回 {sym: {size, entry_price, leverage, margin, unrealised_pnl}}
        """
        try:
            result = _api("GET", "/positions")
            positions = {}
            for p in result:
                size = int(p.get("size", 0) or 0)
                if size == 0:
                    continue
                sym = self._contract_to_sym(p.get("contract", ""))
                positions[sym] = {
                    "size": abs(size),
                    "side": "long" if size > 0 else "short",
                    "entry_price": float(p.get("entry_price", 0) or 0),
                    "leverage": int(p.get("leverage", 1) or 1),
                    "margin": float(p.get("margin", 0) or 0),
                    "unrealised_pnl": float(p.get("unrealised_pnl", 0) or 0),
                }
            self._last_sync = time.time()
            return positions
        except Exception as e:
            _log.error("持仓同步失败: %s", e)
            return {}

    # ── 账户余额 ──

    def get_balance(self) -> float:
        """获取 USDT 可用余额"""
        try:
            # 用合约账户接口
            result = _api("GET", "/accounts", query=f"currency=USDT")
            if isinstance(result, dict) and "available" in result:
                return float(result["available"])
            if isinstance(result, list) and result:
                return float(result[0].get("available", 0))
        except Exception as e:
            _log.error("获取余额失败: %s", e)
        return 0.0

    # ── 熔断状态 ──

    @property
    def is_healthy(self) -> bool:
        """是否可以继续交易"""
        if not self.active:
            return False
        if self._consecutive_errors >= 3:
            _log.error("🚨 连续 %d 次订单错误，熔断！", self._consecutive_errors)
            return False
        return True

    def stats(self) -> dict:
        return {
            "active": self.active,
            "positions": len(self.positions),
            "order_errors": self._order_errors,
            "consecutive_errors": self._consecutive_errors,
            "last_sync": self._last_sync,
        }


# ═══════════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════════

def create_live_engine(mode: Optional[str] = None) -> Optional[LiveEngine]:
    """创建实盘引擎。mode 缺省时读 SHARK_MODE；运行时切换可显式传入 paper/live。"""
    m = (mode if mode is not None else os.environ.get("SHARK_MODE", "paper")).strip().lower()
    if m != "live":
        return None

    key = os.environ.get("GATE_API_KEY", "")
    secret = os.environ.get("GATE_API_SECRET", "")
    if not key or not secret:
        _log.warning("SHARK_MODE=live 但 GATE_API_KEY/SECRET 未配置，回退到 paper")
        return None

    engine = LiveEngine()
    if not engine.active:
        _log.error("实盘引擎初始化失败，回退到 paper 模式")
        return None

    _log.info("🔥 实盘模式已激活")
    return engine
