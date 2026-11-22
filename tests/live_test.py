"""Valida as rotas públicas com os formulários e o cliente HTTP do app."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import App
from api_core import dig


CASES = (
    (0, 0, "Defesa Civil RS / situação", "data.tags_data.qualle_meteorologia"),
    (0, 1, "Defesa Civil RS / histórico", "data.historic.items"),
    (1, 0, "Open-Meteo / previsão", "hourly.time"),
    (1, 1, "Open-Meteo / histórico", "hourly.time"),
    (1, 2, "Open-Meteo / vazão", "daily.river_discharge"),
    (1, 3, "Open-Meteo / busca de local", "results"),
    (1, 4, "Open-Meteo / elevação", "elevation"),
    (2, 0, "OpenZenith / elevação", "elevation"),
    (2, 1, "OpenZenith / health", "status"),
    (2, 2, "OpenZenith / OpenAPI", "paths"),
    (3, 1, "ANA / OpenAPI público", "paths"),
    (4, 0, "REINDESC / portal", None),
)


def check(job):
    tab, request, settings, name, field = job
    result = tab.execute(request, settings)
    value = dig(result.data, field) if field else result.text
    valid = value is not None and (field is not None or "REINDESC" in value.upper())
    if name == "OpenZenith / health":
        valid = value == "healthy"
    return name, result.ok and valid, result.summary()


def main():
    app = App()
    app.withdraw()
    try:
        notebook = next(w for w in app.winfo_children() if isinstance(w, ttk.Notebook))
        tabs = [app.nametowidget(tab_id) for tab_id in notebook.tabs()]
        jobs = []
        for index, option, name, field in CASES:
            tab = tabs[index]
            tab.select_spec(option)
            jobs.append((tab, tab.current_spec().build(tab.form.values()),
                         {**tab.snapshot(), "timeout": 15.0}, name, field))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(check, jobs))
    finally:
        app.destroy()
    for name, passed, detail in results:
        print(f"{'OK' if passed else 'FALHA'} {name}: {detail}")
    return 0 if all(passed for _, passed, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
