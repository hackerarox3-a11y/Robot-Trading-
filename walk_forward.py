"""Walk-forward optimization with non-overlapping train/test windows."""
from typing import Any, Callable, Dict, Sequence
class WalkForwardOptimizer:
    def __init__(self,train_size=100,test_size=25,step=None): self.train_size=int(train_size); self.test_size=int(test_size); self.step=int(step or test_size)
    def run(self,data:Sequence[Any],train_fn:Callable,test_fn:Callable)->Dict[str,Any]:
        windows=[]; start=0
        while start+self.train_size+self.test_size<=len(data):
            train=data[start:start+self.train_size]; test=data[start+self.train_size:start+self.train_size+self.test_size]
            model=train_fn(train); result=test_fn(model,test); windows.append({'start':start,'train_size':len(train),'test_size':len(test),'result':result}); start+=self.step
        scores=[float(w['result'].get('score',w['result'].get('profit',0))) for w in windows]
        return {'windows':windows,'windows_count':len(windows),'average_score':sum(scores)/len(scores) if scores else 0.0}
