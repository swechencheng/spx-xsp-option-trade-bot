from base_option_trade_bot import BaseOptionTradeBot


class XspOptionTradeBot(BaseOptionTradeBot):
    @property
    def ticker_symbol(self) -> str:
        return "XSP"

    @property
    def trading_class(self) -> str:
        return "XSP"

    @property
    def spot_multiplier(self) -> float:
        # XSP is roughly 1/10th of SPX
        return 0.1

    @property
    def default_walk_step(self) -> float:
        return 0.01

    @property
    def strike_offset(self) -> int:
        return 1

    @property
    def default_max_strike_price_away(self) -> float:
        # XSP short strike distance cap from spot price
        return 12.0


if __name__ == "__main__":
    bot = XspOptionTradeBot()
    bot.run()
