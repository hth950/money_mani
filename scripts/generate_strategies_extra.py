"""Generate additional strategies to reach 100+ validated total.

Focus on high-success-rate categories: composite, trend, crossover.
"""

from __future__ import annotations

import gc
import logging
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STRATEGIES_DIR = ROOT / "config" / "strategies"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

from scripts.generate_strategies import ind, rule, strat, fingerprint


def gen_extra_composite() -> list[dict]:
    """More composite strategies (high validation rate)."""
    strategies = []

    # MACD + RSI + MA
    for rsi_thresh, ma_period in [(45, 20), (50, 50), (40, 30)]:
        strategies.append(strat(
            name=f"MACD+RSI+SMA 복합 (RSI {rsi_thresh}, MA {ma_period})",
            description=f"MACD 양수 + RSI>{rsi_thresh} + SMA({ma_period}) 지지 매수",
            category="composite",
            indicators=[
                ind("macd", output_name="MACD_12_26_9", fast=12, slow=26, signal=9),
                ind("rsi", 14, output_name="RSI_14"),
                ind("sma", ma_period, output_name=f"SMA_{ma_period}"),
            ],
            entry=[
                rule("threshold", indicator="MACDh_12_26_9", direction="above", value=0),
                rule("threshold", indicator="RSI_14", direction="above", value=rsi_thresh),
                rule("threshold_compare", indicator_a="Close", indicator_b=f"SMA_{ma_period}", direction="above"),
            ],
            exit_rules=[
                rule("threshold", indicator="MACDh_12_26_9", direction="below", value=0),
            ],
        ))

    # BB + MACD + ADX
    strategies.append(strat(
        name="BB+MACD+ADX 트리플",
        description="BB 상단 미만 + MACD 양수 + ADX 추세 확인 매수",
        category="composite",
        indicators=[
            ind("bbands", 20, output_name="BB_20", std=2.0),
            ind("macd", output_name="MACD_12_26_9", fast=12, slow=26, signal=9),
            ind("adx", 14, output_name="ADX_14"),
        ],
        entry=[
            rule("threshold", indicator="MACDh_12_26_9", direction="above", value=0),
            rule("threshold", indicator="ADX_14", direction="above", value=20),
            rule("threshold_compare", indicator_a="Close", indicator_b="BBU_20_2.0", direction="below"),
        ],
        exit_rules=[
            rule("threshold_compare", indicator_a="Close", indicator_b="BBM_20_2.0", direction="below"),
        ],
    ))

    # RSI + Stoch double oversold
    strategies.append(strat(
        name="RSI+Stochastic 이중 과매도",
        description="RSI<35 + Stochastic<25 이중 과매도 확인 반등 매수",
        category="composite",
        indicators=[
            ind("rsi", 14, output_name="RSI_14"),
            ind("stoch", 14, output_name="STOCH_14"),
        ],
        entry=[
            rule("threshold", indicator="RSI_14", direction="below", value=35),
            rule("threshold", indicator="STOCH_14", direction="below", value=25),
        ],
        exit_rules=[
            rule("threshold", indicator="RSI_14", direction="above", value=65),
        ],
        take_profit=0.12,
    ))

    # EMA + CCI + Volume
    strategies.append(strat(
        name="EMA+CCI+거래량 복합",
        description="EMA 20 지지 + CCI>0 모멘텀 + 거래량 1.5배 매수",
        category="composite",
        indicators=[
            ind("ema", 20, output_name="EMA_20"),
            ind("cci", 20, output_name="CCI_20"),
            ind("sma", 20, column="volume", output_name="VOL_SMA_20"),
        ],
        entry=[
            rule("threshold_compare", indicator_a="Close", indicator_b="EMA_20", direction="above"),
            rule("threshold", indicator="CCI_20", direction="above", value=0),
            rule("threshold", indicator="VOL_RATIO", direction="above", value=1.5),
        ],
        exit_rules=[
            rule("threshold", indicator="CCI_20", direction="below", value=-100),
        ],
    ))

    # Donchian + RSI + MACD
    strategies.append(strat(
        name="Donchian+RSI+MACD 복합",
        description="Donchian 20 채널 내 + RSI>40 + MACD 양수 매수",
        category="composite",
        indicators=[
            ind("donchian", 20, output_name="DC_20"),
            ind("rsi", 14, output_name="RSI_14"),
            ind("macd", output_name="MACD_12_26_9", fast=12, slow=26, signal=9),
        ],
        entry=[
            rule("threshold_compare", indicator_a="Close", indicator_b="DCU_20", direction="above"),
            rule("threshold", indicator="RSI_14", direction="above", value=40),
            rule("threshold", indicator="MACDh_12_26_9", direction="above", value=0),
        ],
        exit_rules=[
            rule("threshold_compare", indicator_a="Close", indicator_b="DCM_20", direction="below"),
        ],
    ))

    # Williams + MACD
    strategies.append(strat(
        name="WILLR+MACD 복합",
        description="Williams %R 과매도 + MACD 히스토그램 전환 매수",
        category="composite",
        indicators=[
            ind("willr", 14, output_name="WILLR_14"),
            ind("macd", output_name="MACD_12_26_9", fast=12, slow=26, signal=9),
        ],
        entry=[
            rule("threshold", indicator="WILLR_14", direction="below", value=-80),
            rule("threshold", indicator="MACDh_12_26_9", direction="above", value=0),
        ],
        exit_rules=[
            rule("threshold", indicator="WILLR_14", direction="above", value=-20),
        ],
    ))

    # PSAR + RSI + EMA
    strategies.append(strat(
        name="PSAR+RSI+EMA 트리플",
        description="PSAR 매수 + RSI>45 + EMA 20 위 매수",
        category="composite",
        indicators=[
            ind("psar", output_name="PSAR", af=0.02, max_af=0.2),
            ind("rsi", 14, output_name="RSI_14"),
            ind("ema", 20, output_name="EMA_20"),
        ],
        entry=[
            rule("threshold_compare", indicator_a="Close", indicator_b="PSAR", direction="above"),
            rule("threshold", indicator="RSI_14", direction="above", value=45),
            rule("threshold_compare", indicator_a="Close", indicator_b="EMA_20", direction="above"),
        ],
        exit_rules=[
            rule("threshold_compare", indicator_a="Close", indicator_b="PSAR", direction="below"),
        ],
    ))

    return strategies


