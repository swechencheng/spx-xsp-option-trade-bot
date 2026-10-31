from base_option_trade_bot import BaseOptionTradeBot


class SpxOptionTradeBot(BaseOptionTradeBot):
    @property
    def ticker_symbol(self) -> str:
        return "SPX"

    @property
    def trading_class(self) -> str:
        return "SPXW"

    @property
    def spot_multiplier(self) -> float:
        # SPX spot is 1x
        return 1.0

    @property
    def default_walk_step(self) -> float:
        return 0.05

    @property
    def strike_offset(self) -> int:
        # SPX spreads are usually 5-wide (equivalent risk to XSP 1-wide)
        return 5


if __name__ == "__main__":
    bot = SpxOptionTradeBot()
    bot.run()
