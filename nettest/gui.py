"""The Network Test Tool window (tkinter, so it runs on Windows and Linux).

Start it with ``python -m nettest`` (no arguments) or ``python -m nettest.gui``.

The tech works top to bottom down the left side: pick the site, pick what's
being tested, type the switch IP and login, press Run. Results fill the right
side, problems first, and a report is saved for the site automatically.
"""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import ConfigError, Status, find_tests, load_test, run_test
from . import gui_logic as logic

COLORS = {
    # status: (row background, banner background, banner text)
    Status.PASS: ("#dcf2df", "#2e7d32", "white"),
    Status.WARN: ("#fff2c4", "#f9a825", "black"),
    Status.FAIL: ("#f9d3cf", "#c62828", "white"),
    Status.NOT_CONFIGURED: ("#e8e8e8", "#616161", "white"),
}


class App(ttk.Frame):
    def __init__(self, root):
        super().__init__(root, padding=10)
        self.root = root
        self.settings = logic.load_settings()
        self.tests = []            # [(label, path)] for the current site
        self.plan = None
        self.report = None
        self.rows = {}             # tree item id -> PortResult
        self.events = queue.Queue()
        self.running = False

        self.site = tk.StringVar(value=self.settings.get("site", ""))
        self.test_path = tk.StringVar()
        self.test_date = tk.StringVar(value=logic.today())
        self.mode = tk.StringVar(value="live")
        self.username = tk.StringVar(value=self.settings.get("username", ""))
        self.password = tk.StringVar()
        self.enable = tk.StringVar()
        self.addresses = {}        # switch id -> StringVar
        self.serials = {}          # switch id -> StringVar, never saved to settings
        self.progress = tk.StringVar(value="Fill in the left side, then press Run test.")
        self.saved = tk.StringVar()

        self._build()
        self.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self._load_sites()

    # ---- layout ---------------------------------------------------------

    def _build(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        left = ttk.Frame(self, padding=(0, 0, 12, 0))
        left.grid(row=0, column=0, sticky="nsw")

        ttk.Label(left, text="1. Site", style="Step.TLabel").grid(sticky="w")
        row = ttk.Frame(left)
        row.grid(sticky="ew", pady=(2, 10))
        self.site_box = ttk.Combobox(row, textvariable=self.site, state="readonly", width=24)
        self.site_box.pack(side="left")
        self.site_box.bind("<<ComboboxSelected>>", lambda e: self._load_tests())
        ttk.Button(row, text="Browse...", command=self._browse_site).pack(side="left", padx=4)

        ttk.Label(left, text="2. What are you testing?", style="Step.TLabel").grid(sticky="w")
        self.tests_frame = ttk.Frame(left)
        self.tests_frame.grid(sticky="ew", pady=(2, 4))
        self.unit_frame = ttk.Frame(left)      # test date and serial numbers
        self.unit_frame.grid(sticky="ew", pady=(0, 10))

        ttk.Label(left, text="3. Connect to the switch", style="Step.TLabel").grid(sticky="w")
        modes = ttk.Frame(left)
        modes.grid(sticky="w", pady=(2, 4))
        ttk.Radiobutton(modes, text="Live switch", value="live", variable=self.mode,
                        command=self._build_connection).pack(side="left")
        ttk.Radiobutton(modes, text="Self Test File", value="offline", variable=self.mode,
                        command=self._build_connection).pack(side="left", padx=8)
        self.conn_frame = ttk.Frame(left)
        self.conn_frame.grid(sticky="ew", pady=(0, 12))
        self.conn_frame.columnconfigure(1, weight=1)

        self.run_button = ttk.Button(left, text="Run test", style="Run.TButton",
                                     command=self._run)
        self.run_button.grid(sticky="ew", ipady=6)
        ttk.Label(left, textvariable=self.progress, wraplength=300,
                  foreground="#555").grid(sticky="w", pady=(8, 0))

        right = ttk.Frame(self)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=3)
        right.rowconfigure(2, weight=2)

        self.banner = tk.Label(right, text="No test run yet", anchor="w", padx=10, pady=8,
                               font=("TkDefaultFont", 13, "bold"), bg="#e8e8e8")
        self.banner.grid(row=0, column=0, sticky="ew")

        table = ttk.Frame(right)
        table.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        cols = ("result", "switch", "port", "target", "ip", "nbr_device", "nbr_port", "found")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", selectmode="browse")
        for col, title, width, stretch in (
                ("result", "Result", 60, False), ("switch", "Switch", 80, False),
                ("port", "From (ETH Switch Port)", 175, False), ("target", "To", 210, False),
                ("ip", "IP address", 105, False),
                ("nbr_device", "Neighbor device ID", 175, False),
                ("nbr_port", "Neighbor port ID", 135, False),
                ("found", "What the test found", 300, True)):
            self.tree.heading(col, text=title, anchor="w")
            self.tree.column(col, width=width, stretch=stretch, anchor="w")
        for status, (bg, _, _) in COLORS.items():
            self.tree.tag_configure(status.value, background=bg)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_details())

        self.details = tk.Text(right, height=8, wrap="word", relief="solid", borderwidth=1,
                               padx=8, pady=6, font="TkDefaultFont")
        self.details.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self.details.insert("1.0", "Click a port to see what was found and how to fix it.")
        self.details.configure(state="disabled")

        bottom = ttk.Frame(right)
        bottom.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.open_report_btn = ttk.Button(bottom, text="Open report", state="disabled",
                                          command=self._open_report)
        self.open_report_btn.pack(side="left")
        ttk.Button(bottom, text="Open reports folder",
                   command=self._open_reports_folder).pack(side="left", padx=6)
        ttk.Label(bottom, textvariable=self.saved, foreground="#555").pack(side="left", padx=6)

    # ---- site / test / connection pickers -------------------------------

    def _load_sites(self):
        sites = logic.list_sites()
        extra = self.settings.get("site_path")
        if extra and os.path.isdir(extra) and os.path.basename(extra) not in sites:
            sites.append(os.path.basename(extra))
        self.site_box["values"] = sites
        if self.site.get() not in sites:
            self.site.set(sites[0] if sites else "")
        self._load_tests()

    def _site_path(self):
        extra = self.settings.get("site_path")
        if extra and os.path.basename(extra) == self.site.get():
            return extra
        return os.path.join(logic.configs_dir(), self.site.get())

    def _browse_site(self):
        path = filedialog.askdirectory(title="Choose the site's config folder",
                                       initialdir=logic.configs_dir())
        if not path:
            return
        self.settings["site_path"] = path
        self.site.set(os.path.basename(path))
        self._load_sites()

    def _load_tests(self):
        for w in self.tests_frame.winfo_children():
            w.destroy()
        site = self._site_path()
        self.tests = find_tests(site) if os.path.isdir(site) else []
        if not self.tests:
            ttk.Label(self.tests_frame, text="No configs found in this site folder.").pack(
                anchor="w")
            self.test_path.set("")
        else:
            paths = [p for _, p in self.tests]
            remembered = self.settings.get("test")
            if self.test_path.get() not in paths:
                self.test_path.set(next((p for p in paths if os.path.basename(p) == remembered),
                                        paths[0]))
            for label, path in self.tests:
                ttk.Radiobutton(self.tests_frame, text=label, value=path,
                                variable=self.test_path,
                                command=self._build_connection).pack(anchor="w")
        self._build_connection()

    def _build_connection(self):
        for w in self.conn_frame.winfo_children() + self.unit_frame.winfo_children():
            w.destroy()
        self.plan = None
        if not self.test_path.get():
            return
        try:
            self.plan = load_test(self.test_path.get())
        except ConfigError as e:
            ttk.Label(self.conn_frame, text="Config problem:\n%s" % e, foreground="#c62828",
                      wraplength=300).grid(columnspan=3, sticky="w")
            return

        offline = self.mode.get() == "offline"
        remembered = self.settings.get("addresses", {})
        fits = (self.register(logic.serial_fits), "%P")
        ttk.Label(self.unit_frame, text="Test date").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(self.unit_frame, textvariable=self.test_date, width=12).grid(
            row=0, column=1, sticky="w", padx=4)
        for r, sw in enumerate(self.plan.switches, start=1):
            sn = self.serials.get(sw.id)
            if sn is None:
                sn = self.serials[sw.id] = tk.StringVar()
            ttk.Label(self.unit_frame, text="%s S/N" % sw.name
                      ).grid(row=r, column=0, sticky="w", pady=2)
            ttk.Entry(self.unit_frame, textvariable=sn, width=12, validate="key",
                      validatecommand=fits).grid(row=r, column=1, sticky="w", padx=4)
        r = 0
        for sw in self.plan.switches:
            var = self.addresses.get(sw.id)
            if var is None:
                var = self.addresses[sw.id] = tk.StringVar(value=remembered.get(sw.id, ""))
            ttk.Label(self.conn_frame, text="%s %s" % (sw.name, "file" if offline else "IP")
                      ).grid(row=r, column=0, sticky="w", pady=2)
            if offline:
                var.set(var.get() if os.path.exists(var.get()) else "")
            ttk.Entry(self.conn_frame, textvariable=var, width=22).grid(
                row=r, column=1, sticky="ew", padx=4)
            if offline:
                ttk.Button(self.conn_frame, text="...", width=3,
                           command=lambda v=var: self._browse_file(v)).grid(row=r, column=2)
            r += 1
        if not offline:
            for label, var, show in (("Username", self.username, ""),
                                     ("Password", self.password, "•"),
                                     ("Enable password", self.enable, "•")):
                ttk.Label(self.conn_frame, text=label).grid(row=r, column=0, sticky="w", pady=2)
                ttk.Entry(self.conn_frame, textvariable=var, show=show, width=22).grid(
                    row=r, column=1, sticky="ew", padx=4)
                r += 1
            ttk.Label(self.conn_frame, text="(enable password only if the switch asks for one)",
                      foreground="#777").grid(row=r, column=0, columnspan=2, sticky="w")

    def _browse_file(self, var):
        path = filedialog.askopenfilename(title="Choose the self test file",
                                          filetypes=[("Logs and text", "*.log *.txt"),
                                                     ("All files", "*.*")])
        if path:
            var.set(path)

    # ---- running a test ---------------------------------------------------

    def _run(self):
        if self.running or self.plan is None:
            return
        try:
            test_date = logic.check_date(self.test_date.get())
            serials = logic.check_serials(self.plan, {sid: v.get() for sid, v in
                                                      self.serials.items()})
            collectors = logic.build_collectors(
                self.plan, self.mode.get() == "offline",
                {sid: v.get() for sid, v in self.addresses.items()},
                self.username.get().strip(), self.password.get(), self.enable.get())
        except logic.FormError as e:
            messagebox.showwarning("Before you run the test", str(e))
            return

        self._remember()
        self.running = True
        self.run_button.configure(state="disabled", text="Testing...")
        self.tree.delete(*self.tree.get_children())
        self._set_details("")
        self.banner.configure(text="Testing...", bg="#e8e8e8", fg="black")
        plan = self.plan

        def work():
            try:
                report = run_test(plan, collectors, serials=serials, test_date=test_date,
                                  on_progress=lambda m: self.events.put(("progress", m)))
                self.events.put(("done", report))
            except Exception as e:  # never leave the window stuck on "Testing..."
                self.events.put(("error", e))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll)

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    self.progress.set(value)
                elif kind == "done":
                    self._finished(value)
                    return
                elif kind == "error":
                    self._failed(value)
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _failed(self, error):
        self.running = False
        self.run_button.configure(state="normal", text="Run test")
        self.banner.configure(text="The test couldn't run", **_banner(Status.FAIL))
        self.progress.set(str(error))
        messagebox.showerror("The test couldn't run", str(error))

    def _finished(self, report):
        self.running = False
        self.report = report
        self.run_button.configure(state="normal", text="Run test")
        c = report.counts()
        self.banner.configure(
            text="%s    (%s)" % (logic.BANNER_TEXT[report.status], logic.counts_text(c)),
            **_banner(report.status))

        show_switch = len(report.switches) > 1
        self.tree.column("switch", width=80 if show_switch else 0,
                         minwidth=0, stretch=False)
        self.rows = {}
        for sw in report.switches:
            if sw.error:
                item = self.tree.insert("", "end", values=(
                    "FAIL", sw.name, "", "", "", "", "",
                    "Could not test %s: %s" % (sw.name, sw.error)),
                    tags=(Status.FAIL.value,))
                self.rows[item] = sw
        for r in logic.sorted_rows(report):
            item = self.tree.insert("", "end", values=(
                logic.STATUS_TEXT[r.status], r.switch_name, r.diagram_label, r.target, r.ip,
                r.neighbor_device, r.neighbor_port, r.summary),
                tags=(r.status.value,))
            self.rows[item] = r
        first = self.tree.get_children()
        if first:
            self.tree.selection_set(first[0])

        try:
            self.report_paths = report.save(logic.reports_dir(self.site.get() or "site"),
                                            site=self.site.get())
            self.saved.set("Report saved: %s" % os.path.basename(self.report_paths[1]))
            self.open_report_btn.configure(state="normal")
        except OSError as e:
            self.saved.set("Couldn't save the report: %s" % e)
        self.progress.set("Finished. Fix anything red or yellow and run the test again.")

    # ---- results pane -----------------------------------------------------

    def _show_details(self):
        sel = self.tree.selection()
        if not sel:
            return
        r = self.rows.get(sel[0])
        if r is None:
            return
        if hasattr(r, "findings"):
            self._set_details(logic.details_text(r))
        else:  # a whole switch that couldn't be reached
            self._set_details("%s could not be tested.\n\n%s" % (r.name, r.error))

    def _set_details(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def _open_report(self):
        if getattr(self, "report_paths", None):
            _open_path(self.report_paths[1])

    def _open_reports_folder(self):
        folder = logic.reports_dir(self.site.get() or "site")
        os.makedirs(folder, exist_ok=True)
        _open_path(folder)

    def _remember(self):
        self.settings.update({
            "site": self.site.get(),
            "test": os.path.basename(self.test_path.get()),
            "username": self.username.get().strip(),
        })
        if self.mode.get() == "live":
            self.settings["addresses"] = {sid: v.get().strip()
                                          for sid, v in self.addresses.items()}
        logic.save_settings(self.settings)


def _banner(status):
    _, bg, fg = COLORS[status]
    return {"bg": bg, "fg": fg}


def _open_path(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: only exists on Windows
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError as e:
        messagebox.showinfo("Open", "Couldn't open it automatically. It's here:\n%s\n\n(%s)"
                            % (path, e))


def main():
    root = tk.Tk()
    root.title("Network Test Tool")
    root.geometry("1540x700")
    root.minsize(900, 520)
    style = ttk.Style(root)
    if "clam" in style.theme_names() and not sys.platform.startswith("win"):
        style.theme_use("clam")
    style.configure("Step.TLabel", font=("TkDefaultFont", 11, "bold"))
    style.configure("Run.TButton", font=("TkDefaultFont", 12, "bold"))
    style.configure("Treeview", rowheight=24)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
