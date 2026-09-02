"""Volatility regime, compression, expansion and explosion detector."""
from typing import Any, Dict, Sequence

class VolatilityIntelligence:
    def __init__(self, atr_period=14): self.atr_period=max(3,int(atr_period))
    def analyze(self, candles: Dict[str, Sequence[float]]) -> Dict[str, Any]:
        high, low = [list(map(float,candles[k])) for k in ('high','low')]
        ranges=[h-l for h,l in zip(high,low)]
        current=sum(ranges[-self.atr_period:])/min(len(ranges),self.atr_period)
        baseline=sum(ranges[-self.atr_period*3:])/min(len(ranges),self.atr_period*3)
        ratio=current/max(baseline,1e-12)
        compression=ratio<=0.75; expansion=ratio>=1.25; explosion=ratio>=1.75
        return {'atr':current,'baseline_atr':baseline,'ratio':ratio,'compression':compression,'expansion':expansion,'explosion':explosion,'regime':'explosion' if explosion else 'expansion' if expansion else 'compression' if compression else 'normal','confidence':min(100.0,50+abs(ratio-1)*50)}
