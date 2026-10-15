"""Smoke test: build the window and run an offline test through it.

Skipped where tkinter or a display isn't available (CI without Xvfb).
"""
import os

import pytest

tk = pytest.importorskip("tkinter")

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr("nettest.gui_logic.SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr("nettest.gui_logic.reports_dir", lambda site: str(tmp_path / "reports"))
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    from nettest import gui
    a = gui.App(root)
    yield a
    root.destroy()


def test_offline_run_fills_table_and_saves_report(app, tmp_path):
    paths = {os.path.basename(p): p for _, p in app.tests}
    app.test_path.set(paths["switch1.json"])
    app.mode.set("offline")
    app._build_connection()
    app.addresses["switch1"].set(os.path.join(HERE, "samples", "switch1_faults.log"))
    app._run()
    while app.running:
        app.root.update()
        app.root.after(20)
    rows = [app.tree.item(i, "values") for i in app.tree.get_children()]
    assert rows[0][0] == "FAIL" and rows[0][2] == "GI 1/5"
    assert app.banner.cget("text").startswith("FAIL")
    assert len(os.listdir(tmp_path / "reports")) == 2
    assert "How to fix" in app.details.get("1.0", "end")
