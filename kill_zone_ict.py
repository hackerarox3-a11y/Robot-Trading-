"""ICT kill-zone policy."""
from datetime import datetime,timezone
class ICTKillZone:
    WINDOWS={'london':(7,10),'new_york':(12,15),'london_close':(15,17)}
    def active(self,now=None):
        now=now or datetime.now(timezone.utc); hour=now.hour+now.minute/60
        return [name for name,(start,end) in self.WINDOWS.items() if start<=hour<end]
    def can_trade(self,now=None): return bool(self.active(now))
