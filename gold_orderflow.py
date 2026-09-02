"""Gold-specific order-flow facade."""
from volume_footprint import VolumeFootprint
class GoldOrderFlow(VolumeFootprint):
    def analyze(self,data):
        result=super().analyze(data); result['symbol']='XAUUSDm'; result['momentum']=(float(data['close'][-1])-float(data['close'][-6])) if len(data.get('close',[]))>=6 else 0.0; return result
