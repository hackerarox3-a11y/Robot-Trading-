"""Gold recovery risk policy after consecutive losses."""
class GoldRecoveryMode:
    def __init__(self,loss_threshold=3,factor=.5): self.loss_threshold=loss_threshold; self.factor=factor
    def multiplier(self,consecutive_losses): return self.factor if int(consecutive_losses)>=self.loss_threshold else 1.0
