from src.config import SystemState, config
from src.utils.logger import log
import time

class RiskManager:
    def __init__(self):
        self.initial_balance = 0.0
        self.current_balance = 0.0
        self.daily_high = 0.0
        self.daily_drawdown = 0.0
        
    def update_balance(self, balance: float):
        if self.initial_balance == 0:
            self.initial_balance = balance
            self.daily_high = balance
            
        self.current_balance = balance
        self.daily_high = max(self.daily_high, balance)
        
        # Calculate drawdown from daily high
        if self.daily_high > 0:
            self.daily_drawdown = (self.daily_high - self.current_balance) / self.daily_high
        
    def check_risk(self) -> bool:
        """
        Returns True if risk is acceptable, False if risk triggered (Cool Down)
        """
        if self.daily_drawdown >= config.RISK.hard_drawdown_limit:
            log.critical(f"HARD DRAWDOWN LIMIT REACHED: {self.daily_drawdown*100:.2f}%")
            return False
            
        if self.daily_drawdown >= config.RISK.daily_drawdown_limit:
            log.warning(f"Daily drawdown limit reached: {self.daily_drawdown*100:.2f}%")
            return False
            
        return True

class StateMachine:
    def __init__(self, license_validator):
        self.state = SystemState.OBSERVE
        self.risk_manager = RiskManager()
        self.license_validator = license_validator
        self.last_license_check = 0
        self.cool_down_until = 0

    def update(self, balance: float, market_data: dict):
        """
        Main loop to determine state
        """
        # 1. License Check (High Priority)
        now = time.time()
        if now - self.last_license_check > 3600: # Check every hour
            if not self.license_validator.validate():
                self.transition_to(SystemState.LICENSE_LOCKED)
                self.last_license_check = now
                return
            self.last_license_check = now
            
        if self.state == SystemState.LICENSE_LOCKED:
            return # Stuck here until restart/re-validate

        # 2. Risk Check
        self.risk_manager.update_balance(balance)
        if not self.risk_manager.check_risk():
            self.transition_to(SystemState.COOL_DOWN)
            self.cool_down_until = now + 3600 # 1 hour cool down
            return

        # 3. Cool Down Logic
        if self.state == SystemState.COOL_DOWN:
            if now < self.cool_down_until:
                return
            else:
                self.transition_to(SystemState.OBSERVE)

        # 4. Strategy Logic (Simplified here, usually driven by Strategy Engine)
        # This is where the strategy engine would signal state changes.
        # For now, we assume external triggers or simple logic.
        
    def transition_to(self, new_state: SystemState):
        if self.state != new_state:
            log.info(f"State Transition: {self.state} -> {new_state}")
            self.state = new_state
            
            if new_state == SystemState.LICENSE_LOCKED:
                log.warning("SYSTEM LOCKED: LICENSE INVALID")
            elif new_state == SystemState.COOL_DOWN:
                log.warning("SYSTEM ENTERING COOL DOWN MODE")
