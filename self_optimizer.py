"""Bounded self-optimization from historical performance."""
from copy import deepcopy
class SelfOptimizer:
    def __init__(self,base_config=None,limits=None):
        self.base_config=deepcopy(base_config or {}); self.limits=limits or {'ema_fast':(3,21),'rsi_period':(7,30),'atr_sl_multiplier':(0.8,3.0),'macd_fast':(5,20)}
    def optimize(self,history, evaluator):
        current=deepcopy(self.base_config); best_score=float(evaluator(current,history)); changes={}
        for key,(low,high) in self.limits.items():
            old=float(current.get(key,low)); candidates=[max(low,min(high,old-step)) for step in (1,)] + [max(low,min(high,old+1))]
            for candidate in candidates:
                trial=deepcopy(current); trial[key]=int(candidate) if key in ('ema_fast','rsi_period','macd_fast') else candidate; score=float(evaluator(trial,history))
                if score>best_score: best_score,current=score,trial; changes[key]=trial[key]
        return {'config':current,'changes':changes,'score':best_score,'bounded':True}
    def apply(self,config,optimization):
        result=deepcopy(config); result.update(optimization.get('config',{})); return result
