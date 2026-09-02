"""Gold Volume Profile with POC, HVN and LVN."""
class GoldVolumeProfile:
    def analyze(self,data,bins=24):
        close=list(map(float,data.get('close',[]))); volume=list(map(float,data.get('volume',data.get('tick_volume',[]))))
        if not close:return {'poc':None,'hvn':[],'lvn':[]}
        low,high=min(close),max(close); width=(high-low)/max(1,bins); buckets=[0.0]*bins
        for price,vol in zip(close,volume or [1]*len(close)): buckets[min(bins-1,max(0,int((price-low)/max(width,1e-12))))]+=vol
        maximum=max(buckets); minimum=min(buckets); return {'poc':low+(buckets.index(maximum)+.5)*max(width,1e-12),'hvn':[i for i,v in enumerate(buckets) if v>=maximum*.7],'lvn':[i for i,v in enumerate(buckets) if v<=minimum*1.3],'bins':buckets}
