"""Service exports with lazy loading to avoid circular imports."""

import importlib
from typing import Any

__all__ = ["database_service", "LLMRegistry", "llm_service"]


_exports = {
    "database_service": ("app.services.database", "database_service"),
    "LLMRegistry": ("app.services.llm", "LLMRegistry"),
    "llm_service": ("app.services.llm", "llm_service"),
}


def __getattr__(name: str) -> Any:
    if name not in _exports:
        raise AttributeError(f"module 'app.services' has no attribute '{name}'")

    module_name, attr = _exports[name]
    module = importlib.import_module(module_name)
    return getattr(module, attr)
