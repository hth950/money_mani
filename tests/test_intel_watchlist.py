from web.services.market_intel_service import MarketIntelService


def test_numeric_news_fragments_do_not_become_us_watchlist_symbols(monkeypatch):
    service = MarketIntelService()
    monkeypatch.setattr(service, "get_issues", lambda days: [{
        "confidence": 0.9,
        "affected_tickers": [{"ticker": value} for value in
                             ("0", "499", "1234567", None, "005930", "BRK-B", "BRK-B")],
    }])
    picks = service.get_high_confidence_tickers()
    assert [row["ticker"] for row in picks["KRX"]] == ["005930"]
    assert [row["ticker"] for row in picks["US"]] == ["BRK-B"]
