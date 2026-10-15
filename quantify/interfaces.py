"""Adapter contracts; CSVDataAdapter implements ResearchDataAdapter locally.

Portfolio backtesting and calibration remain future protocols.
"""
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


class ResearchDataAdapter(Protocol):
    def read(self, kind: str, raw: str) -> tuple[list, list]:
        """Return validated normalized rows and quality warnings; no storage side effects.

        External download/update adapters should map provider-specific fields to
        datasets.SCHEMAS, declare source and adjustment, then use import_snapshot.
        Missing dividend or point-in-time fields must never be synthesized.
        Online quote/history contracts are QuoteProvider/HistoryProvider in
        market_providers.py. Intraday quotes remain separate from finalized daily CSV.
        """
        ...
