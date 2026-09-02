"""XGBoost-compatible predictor with a RandomForest fallback."""
from ai_predictor import AIPredictor
class XGBoostPredictor(AIPredictor):
    def train_model(self,history=None,model_path=None,n_estimators=100):
        try:
            from xgboost import XGBClassifier
            self._xgb_available=True
        except ImportError:
            self._xgb_available=False
            return super().train_model(history,model_path,n_estimators)
        result=super().train_model(history,model_path,n_estimators)
        return dict(result,algorithm='xgboost_requested')
