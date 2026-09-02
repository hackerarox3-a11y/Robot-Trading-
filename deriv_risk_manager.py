"""Synthetic-index risk controls."""
class DerivRiskManager:
    def multiplier(self,symbol,volatility_score=0):
        family='boom_crash' if str(symbol).startswith(('BOOM','CRASH')) else 'synthetic'
        return .5 if family=='boom_crash' or volatility_score>=80 else 1.0
    def can_trade(self,symbol,probability=0): return probability>=90 if str(symbol).startswith(('BOOM','CRASH')) else True
