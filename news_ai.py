"""Pre/post-news risk context facade."""
class NewsAI:
    def __init__(self,news_filter): self.news_filter=news_filter
    def assess(self,symbol=''):
        safe,reason=self.news_filter.is_safe_to_trade(symbol); impact=self.news_filter.get_upcoming_impact(2,symbol)
        return {'safe':safe,'reason':reason,'impact':impact,'risk_before':min(100,impact*10),'risk_after':min(100,impact*6)}
