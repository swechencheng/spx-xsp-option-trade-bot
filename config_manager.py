"""Configuration Manager for XSP/SPX Options Trading Strategy.

Loads settings from config.json (matching the TastyAgent configuration pattern)
and manages dual IBKR gateway configurations:
1. Real Account (Market Data): supplies real-time streaming market data, Greeks, and spot price.
2. Trading Account (Paper / Real): places BAG combo orders and executes the strategy.
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.json"
)


@dataclass
class GatewayConfig:
    host: str
    port: int
    client_id: int
    account: Optional[str] = None


@dataclass
class StrategyConfig:
    real: GatewayConfig
    paper: GatewayConfig
    raw_config: dict


def load_config(config_path: Optional[str] = None) -> StrategyConfig:
    """Load configuration from config.json if available, or return standard defaults."""
    path = config_path or DEFAULT_CONFIG_PATH
    data = {}

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info("Loaded configuration from %s", path)
        except Exception as e:
            logger.warning("Failed to parse %s: %s. Using default settings.", path, e)
    else:
        logger.info("%s not found. Using default IBKR gateway settings.", path)

    ibkr_section = data.get("IBKR", {})
    real_sec = ibkr_section.get("real", {})
    paper_sec = ibkr_section.get("paper", {})

    real_cfg = GatewayConfig(
        host=real_sec.get("host", "127.0.0.1"),
        port=int(real_sec.get("port", 4001)),
        client_id=int(real_sec.get("client_id", 15)),
        account=real_sec.get("account"),
    )

    paper_cfg = GatewayConfig(
        host=paper_sec.get("host", "127.0.0.1"),
        port=int(paper_sec.get("port", 4002)),
        client_id=int(paper_sec.get("client_id", 16)),
        account=paper_sec.get("account"),
    )

    return StrategyConfig(real=real_cfg, paper=paper_cfg, raw_config=data)

