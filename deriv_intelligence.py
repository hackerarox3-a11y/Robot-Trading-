"""Specialized synthetic-index intelligence and selector."""
from typing import Any, Dict, Sequence
class DerivIntelligence:
    def analyze(self,symbol:str,candles:Dict[str,Sequence[float]])->Dict[str,Any]:
        close=list(map(float,candles.get('close',[]))); high=list(map(float,candles.get('high',[]))); low=list(map(float,candles.get('low',[])))
        if len(close)<10:return {'symbol':symbol,'signal':'WAIT','probability':0.0,'reason':'insufficient_history'}
        momentum=close[-1]-close[-6]; atr=sum(h-l for h,l in zip(high[-14:],low[-14:]))/min(14,len(close)); trend='bullish' if momentum>atr else 'bearish' if momentum<-atr else 'range'
        family='volatility' if symbol.startswith('R_') else 'boom' if symbol.startswith('BOOM') else 'crash' if symbol.startswith('CRASH') else 'step' if 'STEP' in symbol.upper() else 'jump' if 'JUMP' in symbol.upper() else 'range'
        explosion=abs(momentum)>atr*2; probability=min(99.0,50+abs(momentum)/max(atr,1e-12)*15)
        signal='BUY SPIKE' if family=='boom' and explosion else 'SELL SPIKE' if family=='crash' and explosion else 'BUY' if family in ('volatility','step','jump') and trend=='bullish' else 'SELL' if family in ('volatility','step','jump') and trend=='bearish' else 'WAIT'
        return {'symbol':symbol,'family':family,'trend':trend,'pullback':abs(momentum)<atr,'explosion':explosion,'signal':signal,'probability':round(probability,2),'confidence':round(probability,2),'risk_factor':0.5 if family in ('boom','crash') else 1.0}
    def select_market(self,markets:Dict[str,Dict[str,Sequence[float]]])->Dict[str,Any]:
        ranked=[self.analyze(symbol,data) for symbol,data in markets.items()]; return max(ranked,key=lambda x:x.get('confidence',0),default={'signal':'WAIT'})
