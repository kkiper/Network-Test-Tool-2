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
    app.serials["switch1"].set("fcw2233a1b")
    app._run()
    while app.running:
        app.root.update()
        app.root.after(20)
    rows = [app.tree.item(i, "values") for i in app.tree.get_children()]
    assert rows[0][0] == "FAIL" and rows[0][2] == "GI 1/5"
    assert app.banner.cget("text").startswith("FAIL")
    saved = sorted(os.listdir(tmp_path / "reports"))
    assert len(saved) == 2 and "_fcw2233a1b_" in saved[0]
    assert "Serial number: FCW2233A1B" in open(tmp_path / "reports" / saved[1]).read()
    assert "How to fix" in app.details.get("1.0", "end")


def test_serial_box_holds_at_most_ten_characters(app):
    paths = {os.path.basename(p): p for _, p in app.tests}
    app.test_path.set(paths["combined.json"])
    app._build_connection()
    assert sorted(app.serials) == ["switch1", "switch2"]
    boxes = [w for w in app.unit_frame.winfo_children()
             if isinstance(w, tk.ttk.Entry) and w.cget("textvariable") in
             (str(app.serials["switch1"]), str(app.serials["switch2"]))]
    assert len(boxes) == 2
    boxes[0].insert(0, "ABCDEFGHIJ")
    boxes[0].insert("end", "K")        # an 11th character is refused
    assert boxes[0].get() == "ABCDEFGHIJ"
