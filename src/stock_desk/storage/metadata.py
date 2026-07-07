"""Complete application metadata for migrations and schema validation."""

from stock_desk.storage.base import Base
from stock_desk.storage import models as _storage_models  # noqa: F401

# Import formula models only after the core storage models are fully initialized.
# This keeps market modules free to import storage models before formula modules.
from stock_desk.formula import models as _formula_models  # noqa: F401,E402
from stock_desk.backtest import models as _backtest_models  # noqa: F401,E402
from stock_desk.analysis import models as _analysis_models  # noqa: F401,E402


__all__ = ["Base"]
