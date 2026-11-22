#!/usr/bin/env python3
"""API Explorer - interface gráfica (Tkinter) para testar APIs hidrometeorológicas.

Abas: Defesa Civil RS, Open-Meteo, OpenZenith, ANA HidroWebService, REINDESC.
Execute:  python app.py
"""
from __future__ import annotations

import csv
import dataclasses
import json
import math
import queue
import sys
import threading
from urllib.parse import urlsplit

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover
    sys.exit("Tkinter não encontrado. No Linux instale com:  sudo apt install python3-tk")

import api_core as core
from api_core import HttpResult, PreparedRequest, RequestSpec

MAX_ROWS = 5000
JSON_DISPLAY_LIMIT = 1_500_000


# ==========================================================================
# Formulário dinâmico
# ==========================================================================
class FormView(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.columnconfigure(1, weight=1)
        self._getters: dict = {}
        self._rows: dict = {}

    def render(self, fields: list) -> None:
        for w in self.winfo_children():
            w.destroy()
        self._getters = {}
        self._rows = {}
        for r, f in enumerate(fields):
            label = f.label + (" *" if f.required else "")
            heading = ttk.Label(self, text=label)
            heading.grid(row=r, column=0, sticky="nw", padx=(0, 8), pady=2)
            if f.kind == "text":
                w = tk.Text(self, height=4, width=60, wrap="word")
                w.insert("1.0", f.initial())
                getter = (lambda w=w: w.get("1.0", "end-1c"))
            else:
                var = tk.StringVar(value=f.initial())
                if f.kind == "combo":
                    w = ttk.Combobox(self, textvariable=var, values=list(f.choices))
                else:
                    w = ttk.Entry(self, textvariable=var)
                getter = var.get
            w.grid(row=r, column=1, sticky="ew", pady=2)
            widgets = [heading, w]
            if f.help:
                help_label = ttk.Label(self, text=f.help, foreground="#666666", wraplength=360, justify="left")
                help_label.grid(row=r, column=2, sticky="w", padx=8)
                widgets.append(help_label)
            self._rows[f.key] = widgets
            self._getters[f.key] = getter

    def show_fields(self, keys: tuple[str, ...] | None) -> None:
        for key, widgets in self._rows.items():
            for widget in widgets:
                if keys is None or key in keys:
                    widget.grid()
                else:
                    widget.grid_remove()

    def values(self) -> dict:
        return {k: g() for k, g in self._getters.items()}


# ==========================================================================
# Painel de resultados
# ==========================================================================
def _sort_key(v):
    try:
        return (0, float(v))
    except (TypeError, ValueError):
        return (1, str(v).lower())


class ResultPanel(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.tables: dict = {}
        self.cols: list = []
        self.rows: list = []
        self.view: list = []
        self.result: HttpResult | None = None
        self._sort_desc: dict = {}
        self.page = 0
        self.paginate = False

        top = ttk.Frame(self)
        top.pack(fill="x")
        self.status = ttk.Label(top, text="Nenhuma requisição executada ainda.")
        self.status.pack(side="left")

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=4)
        ttk.Label(bar, text="Tabela:").pack(side="left")
        self.table_var = tk.StringVar()
        self.table_cb = ttk.Combobox(bar, textvariable=self.table_var, state="readonly", width=38)
        self.table_cb.pack(side="left", padx=(4, 12))
        self.table_cb.bind("<<ComboboxSelected>>", lambda e: self.select_table(self.table_var.get()))
        ttk.Label(bar, text="Filtrar:").pack(side="left")
        self.filter_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.filter_var, width=24).pack(side="left", padx=4)
        self.filter_var.trace_add("write", lambda *a: self.apply_filter())
        self.count_lbl = ttk.Label(bar, text="")
        self.count_lbl.pack(side="left", padx=8)
        ttk.Button(bar, text="Copiar JSON", command=self.copy_json).pack(side="right", padx=2)
        ttk.Button(bar, text="Exportar JSON", command=self.export_json).pack(side="right", padx=2)
        ttk.Button(bar, text="Exportar CSV", command=self.export_csv).pack(side="right", padx=2)

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)

        f_table = ttk.Frame(self.nb)
        self.page_controls = ttk.Frame(f_table)
        self.prev_page_btn = ttk.Button(self.page_controls, text="Anterior", command=lambda: self.change_page(-1))
        self.prev_page_btn.pack(side="left")
        self.page_lbl = ttk.Label(self.page_controls)
        self.page_lbl.pack(side="left", padx=8)
        self.next_page_btn = ttk.Button(self.page_controls, text="Próxima", command=lambda: self.change_page(1))
        self.next_page_btn.pack(side="left")
        self.tree = ttk.Treeview(f_table, show="headings")
        vsb = ttk.Scrollbar(f_table, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(f_table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        f_table.rowconfigure(0, weight=1)
        f_table.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self.on_row_double_click)
        self.nb.add(f_table, text="Tabela")

        self.json_text = self._make_text_tab("JSON / resposta bruta")
        self.req_text = self._make_text_tab("Requisição e headers")

    def _make_text_tab(self, title: str):
        f = ttk.Frame(self.nb)
        t = tk.Text(f, wrap="none", height=10)
        vsb = ttk.Scrollbar(f, orient="vertical", command=t.yview)
        hsb = ttk.Scrollbar(f, orient="horizontal", command=t.xview)
        t.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set, state="disabled")
        t.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        f.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)
        self.nb.add(f, text=title)
        return t

    @staticmethod
    def _set_text(widget, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    # ---- exibição -------------------------------------------------------
    def set_busy(self) -> None:
        self.status.configure(text="Executando...", foreground="#444444")

    def show_archive_progress(self, completed: int, total: int) -> None:
        self.status.configure(text=f"Baixando histórico do Open-Meteo · {completed}/{total} blocos concluídos")

    def show_request_text(self, text: str) -> None:
        self._set_text(self.req_text, text)
        self.nb.select(2)

    def show(self, res: HttpResult, curl_text: str, extra: dict | None = None, *, paginate: bool = False) -> None:
        self.result = res
        self.paginate = paginate
        self.status.configure(text=res.summary(), foreground=("#b00020" if (res.error or not res.ok) else "#1b6e1b"))

        hdrs = "\n".join(
            f"{k}: {'***' if k.lower() in core.SECRET_HEADERS else core.mask_query_secrets(v)}"
            for k, v in res.resp_headers.items()
        )
        final_url = core.mask_query_secrets(res.final_url)
        self._set_text(self.req_text, f"{curl_text}\n\n--- URL final ---\n{final_url}\n\n--- Headers da resposta ---\n{hdrs}")

        if res.data is not None:
            body = json.dumps(res.data, ensure_ascii=False, indent=2)
        else:
            body = res.text or res.error
        if len(body) > JSON_DISPLAY_LIMIT:
            body = body[:JSON_DISPLAY_LIMIT] + "\n... (truncado na exibição; use 'Exportar JSON' para o conteúdo completo)"
        self._set_text(self.json_text, body)

        self.tables = dict(extra or {})
        if res.data is not None and not res.tables:
            for k, v in core.extract_tables(res.data).items():
                self.tables.setdefault(k, v)
        names = list(self.tables)
        self.table_cb.configure(values=names)
        if names:
            self.table_var.set(names[0])
            self.select_table(names[0])
            self.nb.select(0)
        else:
            self.table_var.set("")
            self.cols, self.rows = [], []
            self.filter_var.set("")
            self.nb.select(1)

    def select_table(self, name: str) -> None:
        self.cols, self.rows = self.tables.get(name, ([], []))
        self.filter_var.set("")

    def apply_filter(self) -> None:
        self.page = 0
        q = self.filter_var.get().strip().lower()
        if q:
            self.view = [r for r in self.rows if any(q in str(c).lower() for c in r)]
        else:
            self.view = list(self.rows)
        self._fill_tree()

    def change_page(self, step: int) -> None:
        page = self.page + step
        if 0 <= page * MAX_ROWS < len(self.view):
            self.page = page
            self._fill_tree()

    def _fill_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        ids = [str(i) for i in range(len(self.cols))]
        self.tree.configure(columns=ids)
        sample = self.view[:200]
        for i, c in enumerate(self.cols):
            width = max([len(str(c)) * 8 + 24] + [len(str(r[i])) * 7 + 16 for r in sample if i < len(r)])
            self.tree.heading(str(i), text=c, command=lambda i=i: self.sort_by(i))
            self.tree.column(str(i), width=min(width, 340), minwidth=50, stretch=False, anchor="w")
        offset = self.page * MAX_ROWS if self.paginate else 0
        for r in self.view[offset:offset + MAX_ROWS]:
            self.tree.insert("", "end", values=[("" if c is None else c) for c in r])
        if self.paginate and len(self.view) > MAX_ROWS:
            pages = (len(self.view) + MAX_ROWS - 1) // MAX_ROWS
            self.page_controls.grid(row=2, column=0, sticky="w", pady=(4, 0))
            self.page_lbl.configure(text=f"Página {self.page + 1} de {pages}")
            self.prev_page_btn.configure(state="normal" if self.page else "disabled")
            self.next_page_btn.configure(state="normal" if self.page + 1 < pages else "disabled")
            extra = ""
        else:
            self.page_controls.grid_remove()
            extra = f" (exibindo {MAX_ROWS})" if len(self.view) > MAX_ROWS else ""
        self.count_lbl.configure(text=f"{len(self.view)} de {len(self.rows)} linhas{extra}" if self.cols else "")

    def sort_by(self, idx: int) -> None:
        desc = not self._sort_desc.get(idx, True)
        self._sort_desc = {idx: desc}
        self.view.sort(key=lambda r: _sort_key(r[idx] if idx < len(r) else ""), reverse=desc)
        self.page = 0
        self._fill_tree()

    def on_row_double_click(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        win = tk.Toplevel(self)
        win.title("Detalhe da linha")
        win.geometry("560x520")
        tv = ttk.Treeview(win, columns=("campo", "valor"), show="headings")
        tv.heading("campo", text="campo")
        tv.heading("valor", text="valor")
        tv.column("campo", width=220)
        tv.column("valor", width=300)
        for c, v in zip(self.cols, vals):
            tv.insert("", "end", values=(c, v))
        tv.pack(fill="both", expand=True)

    # ---- exportação -----------------------------------------------------
    def export_csv(self) -> None:
        if not self.cols:
            messagebox.showinfo("Exportar CSV", "Não há tabela para exportar.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(self.cols)
            w.writerows(self.view)
        messagebox.showinfo("Exportar CSV", f"{len(self.view)} linhas salvas em:\n{path}")

    def _raw_json(self) -> str:
        if self.result is None:
            return ""
        if self.result.data is not None:
            return json.dumps(self.result.data, ensure_ascii=False, indent=2)
        return self.result.text

    def export_json(self) -> None:
        raw = self._raw_json()
        if not raw:
            messagebox.showinfo("Exportar JSON", "Não há resposta para exportar.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json"), ("Texto", "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(raw)

    def copy_json(self) -> None:
        raw = self._raw_json()
        if raw:
            self.clipboard_clear()
            self.clipboard_append(raw)


# ==========================================================================
# Aba base
# ==========================================================================
class ApiTab(ttk.Frame):
    title = "API"

    def __init__(self, master, app: "App"):
        super().__init__(master, padding=8)
        self.app = app
        self.specs: list = []

        self.top = ttk.Frame(self)
        self.top.pack(fill="x")
        self.build_top(self.top)

        self.quick_bar = ttk.LabelFrame(self, text="Consultas prontas", padding=(6, 4))
        self.quick_bar.pack(fill="x", pady=(8, 0))

        self.routes_frame = ttk.Frame(self)
        ttk.Label(self.routes_frame, text="Rotas carregadas").pack(side="left")
        self.spec_var = tk.StringVar()
        self.routes_cb = ttk.Combobox(self.routes_frame, state="readonly")
        self.routes_cb.pack(side="left", fill="x", expand=True, padx=6)
        self.routes_cb.bind("<<ComboboxSelected>>", self.select_loaded_route)

        self.desc = ttk.Label(self, wraplength=1050, foreground="#444444", justify="left")
        self.desc.pack(fill="x", pady=(4, 0))

        self.advanced_var = tk.BooleanVar(value=False)
        self.advanced_cb = ttk.Checkbutton(self, text="Mostrar opções avançadas", variable=self.advanced_var,
                                           command=self.update_visible_fields)
        self.form = FormView(self)
        self.form.pack(fill="x", pady=6)

        btns = ttk.Frame(self)
        btns.pack(fill="x")
        self.run_btn = ttk.Button(btns, text="Executar", command=self.on_run)
        self.run_btn.pack(side="left")
        ttk.Button(btns, text="Ver requisição (curl)", command=self.on_preview).pack(side="left", padx=6)

        self.panel = ResultPanel(self)
        self.panel.pack(fill="both", expand=True, pady=(8, 0))

        self.set_specs(self.make_specs())

    # ---- ganchos --------------------------------------------------------
    def build_top(self, parent) -> None:
        pass

    def make_specs(self) -> list:
        return []

    def snapshot(self) -> dict:
        return self.app.settings()

    def execute(self, prep: PreparedRequest, snap: dict) -> HttpResult:
        http = dict(verify=snap["verify"], timeout=snap["timeout"], user_agent=snap["ua"])
        if prep.archive_batch:
            return core.fetch_open_meteo_archive(prep, progress=snap.get("archive_progress"), **http)
        return core.do_request(prep, **http)

    def on_new_token(self, token: str) -> None:
        pass

    # ---- especificações -------------------------------------------------
    def set_specs(self, specs: list, select: int = 0) -> None:
        self.specs = specs
        for widget in self.quick_bar.winfo_children():
            widget.destroy()
        buttons = [s for s in specs if s.button]
        for column, spec in enumerate(buttons):
            ttk.Radiobutton(self.quick_bar, text=spec.button, style="Toolbutton",
                            variable=self.spec_var, value=spec.name, command=self.render_selected
                            ).grid(row=column // 4, column=column % 4, sticky="ew", padx=3, pady=3)
        routes = [s.name for s in specs if not s.button]
        self.routes_cb.configure(values=routes)
        self.routes_frame.pack_forget()
        if routes:
            self.routes_frame.pack(before=self.desc, fill="x", pady=(6, 0))
        if specs:
            self.select_spec(min(select, len(specs) - 1))

    def select_spec(self, index: int) -> None:
        self.spec_var.set(self.specs[index].name)
        self.render_selected()

    def select_loaded_route(self, _event=None) -> None:
        self.spec_var.set(self.routes_cb.get())
        self.render_selected()

    def current_spec(self) -> RequestSpec:
        name = self.spec_var.get()
        return next(spec for spec in self.specs if spec.name == name)

    def render_selected(self) -> None:
        spec = self.current_spec()
        self.routes_cb.set("" if spec.button else spec.name)
        self.desc.configure(text=spec.description)
        self.advanced_var.set(False)
        self.advanced_cb.pack_forget()
        if spec.quick_fields is not None and len(spec.quick_fields) < len(spec.fields):
            self.advanced_cb.pack(before=self.form, anchor="w", pady=(4, 0))
        self.form.render(spec.fields)
        self.update_visible_fields()

    def update_visible_fields(self) -> None:
        quick_fields = self.current_spec().quick_fields
        self.form.show_fields(None if self.advanced_var.get() else quick_fields)

    def _build(self) -> PreparedRequest | None:
        spec = self.current_spec()
        values = self.form.values()
        for f in spec.fields:
            if f.required and not values.get(f.key, "").strip():
                messagebox.showerror("Campo obrigatório", f"Preencha o campo: {f.label}")
                return None
        try:
            return spec.build(values)
        except ValueError as e:
            messagebox.showerror("Campo inválido", str(e))
            return None

    # ---- ações ----------------------------------------------------------
    def on_preview(self) -> None:
        prep = self._build()
        if prep:
            self.panel.show_request_text(core.build_request_preview(prep, self.app.settings()["ua"]))

    def on_run(self) -> None:
        prep = self._build()
        if prep:
            self.start(prep)

    def start(self, prep: PreparedRequest) -> None:
        snap = self.snapshot()
        self.run_btn.configure(state="disabled")
        self.panel.set_busy()
        q: queue.Queue = queue.Queue()
        if prep.archive_batch:
            snap["archive_progress"] = lambda done, total: q.put((done, total))

        def work():
            try:
                res = self.execute(prep, snap)
            except Exception as e:  # noqa: BLE001
                res = HttpResult(error=f"{type(e).__name__}: {e}")
            q.put(res)

        threading.Thread(target=work, daemon=True).start()
        self.after(100, lambda: self._poll(q, prep, snap))

    def _poll(self, q: queue.Queue, prep: PreparedRequest, snap: dict) -> None:
        try:
            res = q.get_nowait()
            while isinstance(res, tuple):
                self.panel.show_archive_progress(*res)
                res = q.get_nowait()
        except queue.Empty:
            self.after(100, lambda: self._poll(q, prep, snap))
            return
        self.run_btn.configure(state="normal")
        if res.new_token:
            self.on_new_token(res.new_token)
        extra = res.tables
        if not extra and prep.postprocess and res.data is not None:
            try:
                extra = prep.postprocess(res.data) or {}
            except Exception:  # noqa: BLE001
                extra = {}
        self.panel.show(res, core.build_request_preview(prep, snap["ua"]), extra, paginate=prep.archive_batch)
        if prep.on_done:
            try:
                prep.on_done(res)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("Erro", str(e))


# ==========================================================================
# Abas
# ==========================================================================
class DefesaCivilTab(ApiTab):
    title = "Defesa Civil RS"
    boundaries = ()

    def build_top(self, parent):
        box = ttk.LabelFrame(parent, text="Defesa Civil RS - Rede Hidrometeorológica (GraphQL, sem cadastro)")
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="Endpoint:").grid(row=0, column=0, padx=4, pady=2, sticky="w")
        self.endpoint_var = tk.StringVar(value=core.DCRS_ENDPOINT)
        ttk.Entry(box, textvariable=self.endpoint_var).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(box, foreground="#666666", wraplength=1000, justify="left",
                  text="A API só aceita as consultas fixas do guia oficial. Limite: 120 requisições/min por IP; "
                       "os dados mudam a cada 5 min. 'historic' cobre no máximo os últimos 3 meses."
                  ).grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=2)

    def make_specs(self):
        return core.make_dcrs_specs(lambda: self.endpoint_var.get().strip())

    def execute(self, prep: PreparedRequest, snap: dict) -> HttpResult:
        result = super().execute(prep, snap)
        if not result.ok or prep.postprocess is not core.dcrs_station_table:
            return result
        stations = core.dig(result.data, "data.tags_data.qualle_meteorologia")
        if not isinstance(stations, list):
            return dataclasses.replace(result, error="A resposta não contém a lista de estações.")
        if stations and not self.boundaries:
            try:
                self.boundaries = core.ibge_municipal_boundaries(verify=snap["verify"], timeout=min(15, snap["timeout"]),
                                                                user_agent=snap["ua"])
            except (KeyError, TypeError, ValueError):
                self.boundaries = ()
        result.tables = core.dcrs_station_table(result.data, self.boundaries)
        rows = result.tables["Cadastro de estações"][1]
        missing = sum(not row[2] for row in rows)
        result.note = f"{len(rows)} estações" + (f" · {missing} sem município identificado" if missing else "")
        return result


class OpenMeteoTab(ApiTab):
    title = "Open-Meteo"

    def build_top(self, parent):
        box = ttk.LabelFrame(parent, text="Open-Meteo (sem cadastro para uso não comercial)")
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="API key (opcional):").grid(row=0, column=0, padx=4, pady=2, sticky="w")
        self.key_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.key_var, show="*").grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(box, foreground="#666666", wraplength=1000, justify="left",
                  text="Só para assinatura comercial: com chave, as chamadas vão para os hosts customer-*. "
                       "Uso gratuito (não comercial): 600/min, 5.000/h e menos de 10.000 chamadas/dia. Dados CC BY 4.0 - cite 'Weather data by Open-Meteo.com'."
                  ).grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=2)

    def make_specs(self):
        return core.make_open_meteo_specs(lambda: self.key_var.get())


