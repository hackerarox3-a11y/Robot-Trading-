"""Cross-market correlation and exposure context."""
from typing import Any, Dict, Sequence
import math
class CorrelationEngine:
    SYMBOLS=('DXY','XAUUSDm','EURUSDm','GBPUSDm','USDJPYm')
    def analyze(self, markets: Dict[str, Sequence[float]]) -> Dict[str, Any]:
        result={}
        base=markets.get('DXY')
        for symbol in self.SYMBOLS:
            values=markets.get(symbol)
            if not values or not base or len(values)!=len(base) or len(values)<3: result[symbol]={'correlation':None,'available':False}; continue
            x,y=list(map(float,base)),list(map(float,values)); mx,my=sum(x)/len(x),sum(y)/len(y); num=sum((a-mx)*(b-my) for a,b in zip(x,y)); den=math.sqrt(sum((a-mx)**2 for a in x)*sum((b-my)**2 for b in y)); result[symbol]={'correlation':round(num/den,4) if den else 0.0,'available':True}
        return {'markets':result,'gold_dollar_inverse':result.get('XAUUSDm',{}).get('correlation'),'bias':'risk_on' if result.get('DXY',{}).get('correlation',0)>0 else 'neutral'}
