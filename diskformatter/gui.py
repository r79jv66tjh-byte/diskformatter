"""tkinter-Oberfläche von DiskFormatter."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import disks, ops, ps
from .disks import fmt_size
from .settings import Settings

ACTIONS = [("format", "Formatieren"),
           ("wipe_volume", "Volume mit Nullen füllen (sicheres Löschen, Partitionen bleiben)"),
           ("wipe_disk", "Ganzen Datenträger mit Nullen füllen (inkl. Partitionstabelle)")]


class ConfirmDialog(tk.Toplevel):
    """Zusammenfassung + Eintippen des Ziels. ``result`` = getippter Text oder None."""

    def __init__(self, master, plan: ops.Plan):
        super().__init__(master)
        self.title("Bitte bestätigen")
        self.transient(master)
        self.resizable(False, False)
        self.result: str | None = None
        self.plan = plan
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Folgendes wird unwiderruflich ausgeführt:",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        txt = tk.Text(frm, width=92, height=len(plan.summary_lines()) + len(plan.steps) + 3,
                      font=("Consolas", 10), relief="solid", borderwidth=1, wrap="word")
        txt.insert("end", "\n".join(plan.summary_lines()) + "\n\nSchritte:\n")
        txt.insert("end", "\n".join(f"  {i}. {s}" for i, s in enumerate(plan.steps, 1)))
        txt.configure(state="disabled")
        txt.pack(fill="x", pady=(6, 8))
        for w in plan.warnings:
            ttk.Label(frm, text="⚠ " + w, foreground="#b00020", wraplength=640).pack(anchor="w")
        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(12, 0))
        what = "Laufwerksbuchstaben" if plan.kind == "volume" else "Datenträgernummer"
        ttk.Label(row, text=f"Zum Bestätigen den {what} eintippen:  ").pack(side="left")
        ttk.Label(row, text=plan.confirm_text, font=("Consolas", 12, "bold")).pack(side="left")
        self.var = tk.StringVar()
        self.entry = ttk.Entry(frm, textvariable=self.var, width=12, font=("Consolas", 12))
        self.entry.pack(anchor="w", pady=6)
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(8, 0))
        self.ok = ttk.Button(btns, text="Jetzt ausführen", state="disabled", command=self._ok)
        self.ok.pack(side="right")
        ttk.Button(btns, text="Abbrechen", command=self.destroy).pack(side="right", padx=6)
        self.var.trace_add("write", self._check)
        self.entry.focus_set()
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _check(self, *_):
        ok = disks.confirm_matches(self.plan.kind, self.plan.ident, self.var.get())
        self.ok.configure(state="normal" if ok else "disabled")

    def _ok(self):
        if disks.confirm_matches(self.plan.kind, self.plan.ident, self.var.get()):
            self.result = self.var.get()
            self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DiskFormatter – Laufwerke formatieren und sicher löschen")
        self.geometry("1100x720")
        self.minsize(900, 600)
        self.settings = Settings.load()
        self.snap: disks.Snapshot | None = None
        self.rows: dict[str, tuple[str, object]] = {}     # iid -> ("disk", Disk) | ("volume", Volume)
        self.q: queue.Queue = queue.Queue()
        self.job: ops.Job | None = None
        self.last_result: dict | None = None
        self.last_error: str | None = None
        self.admin = ps.is_admin()
        self._build()
        self.after(100, self._poll)
        self.refresh()

    # ------------------------------------------------------------ Aufbau
    def _build(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        m = tk.Menu(self)
        fm = tk.Menu(m, tearoff=False)
        fm.add_command(label="Aktualisieren (F5)", command=self.refresh)
        fm.add_separator()
        fm.add_command(label="Beenden", command=self.destroy)
        m.add_cascade(label="Datei", menu=fm)
        sm = tk.Menu(m, tearoff=False)
        self.var_internal = tk.BooleanVar(value=self.settings.allow_internal)
        sm.add_checkbutton(label="Interne Festplatten freischalten …", variable=self.var_internal,
                           command=self._toggle_internal)
        m.add_cascade(label="Einstellungen", menu=sm)
        self.config(menu=m)
        self.bind("<F5>", lambda e: self.refresh())

        top = ttk.Frame(self, padding=(10, 8, 10, 0))
        top.pack(fill="x")
        self.lbl_status = ttk.Label(top, text="")
        self.lbl_status.pack(side="left")
        ttk.Button(top, text="Aktualisieren", command=self.refresh).pack(side="right")

        cols = ("name", "size", "fs", "bus", "removable", "system", "state")
        self.tree = ttk.Treeview(self, columns=cols, show="tree headings", height=10, selectmode="browse")
        self.tree.heading("#0", text="Laufwerk / Datenträger")
        self.tree.column("#0", width=250)
        for c, t, w in [("name", "Name / Modell", 210), ("size", "Größe", 80), ("fs", "Dateisystem", 85),
                        ("bus", "Bus", 90), ("removable", "Wechsel", 65), ("system", "System", 60),
                        ("state", "Status", 330)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("locked", foreground="#888888")
        self.tree.tag_configure("ok", foreground="#0b6b0b")
        self.tree.pack(fill="both", expand=True, padx=10, pady=8)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())

        act = ttk.LabelFrame(self, text="Aktion", padding=10)
        act.pack(fill="x", padx=10)
        self.lbl_target = ttk.Label(act, text="Kein Ziel ausgewählt", font=("Segoe UI", 10, "bold"))
        self.lbl_target.grid(row=0, column=0, columnspan=4, sticky="w")
        self.var_action = tk.StringVar(value="format")
        self.rb = {}
        for i, (key, text) in enumerate(ACTIONS):
            self.rb[key] = ttk.Radiobutton(act, text=text, value=key, variable=self.var_action,
                                           command=self._update_controls)
            self.rb[key].grid(row=1 + i, column=0, columnspan=2, sticky="w")
        ttk.Label(act, text="Dateisystem:").grid(row=1, column=2, sticky="e", padx=(20, 4))
        self.var_fs = tk.StringVar(value="exFAT")
        ttk.Combobox(act, textvariable=self.var_fs, values=ops.FILESYSTEMS, width=10,
                     state="readonly").grid(row=1, column=3, sticky="w")
        ttk.Label(act, text="Volumenname:").grid(row=2, column=2, sticky="e", padx=(20, 4))
        self.var_label = tk.StringVar(value="")
        ttk.Entry(act, textvariable=self.var_label, width=20).grid(row=2, column=3, sticky="w")
        self.var_full = tk.BooleanVar(value=False)
        self.chk_full = ttk.Checkbutton(act, text="Vollständig formatieren (langsam, prüft Sektoren)",
                                        variable=self.var_full)
        self.chk_full.grid(row=3, column=2, columnspan=2, sticky="w", padx=(20, 0))
        ttk.Label(act, text="Partitionsstil:").grid(row=4, column=2, sticky="e", padx=(20, 4))
        self.var_style = tk.StringVar(value="Automatisch")
        self.cb_style = ttk.Combobox(act, textvariable=self.var_style, values=("Automatisch", "MBR", "GPT"),
                                     width=12, state="readonly")
        self.cb_style.grid(row=4, column=3, sticky="w")
        self.btn_start = ttk.Button(act, text="Weiter …", command=self.start, state="disabled")
        self.btn_start.grid(row=1, column=4, rowspan=2, sticky="e", padx=(30, 0))
        act.columnconfigure(4, weight=1)
        self.lbl_block = ttk.Label(act, text="", foreground="#b00020", wraplength=1000)
        self.lbl_block.grid(row=5, column=0, columnspan=5, sticky="w", pady=(6, 0))

        pr = ttk.LabelFrame(self, text="Fortschritt", padding=10)
        pr.pack(fill="x", padx=10, pady=8)
        self.pbar = ttk.Progressbar(pr, maximum=100)
        self.pbar.pack(fill="x")
        row = ttk.Frame(pr)
        row.pack(fill="x", pady=(4, 0))
        self.lbl_prog = ttk.Label(row, text="Bereit.")
        self.lbl_prog.pack(side="left")
        self.btn_cancel = ttk.Button(row, text="Abbrechen", command=self.cancel, state="disabled")
        self.btn_cancel.pack(side="right")
        self.log = tk.Text(pr, height=6, font=("Consolas", 9), state="disabled")
        self.log.pack(fill="x", pady=(6, 0))

    # ------------------------------------------------------------ Daten
    def refresh(self):
        if self.job is not None:
            return
        self.lbl_status.configure(text="Lese Laufwerke …")
        self.update_idletasks()
        try:
            self.snap = disks.query()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("DiskFormatter", f"Laufwerke konnten nicht gelesen werden:\n{e}", parent=self)
            return
        sel = self.selected()
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        for d in self.snap.disks:
            b = d.blocks(self.settings)
            state = ("🔒 " + b[0]) if d.hard_locks else (("gesperrt: " + b[0]) if b else "auswählbar")
            iid = f"disk{d.number}"
            self.tree.insert("", "end", iid=iid, open=True, text=f"Datenträger {d.number}",
                             values=(d.model, fmt_size(d.size), d.partition_style, d.bus_type,
                                     "ja" if d.removable else "nein", "ja" if d.hard_locks else "nein", state),
                             tags=("locked" if b else "ok",))
            self.rows[iid] = ("disk", d)
            for v in d.volumes:
                viid = f"vol{d.number}_{v.partition_number}_{v.letter}"
                self.tree.insert(iid, "end", iid=viid, text=(f"   {v.letter}:" if v.letter else "   (ohne Buchstabe)"),
                                 values=(v.label, fmt_size(v.size), v.filesystem or "RAW", "", "", "", ""),
                                 tags=("locked" if b else "ok",))
                self.rows[viid] = ("volume", v)
        for v in self.snap.orphans:
            viid = f"orph_{v.letter}_{len(self.rows)}"
            self.tree.insert("", "end", iid=viid, text=(f"{v.letter}:" if v.letter else "?"),
                             values=(v.label, fmt_size(v.size), v.filesystem, v.drive_type, "", "",
                                     "gesperrt: kein lokaler Datenträger"), tags=("locked",))
            self.rows[viid] = ("volume", v)
        if sel and sel in self.rows:
            self.tree.selection_set(sel)
        adm = "Administrator" if self.admin else "OHNE Adminrechte (nur Ansicht)"
        intern = "interne Platten FREIGESCHALTET" if self.settings.allow_internal else "nur Wechseldatenträger/USB"
        self.lbl_status.configure(text=f"{len(self.snap.disks)} Datenträger · {adm} · {intern}")
        self._on_select()

    def selected(self) -> str | None:
        s = self.tree.selection()
        return s[0] if s else None

    def select(self, kind: str, ident) -> bool:
        """Ziel programmatisch auswählen (für Tests): ('volume','E') oder ('disk',3)."""
        for iid, (k, obj) in self.rows.items():
            if k == kind and ((kind == "disk" and obj.number == int(ident))
                              or (kind == "volume" and obj.letter == str(ident).upper())):
                self.tree.selection_set(iid)
                self.tree.see(iid)
                self._on_select()
                return True
        return False

    def _target(self) -> str | None:
        iid = self.selected()
        if not iid:
            return None
        kind, obj = self.rows[iid]
        act = self.var_action.get()
        if act == "wipe_disk":
            n = obj.number if kind == "disk" else obj.disk_number
            return None if n is None else str(n)
        return f"{obj.letter}:" if kind == "volume" and obj.letter else None

    def _on_select(self):
        iid = self.selected()
        if iid:
            kind, obj = self.rows[iid]
            if kind == "disk":
                self.var_action.set("wipe_disk")
                self.lbl_target.configure(text=obj.title)
            else:
                if self.var_action.get() == "wipe_disk":
                    self.var_action.set("format")
                self.lbl_target.configure(text=f"Laufwerk {obj.name} „{obj.label or '-'}“, {fmt_size(obj.size)}"
                                          + (f" auf Datenträger {obj.disk_number}" if obj.disk_number is not None else ""))
        else:
            self.lbl_target.configure(text="Kein Ziel ausgewählt")
        self._update_controls()

    def _plan(self) -> ops.Plan | None:
        tgt = self._target()
        if not tgt or self.snap is None:
            return None
        act = self.var_action.get()
        style = self.var_style.get() if self.var_style.get() in ("MBR", "GPT") else None
        return ops.make_plan(self.snap, self.settings, "format" if act == "format" else "wipe", tgt,
                             self.var_fs.get(), self.var_label.get(), self.var_full.get(), style)

    def _update_controls(self):
        iid = self.selected()
        kind = self.rows[iid][0] if iid else None
        has_letter = bool(iid and kind == "volume" and self.rows[iid][1].letter)
        self.rb["format"].configure(state="normal" if has_letter else "disabled")
        self.rb["wipe_volume"].configure(state="normal" if has_letter else "disabled")
        self.rb["wipe_disk"].configure(state="normal" if iid else "disabled")
        act = self.var_action.get()
        self.chk_full.configure(state="normal" if act == "format" else "disabled")
        self.cb_style.configure(state="readonly" if act == "wipe_disk" else "disabled")
        msg, ok = "", False
        try:
            plan = self._plan()
            if plan is not None:
                ok = plan.allowed
                msg = ("Gesperrt: " + " · ".join(plan.blocked)) if plan.blocked else ""
                if ok and not self.admin:
                    ok, msg = False, "Ohne Administratorrechte nicht möglich – DiskFormatter als Administrator starten."
        except ops.OpError as e:
            msg = str(e)
        if self.job is not None:
            ok = False
        self.lbl_block.configure(text=msg)
        self.btn_start.configure(state="normal" if ok else "disabled")

    # ------------------------------------------------------------ Einstellungen
    def _toggle_internal(self):
        if self.var_internal.get():
            if not messagebox.askyesno(
                    "Interne Festplatten freischalten",
                    "ACHTUNG: Damit werden auch fest eingebaute Festplatten/SSDs auswählbar.\n\n"
                    "Das Systemlaufwerk und der Windows-Datenträger bleiben trotzdem immer gesperrt.\n"
                    "Eine falsche Auswahl löscht alle Daten auf der Platte unwiderruflich.\n\n"
                    "Wirklich freischalten?", icon="warning", parent=self):
                self.var_internal.set(False)
                return
        self.settings.allow_internal = bool(self.var_internal.get())
        self.settings.save()
        self.refresh()

    # ------------------------------------------------------------ Ausführen
    def ask_confirmation(self, plan: ops.Plan) -> str | None:
        dlg = ConfirmDialog(self, plan)
        self.wait_window(dlg)
        return dlg.result

    def start(self):
        try:
            plan = self._plan()
        except ops.OpError as e:
            messagebox.showerror("DiskFormatter", str(e), parent=self)
            return
        if plan is None or not plan.allowed:
            return
        typed = self.ask_confirmation(plan)
        if not typed:
            return
        self.run_plan(plan, typed)

    def run_plan(self, plan: ops.Plan, typed: str):
        self.job = ops.Job(progress=lambda pct, msg: self.q.put(("prog", pct, msg)))
        self.last_result, self.last_error = None, None
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.pbar.configure(value=0)
        self._log(f"== {plan.mode_name}: {plan.confirm_text}")
        uid = ops.disk_uid(plan.disk) if plan.disk else None
        kw = dict(filesystem=plan.filesystem, label=plan.label, full=plan.full,
                  partition_style=plan.partition_style or None, expect_uid=uid, settings=self.settings)
        target = plan.confirm_text
        job = self.job

        def work():
            try:
                res = ops.execute(plan.action, target, typed, job=job, **kw)
                self.q.put(("done", res, None))
            except Exception as e:  # noqa: BLE001
                self.q.put(("done", None, str(e)))
        threading.Thread(target=work, daemon=True).start()

    def cancel(self):
        if self.job and messagebox.askyesno(
                "Abbrechen?", "Vorgang abbrechen?\n\nDas Laufwerk ist danach unvollständig formatiert bzw. "
                "nur teilweise genullt und muss neu formatiert werden.", parent=self):
            self.job.request_cancel()
            self._log("Abbruch angefordert …")

    def _log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "prog":
                    _, pct, msg = item
                    if pct is None:
                        if str(self.pbar.cget("mode")) != "indeterminate":
                            self.pbar.configure(mode="indeterminate")
                            self.pbar.start(15)
                    else:
                        if str(self.pbar.cget("mode")) != "determinate":
                            self.pbar.stop()
                            self.pbar.configure(mode="determinate")
                        self.pbar.configure(value=pct)
                    self.lbl_prog.configure(text=msg)
                    if not msg[:3].strip().rstrip("%").isdigit():
                        self._log(msg)
                else:
                    _, res, err = item
                    self._finish(res, err)
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _finish(self, res: dict | None, err: str | None):
        self.pbar.stop()
        self.pbar.configure(mode="determinate", value=100 if res else 0)
        self.btn_cancel.configure(state="disabled")
        self.job = None
        self.last_result, self.last_error = res, err
        if res:
            v = res.get("verify")
            ver = (f"\nStichprobe: {v['zero_samples']} von {v['samples']} gelesenen Blöcken sind 0 "
                   f"({v['zero_ratio'] * 100:.1f} %)") if v else ""
            text = (f"Fertig in {ops.fmt_duration(res['seconds'])}: {res['mode']}.\n"
                    f"Neues Laufwerk: {res.get('letter') or '-'} ({res['filesystem']}, „{res['label']}“){ver}")
            self.lbl_prog.configure(text="Fertig.")
            self._log(text)
            if not getattr(self, "quiet", False):
                messagebox.showinfo("DiskFormatter", text, parent=self)
        else:
            self.lbl_prog.configure(text="Fehler / abgebrochen.")
            self._log("FEHLER: " + str(err))
            if not getattr(self, "quiet", False):
                messagebox.showerror("DiskFormatter", str(err), parent=self)
        self.refresh()


def main():
    App().mainloop()
