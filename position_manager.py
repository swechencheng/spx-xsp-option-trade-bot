"""Position Manager & Persistence for SPX/XSP Options Trading Strategy.

Records open positions upon strategy execution so that subsequent intraday
monitors (such as the 0DTE Delta Monitor) can track legs, delta risk,
and manage emergency stop-losses or natural expiration.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_ACTIVE_TRADE_FILE = DEFAULT_DATA_DIR / "active_position.json"
DEFAULT_HISTORY_FILE = DEFAULT_DATA_DIR / "trade_history.jsonl"


@dataclass
class OptionLegRecord:
    strike: float
    option_type: str  # "P" or "C"
    expiry: str  # YYYYMMDD
    action: str  # "SELL" or "BUY"
    con_id: Optional[int] = None
    initial_delta: Optional[float] = None
    initial_price: Optional[float] = None
    trading_class: Optional[str] = None


@dataclass
class ActiveTradeRecord:
    symbol: str  # "SPX" or "XSP"
    trading_class: str  # "SPXW" or "XSP"
    strategy: str  # "bull", "bear", or "iron_condor"
    opened_at: str  # ISO timestamp
    expiry: str  # YYYYMMDD
    quantity: int
    fill_price: float  # Credit received per share
    status: str = "OPEN"  # "OPEN" or "CLOSED"
    short_leg: Optional[OptionLegRecord] = None
    long_leg: Optional[OptionLegRecord] = None
    # For iron condor:
    iron_condor_legs: Optional[Dict[str, Any]] = None
    close_info: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ActiveTradeRecord:
        short_data = data.get("short_leg")
        long_data = data.get("long_leg")
        short_leg = OptionLegRecord(**short_data) if short_data else None
        long_leg = OptionLegRecord(**long_data) if long_data else None

        return cls(
            symbol=data.get("symbol", "SPX"),
            trading_class=data.get("trading_class", "SPXW"),
            strategy=data.get("strategy", "bull"),
            opened_at=data.get("opened_at", ""),
            expiry=data.get("expiry", ""),
            quantity=int(data.get("quantity", 1)),
            fill_price=float(data.get("fill_price", 0.0)),
            status=data.get("status", "OPEN"),
            short_leg=short_leg,
            long_leg=long_leg,
            iron_condor_legs=data.get("iron_condor_legs"),
            close_info=data.get("close_info"),
        )


class PositionManager:
    """Manages active position file and trade history persistence."""

    def __init__(
        self,
        active_trade_file: Path | str = DEFAULT_ACTIVE_TRADE_FILE,
        history_file: Path | str = DEFAULT_HISTORY_FILE,
    ):
        self.active_trade_file = Path(active_trade_file)
        self.history_file = Path(history_file)
        self.active_trade_file.parent.mkdir(parents=True, exist_ok=True)

    def save_active_trade(self, record: ActiveTradeRecord | Dict[str, Any]) -> ActiveTradeRecord:
        """Save open trade record to active position file and append to history."""
        if isinstance(record, dict):
            trade_obj = ActiveTradeRecord.from_dict(record)
        else:
            trade_obj = record

        data = trade_obj.to_dict()
        try:
            with open(self.active_trade_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.info("Saved active trade to %s", self.active_trade_file)

            # Append to history JSONL
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("Failed to save active trade: %s", e)
            raise

        return trade_obj

    def load_active_trade(self) -> Optional[ActiveTradeRecord]:
        """Load currently active trade if file exists and status is OPEN."""
        if not self.active_trade_file.exists():
            return None

        try:
            with open(self.active_trade_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return ActiveTradeRecord.from_dict(data)
        except Exception as e:
            logger.warning("Failed to load active trade from %s: %s", self.active_trade_file, e)
            return None

    def mark_trade_closed(
        self,
        close_price: float,
        reason: str = "MANUAL",
        close_time: Optional[datetime] = None,
        extra_info: Optional[Dict[str, Any]] = None,
    ) -> Optional[ActiveTradeRecord]:
        """Mark active trade as CLOSED and update close details."""
        trade = self.load_active_trade()
        if not trade:
            logger.warning("No active trade found to mark closed.")
            return None

        close_dt = close_time or datetime.now()
        close_data = {
            "closed_at": close_dt.isoformat(),
            "close_price": float(close_price),
            "reason": reason,
        }
        if extra_info:
            close_data.update(extra_info)

        trade.status = "CLOSED"
        trade.close_info = close_data

        try:
            with open(self.active_trade_file, "w", encoding="utf-8") as f:
                json.dump(trade.to_dict(), f, indent=2, ensure_ascii=False)

            # Also append close event to history log
            history_entry = {
                "event": "TRADE_CLOSED",
                "symbol": trade.symbol,
                "strategy": trade.strategy,
                "expiry": trade.expiry,
                **close_data,
            }
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(history_entry, ensure_ascii=False) + "\n")

            logger.info("Marked trade as CLOSED: reason=%s, close_price=%.2f", reason, close_price)
        except Exception as e:
            logger.error("Failed to update closed trade in %s: %s", self.active_trade_file, e)

        return trade

    def clear_active_trade(self) -> None:
        """Remove active trade file if desired."""
        if self.active_trade_file.exists():
            try:
                self.active_trade_file.unlink()
                logger.info("Cleared %s", self.active_trade_file)
            except Exception as e:
                logger.error("Failed to remove %s: %s", self.active_trade_file, e)
