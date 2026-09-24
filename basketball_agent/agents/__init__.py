"""Agent layer: orchestration, validation, risk, ensemble and prediction.

Agents never fabricate probabilities — they only interpret data and the
outputs of statistical models / simulations.

Submodules are imported by their full paths (``agents.prediction_agent``
etc.); this package has no eager re-exports so the agent layer can never
re-enter ``evaluation.backtest`` mid-import (latent cycle found and removed
in Phase 8-C1).
"""
