"""Incremental trade-learning helper."""
from ai_predictor import AIPredictor
class AutoLearning:
    def __init__(self, predictor=None): self.predictor=predictor or AIPredictor()
    def learn_from_trades(self, trades, model_path=None): return self.predictor.train_model(trades,model_path=model_path)
    def retrain(self, history='trades_history.csv'): return self.predictor.train_model(history)