class OpenZenithTab(ApiTab):
    title = "OpenZenith"

    def build_top(self, parent):
        box = ttk.LabelFrame(parent, text="OpenZenith (sem cadastro)")
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="URL base da API:").grid(row=0, column=0, padx=4, pady=2, sticky="w")
        self.base_var = tk.StringVar(value=core.OZ_BASES[0])
        ttk.Combobox(box, textvariable=self.base_var, values=list(core.OZ_BASES)).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(box, foreground="#666666", wraplength=1000, justify="left",
                  text="Serviço pequeno, hospedado em plano gratuito. Use 'Carregar rotas' para listar os endpoints "
                       "publicados no OpenAPI."
                  ).grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=2)

    def make_specs(self):
        return core.make_openzenith_specs(lambda: self.base_var.get(), self.install_openapi)

    def install_openapi(self, spec: dict) -> None:
        dyn = core.specs_from_openapi(spec, lambda: self.base_var.get())
        base = self.make_specs()
        self.set_specs(base + dyn, select=2)
        messagebox.showinfo("OpenAPI", f"{len(dyn)} rotas adicionadas à lista de requisições.")


class AnaTab(ApiTab):
    title = "ANA HidroWebService"

    def build_top(self, parent):
        box = ttk.LabelFrame(parent, text="ANA HidroWebService - autenticação (identificador e senha recebidos da ANA)")
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)
        box.columnconfigure(3, weight=1)
        self.base_var = tk.StringVar(value=core.ANA_BASE)
        self.auth_path_var = tk.StringVar(value=core.ANA_AUTH_PATH)
        self.id_var = tk.StringVar()
        self.pw_var = tk.StringVar()
        self.token_var = tk.StringVar()
        self.show_pw = tk.BooleanVar(value=False)

        ttk.Label(box, text="URL base:").grid(row=0, column=0, padx=4, pady=2, sticky="w")
        ttk.Entry(box, textvariable=self.base_var).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(box, text="Rota de autenticação:").grid(row=0, column=2, padx=4, sticky="w")
        ttk.Entry(box, textvariable=self.auth_path_var).grid(row=0, column=3, sticky="ew", padx=4)

        ttk.Label(box, text="Identificador:").grid(row=1, column=0, padx=4, pady=2, sticky="w")
        ttk.Entry(box, textvariable=self.id_var).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Label(box, text="Senha:").grid(row=1, column=2, padx=4, sticky="w")
        self.pw_entry = ttk.Entry(box, textvariable=self.pw_var, show="*")
        self.pw_entry.grid(row=1, column=3, sticky="ew", padx=4)
        ttk.Checkbutton(box, text="mostrar", variable=self.show_pw, command=self._toggle_pw).grid(row=1, column=4, padx=4)

        ttk.Label(box, text="Token:").grid(row=2, column=0, padx=4, pady=2, sticky="w")
        ttk.Entry(box, textvariable=self.token_var, show="*").grid(row=2, column=1, columnspan=3, sticky="ew", padx=4)
        bf = ttk.Frame(box)
        bf.grid(row=2, column=4, padx=4)
        ttk.Button(bf, text="Obter token", command=self.get_token).pack(side="left")
        ttk.Button(bf, text="Limpar", command=lambda: self.token_var.set("")).pack(side="left", padx=2)
        ttk.Label(box, foreground="#666666", wraplength=1000, justify="left",
                  text="Credenciais ficam só na memória (não são gravadas). Se o token estiver vazio, ele é obtido "
                       "automaticamente ao executar; em caso de 401/403 é renovado uma vez. Você também pode colar um token."
                  ).grid(row=3, column=0, columnspan=5, sticky="w", padx=4, pady=2)

    def _toggle_pw(self):
        self.pw_entry.configure(show="" if self.show_pw.get() else "*")

    def make_specs(self):
        return core.make_ana_specs(
            lambda: self.base_var.get(), self.install_openapi,
            lambda: (self.base_var.get(), self.auth_path_var.get(), self.id_var.get().strip(), self.pw_var.get()))

    def install_openapi(self, spec: dict) -> None:
        dyn = core.specs_from_openapi(spec, lambda: self.base_var.get())
        self.set_specs(self.make_specs() + dyn, select=1)
        messagebox.showinfo("OpenAPI", f"{len(dyn)} rotas adicionadas à lista de requisições.")

    def snapshot(self) -> dict:
        s = super().snapshot()
        s.update(base=self.base_var.get(), auth_path=self.auth_path_var.get(),
                 id=self.id_var.get().strip(), pw=self.pw_var.get(), token=self.token_var.get().strip())
        return s

    def on_new_token(self, token: str) -> None:
        self.token_var.set(token)

    def get_token(self) -> None:
        if not self.id_var.get().strip() or not self.pw_var.get():
            messagebox.showerror("Autenticação", "Preencha Identificador e Senha.")
            return
        prep = core.ana_auth_request(self.base_var.get(), self.auth_path_var.get(),
                                     self.id_var.get().strip(), self.pw_var.get())
        self.start(prep)

    def _fetch_token(self, snap: dict) -> HttpResult:
        kw = dict(verify=snap["verify"], timeout=snap["timeout"], user_agent=snap["ua"])
        prep = core.ana_auth_request(snap["base"], snap["auth_path"], snap["id"], snap["pw"], "GET")
        res = core.do_request(prep, **kw)
        if res.status == 405:
            res = core.do_request(dataclasses.replace(prep, method="POST"), **kw)
        res.new_token = core.find_token(res.data) if res.ok else None
        if res.new_token:
            res.data = core.redact_ana_tokens(res.data)
            res.text = json.dumps(res.data, ensure_ascii=False)
        if res.ok and not res.new_token:
            res.error = "Token não encontrado na resposta da ANA."
        return res

    def execute(self, prep: PreparedRequest, snap: dict) -> HttpResult:
        kw = dict(verify=snap["verify"], timeout=snap["timeout"], user_agent=snap["ua"])
        if prep.is_auth:
            return self._fetch_token(snap)
        target, base = urlsplit(prep.url), urlsplit(snap["base"])
        if (prep.is_openapi or (target.scheme, target.netloc.lower()) != (base.scheme, base.netloc.lower())
                or any(k.lower() == "authorization" for k in prep.headers)):
            return core.do_request(prep, **kw)
        have_creds = bool(snap["id"] and snap["pw"])
        token, new = snap["token"], None
        if not token and have_creds:
            r = self._fetch_token(snap)
            if not r.new_token:
                return r
            token = new = r.new_token

        def go(tok: str) -> HttpResult:
            p = prep
            if tok:
                p = dataclasses.replace(prep, headers={**prep.headers, "Authorization": f"Bearer {tok}"})
            return core.do_request(p, **kw)

        res = go(token)
        if res.status in (401, 403) and have_creds and new is None:
            r = self._fetch_token(snap)
            if r.new_token:
                token = new = r.new_token
                res = go(token)
        res.new_token = new
        return res