def gen_extra_trend() -> list[dict]:
    """More trend strategies."""
    strategies = []

    # Triple MA alignment
    for s, m, l in [(5, 15, 45), (8, 20, 60), (10, 25, 75)]:
        strategies.append(strat(
            name=f"SMA 삼중 정렬 ({s}-{m}-{l})",
            description=f"SMA {s} > {m} > {l} 정렬 확인 추세 매수",
            category="trend",
            indicators=[
                ind("sma", s, output_name=f"SMA_{s}"),
                ind("sma", m, output_name=f"SMA_{m}"),
                ind("sma", l, output_name=f"SMA_{l}"),
            ],
            entry=[
                rule("threshold_compare", indicator_a=f"SMA_{s}", indicator_b=f"SMA_{m}", direction="above"),
                rule("threshold_compare", indicator_a=f"SMA_{m}", indicator_b=f"SMA_{l}", direction="above"),
            ],
            exit_rules=[
                rule("threshold_compare", indicator_a=f"SMA_{s}", indicator_b=f"SMA_{m}", direction="below"),
            ],
        ))

    # EMA + ADX trend
    for ema_period in [10, 20, 30]:
        strategies.append(strat(
            name=f"EMA({ema_period})+ADX 추세",
            description=f"EMA({ema_period}) 위 + ADX>20 추세 매수",
            category="trend",
            indicators=[
                ind("ema", ema_period, output_name=f"EMA_{ema_period}"),
                ind("adx", 14, output_name="ADX_14"),
            ],
            entry=[
                rule("threshold_compare", indicator_a="Close", indicator_b=f"EMA_{ema_period}", direction="above"),
                rule("threshold", indicator="ADX_14", direction="above", value=20),
                rule("threshold_compare", indicator_a="DMP_14", indicator_b="DMN_14", direction="above"),
            ],
            exit_rules=[
                rule("threshold_compare", indicator_a="Close", indicator_b=f"EMA_{ema_period}", direction="below"),
            ],
        ))

    # ROC + SMA trend
    for roc_period, roc_thresh in [(20, 5), (60, 8)]:
        strategies.append(strat(
            name=f"ROC({roc_period})+SMA 추세",
            description=f"ROC({roc_period})>{roc_thresh}% + SMA 50 위 모멘텀 추세",
            category="trend",
            indicators=[
                ind("roc", roc_period, output_name=f"ROC_{roc_period}"),
                ind("sma", 50, output_name="SMA_50"),
            ],
            entry=[
                rule("threshold", indicator=f"ROC_{roc_period}", direction="above", value=roc_thresh),
                rule("threshold_compare", indicator_a="Close", indicator_b="SMA_50", direction="above"),
            ],
            exit_rules=[
                rule("threshold", indicator=f"ROC_{roc_period}", direction="below", value=0),
            ],
        ))

    return strategies


