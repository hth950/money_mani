"""Generate 70+ new trading strategies and optionally batch-validate them.

Usage:
    python scripts/generate_strategies.py --generate     # generate YAML files only
    python scripts/generate_strategies.py --validate     # validate draft strategies only
    python scripts/generate_strategies.py --all          # generate + validate
    python scripts/generate_strategies.py --dry-run      # preview without writing
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import logging
import sys
from pathlib import Path

import yaml

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STRATEGIES_DIR = ROOT / "config" / "strategies"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strategy builder helpers
# ---------------------------------------------------------------------------

def ind(itype: str, period: int = 14, column: str = "close",
        output_name: str = "", **kwargs) -> dict:
    """Build an indicator dict."""
    d = {"type": itype, "column": column, "output_name": output_name or f"{itype.upper()}_{period}"}
    if itype not in ("obv", "vwap"):
        d["period"] = period
    d.update(kwargs)
    return d


def rule(condition: str, **kwargs) -> dict:
    """Build a rule dict."""
    d = {"condition": condition}
    d.update(kwargs)
    return d


def strat(name: str, description: str, category: str, indicators: list,
          entry: list, exit_rules: list, position_size: float = 1.0,
          stop_loss: float | None = 0.05, take_profit: float | None = None,
          source: str = "auto_generated") -> dict:
    """Build a full strategy dict."""
    params = {"position_size": position_size}
    if stop_loss is not None:
        params["stop_loss"] = stop_loss
    if take_profit is not None:
        params["take_profit"] = take_profit
    return {
        "name": name,
        "description": description,
        "source": source,
        "category": category,
        "status": "draft",
        "rules": {"entry": entry, "exit": exit_rules},
        "indicators": indicators,
        "parameters": params,
        "backtest_results": {},
    }


def fingerprint(s: dict) -> str:
    """Structural fingerprint for dedup (indicators + rules only)."""
    key = json.dumps({"i": s["indicators"], "r": s["rules"]}, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Strategy generation - 8 categories
# ---------------------------------------------------------------------------

def gen_crossover() -> list[dict]:
    """A. Crossover variants."""
    strategies = []

    # SMA crossover pairs
    sma_pairs = [(5, 20), (10, 30), (10, 50), (20, 60), (20, 120), (50, 200)]
    for short, long in sma_pairs:
        strategies.append(strat(
            name=f"SMA 크로스 ({short}/{long})",
            description=f"SMA {short}일이 SMA {long}일을 상향 돌파시 매수, 하향 돌파시 매도",
            category="crossover",
            indicators=[
                ind("sma", short, output_name=f"SMA_{short}"),
                ind("sma", long, output_name=f"SMA_{long}"),
            ],
            entry=[rule("crossover", indicator_a=f"SMA_{short}", indicator_b=f"SMA_{long}", direction="above")],
            exit_rules=[rule("crossover", indicator_a=f"SMA_{short}", indicator_b=f"SMA_{long}", direction="below")],
        ))

    # EMA crossover pairs
    ema_pairs = [(5, 13), (8, 21), (10, 30), (12, 50), (20, 60)]
    for short, long in ema_pairs:
        strategies.append(strat(
            name=f"EMA 크로스 ({short}/{long})",
            description=f"EMA {short}일이 EMA {long}일을 상향 돌파시 매수",
            category="crossover",
            indicators=[
                ind("ema", short, output_name=f"EMA_{short}"),
                ind("ema", long, output_name=f"EMA_{long}"),
            ],
            entry=[rule("crossover", indicator_a=f"EMA_{short}", indicator_b=f"EMA_{long}", direction="above")],
            exit_rules=[rule("crossover", indicator_a=f"EMA_{short}", indicator_b=f"EMA_{long}", direction="below")],
        ))

    # MACD variants
    macd_params = [(8, 17, 9), (5, 35, 5)]
    for fast, slow, sig in macd_params:
        strategies.append(strat(
            name=f"MACD 크로스 ({fast}/{slow}/{sig})",
            description=f"MACD({fast},{slow},{sig}) 시그널선 상향 돌파시 매수",
            category="crossover",
            indicators=[ind("macd", output_name=f"MACD_{fast}_{slow}_{sig}", fast=fast, slow=slow, signal=sig)],
            entry=[rule("crossover", indicator_a=f"MACD_{fast}_{slow}_{sig}", indicator_b=f"MACDs_{fast}_{slow}_{sig}", direction="above")],
            exit_rules=[rule("crossover", indicator_a=f"MACD_{fast}_{slow}_{sig}", indicator_b=f"MACDs_{fast}_{slow}_{sig}", direction="below")],
        ))

    # Stochastic K/D crossover
    for period in [9, 21]:
        strategies.append(strat(
            name=f"Stochastic 크로스 ({period})",
            description=f"Stochastic K({period})가 D를 과매도 구간에서 상향 돌파시 매수",
            category="crossover",
            indicators=[ind("stoch", period, output_name=f"STOCH_{period}")],
            entry=[
                rule("threshold", indicator=f"STOCH_{period}", direction="below", value=25),
            ],
            exit_rules=[
                rule("threshold", indicator=f"STOCH_{period}", direction="above", value=80),
            ],
        ))

    return strategies


def gen_momentum() -> list[dict]:
    """B. Momentum threshold variants."""
    strategies = []

    # RSI variants
    rsi_params = [(7, 25, 75), (10, 30, 65), (21, 35, 65)]
    for period, entry_val, exit_val in rsi_params:
        strategies.append(strat(
            name=f"RSI 반등 ({period}/{entry_val}/{exit_val})",
            description=f"RSI({period}) {entry_val} 이하 진입, {exit_val} 이상 청산",
            category="momentum",
            indicators=[ind("rsi", period, output_name=f"RSI_{period}")],
            entry=[rule("threshold", indicator=f"RSI_{period}", direction="below", value=entry_val)],
            exit_rules=[rule("threshold", indicator=f"RSI_{period}", direction="above", value=exit_val)],
            take_profit=0.15,
        ))

    # CCI variants
    for period, entry_val, exit_val in [(14, -150, 150), (20, -100, 100)]:
        strategies.append(strat(
            name=f"CCI 반등 ({period}/{entry_val})",
            description=f"CCI({period}) {entry_val} 이하 진입, {exit_val} 이상 청산",
            category="momentum",
            indicators=[ind("cci", period, output_name=f"CCI_{period}")],
            entry=[rule("threshold", indicator=f"CCI_{period}", direction="below", value=entry_val)],
            exit_rules=[rule("threshold", indicator=f"CCI_{period}", direction="above", value=exit_val)],
        ))

    # Williams %R variants
    for period, entry_val, exit_val in [(10, -85, -15), (21, -90, -10)]:
        strategies.append(strat(
            name=f"Williams %R ({period}/{entry_val})",
            description=f"Williams %R({period}) {entry_val} 이하 진입, {exit_val} 이상 청산",
            category="momentum",
            indicators=[ind("willr", period, output_name=f"WILLR_{period}")],
            entry=[rule("threshold", indicator=f"WILLR_{period}", direction="below", value=entry_val)],
            exit_rules=[rule("threshold", indicator=f"WILLR_{period}", direction="above", value=exit_val)],
        ))

    # ROC variants
    for period, entry_val in [(10, 5), (20, 3), (60, 10)]:
        strategies.append(strat(
            name=f"ROC 모멘텀 ({period}/{entry_val}%)",
            description=f"ROC({period}) {entry_val}% 이상 상승 모멘텀시 매수, 0% 이하 청산",
            category="momentum",
            indicators=[ind("roc", period, output_name=f"ROC_{period}")],
            entry=[rule("threshold", indicator=f"ROC_{period}", direction="above", value=entry_val)],
            exit_rules=[rule("threshold", indicator=f"ROC_{period}", direction="below", value=0)],
        ))

    # MFI variants
    for period, entry_val, exit_val in [(10, 15, 85), (20, 25, 75)]:
        strategies.append(strat(
            name=f"MFI 반등 ({period}/{entry_val})",
            description=f"MFI({period}) {entry_val} 이하 진입, {exit_val} 이상 청산",
            category="momentum",
            indicators=[ind("mfi", period, output_name=f"MFI_{period}")],
            entry=[rule("threshold", indicator=f"MFI_{period}", direction="below", value=entry_val)],
            exit_rules=[rule("threshold", indicator=f"MFI_{period}", direction="above", value=exit_val)],
        ))

    return strategies


def gen_trend() -> list[dict]:
    """C. Trend following variants."""
    strategies = []

    # ADX + DI variants
    for period, adx_thresh in [(10, 20), (14, 30), (20, 25)]:
        strategies.append(strat(
            name=f"ADX 추세 ({period}/ADX>{adx_thresh})",
            description=f"ADX({period})>{adx_thresh}이고 DI+ > DI- 시 매수",
            category="trend",
            indicators=[ind("adx", period, output_name=f"ADX_{period}")],
            entry=[
                rule("threshold", indicator=f"ADX_{period}", direction="above", value=adx_thresh),
                rule("threshold_compare", indicator_a=f"DMP_{period}", indicator_b=f"DMN_{period}", direction="above"),
            ],
            exit_rules=[
                rule("threshold_compare", indicator_a=f"DMP_{period}", indicator_b=f"DMN_{period}", direction="below"),
            ],
        ))

    # PSAR variants
    for af, max_af in [(0.01, 0.1), (0.03, 0.3)]:
        strategies.append(strat(
            name=f"PSAR 추세 (af={af})",
            description=f"Parabolic SAR(af={af}, max={max_af}) 가격 하방에 있을 때 매수",
            category="trend",
            indicators=[ind("psar", output_name="PSAR", af=af, max_af=max_af)],
            entry=[rule("threshold_compare", indicator_a="Close", indicator_b="PSAR", direction="above")],
            exit_rules=[rule("threshold_compare", indicator_a="Close", indicator_b="PSAR", direction="below")],
        ))

    # Ichimoku variants
    for tenkan, kijun, senkou in [(7, 22, 44), (13, 34, 68)]:
        strategies.append(strat(
            name=f"Ichimoku 구름 ({tenkan}/{kijun}/{senkou})",
            description=f"Ichimoku({tenkan},{kijun},{senkou}) 구름 상방 돌파시 매수",
            category="trend",
            indicators=[ind("ichimoku", output_name="ICHIMOKU", tenkan=tenkan, kijun=kijun, senkou=senkou)],
            entry=[
                rule("threshold_compare", indicator_a="Close", indicator_b=f"ISA_{tenkan}", direction="above"),
                rule("threshold_compare", indicator_a="Close", indicator_b=f"ISB_{kijun}", direction="above"),
            ],
            exit_rules=[
                rule("threshold_compare", indicator_a="Close", indicator_b=f"IKS_{kijun}", direction="below"),
            ],
        ))

    # EMA Ribbon (alignment of 4 EMAs)
    strategies.append(strat(
        name="EMA 리본 (8/13/21/55)",
        description="EMA 8 > 13 > 21 > 55 정렬시 강한 상승 추세, 역전시 청산",
        category="trend",
        indicators=[
            ind("ema", 8, output_name="EMA_8"),
            ind("ema", 13, output_name="EMA_13"),
            ind("ema", 21, output_name="EMA_21"),
            ind("ema", 55, output_name="EMA_55"),
        ],
        entry=[
            rule("threshold_compare", indicator_a="EMA_8", indicator_b="EMA_13", direction="above"),
            rule("threshold_compare", indicator_a="EMA_13", indicator_b="EMA_21", direction="above"),
            rule("threshold_compare", indicator_a="EMA_21", indicator_b="EMA_55", direction="above"),
        ],
        exit_rules=[
            rule("threshold_compare", indicator_a="EMA_8", indicator_b="EMA_21", direction="below"),
        ],
    ))

    # MA + trend filter
    for ma_type, long_p, short, mid in [("sma", 100, 10, 30), ("ema", 150, 5, 20)]:
        tname = ma_type.upper()
        strategies.append(strat(
            name=f"{tname} 장기필터 ({long_p}+{short}/{mid})",
            description=f"{tname}{long_p} 위에서 {short}/{mid} 크로스 매수",
            category="trend",
            indicators=[
                ind(ma_type, long_p, output_name=f"{tname}_{long_p}"),
                ind(ma_type, short, output_name=f"{tname}_{short}"),
                ind(ma_type, mid, output_name=f"{tname}_{mid}"),
            ],
            entry=[
                rule("threshold_compare", indicator_a="Close", indicator_b=f"{tname}_{long_p}", direction="above"),
                rule("crossover", indicator_a=f"{tname}_{short}", indicator_b=f"{tname}_{mid}", direction="above"),
            ],
            exit_rules=[
                rule("crossover", indicator_a=f"{tname}_{short}", indicator_b=f"{tname}_{mid}", direction="below"),
            ],
        ))

    # ADX + MACD histogram
    for adx_thresh in [20, 30]:
        strategies.append(strat(
            name=f"ADX+MACD 추세 (ADX>{adx_thresh})",
            description=f"ADX>{adx_thresh} 강한 추세 + MACD 히스토그램 양수 매수",
            category="trend",
            indicators=[
                ind("adx", 14, output_name="ADX_14"),
                ind("macd", output_name="MACD_12_26_9", fast=12, slow=26, signal=9),
            ],
            entry=[
                rule("threshold", indicator="ADX_14", direction="above", value=adx_thresh),
                rule("threshold", indicator="MACDh_12_26_9", direction="above", value=0),
            ],
            exit_rules=[
                rule("threshold", indicator="MACDh_12_26_9", direction="below", value=0),
            ],
        ))

    return strategies


def gen_breakout() -> list[dict]:
    """D. Breakout variants."""
    strategies = []

    # Donchian channel breakout
    for period in [10, 30, 55]:
        strategies.append(strat(
            name=f"Donchian 돌파 ({period})",
            description=f"Donchian Channel {period}일 상한 돌파시 매수, 하한 이탈시 매도",
            category="breakout",
            indicators=[ind("donchian", period, output_name=f"DC_{period}")],
            entry=[rule("threshold_compare", indicator_a="Close", indicator_b=f"DCU_{period}", direction="above")],
            exit_rules=[rule("threshold_compare", indicator_a="Close", indicator_b=f"DCL_{period}", direction="below")],
        ))

    # BB squeeze breakout variants
    for period, std in [(15, 2.0), (25, 2.0)]:
        strategies.append(strat(
            name=f"BB 수축 돌파 ({period})",
            description=f"볼린저밴드({period}) 수축 후 상단 돌파시 매수",
            category="breakout",
            indicators=[ind("bbands", period, output_name=f"BB_{period}", std=std)],
            entry=[rule("threshold_compare", indicator_a="Close", indicator_b=f"BBU_{period}_{std}", direction="above")],
            exit_rules=[rule("threshold_compare", indicator_a="Close", indicator_b=f"BBM_{period}_{std}", direction="below")],
            position_size=0.8,
        ))

    # KC breakout
    for period, scalar in [(10, 1.5), (20, 2.5)]:
        strategies.append(strat(
            name=f"Keltner 돌파 ({period}/{scalar})",
            description=f"Keltner Channel({period}, {scalar}) 상단 돌파시 매수",
            category="breakout",
            indicators=[ind("kc", period, output_name=f"KC_{period}", scalar=scalar)],
            entry=[rule("threshold_compare", indicator_a="Close", indicator_b=f"KCU_{period}", direction="above")],
            exit_rules=[rule("threshold_compare", indicator_a="Close", indicator_b=f"KCM_{period}", direction="below")],
        ))

    # Highest N-day breakout
    for period in [20, 60, 120]:
        strategies.append(strat(
            name=f"{period}일 신고가 돌파",
            description=f"{period}일 최고가 돌파시 모멘텀 매수",
            category="breakout",
            indicators=[
                ind("highest", period, output_name=f"HIGH_{period}"),
                ind("rsi", 14, output_name="RSI_14"),
            ],
            entry=[
                rule("threshold_compare", indicator_a="Close", indicator_b=f"HIGH_{period}", direction="above"),
            ],
            exit_rules=[
                rule("threshold", indicator="RSI_14", direction="above", value=75),
            ],
            stop_loss=0.07,
        ))

    # ATR breakout + RSI filter
    strategies.append(strat(
        name="ATR 돌파 + RSI 필터",
        description="ATR 변동성 돌파 + RSI 50 이상 모멘텀 확인 매수",
        category="breakout",
        indicators=[
            ind("atr", 14, output_name="ATR_14"),
            ind("rsi", 14, output_name="RSI_14"),
        ],
        entry=[
            rule("threshold", indicator="ATR_BREAKOUT", direction="above", value=0),
            rule("threshold", indicator="RSI_14", direction="above", value=50),
        ],
        exit_rules=[
            rule("threshold", indicator="RSI_14", direction="above", value=75),
        ],
    ))

    return strategies


def gen_volatility() -> list[dict]:
    """E. Volatility-based strategies."""
    strategies = []

    # BB bandwidth low (squeeze) entry
    for period, std, bbb_thresh in [(15, 2.0, 0.04), (30, 2.0, 0.06)]:
        strategies.append(strat(
            name=f"BB 수축 대기 ({period}/BBB<{bbb_thresh})",
            description=f"BB({period}) 밴드폭 {bbb_thresh} 이하 수축 후 상단 돌파",
            category="volatility",
            indicators=[ind("bbands", period, output_name=f"BB_{period}", std=std)],
            entry=[
                rule("threshold", indicator=f"BBB_{period}_{std}", direction="below", value=bbb_thresh * 100),
                rule("threshold_compare", indicator_a="Close", indicator_b=f"BBU_{period}_{std}", direction="above"),
            ],
            exit_rules=[
                rule("threshold_compare", indicator_a="Close", indicator_b=f"BBM_{period}_{std}", direction="below"),
            ],
        ))

    # KC inside BB (Bollinger Squeeze)
    strategies.append(strat(
        name="볼린저 스퀴즈 (KC 안쪽)",
        description="KC가 BB 안쪽에 있다가(수축) BB 상단 돌파시 매수",
        category="volatility",
        indicators=[
            ind("bbands", 20, output_name="BB_20", std=2.0),
            ind("kc", 20, output_name="KC_20", scalar=1.5),
        ],
        entry=[
            rule("threshold_compare", indicator_a="Close", indicator_b="BBU_20_2.0", direction="above"),
        ],
        exit_rules=[
            rule("threshold_compare", indicator_a="Close", indicator_b="BBM_20_2.0", direction="below"),
        ],
    ))

    # ATR contraction then expansion
    for atr_period in [10, 20]:
        strategies.append(strat(
            name=f"ATR 수축확장 ({atr_period})",
            description=f"ATR({atr_period}) 수축 후 돌파 감지 매수",
            category="volatility",
            indicators=[
                ind("atr", atr_period, output_name=f"ATR_{atr_period}"),
                ind("sma", 20, output_name="SMA_20"),
            ],
            entry=[
                rule("threshold", indicator="ATR_BREAKOUT", direction="above", value=0),
                rule("threshold_compare", indicator_a="Close", indicator_b="SMA_20", direction="above"),
            ],
            exit_rules=[
                rule("threshold_compare", indicator_a="Close", indicator_b="SMA_20", direction="below"),
            ],
        ))

    # MA convergence-divergence
    strategies.append(strat(
        name="MA 수렴 확산 (10/20/50)",
        description="SMA 10/20/50 수렴 후 확산시 추세 시작 매수",
        category="volatility",
        indicators=[
            ind("sma", 10, output_name="SMA_10"),
            ind("sma", 20, output_name="SMA_20"),
            ind("sma", 50, output_name="SMA_50"),
        ],
        entry=[
            rule("crossover", indicator_a="SMA_10", indicator_b="SMA_20", direction="above"),
            rule("threshold_compare", indicator_a="SMA_20", indicator_b="SMA_50", direction="above"),
        ],
        exit_rules=[
            rule("crossover", indicator_a="SMA_10", indicator_b="SMA_20", direction="below"),
        ],
    ))

    return strategies


def gen_mean_reversion() -> list[dict]:
    """F. Mean reversion strategies."""
    strategies = []

    # RSI + BB lower
    for rsi_thresh, bb_period in [(25, 15), (35, 25)]:
        strategies.append(strat(
            name=f"RSI+BB 하단 반등 (RSI<{rsi_thresh}/BB{bb_period})",
            description=f"RSI<{rsi_thresh} + BB({bb_period}) 하단 이하 → 과매도 반등 매수",
            category="mean_reversion",
            indicators=[
                ind("rsi", 14, output_name="RSI_14"),
                ind("bbands", bb_period, output_name=f"BB_{bb_period}", std=2.0),
            ],
            entry=[
                rule("threshold", indicator="RSI_14", direction="below", value=rsi_thresh),
                rule("threshold_compare", indicator_a="Close", indicator_b=f"BBL_{bb_period}_2.0", direction="below"),
            ],
            exit_rules=[
                rule("threshold_compare", indicator_a="Close", indicator_b=f"BBM_{bb_period}_2.0", direction="above"),
            ],
            take_profit=0.10,
        ))

    # VWAP distance reversion
    for dist in [-2.0, -3.0]:
        strategies.append(strat(
            name=f"VWAP 이탈 반등 ({dist}%)",
            description=f"VWAP 대비 {dist}% 이상 하락시 평균회귀 매수",
            category="mean_reversion",
            indicators=[
                ind("vwap", output_name="VWAP"),
                ind("rsi", 14, output_name="RSI_14"),
            ],
            entry=[
                rule("threshold", indicator="VWAP_DIST", direction="below", value=dist),
            ],
            exit_rules=[
                rule("threshold", indicator="VWAP_DIST", direction="above", value=0),
            ],
            take_profit=0.08,
        ))

    # CCI + BB lower
    strategies.append(strat(
        name="CCI+BB 하단 반등",
        description="CCI<-100 + BB 하단 이하 → 극단적 과매도 반등",
        category="mean_reversion",
        indicators=[
            ind("cci", 20, output_name="CCI_20"),
            ind("bbands", 20, output_name="BB_20", std=2.0),
        ],
        entry=[
            rule("threshold", indicator="CCI_20", direction="below", value=-100),
            rule("threshold_compare", indicator_a="Close", indicator_b="BBL_20_2.0", direction="below"),
        ],
        exit_rules=[
            rule("threshold_compare", indicator_a="Close", indicator_b="BBM_20_2.0", direction="above"),
        ],
    ))

    # Williams %R + KC lower
    strategies.append(strat(
        name="WILLR+KC 하단 반등",
        description="Williams %R<-90 + Keltner 하한 이하 반등 매수",
        category="mean_reversion",
        indicators=[
            ind("willr", 14, output_name="WILLR_14"),
            ind("kc", 20, output_name="KC_20", scalar=2.0),
        ],
        entry=[
            rule("threshold", indicator="WILLR_14", direction="below", value=-90),
            rule("threshold_compare", indicator_a="Close", indicator_b="KCL_20", direction="below"),
        ],
        exit_rules=[
            rule("threshold_compare", indicator_a="Close", indicator_b="KCM_20", direction="above"),
        ],
    ))

    # MFI + BB lower
    strategies.append(strat(
        name="MFI+BB 하단 반등",
        description="MFI<20 + BB 하단 이하 → 자금유출 과매도 반등",
        category="mean_reversion",
        indicators=[
            ind("mfi", 14, output_name="MFI_14"),
            ind("bbands", 20, output_name="BB_20", std=2.0),
        ],
        entry=[
            rule("threshold", indicator="MFI_14", direction="below", value=20),
            rule("threshold_compare", indicator_a="Close", indicator_b="BBL_20_2.0", direction="below"),
        ],
        exit_rules=[
            rule("threshold_compare", indicator_a="Close", indicator_b="BBM_20_2.0", direction="above"),
        ],
    ))

    # RSI + KC lower
    strategies.append(strat(
        name="RSI+KC 하단 반등",
        description="RSI<30 + Keltner 하한 이하 반등 매수",
        category="mean_reversion",
        indicators=[
            ind("rsi", 14, output_name="RSI_14"),
            ind("kc", 20, output_name="KC_20", scalar=2.0),
        ],
        entry=[
            rule("threshold", indicator="RSI_14", direction="below", value=30),
            rule("threshold_compare", indicator_a="Close", indicator_b="KCL_20", direction="below"),
        ],
        exit_rules=[
            rule("threshold_compare", indicator_a="Close", indicator_b="KCM_20", direction="above"),
        ],
    ))

    return strategies


def gen_volume() -> list[dict]:
    """G. Volume-based strategies."""
    strategies = []

    # Volume surge + RSI filter
    for vol_sma_period, rsi_thresh in [(20, 40), (20, 50)]:
        strategies.append(strat(
            name=f"거래량 급증+RSI ({vol_sma_period}/RSI>{rsi_thresh})",
            description=f"거래량 SMA({vol_sma_period}) 2배 이상 급증 + RSI>{rsi_thresh} 매수",
            category="volume",
            indicators=[
                ind("sma", vol_sma_period, column="volume", output_name=f"VOL_SMA_{vol_sma_period}"),
                ind("rsi", 14, output_name="RSI_14"),
            ],
            entry=[
                rule("threshold", indicator="VOL_RATIO", direction="above", value=2.0),
                rule("threshold", indicator="RSI_14", direction="above", value=rsi_thresh),
            ],
            exit_rules=[
                rule("threshold", indicator="RSI_14", direction="above", value=70),
            ],
        ))

    # OBV trend + MA
    for ma_period in [20, 50]:
        strategies.append(strat(
            name=f"OBV 상승+MA ({ma_period})",
            description=f"OBV 기울기 양수 + 가격이 SMA({ma_period}) 위일 때 매수",
            category="volume",
            indicators=[
                ind("obv", output_name="OBV"),
                ind("sma", ma_period, output_name=f"SMA_{ma_period}"),
            ],
            entry=[
                rule("threshold", indicator="OBV_SLOPE", direction="above", value=0),
                rule("threshold_compare", indicator_a="Close", indicator_b=f"SMA_{ma_period}", direction="above"),
            ],
            exit_rules=[
                rule("threshold_compare", indicator_a="Close", indicator_b=f"SMA_{ma_period}", direction="below"),
            ],
        ))

    # MFI + ROC
    strategies.append(strat(
        name="MFI 반등+ROC 확인",
        description="MFI<25 과매도 + ROC(5) 양수(모멘텀 전환) 매수",
        category="volume",
        indicators=[
            ind("mfi", 14, output_name="MFI_14"),
            ind("roc", 5, output_name="ROC_5"),
        ],
        entry=[
            rule("threshold", indicator="MFI_14", direction="below", value=25),
            rule("threshold", indicator="ROC_5", direction="above", value=0),
        ],
        exit_rules=[
            rule("threshold", indicator="MFI_14", direction="above", value=75),
        ],
    ))

    # Volume surge + MACD
    strategies.append(strat(
        name="거래량 급증+MACD",
        description="거래량 2배 급증 + MACD 히스토그램 양수시 매수",
        category="volume",
        indicators=[
            ind("sma", 20, column="volume", output_name="VOL_SMA_20"),
            ind("macd", output_name="MACD_12_26_9", fast=12, slow=26, signal=9),
        ],
        entry=[
            rule("threshold", indicator="VOL_RATIO", direction="above", value=2.0),
            rule("threshold", indicator="MACDh_12_26_9", direction="above", value=0),
        ],
        exit_rules=[
            rule("threshold", indicator="MACDh_12_26_9", direction="below", value=0),
        ],
    ))

    return strategies


def gen_composite() -> list[dict]:
    """H. Composite multi-indicator strategies."""
    strategies = []

    # RSI + MACD + BB
    for rsi_thresh in [40, 45]:
        strategies.append(strat(
            name=f"RSI+MACD+BB 복합 (RSI>{rsi_thresh})",
            description=f"RSI>{rsi_thresh} + MACD히스토그램 양수 + BB 상단 미만 → 상승 여력 매수",
            category="composite",
            indicators=[
                ind("rsi", 14, output_name="RSI_14"),
                ind("macd", output_name="MACD_12_26_9", fast=12, slow=26, signal=9),
                ind("bbands", 20, output_name="BB_20", std=2.0),
            ],
            entry=[
                rule("threshold", indicator="RSI_14", direction="above", value=rsi_thresh),
                rule("threshold", indicator="MACDh_12_26_9", direction="above", value=0),
                rule("threshold_compare", indicator_a="Close", indicator_b="BBU_20_2.0", direction="below"),
            ],
            exit_rules=[
                rule("threshold", indicator="RSI_14", direction="above", value=70),
            ],
        ))

    # ADX + RSI + EMA
    for adx_thresh in [20, 25]:
        strategies.append(strat(
            name=f"ADX+RSI+EMA ({adx_thresh})",
            description=f"ADX>{adx_thresh} 추세 + RSI>50 모멘텀 + EMA 정렬시 매수",
            category="composite",
            indicators=[
                ind("adx", 14, output_name="ADX_14"),
                ind("rsi", 14, output_name="RSI_14"),
                ind("ema", 10, output_name="EMA_10"),
                ind("ema", 30, output_name="EMA_30"),
            ],
            entry=[
                rule("threshold", indicator="ADX_14", direction="above", value=adx_thresh),
                rule("threshold", indicator="RSI_14", direction="above", value=50),
                rule("threshold_compare", indicator_a="EMA_10", indicator_b="EMA_30", direction="above"),
            ],
            exit_rules=[
                rule("threshold_compare", indicator_a="EMA_10", indicator_b="EMA_30", direction="below"),
            ],
        ))

    # Stoch + MACD
    strategies.append(strat(
        name="Stochastic+MACD 복합",
        description="Stochastic 과매도 + MACD 시그널 크로스 매수",
        category="composite",
        indicators=[
            ind("stoch", 14, output_name="STOCH_14"),
            ind("macd", output_name="MACD_12_26_9", fast=12, slow=26, signal=9),
        ],
        entry=[
            rule("threshold", indicator="STOCH_14", direction="below", value=30),
            rule("threshold", indicator="MACDh_12_26_9", direction="above", value=0),
        ],
        exit_rules=[
            rule("threshold", indicator="STOCH_14", direction="above", value=80),
        ],
    ))

    # Ichimoku + RSI
    strategies.append(strat(
        name="Ichimoku+RSI 복합",
        description="Ichimoku 구름 위 + RSI>50 매수",
        category="composite",
        indicators=[
            ind("ichimoku", output_name="ICHIMOKU", tenkan=9, kijun=26, senkou=52),
            ind("rsi", 14, output_name="RSI_14"),
        ],
        entry=[
            rule("threshold_compare", indicator_a="Close", indicator_b="ISA_9", direction="above"),
            rule("threshold", indicator="RSI_14", direction="above", value=50),
        ],
        exit_rules=[
            rule("threshold_compare", indicator_a="Close", indicator_b="IKS_26", direction="below"),
        ],
    ))

    # PSAR + ADX
    strategies.append(strat(
        name="PSAR+ADX 강추세",
        description="PSAR 가격 아래 + ADX>25 강한 추세 확인 매수",
        category="composite",
        indicators=[
            ind("psar", output_name="PSAR", af=0.02, max_af=0.2),
            ind("adx", 14, output_name="ADX_14"),
        ],
        entry=[
            rule("threshold_compare", indicator_a="Close", indicator_b="PSAR", direction="above"),
            rule("threshold", indicator="ADX_14", direction="above", value=25),
        ],
        exit_rules=[
            rule("threshold_compare", indicator_a="Close", indicator_b="PSAR", direction="below"),
        ],
    ))

    # BB + RSI + Volume
    strategies.append(strat(
        name="BB+RSI+거래량 복합",
        description="BB 수축(BBB<5) + RSI 40~60 중립 + 거래량 급증 → 폭발 직전 매수",
        category="composite",
        indicators=[
            ind("bbands", 20, output_name="BB_20", std=2.0),
            ind("rsi", 14, output_name="RSI_14"),
            ind("sma", 20, column="volume", output_name="VOL_SMA_20"),
        ],
        entry=[
            rule("threshold", indicator="RSI_14", direction="above", value=40),
            rule("threshold", indicator="VOL_RATIO", direction="above", value=1.5),
            rule("threshold_compare", indicator_a="Close", indicator_b="BBU_20_2.0", direction="above"),
        ],
        exit_rules=[
            rule("threshold", indicator="RSI_14", direction="above", value=75),
        ],
    ))

    # EMA cross + RSI + ATR
    for ema_s, ema_l in [(5, 20), (10, 40)]:
        strategies.append(strat(
            name=f"EMA({ema_s}/{ema_l})+RSI+ATR",
            description=f"EMA {ema_s}/{ema_l} 크로스 + RSI>45 + ATR 돌파 확인 매수",
            category="composite",
            indicators=[
                ind("ema", ema_s, output_name=f"EMA_{ema_s}"),
                ind("ema", ema_l, output_name=f"EMA_{ema_l}"),
                ind("rsi", 14, output_name="RSI_14"),
                ind("atr", 14, output_name="ATR_14"),
            ],
            entry=[
                rule("crossover", indicator_a=f"EMA_{ema_s}", indicator_b=f"EMA_{ema_l}", direction="above"),
                rule("threshold", indicator="RSI_14", direction="above", value=45),
            ],
            exit_rules=[
                rule("crossover", indicator_a=f"EMA_{ema_s}", indicator_b=f"EMA_{ema_l}", direction="below"),
            ],
        ))

    # Donchian + ADX
    strategies.append(strat(
        name="Donchian+ADX 돌파",
        description="Donchian 20일 상한 돌파 + ADX>20 추세 확인 매수",
        category="composite",
        indicators=[
            ind("donchian", 20, output_name="DC_20"),
            ind("adx", 14, output_name="ADX_14"),
        ],
        entry=[
            rule("threshold_compare", indicator_a="Close", indicator_b="DCU_20", direction="above"),
            rule("threshold", indicator="ADX_14", direction="above", value=20),
        ],
        exit_rules=[
            rule("threshold_compare", indicator_a="Close", indicator_b="DCL_20", direction="below"),
        ],
    ))

    # KC + MACD + RSI
    strategies.append(strat(
        name="KC+MACD+RSI 트리플",
        description="Keltner 상한 돌파 + MACD 양수 + RSI<70 여력 확인 매수",
        category="composite",
        indicators=[
            ind("kc", 20, output_name="KC_20", scalar=2.0),
            ind("macd", output_name="MACD_12_26_9", fast=12, slow=26, signal=9),
            ind("rsi", 14, output_name="RSI_14"),
        ],
        entry=[
            rule("threshold_compare", indicator_a="Close", indicator_b="KCU_20", direction="above"),
            rule("threshold", indicator="MACDh_12_26_9", direction="above", value=0),
            rule("threshold", indicator="RSI_14", direction="below", value=70),
        ],
        exit_rules=[
            rule("threshold_compare", indicator_a="Close", indicator_b="KCM_20", direction="below"),
        ],
    ))

    return strategies


# ---------------------------------------------------------------------------
# Generator & Validator
# ---------------------------------------------------------------------------

class StrategyGenerator:
    def __init__(self, output_dir: Path = STRATEGIES_DIR):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _load_existing_fingerprints(self) -> set[str]:
        fps = set()
        for p in self.output_dir.glob("*.yaml"):
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data:
                fps.add(fingerprint(data))
        return fps

    def _load_existing_names(self) -> set[str]:
        names = set()
        for p in self.output_dir.glob("*.yaml"):
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data and data.get("name"):
                names.add(data["name"])
        return names

    def generate(self, dry_run: bool = False) -> list[dict]:
        existing_fps = self._load_existing_fingerprints()
        existing_names = self._load_existing_names()

        generators = [
            ("Crossover", gen_crossover),
            ("Momentum", gen_momentum),
            ("Trend", gen_trend),
            ("Breakout", gen_breakout),
            ("Volatility", gen_volatility),
            ("Mean Reversion", gen_mean_reversion),
            ("Volume", gen_volume),
            ("Composite", gen_composite),
        ]

        all_new = []
        skipped_dup = 0
        skipped_name = 0

        for cat_name, gen_fn in generators:
            strategies = gen_fn()
            cat_count = 0
            for s in strategies:
                fp = fingerprint(s)
                if fp in existing_fps:
                    skipped_dup += 1
                    continue
                if s["name"] in existing_names:
                    skipped_name += 1
                    continue

                existing_fps.add(fp)
                existing_names.add(s["name"])

                if not dry_run:
                    import re
                    safe_name = re.sub(r'[<>:"/\\|?*]', '_', s["name"])
                    path = self.output_dir / f"{safe_name}.yaml"
                    with open(path, "w", encoding="utf-8") as f:
                        yaml.safe_dump(s, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

                all_new.append(s)
                cat_count += 1

            logger.info(f"  {cat_name}: {cat_count} strategies {'(preview)' if dry_run else 'generated'}")

        logger.info(f"\nTotal: {len(all_new)} new, {skipped_dup} structural duplicates, {skipped_name} name duplicates")
        return all_new


class BatchValidator:
    def __init__(self, tickers: list[str] = None, start_date: str = "2020-01-01"):
        from backtester.engine import BacktestEngine
        from market_data import KRXFetcher
        from strategy.registry import StrategyRegistry

        self.tickers = tickers or ["005930", "000660", "035420", "035720", "051910"]
        self.start_date = start_date
        self.engine = BacktestEngine(initial_capital=10_000_000, commission=0.00015)
        self.fetcher = KRXFetcher(delay=0.5)
        self.registry = StrategyRegistry()

    def validate_all(self) -> dict:
        results = {"validated": [], "rejected": [], "errors": []}

        # Pre-fetch data for all tickers
        data_cache = {}
        for ticker in self.tickers:
            try:
                df = self.fetcher.get_ohlcv(ticker, self.start_date)
                if df is not None and len(df) >= 60:
                    data_cache[ticker] = df
                    logger.info(f"  Fetched {ticker}: {len(df)} rows")
            except Exception as e:
                logger.warning(f"  Failed to fetch {ticker}: {e}")

        if not data_cache:
            logger.error("No data fetched. Aborting validation.")
            return results

        draft_names = []
        for name in self.registry.list_strategies():
            try:
                strat = self.registry.load(name)
                if strat.status == "draft":
                    draft_names.append(name)
            except Exception:
                pass

        logger.info(f"\nValidating {len(draft_names)} draft strategies against {len(data_cache)} tickers...")

        for i, name in enumerate(draft_names, 1):
            try:
                strategy = self.registry.load(name)
            except Exception as e:
                logger.warning(f"  [{i}/{len(draft_names)}] Load failed: {name} - {e}")
                results["errors"].append(name)
                continue

            valid_count = 0
            total_count = 0

            for ticker, df in data_cache.items():
                try:
                    result = self.engine.run(df, strategy, ticker)
                    total_count += 1
                    if result.is_valid:
                        valid_count += 1
                except Exception as e:
                    logger.debug(f"  Backtest error {name}/{ticker}: {e}")

            # Majority validation
            is_valid = valid_count >= max(1, total_count // 2) if total_count > 0 else False

            if is_valid:
                strategy.status = "validated"
                results["validated"].append(name)
            else:
                strategy.status = "rejected"
                results["rejected"].append(name)

            self.registry.save_strategy(strategy)
            status_icon = "V" if is_valid else "X"
            logger.info(f"  [{i}/{len(draft_names)}] [{status_icon}] {name}: {valid_count}/{total_count} tickers valid")
            gc.collect()

        return results


def main():
    parser = argparse.ArgumentParser(description="Generate and validate trading strategies")
    parser.add_argument("--generate", action="store_true", help="Generate YAML strategy files")
    parser.add_argument("--validate", action="store_true", help="Validate draft strategies via backtesting")
    parser.add_argument("--all", action="store_true", help="Generate + Validate")
    parser.add_argument("--dry-run", action="store_true", help="Preview strategies without writing files")
    parser.add_argument("--tickers", type=str, default=None, help="Comma-separated ticker list")
    parser.add_argument("--start-date", type=str, default="2020-01-01", help="Backtest start date")
    args = parser.parse_args()

    if not any([args.generate, args.validate, args.all, args.dry_run]):
        parser.print_help()
        return

    if args.generate or args.all or args.dry_run:
        logger.info("=== Strategy Generation ===")
        gen = StrategyGenerator()
        new_strategies = gen.generate(dry_run=args.dry_run)
        logger.info(f"\n{'Preview' if args.dry_run else 'Generated'}: {len(new_strategies)} new strategies")

        total = len(list(STRATEGIES_DIR.glob("*.yaml")))
        logger.info(f"Total strategies on disk: {total}")

    if args.validate or args.all:
        logger.info("\n=== Batch Validation ===")
        tickers = args.tickers.split(",") if args.tickers else None
        validator = BatchValidator(tickers=tickers, start_date=args.start_date)
        results = validator.validate_all()

        logger.info(f"\n=== Validation Results ===")
        logger.info(f"  Validated: {len(results['validated'])}")
        logger.info(f"  Rejected:  {len(results['rejected'])}")
        logger.info(f"  Errors:    {len(results['errors'])}")

        # Count total validated
        from strategy.registry import StrategyRegistry
        reg = StrategyRegistry()
        validated = [n for n in reg.list_strategies()
                     if reg.load(n).status == "validated"]
        logger.info(f"\nTotal validated strategies: {len(validated)}")


if __name__ == "__main__":
    main()