class ReindescTab(ApiTab):
    title = "REINDESC (Cemaden)"

    def build_top(self, parent):
        box = ttk.LabelFrame(parent, text="REINDESC - Cemaden")
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="URL base:").grid(row=0, column=0, padx=4, pady=2, sticky="w")
        self.base_var = tk.StringVar(value=core.REINDESC_BASE)
        ttk.Entry(box, textvariable=self.base_var).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(box, foreground="#666666", wraplength=1000, justify="left",
                  text="O REINDESC registra deslizamentos, inundações, enxurradas e alagamentos. "
                       "Em Consulta manual, preencha os campos de acordo com uma rota que você já conheça."
                  ).grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=2)

    def make_specs(self):
        return core.make_reindesc_specs(lambda: self.base_var.get())


# ==========================================================================
# Janela principal
# ==========================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("API Explorer - dados hidrometeorológicos")
        self.geometry("1280x860")
        self.minsize(900, 600)

        self.verify_var = tk.BooleanVar(value=True)
        self.timeout_var = tk.StringVar(value="60")
        self.ua_var = tk.StringVar(value="ApiExplorer/1.0")

        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Checkbutton(bar, text="Verificar SSL", variable=self.verify_var).pack(side="left")
        ttk.Label(bar, text="Timeout (s):").pack(side="left", padx=(16, 4))
        ttk.Entry(bar, textvariable=self.timeout_var, width=5).pack(side="left")
        ttk.Label(bar, text="User-Agent:").pack(side="left", padx=(16, 4))
        ttk.Entry(bar, textvariable=self.ua_var, width=40).pack(side="left")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        for cls in (DefesaCivilTab, OpenMeteoTab, OpenZenithTab, AnaTab, ReindescTab):
            nb.add(cls(nb, self), text=cls.title)

    def settings(self) -> dict:
        try:
            timeout = float(self.timeout_var.get())
        except ValueError:
            timeout = 60.0
        if not math.isfinite(timeout) or timeout <= 0:
            timeout = 60.0
        return {"verify": bool(self.verify_var.get()), "timeout": timeout,
                "ua": self.ua_var.get().strip() or "ApiExplorer/1.0"}


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
