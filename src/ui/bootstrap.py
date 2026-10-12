"""Builds the real agent + RunManager. Heavy imports (OCR models) live here, not in app/runner."""

from __future__ import annotations

import os

from src.ui.rule_summary import RuleCatalog
from src.ui.runner import RunManager
from src.ui.web_presenter import WebPresenter


def build_manager(config_path: str, output_root: str = "output/ui") -> RunManager:
    # Reuse the CLI's own startup recipe so the UI behaves exactly like `python main.py`.
    from main import _load_dotenv_files, apply_configured_log_level, load_app_config, load_config_store
    from src.agent.agent import InvoiceAgent

    _load_dotenv_files()
    app_config = load_app_config(config_path)
    apply_configured_log_level(app_config, presentation=True)

    presenter = WebPresenter()
    store, config_summary = load_config_store(app_config)
    agent = InvoiceAgent(config=app_config, store=store, presenter=presenter)
    presenter.set_rule_catalog(RuleCatalog.from_store(store))
    presenter.startup_context(
        config_summary,
        app_config,
        ocr_enabled=getattr(agent, "surya_models", None) is not None,
    )
    return RunManager(agent, presenter, output_root)


def default_config_path() -> str:
    return os.environ.get("CONFIG_PATH", "config/config.yaml")
