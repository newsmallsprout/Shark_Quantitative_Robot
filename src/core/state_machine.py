import os
import time
from typing import Optional

from src.config import SystemState
from src.utils.logger import log

class RiskManager:
    def __init__(self):
        self.initial_balance = 0.0
        self.current_balance = 0.0
        self.daily_high = 0.0
        self.daily_drawdown = 0.0
        self._last_hard_dd_log_t: float = 0.0
        self._last_daily_dd_log_t: float = 0.0

    def update_balance(self, balance: float):
        if self.initial_balance == 0:
            self.initial_balance = balance
            self.daily_high = balance
            
        self.current_balance = balance
        self.daily_high = max(self.daily_high, balance)
        
        # Calculate drawdown from daily high
        if self.daily_high > 0:
            self.daily_drawdown = (self.daily_high - self.current_balance) / self.daily_high
        
    def check_risk(self, hard_limit: float, daily_limit: float) -> bool:
        """
        Returns True for trading continuity.
        Daily/hard drawdown are telemetry / warning only and no longer block trading.
        Limits come from config_manager (settings.yaml), not legacy src.config.RISK.
        """
        now = time.time()
        min_log_gap = 300.0

        if self.daily_drawdown >= hard_limit:
            if now - self._last_hard_dd_log_t >= min_log_gap:
                log.critical(
                    f"HARD DRAWDOWN WARNING: {self.daily_drawdown*100:.2f}% >= {hard_limit*100:.2f}%"
                )
                self._last_hard_dd_log_t = now
            return True

        if self.daily_drawdown >= daily_limit:
            if now - self._last_daily_dd_log_t >= min_log_gap:
                log.warning(
                    f"Daily drawdown warning: {self.daily_drawdown*100:.2f}% >= {daily_limit*100:.2f}%"
                )
                self._last_daily_dd_log_t = now
            return True

        return True

class StateMachine:
    def __init__(self, license_validator):
        self.state = SystemState.OBSERVE
        self.risk_manager = RiskManager()
        self.license_validator = license_validator
        self.last_license_check = 0
        self.cool_down_until = 0
        # When set to NEUTRAL or ATTACK, strategy engine skips automatic regime-based mode switching.
        self.manual_trading_mode: Optional[SystemState] = None

    def set_trading_mode_from_ui(self, mode: str) -> bool:
        """Dashboard: lock NEUTRAL, ATTACK, or BERSERKER；COOL_DOWN 下点选可立即切回（不再被下一 tick 抹掉 manual）。"""
        if self.state == SystemState.LICENSE_LOCKED:
            return False
        key = (mode or "").strip().upper()
        if key == "NEUTRAL":
            self.manual_trading_mode = SystemState.NEUTRAL
            self.transition_to(SystemState.NEUTRAL)
            log.info("Trading mode (manual): NEUTRAL")
            return True
        if key == "ATTACK":
            self.manual_trading_mode = SystemState.ATTACK
            self.transition_to(SystemState.ATTACK)
            log.info("Trading mode (manual): ATTACK")
            return True
        if key == "BERSERKER":
            self.manual_trading_mode = SystemState.BERSERKER
            self.transition_to(SystemState.BERSERKER)
            log.info("Trading mode (manual): BERSERKER")
            return True
        if key == "AUTO" or key == "OBSERVE":
            self.manual_trading_mode = None
            self.transition_to(SystemState.OBSERVE)
            log.info("Trading mode: manual lock released -> OBSERVE (auto regime switch allowed)")
            return True
        return False

    def update(self, balance: float, market_data: dict):
        """
        Main loop to determine state
        """
        from src.core.config_manager import config_manager

        rc = config_manager.get_config().risk
        hard_limit = float(rc.hard_drawdown_limit)
        daily_limit = float(rc.daily_drawdown_limit)
        cool_enabled = bool(getattr(rc, "drawdown_cool_down_enabled", True))
        cool_sec = float(getattr(rc, "drawdown_cool_down_sec", 3600.0) or 0.0)

        # 1. License Check (High Priority)
        now = time.time()
        skip_license = os.environ.get("SKIP_LICENSE_CHECK", "").lower() in ("1", "true", "yes")
        if skip_license:
            self.last_license_check = now
        elif now - self.last_license_check > 3600:  # Check every hour
            if not self.license_validator.validate():
                self.transition_to(SystemState.LICENSE_LOCKED)
                self.last_license_check = now
                return
            self.last_license_check = now
            
        if self.state == SystemState.LICENSE_LOCKED:
            return # Stuck here until restart/re-validate

        self.risk_manager.update_balance(balance)
        risk_ok = self.risk_manager.check_risk(hard_limit, daily_limit)

        # 冷静期结束：回到用户锁定的模式（不再误清 manual 后永远停在 OBSERVE）
        if self.state == SystemState.COOL_DOWN:
            timer_done = (not cool_enabled) or cool_sec <= 0 or (now >= self.cool_down_until)
            if risk_ok or timer_done:
                restore = self.manual_trading_mode or SystemState.OBSERVE
                self.transition_to(restore)
                log.info(f"COOL_DOWN cleared -> {restore.name} (manual_lock={self.manual_trading_mode is not None})")
            if self.state == SystemState.COOL_DOWN:
                return

        # 回撤：仅「未手动锁模式」时进入 COOL_DOWN；已锁 NEUTRAL/ATTACK/BERSERKER 则保持状态，由 risk_engine 是否停机决定能否下单
        if not risk_ok:
            if cool_enabled and self.manual_trading_mode is None:
                self.transition_to(SystemState.COOL_DOWN)
                self.cool_down_until = now + max(0.0, cool_sec)
            return

        # Strategy Logic (Simplified here, usually driven by Strategy Engine)
        # This is where the strategy engine would signal state changes.
        # For now, we assume external triggers or simple logic.
        
    def transition_to(self, new_state: SystemState):
        if self.state != new_state:
            log.info(f"State Transition: {self.state} -> {new_state}")
            self.state = new_state

            # 仅许可证失效时清手动锁；COOL_DOWN 必须保留 manual，否则 UI 选 ATTACK 会在下一 tick 被反复打回
            if new_state == SystemState.LICENSE_LOCKED:
                self.manual_trading_mode = None

            if new_state == SystemState.LICENSE_LOCKED:
                log.warning("SYSTEM LOCKED: LICENSE INVALID")
            elif new_state == SystemState.COOL_DOWN:
                log.warning("SYSTEM ENTERING COOL DOWN MODE")
