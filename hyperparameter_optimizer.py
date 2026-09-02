"""Small deterministic grid optimizer for EMA/RSI/ATR/MACD settings."""
class HyperparameterOptimizer:
    def __init__(self, evaluator): self.evaluator=evaluator
    def optimize(self, candidates=None):
        candidates=candidates or [{'ema_fast':8,'rsi_period':14,'atr_period':14,'macd_fast':12},{'ema_fast':13,'rsi_period':14,'atr_period':14,'macd_fast':12},{'ema_fast':5,'rsi_period':10,'atr_period':14,'macd_fast':8}]
        scored=[(float(self.evaluator(candidate)),candidate) for candidate in candidates]
        score,params=max(scored,key=lambda item:item[0]); return {'best_params':params,'score':score,'tested':len(scored)}
