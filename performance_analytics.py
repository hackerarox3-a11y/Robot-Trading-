"""Trading performance analytics."""
import math
from typing import Iterable, Dict
class PerformanceAnalytics:
    @staticmethod
    def calculate(profits: Iterable[float], initial_balance: float = 0.0) -> Dict[str,float]:
        values=[float(x) for x in profits]; wins=[x for x in values if x>0]; losses=[x for x in values if x<0]
        win_rate=len(wins)/len(values) if values else 0.0
        gross_profit=sum(wins); gross_loss=abs(sum(losses)); pf=gross_profit/gross_loss if gross_loss else (math.inf if gross_profit else 0.0)
        expectancy=sum(values)/len(values) if values else 0.0
        mean=expectancy; variance=sum((x-mean)**2 for x in values)/(len(values)-1) if len(values)>1 else 0.0
        sharpe=mean/math.sqrt(variance)*math.sqrt(len(values)) if variance else 0.0
        equity=float(initial_balance); peak=equity; max_dd=0.0
        for value in values:
            equity+=value; peak=max(peak,equity); max_dd=max(max_dd,peak-equity)
        return {'trades':len(values),'win_rate':win_rate,'profit_factor':pf,'expectancy':expectancy,'sharpe_ratio':sharpe,'drawdown':max_dd,'drawdown_percent':max_dd/peak*100 if peak>0 else 0.0,'total_profit':sum(values)}
    def analyze(self,profits,initial_balance=0.0): return self.calculate(profits,initial_balance)
