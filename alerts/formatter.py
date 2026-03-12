"""Discord embed formatter for trading alerts."""


class AlertFormatter:
    """Format trading data into Discord embed dicts."""

    COLOR_BUY = 0x00B050   # green
    COLOR_SELL = 0xFF0000  # red
    COLOR_INFO = 0x3498DB  # blue
    COLOR_WARN = 0xF39C12  # orange

    @staticmethod
    def format_signal_alert(signal: dict, extra_info: dict = None) -> dict:
        """Format a trading signal for a Discord embed.

        Args:
            signal: {
                strategy_name, ticker, ticker_name, signal_type (BUY/SELL),
                price, indicators: {}, date, consensus_count, consensus_strategies
            }
            extra_info: Optional dict with consensus/strategies display info.

        Returns:
            Discord embed dict.
        """
        signal_type = signal.get("signal_type", "").upper()
        color = AlertFormatter.COLOR_BUY if signal_type == "BUY" else AlertFormatter.COLOR_SELL
        action_label = "매수" if signal_type == "BUY" else "매도"
        action_emoji = "🟢" if signal_type == "BUY" else "🔴"

        consensus_n = signal.get("consensus_count")
        if consensus_n:
            title = f"{action_emoji} [{action_label}] {signal.get('ticker_name', signal.get('ticker', ''))} — {consensus_n}개 전략 합의"
        else:
            title = f"{action_emoji} [{action_label}] {signal.get('ticker_name', signal.get('ticker', ''))} 신호 발생"

        fields = [
            {"name": "종목", "value": f"{signal.get('ticker_name', '-')} ({signal.get('ticker', '-')})", "inline": True},
            {"name": "신호", "value": f"{action_emoji} {action_label}", "inline": True},
            {"name": "현재가", "value": f"{signal.get('price', 0):,.0f} 원", "inline": True},
            {"name": "날짜", "value": signal.get("date", "-"), "inline": True},
        ]

        # Add consensus info
        if extra_info:
            if extra_info.get("consensus"):
                fields.append({"name": "합의", "value": extra_info["consensus"], "inline": True})
            if extra_info.get("strategies"):
                fields.append({"name": "동의 전략", "value": extra_info["strategies"], "inline": False})
            # Score breakdown (if multi-layer scoring is active)
            if extra_info.get("composite_score"):
                fields.append({
                    "name": "📊 종합 점수",
                    "value": extra_info["composite_score"],
                    "inline": True,
                })
            if extra_info.get("score_decision"):
                fields.append({
                    "name": "결정",
                    "value": extra_info["score_decision"],
                    "inline": True,
                })
            if extra_info.get("score_breakdown"):
                fields.append({
                    "name": "점수 상세",
                    "value": extra_info["score_breakdown"],
                    "inline": False,
                })
        elif consensus_n:
            strats = signal.get("consensus_strategies", [])
            fields.append({"name": "합의 수", "value": f"{consensus_n}개 전략", "inline": True})
            if strats:
                strat_text = ", ".join(strats[:5])
                if len(strats) > 5:
                    strat_text += f" 외 {len(strats)-5}개"
                fields.append({"name": "동의 전략", "value": strat_text, "inline": False})

        indicators = signal.get("indicators", {})
        if indicators:
            ind_lines = "\n".join(
                f"**{k}**: {v:,.0f}" if isinstance(v, (int, float)) else f"**{k}**: {v}"
                for k, v in list(indicators.items())[:6]
            )
            fields.append({"name": "지표", "value": ind_lines, "inline": False})

        return {
            "title": title,
            "color": color,
            "fields": fields,
            "footer": {"text": "Money Mani 앙상블 트레이딩"},
        }

    @staticmethod
    def format_exit_signal_alert(signal: dict) -> dict:
        """Format an exit scoring signal for Discord embed.

        Args:
            signal: Exit signal dict with exit_decision, exit_score, exit_reason,
                    exit_scores, exit_details, ticker, pnl_pct, etc.

        Returns:
            Discord embed dict.
        """
        decision = signal.get("exit_decision", "")
        if decision == "SELL_EXECUTE":
            color = AlertFormatter.COLOR_SELL
            emoji = "🔴"
            label = "즉시 매도 권장"
        else:
            color = AlertFormatter.COLOR_WARN
            emoji = "🟡"
            label = "매도 주의"

        pnl = signal.get("pnl_pct", 0)
        pnl_sign = "+" if pnl >= 0 else ""

        fields = [
            {"name": "종목", "value": f"{signal.get('ticker_name', '-')} ({signal.get('ticker', '-')})", "inline": True},
            {"name": "결정", "value": f"{emoji} {label}", "inline": True},
            {"name": "현재가", "value": f"{signal.get('price', 0):,.0f}원", "inline": True},
            {"name": "진입가", "value": f"{signal.get('entry_price', 0):,.0f}원", "inline": True},
            {"name": "수익률", "value": f"{pnl_sign}{pnl:.2%}", "inline": True},
            {"name": "종합 점수", "value": f"{signal.get('exit_score', 0):.2f}", "inline": True},
        ]

        # Score breakdown
        scores = signal.get("exit_scores", {})
        if scores:
            score_text = (
                f"추세: {scores.get('trend', 0):.2f} | "
                f"모멘텀: {scores.get('momentum', 0):.2f} | "
                f"트레일링: {scores.get('trailing_stop', 0):.2f}"
            )
            fields.append({"name": "점수 상세", "value": score_text, "inline": False})

        # Reason
        fields.append({"name": "사유", "value": signal.get("exit_reason", "-"), "inline": False})

        # Technical details
        details = signal.get("exit_details", {})
        detail_lines = []
        if "ema5" in details:
            detail_lines.append(f"EMA5: {details['ema5']:,.0f} | EMA20: {details['ema20']:,.0f}")
        if "rsi14" in details:
            detail_lines.append(f"RSI(14): {details['rsi14']:.1f}")
        if "trailing_stop_price" in details:
            detail_lines.append(f"트레일링 스톱: {details['trailing_stop_price']:,.0f}원")
        if "high_since_entry" in details:
            detail_lines.append(f"진입 후 고점: {details['high_since_entry']:,.0f}원")
        if detail_lines:
            fields.append({"name": "기술적 지표", "value": "\n".join(detail_lines), "inline": False})

        return {
            "title": f"{emoji} [매도 {label}] {signal.get('ticker_name', '')}",
            "color": color,
            "fields": fields,
            "footer": {"text": "Money Mani 추세 기반 매도 판단"},
        }

    @staticmethod
    def format_daily_scoring_report(summary: dict) -> dict:
        """Format daily scoring summary for Discord."""
        execute = summary.get("execute_count", 0)
        watch = summary.get("watch_count", 0)
        skip = summary.get("skip_count", 0)
        blocked = summary.get("blocked_count", 0)
        avg_score = summary.get("avg_composite_score", 0)

        description = (
            f"**EXECUTE**: {execute}건 | **WATCH**: {watch}건 | "
            f"**SKIP**: {skip}건 | **BLOCKED**: {blocked}건\n"
            f"평균 종합점수: {avg_score:.0%}" if avg_score else
            f"**EXECUTE**: {execute}건 | **WATCH**: {watch}건 | "
            f"**SKIP**: {skip}건 | **BLOCKED**: {blocked}건"
        )

        embed = {
            "title": f"📊 일일 스코어링 리포트 ({summary.get('date', 'today')})",
            "description": description,
            "color": 0x3498DB,
            "fields": [],
        }

        # Top scorers
        top_scores = summary.get("top_scores", [])
        if top_scores:
            top_text = "\n".join(
                f"• {s['ticker']} ({s.get('ticker_name', '')}) - {s['composite_score']:.0%} [{s['decision']}]"
                for s in top_scores[:5]
            )
            embed["fields"].append({
                "name": "🏆 상위 종목",
                "value": top_text,
                "inline": False,
            })

        return embed

    @staticmethod
    def format_backtest_report(result: dict) -> dict:
        """Format backtest results as a Discord embed.

        Args:
            result: backtest result dict with strategy info and metrics.

        Returns:
            Discord embed dict.
        """
        metrics = result.get("metrics", result)
        is_valid = result.get("is_valid", True)
        color = AlertFormatter.COLOR_INFO if is_valid else AlertFormatter.COLOR_WARN
        validity_label = "✅ 유효" if is_valid else "❌ 무효"

        total_return = metrics.get("total_return", metrics.get("return", 0))
        if isinstance(total_return, float) and abs(total_return) < 10:
            return_str = f"{total_return:.2%}"
        else:
            return_str = f"{total_return:.2f}%"

        sharpe = metrics.get("sharpe_ratio", metrics.get("sharpe", 0))
        mdd = metrics.get("max_drawdown", metrics.get("mdd", 0))
        if isinstance(mdd, float) and abs(mdd) <= 1:
            mdd_str = f"{mdd:.2%}"
        else:
            mdd_str = f"{mdd:.2f}%"

        win_rate = metrics.get("win_rate", 0)
        if isinstance(win_rate, float) and win_rate <= 1:
            win_rate_str = f"{win_rate:.2%}"
        else:
            win_rate_str = f"{win_rate:.1f}%"

        fields = [
            {"name": "전략명", "value": result.get("strategy_name", "-"), "inline": True},
            {"name": "종목", "value": result.get("ticker", "-"), "inline": True},
            {"name": "기간", "value": result.get("period", "-"), "inline": True},
            {"name": "수익률", "value": return_str, "inline": True},
            {"name": "샤프 비율", "value": f"{sharpe:.2f}", "inline": True},
            {"name": "최대낙폭 (MDD)", "value": mdd_str, "inline": True},
            {"name": "승률", "value": win_rate_str, "inline": True},
            {"name": "거래 횟수", "value": str(metrics.get("trade_count", metrics.get("total_trades", 0))), "inline": True},
            {"name": "검증 결과", "value": validity_label, "inline": True},
        ]

        return {
            "title": f"📊 백테스트 결과 - {result.get('strategy_name', '전략')}",
            "color": color,
            "fields": fields,
            "footer": {"text": "Money Mani 트레이딩 시스템"},
        }

    @staticmethod
    def format_realtime_signal(signal: dict) -> dict:
        """Format a real-time trading signal for Discord embed."""
        signal_type = signal.get("signal_type", "").upper()
        is_holding = signal.get("is_holding", False)
        color = AlertFormatter.COLOR_BUY if signal_type == "BUY" else AlertFormatter.COLOR_SELL
        action_emoji = "🟢" if signal_type == "BUY" else "🔴"
        action_label = "매수 신호" if signal_type == "BUY" else "매도 신호"
        currency = signal.get("currency", "원")

        ticker_display = f"{signal.get('ticker_name', '')}({signal.get('ticker', '')})"
        title_suffix = " - 보유중" if is_holding and signal_type == "SELL" else ""
        title = f"{action_emoji} [{action_label}] {ticker_display}{title_suffix}"

        fields = [
            {"name": "전략", "value": signal.get("strategy_name", "-"), "inline": True},
            {"name": "현재가", "value": f"{signal.get('price', 0):,.0f}{currency}", "inline": True},
            {"name": "시간", "value": signal.get("timestamp", "-"), "inline": True},
        ]

        holding = signal.get("holding")
        if holding and signal_type == "SELL":
            avg = holding.avg_price if hasattr(holding, "avg_price") else holding.get("avg_price", 0)
            pnl = holding.pnl_pct if hasattr(holding, "pnl_pct") else holding.get("pnl_pct", 0)
            pnl_sign = "+" if pnl >= 0 else ""
            fields.append({"name": "매수가", "value": f"{avg:,.0f}{currency}", "inline": True})
            fields.append({"name": "수익률", "value": f"{pnl_sign}{pnl:.2f}%", "inline": True})

        indicators = signal.get("indicators", {})
        if indicators:
            ind_lines = " | ".join(
                f"{k}: {v:,.1f}" if isinstance(v, (int, float)) else f"{k}: {v}"
                for k, v in list(indicators.items())[:6]
            )
            fields.append({"name": "지표", "value": ind_lines, "inline": False})

        return {
            "title": title,
            "color": color,
            "fields": fields,
            "footer": {"text": "Money Mani 실시간 모니터링"},
        }

    @staticmethod
    def format_daily_summary(signals: list, date: str, ensemble_n: int = None,
                              consensus_summary: dict = None) -> dict:
        """Format a daily summary grouped by ticker with consensus.

        Args:
            signals: list of signal dicts (ensemble-filtered).
            date: date string (YYYY-MM-DD).
            ensemble_n: Ensemble consensus threshold used.
            consensus_summary: Per-ticker signal counts from all strategies.

        Returns:
            Discord embed dict.
        """
        count = len(signals)
        color = AlertFormatter.COLOR_BUY if count > 0 else AlertFormatter.COLOR_INFO

        if count == 0:
            fields = [
                {"name": "날짜", "value": date, "inline": True},
                {"name": "결과", "value": "오늘 발생한 신호가 없습니다.", "inline": False},
            ]
        else:
            buy_total = sum(1 for s in signals if s.get("signal_type") == "BUY")
            sell_total = sum(1 for s in signals if s.get("signal_type") == "SELL")

            # Group by ticker
            tickers = {}
            for s in signals:
                t = s.get("ticker", "")
                if t not in tickers:
                    tickers[t] = {"name": s.get("ticker_name", t), "buy": [], "sell": [], "price": s.get("price", 0)}
                if s.get("signal_type") == "BUY":
                    tickers[t]["buy"].append(s.get("strategy_name", "?"))
                else:
                    tickers[t]["sell"].append(s.get("strategy_name", "?"))

            # Build per-ticker consensus lines
            lines = []
            for ticker, info in tickers.items():
                b = len(info["buy"])
                s = len(info["sell"])
                total = b + s
                if b > 0 and s > 0:
                    emoji = "🟡"
                    label = "MIXED"
                elif b > 0:
                    emoji = "🟢"
                    label = "BUY"
                else:
                    emoji = "🔴"
                    label = "SELL"

                line = f"{emoji} **{info['name']}** ({ticker}) @ {info['price']:,.0f}원 → **{label}**"
                if b > 0:
                    line += f"\n　　🟢 매수({b}): {', '.join(info['buy'][:3])}"
                    if b > 3:
                        line += f" 외 {b-3}개"
                if s > 0:
                    line += f"\n　　🔴 매도({s}): {', '.join(info['sell'][:3])}"
                    if s > 3:
                        line += f" 외 {s-3}개"
                lines.append(line)

            fields = [
                {"name": "날짜", "value": date, "inline": True},
                {"name": "합의 시그널", "value": f"{count}건 (🟢{buy_total} / 🔴{sell_total})", "inline": True},
                {"name": "종목 수", "value": f"{len(tickers)}개", "inline": True},
            ]
            if ensemble_n:
                fields.append({"name": "합의 기준", "value": f"N >= {ensemble_n}", "inline": True})

            # Split into chunks if too long (Discord 1024 char limit per field)
            summary_text = "\n\n".join(lines)
            if len(summary_text) <= 1024:
                fields.append({"name": "종목별 합의", "value": summary_text, "inline": False})
            else:
                for i in range(0, len(lines), 4):
                    chunk = "\n\n".join(lines[i:i+4])
                    label = "종목별 합의" if i == 0 else "​"
                    fields.append({"name": label, "value": chunk[:1024], "inline": False})

        return {
            "title": f"📅 앙상블 시그널 요약 ({date})" + (f" [N>={ensemble_n}]" if ensemble_n else ""),
            "color": color,
            "fields": fields,
            "footer": {"text": "Money Mani 트레이딩 시스템"},
        }

    @staticmethod
    def format_discovery_report(report) -> dict:
        """Format strategy discovery results as a Discord embed.

        Args:
            report: DiscoveryReport with rankings and summary.

        Returns:
            Discord embed dict.
        """
        rankings = report.rankings
        color = AlertFormatter.COLOR_BUY if report.strategies_validated > 0 else AlertFormatter.COLOR_INFO

        fields = [
            {"name": "탐색일시", "value": report.date, "inline": True},
            {"name": "시장", "value": report.market, "inline": True},
            {"name": "검색 쿼리", "value": f"{len(report.queries_used)}개", "inline": True},
            {"name": "영상 수집", "value": f"{report.videos_found}개", "inline": True},
            {"name": "전략 추출", "value": f"{report.strategies_extracted}개", "inline": True},
            {"name": "검증 통과", "value": f"{report.strategies_validated}개", "inline": True},
        ]

        if rankings:
            rank_lines = []
            for i, s in enumerate(rankings[:5], 1):
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                ret_sign = "+" if s.avg_return >= 0 else ""
                rank_lines.append(
                    f"{medal} **{s.strategy_name}**\n"
                    f"수익률: {ret_sign}{s.avg_return:.1%} | "
                    f"샤프: {s.avg_sharpe:.2f} | "
                    f"MDD: {s.avg_mdd:.1%} | "
                    f"승률: {s.avg_win_rate:.1%}\n"
                    f"점수: {s.composite_score:.3f} | "
                    f"종목: {s.num_tickers}개 | "
                    f"거래: {s.avg_trades:.0f}회"
                )
            fields.append({
                "name": "전략 랭킹 (상위 5개)",
                "value": "\n\n".join(rank_lines),
                "inline": False,
            })
        else:
            fields.append({
                "name": "결과",
                "value": "랭킹 가능한 전략이 없습니다.",
                "inline": False,
            })

        # Add detected trends if available
        trends = getattr(report, "trends", [])
        if trends:
            trend_lines = []
            for t in trends[:5]:
                sector = t.get("sector", "?")
                reason = t.get("reason", "")
                keywords = ", ".join(t.get("keywords", [])[:3])
                trend_lines.append(f"**{sector}** - {reason}\n키워드: {keywords}")
            fields.insert(0, {
                "name": "감지된 시장 트렌드",
                "value": "\n\n".join(trend_lines),
                "inline": False,
            })

        title = "🔍 트렌드 기반 전략 탐색 결과" if trends else "🔍 전략 자동 탐색 결과"

        return {
            "title": title,
            "color": color,
            "fields": fields,
            "footer": {"text": "Money Mani 전략 탐색 시스템"},
        }

    @staticmethod
    def format_consensus_alert(group) -> dict:
        """Format a consensus alert for conflicting signals on one ticker."""
        consensus = group.consensus
        if consensus == "BUY":
            color = AlertFormatter.COLOR_BUY
            emoji = "🟢"
        elif consensus == "SELL":
            color = AlertFormatter.COLOR_SELL
            emoji = "🔴"
        else:
            color = AlertFormatter.COLOR_WARN
            emoji = "🟡"

        ticker_display = f"{group.ticker_name} ({group.ticker})"
        buy_list = ", ".join(group.buy_strategies) if group.buy_strategies else "-"
        sell_list = ", ".join(group.sell_strategies) if group.sell_strategies else "-"

        prices = [s.get("price", 0) for s in group.signals if s.get("price")]
        avg_price = sum(prices) / len(prices) if prices else 0

        fields = [
            {"name": "종목", "value": ticker_display, "inline": True},
            {"name": "합의", "value": f"{emoji} {consensus}", "inline": True},
            {"name": "현재가", "value": f"{avg_price:,.0f}원", "inline": True},
            {"name": f"🟢 매수 ({group.buy_count})", "value": buy_list, "inline": True},
            {"name": f"🔴 매도 ({group.sell_count})", "value": sell_list, "inline": True},
        ]

        return {
            "title": f"{emoji} [합의: {consensus}] {ticker_display}",
            "color": color,
            "fields": fields,
            "footer": {"text": "Money Mani 시그널 합의"},
        }

    @staticmethod
    def format_strategy_leaderboard(stats: list[dict]) -> dict:
        """Format strategy leaderboard as a Discord embed."""
        if not stats:
            return {
                "title": "🏆 전략 리더보드",
                "color": AlertFormatter.COLOR_INFO,
                "fields": [{"name": "결과", "value": "아직 데이터가 없습니다.", "inline": False}],
                "footer": {"text": "Money Mani 성과 분석"},
            }

        lines = []
        for i, s in enumerate(stats[:10], 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            pnl_sign = "+" if s.get("avg_pnl_pct", 0) >= 0 else ""
            lines.append(
                f"{medal} **{s['strategy_name']}**\n"
                f"거래: {s.get('total_trades', 0)}건 | "
                f"승률: {s.get('win_rate', 0):.1f}% | "
                f"평균P&L: {pnl_sign}{s.get('avg_pnl_pct', 0):.2f}% | "
                f"보유기간: {s.get('avg_holding_days', 0):.0f}일"
            )

        return {
            "title": "🏆 전략 리더보드 (실전 성과)",
            "color": AlertFormatter.COLOR_INFO,
            "fields": [{"name": "상위 전략", "value": "\n\n".join(lines), "inline": False}],
            "footer": {"text": "Money Mani 성과 분석"},
        }

    @staticmethod
    def format_market_intel_alert(issues: list, scan_time: str,
                                   scan_type: str, label: str) -> dict:
        """Format market intelligence scan results as a Discord embed.

        Args:
            issues: List of issue dicts from MarketIntelScanner.
            scan_time: Scan time string (HH:MM).
            scan_type: Scan type key.
            label: Human-readable scan type label.

        Returns:
            Discord embed dict.
        """
        if not issues:
            return {
                "title": f"🔍 시장 인텔리전스 ({scan_time} {label})",
                "color": AlertFormatter.COLOR_INFO,
                "fields": [{"name": "결과", "value": "감지된 이슈가 없습니다.", "inline": False}],
                "footer": {"text": "Money Mani 시장 인텔리전스"},
            }

        fields = [
            {"name": "스캔 시간", "value": scan_time, "inline": True},
            {"name": "유형", "value": label, "inline": True},
            {"name": "이슈 수", "value": f"{len(issues)}건", "inline": True},
        ]

        category_emoji = {
            "policy": "📋", "earnings": "💰", "sector": "📊",
            "global": "🌍", "event": "📰", "supply_demand": "📈",
        }
        sentiment_emoji = {
            "positive": "🟢", "negative": "🔴", "neutral": "⚪", "mixed": "🟡",
        }

        for issue in issues[:6]:
            cat_em = category_emoji.get(issue.get("category", ""), "📰")
            sent_em = sentiment_emoji.get(issue.get("sentiment", ""), "⚪")
            cat_label = issue.get("category", "")

            tickers = issue.get("affected_tickers", [])
            ticker_lines = []
            for t in tickers[:5]:
                direction = "↑" if t.get("direction") == "up" else "↓"
                ticker_lines.append(
                    f"  {direction} {t.get('ticker', '')} {t.get('name', '')}"
                )
            ticker_text = "\n".join(ticker_lines) if ticker_lines else "  (종목 없음)"

            conf = issue.get("confidence", 0)
            field_value = (
                f"{sent_em} {issue.get('summary', '')[:120]}\n"
                f"확신도: {conf:.0%}\n{ticker_text}"
            )

            fields.append({
                "name": f"{cat_em} [{cat_label}] {issue.get('title', '')}",
                "value": field_value[:1024],
                "inline": False,
            })

        return {
            "title": f"🔍 시장 인텔리전스 ({scan_time} {label})",
            "color": AlertFormatter.COLOR_INFO,
            "fields": fields,
            "footer": {"text": "Money Mani 시장 인텔리전스"},
        }

    @staticmethod
    def format_performance_report(summary: dict, report_type: str = "daily") -> dict:
        """Format a performance report as a Discord embed.

        Args:
            summary: Performance summary dict from PerformanceService.
            report_type: 'daily' or 'weekly'.

        Returns:
            Discord embed dict.
        """
        period = summary.get("period", "")
        total = summary.get("total_signals", 0)
        avg_pnl = summary.get("avg_pnl_pct", 0)
        total_pnl = summary.get("total_pnl_pct", 0)
        win_rate = summary.get("win_rate", 0)

        if total_pnl > 0:
            color = AlertFormatter.COLOR_BUY
        elif total_pnl < 0:
            color = AlertFormatter.COLOR_SELL
        else:
            color = AlertFormatter.COLOR_INFO

        pnl_sign = "+" if total_pnl >= 0 else ""
        avg_sign = "+" if avg_pnl >= 0 else ""
        type_label = "일일" if report_type == "daily" else "주간"

        fields = [
            {"name": "기간", "value": period, "inline": True},
            {"name": "총 시그널", "value": f"{total}건", "inline": True},
            {"name": "승률", "value": f"{win_rate:.1f}%", "inline": True},
            {"name": "총 수익률", "value": f"{pnl_sign}{total_pnl:.2f}%", "inline": True},
            {"name": "평균 수익률", "value": f"{avg_sign}{avg_pnl:.2f}%", "inline": True},
            {
                "name": "매수/매도",
                "value": f"{summary.get('buy_signals', 0)}건 / {summary.get('sell_signals', 0)}건",
                "inline": True,
            },
            {
                "name": "승/패",
                "value": f"{summary.get('win_count', 0)}승 {summary.get('lose_count', 0)}패",
                "inline": True,
            },
        ]

        best = summary.get("best")
        if best:
            b_sign = "+" if best["pnl_pct"] >= 0 else ""
            fields.append({
                "name": "최고 수익",
                "value": f"{best.get('ticker_name', best['ticker'])} ({best['signal_type']}) {b_sign}{best['pnl_pct']:.2f}%",
                "inline": True,
            })

        worst = summary.get("worst")
        if worst:
            w_sign = "+" if worst["pnl_pct"] >= 0 else ""
            fields.append({
                "name": "최저 수익",
                "value": f"{worst.get('ticker_name', worst['ticker'])} ({worst['signal_type']}) {w_sign}{worst['pnl_pct']:.2f}%",
                "inline": True,
            })

        # Signal details list
        records = summary.get("records", [])
        if records:
            lines = []
            for r in records[:10]:
                emoji = "🟢" if (r.get("pnl_pct") or 0) >= 0 else "🔴"
                name = r.get("ticker_name") or r.get("ticker", "?")
                r_sign = "+" if (r.get("pnl_pct") or 0) >= 0 else ""
                sig_type = "매수" if r.get("signal_type") == "BUY" else "매도"
                lines.append(
                    f"{emoji} {name} [{sig_type}] "
                    f"{r.get('signal_price', 0):,.0f} → {r.get('close_price', 0):,.0f} "
                    f"({r_sign}{r.get('pnl_pct', 0):.2f}%)"
                )
            fields.append({
                "name": "시그널 상세",
                "value": "\n".join(lines),
                "inline": False,
            })

        return {
            "title": f"📈 {type_label} 시그널 성과 리포트 ({period})",
            "color": color,
            "fields": fields,
            "footer": {"text": "Money Mani 성과 추적 시스템"},
        }
