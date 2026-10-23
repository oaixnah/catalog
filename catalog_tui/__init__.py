from __future__ import annotations


def run_tui(db_path: str, initial_view: str = "models") -> None:
    from .app import CatalogApp

    CatalogApp(db_path, initial_view).run()
