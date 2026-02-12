from src.shell.contract import StrategyBase, Signal, RiskLimits

class Strategy(StrategyBase):
    """Test strategy."""
    def initialize(self, risk_limits: RiskLimits, symbols: list[str]) -> None:
        pass
    def analyze(self, markets, portfolio, timestamp):
        return []