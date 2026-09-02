"""Gold Expert: Asian range, London breakout, NY reversal and ICT kill zones."""
from datetime import datetime, timezone
class GoldExpert:
    KILL_ZONES={'london':(7,10),'new_york':(12,15),'ny_reversal':(15,17)}
    def analyze(self,candles,timestamp=None,timeframe='M5'):
        now=timestamp or datetime.now(timezone.utc); hour=now.hour+now.minute/60
        session='closed'
        for name,(start,end) in self.KILL_ZONES.items():
            if start<=hour<end: session=name; break
        high,low,close=candles.get('high',[]),candles.get('low',[]),candles.get('close',[])
        asian_high=max(high[:min(24,len(high))]) if len(high) else None; asian_low=min(low[:min(24,len(low))]) if len(low) else None
        breakout=bool(len(close) and asian_high is not None and (close[-1]>asian_high or close[-1]<asian_low))
        mode='scalp' if timeframe in ('M1','M5') else 'swing' if timeframe in ('H1','H4') else 'inactive'
        return {'symbol':'XAUUSDm','timeframe':timeframe,'mode':mode,'session':session,'kill_zone':session!='closed','asian_range':{'high':asian_high,'low':asian_low},'london_breakout':breakout and session=='london','ny_reversal':session=='ny_reversal','allowed':session!='closed','confidence':70.0 if session!='closed' else 0.0}
