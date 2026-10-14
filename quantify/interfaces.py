"""Future adapters. Protocols only: no backtest or calibration is implemented."""
from typing import Protocol


class DataProvider(Protocol):
    def list_stocks(self) -> list: ...
    def get_stock(self, symbol: str) -> dict:
        """Include source, unit, period_end, available_at, price adjustment and as_of."""
        ...


class WalkForwardBacktester(Protocol):
    def evaluate(self, observations: list, train_window: int, test_window: int,
                 available_at_key: str, horizon_days: int) -> dict:
        """Chronological out-of-sample folds; purge overlap; account for costs and delistings."""
        ...


class ProbabilityCalibrator(Protocol):
    def fit(self, out_of_sample_predictions: list, realized_events: list) -> None: ...
    def predict(self, probabilities: list) -> list: ...
    def diagnostics(self) -> dict:
        """Report reliability bins, Brier score, log loss and independent test window."""
        ...
