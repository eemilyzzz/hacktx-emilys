from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_demo_review_and_export_flow():
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "main.py", default_timeout=30).run()
    assert not app.exception
    next(widget for widget in app.checkbox if widget.label == "Read text from images").uncheck().run()
    next(widget for widget in app.button if widget.label == "Try fictional demo posts").click().run()
    assert not app.exception
    items = app.session_state["candidates"]
    assert len(items) == 2
    assert not any(item.reviewed for item in items)
    next(widget for widget in app.button if widget.label == "Save & approve").click().run()
    assert not app.exception
    assert sum(item.reviewed for item in app.session_state["candidates"]) == 1
    downloads = app.get("download_button")
    assert not downloads[0].disabled
