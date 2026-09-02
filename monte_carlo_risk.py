"""Monte Carlo risk simulation from observed trade returns."""
import random
from typing import Iterable,Dict
class MonteCarloRisk:
    def simulate(self,profits:Iterable[float],initial_balance=100.0,iterations=2000,trades=None,seed=42)->Dict[str,float]:
        values=[float(x) for x in profits]; trades=int(trades or len(values)); rng=random.Random(seed); endings=[]; drawdowns=[]
        if not values:return {'iterations':0,'probability_loss':0.0,'worst_drawdown':0.0,'percentile_5_end_balance':initial_balance}
        for _ in range(max(1,int(iterations))):
            equity=float(initial_balance); peak=equity; dd=0.0
            for value in (rng.choice(values) for _ in range(trades)):
                equity+=value; peak=max(peak,equity); dd=max(dd,peak-equity)
            endings.append(equity); drawdowns.append(dd)
        endings.sort(); index=max(0,int(len(endings)*.05)-1)
        return {'iterations':len(endings),'probability_loss':sum(x<initial_balance for x in endings)/len(endings),'worst_drawdown':max(drawdowns),'percentile_5_end_balance':endings[index],'median_end_balance':endings[len(endings)//2]}
    run=simulate