def gen_extra_crossover() -> list[dict]:
    """More crossover strategies."""
    strategies = []

    # EMA + RSI filter crossover
    for s, l, rsi_thresh in [(5, 20, 45), (8, 21, 50), (12, 26, 40)]:
        strategies.append(strat(
            name=f"EMA({s}-{l})+RSI 필터 크로스",
            description=f"EMA {s}/{l} 크로스 + RSI>{rsi_thresh} 필터 매수",
            category="crossover",
            indicators=[
                ind("ema", s, output_name=f"EMA_{s}"),
                ind("ema", l, output_name=f"EMA_{l}"),
                ind("rsi", 14, output_name="RSI_14"),
            ],
            entry=[
                rule("crossover", indicator_a=f"EMA_{s}", indicator_b=f"EMA_{l}", direction="above"),
                rule("threshold", indicator="RSI_14", direction="above", value=rsi_thresh),
            ],
            exit_rules=[
                rule("crossover", indicator_a=f"EMA_{s}", indicator_b=f"EMA_{l}", direction="below"),
            ],
        ))

    # SMA + Volume crossover
    for s, l in [(10, 30), (20, 50)]:
        strategies.append(strat(
            name=f"SMA({s}-{l})+거래량 크로스",
            description=f"SMA {s}/{l} 크로스 + 거래량 1.5배 확인 매수",
            category="crossover",
            indicators=[
                ind("sma", s, output_name=f"SMA_{s}"),
                ind("sma", l, output_name=f"SMA_{l}"),
                ind("sma", 20, column="volume", output_name="VOL_SMA_20"),
            ],
            entry=[
                rule("crossover", indicator_a=f"SMA_{s}", indicator_b=f"SMA_{l}", direction="above"),
                rule("threshold", indicator="VOL_RATIO", direction="above", value=1.5),
            ],
            exit_rules=[
                rule("crossover", indicator_a=f"SMA_{s}", indicator_b=f"SMA_{l}", direction="below"),
            ],
        ))

    return strategies


def generate_and_validate():
    from scripts.generate_strategies import StrategyGenerator, BatchValidator

    all_strats = gen_extra_composite() + gen_extra_trend() + gen_extra_crossover()
    logger.info(f"Generated {len(all_strats)} extra strategy definitions")

    # Load existing fingerprints
    gen = StrategyGenerator()
    existing_fps = gen._load_existing_fingerprints()
    existing_names = gen._load_existing_names()

    written = 0
    for s in all_strats:
        fp = fingerprint(s)
        if fp in existing_fps or s["name"] in existing_names:
            logger.info(f"  Skip (duplicate): {s['name']}")
            continue

        safe_name = re.sub(r'[<>:"/\\|?*]', '_', s["name"])
        path = STRATEGIES_DIR / f"{safe_name}.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(s, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        existing_fps.add(fp)
        existing_names.add(s["name"])
        written += 1

    logger.info(f"Written {written} new strategies")
    total = len(list(STRATEGIES_DIR.glob("*.yaml")))
    logger.info(f"Total on disk: {total}")

    # Validate
    logger.info("\nValidating new draft strategies...")
    validator = BatchValidator()
    results = validator.validate_all()
    logger.info(f"Validated: {len(results['validated'])}, Rejected: {len(results['rejected'])}")

    from strategy.registry import StrategyRegistry
    reg = StrategyRegistry()
    validated = sum(1 for n in reg.list_strategies() if reg.load(n).status == "validated")
    logger.info(f"\nTotal validated: {validated}")


if __name__ == "__main__":
    generate_and_validate()
