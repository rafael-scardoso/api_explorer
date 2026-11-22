"""Teste de fumaça: tkinter simulado + servidor HTTP local. Não faz chamadas externas."""
import json, sys, threading, time, types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------- tkinter falso ----------------
class W:
    def __init__(self, *a, **k): self.kw = dict(k); self.children = []; self.text = ""
    def __getattr__(self, n):
        if n.startswith("__"): raise AttributeError(n)
        return lambda *a, **k: None
    def winfo_children(self): return []
    def configure(self, **k): self.kw.update(k)
    config = configure
    def __setitem__(self, k, v): self.kw[k] = v
    def after(self, ms, fn): time.sleep(ms / 1000); fn()
class Var:
    def __init__(self, value="", **k): self.v = value
    def get(self): return self.v
    def set(self, v): self.v = v
    def trace_add(self, *a): pass
class Text(W):
    def __init__(self, *a, **k): super().__init__(); self.text = ""
    def insert(self, idx, s): self.text += s
    def delete(self, a, b): self.text = ""
    def get(self, a, b): return self.text
class Combo(W):
    def __init__(self, *a, **k):
        super().__init__(*a, **k); self.var = k.get("textvariable"); self.values = list(k.get("values", []))
    def configure(self, **k):
        if "values" in k: self.values = list(k["values"])
    def current(self, i=None):
        if i is None:
            v = self.var.get(); return self.values.index(v) if v in self.values else -1
        self.var.set(self.values[i])
class Tree(W):
    def __init__(self, *a, **k): super().__init__(); self.items = []
    def delete(self, *ids): self.items = []
    def get_children(self): return list(range(len(self.items)))
    def insert(self, p, i, values=()): self.items.append(values)
class Tk(W):
    def mainloop(self): pass
tk = types.ModuleType("tkinter"); ttk = types.ModuleType("tkinter.ttk")
mb = types.ModuleType("tkinter.messagebox"); fd = types.ModuleType("tkinter.filedialog")
MSGS = []
for n in ("Toplevel",): setattr(tk, n, W)
tk.Tk = Tk; tk.Text = Text; tk.StringVar = Var; tk.BooleanVar = Var
for n in ("Frame","Label","Entry","Button","Checkbutton","Radiobutton","LabelFrame","Notebook","Scrollbar"): setattr(ttk, n, W)
ttk.Combobox = Combo; ttk.Treeview = Tree
mb.showerror = lambda t, m: MSGS.append(("erro", m)); mb.showinfo = lambda t, m: MSGS.append(("info", m))
fd.asksaveasfilename = lambda **k: ""
tk.ttk = ttk; tk.messagebox = mb; tk.filedialog = fd
sys.modules.update({"tkinter": tk, "tkinter.ttk": ttk, "tkinter.messagebox": mb, "tkinter.filedialog": fd})

import api_core as core
import app as appmod

# ---------------- servidor local ----------------
STATION = {"codigo": "DCRS-00001", "name": {"prefix": "UHE", "general": "Canoas", "local": "Rio dos Sinos"},
  "timestamp": "2026-11-21T10:00:00.000", "position": {"bacia": "Guaíba", "latitude": -29.9, "longitude": -51.1, "regiao": "Metropolitana", "altitude": 5},
  "data": {"rio": {"rio_nome": {"value": "Sinos"}, "rio_nivel": {"value": 1.23}, "rio_nivel_tendencia": {"value": 0.1},
     "rio_alarmes": {"inundacao": {"ativo": {"value": True}, "status": {"value": 2}}}},
   "chuva": {"acumulado": {"h001": {"value": 0.4}, "h024": {"value": 12.0}}}, "temperatura": {"atual": {"value": 21.5}},
   "vento": {"velocidade_media": {"value": 3.2}}}, "resource": {"estacao": {"estado": {"manutencao": {"value": False}}}}}
SPEC = {"openapi": "3.0.0", "paths": {"/api/elevation": {"get": {"summary": "Elevation", "tags": ["core"],
        "parameters": [{"name": "lat", "in": "query", "required": True, "schema": {"type": "number"}},
                       {"name": "lon", "in": "query", "required": True, "schema": {"type": "number"}}]}},
        "/api/items/{id}": {"post": {"summary": "x", "parameters": [{"name": "id", "in": "path", "required": True}], "requestBody": {}}}}}
LOG = []
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        LOG.append(("POST", self.path, body))
        if self.path == "/graphql":
            if body["query"] == core.TAGS_QUERY:
                return self._send(200, {"data": {"tags_data": {"qualle_meteorologia": [STATION, dict(STATION, codigo="DCRS-00002")], "proximaAtualizacaoEm": "x"}}})
            return self._send(200, {"data": {"historic": {"items": [{"ts": "2026-11-21T09:00:00.000", "codigo": body["variables"]["stationCode"], "rio_nivel": 27.2, "chuva_mm": 0}], "totalCount": 1}}})
        self._send(404, {})
    def do_GET(self):
        LOG.append(("GET", self.path, dict(self.headers)))
        if self.path.startswith("/ana/EstacoesTelemetricas/OAUth/v1"):
            if self.headers.get("Identificador") == "123" and self.headers.get("Senha") == "pw":
                return self._send(200, {"status": "OK", "items": {"tokenautenticacao": "TOK123"}})
            return self._send(401, {"message": "bad"})
        if self.path.startswith("/ana/EstacoesTelemetricas/HidroInventarioEstacoes"):
            if self.headers.get("Authorization") != "Bearer TOK123": return self._send(401, {})
            return self._send(200, {"items": [{"codigoestacao": "1", "Tipo_Estacao": "Fluviometrica"}]})
        if self.path.startswith("/ana" + core.ANA_OPENAPI_PATH): return self._send(200, SPEC)
        if self.path.startswith("/forecast"):
            return self._send(200, {"latitude": 1, "hourly": {"time": ["a", "b"], "temperature_2m": [1.0, None]}, "daily": {"time": ["d"], "x": [3]}, "current": {"temperature_2m": 9}})
        self._send(404, {})
srv = HTTPServer(("127.0.0.1", 0), H); PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

def ok(cond, msg):
    print(("OK   " if cond else "FALHA"), msg)
    if not cond: sys.exit(1)

# ---------------- testes ----------------
a = appmod.App()
tabs = {}
for cls in (appmod.DefesaCivilTab, appmod.OpenMeteoTab, appmod.OpenZenithTab, appmod.AnaTab, appmod.ReindescTab):
    tabs[cls.title] = cls(None, a)
    ok(len(tabs[cls.title].specs) >= 1, f"aba '{cls.title}' criada com {len(tabs[cls.title].specs)} requisições")

# Todas as requisições de todas as abas constroem com os valores padrão
for title, t in tabs.items():
    for i, s in enumerate(t.specs):
        t.select_spec(i)
        vals = t.form.values()
        if title == "ANA HidroWebService" and s.name.startswith("Série"):
            vals["CodigoDaEstacao"] = "15400000"
        if title == "ANA HidroWebService" and s.name.startswith("Autentica"):
            t.id_var.set("123"); t.pw_var.set("pw")
        prep = s.build(vals)
        ok(prep.url.startswith("http"), f"build OK: {title} / {s.name} -> {prep.method} {prep.url[:70]}")
        core.build_curl(prep, "UA")

# Defesa Civil: tags_data + historic contra o servidor local
d = tabs["Defesa Civil RS"]; d.endpoint_var.set(BASE + "/graphql")
d.select_spec(0); d.on_run()
ok(d.panel.result.ok, "DCRS tags_data executou: " + d.panel.result.summary())
ok(list(d.panel.tables)[0] == "Resumo das estações", "tabela de resumo vem primeiro")
cols, rows = d.panel.tables["Resumo das estações"]
ok(len(rows) == 2 and rows[0][cols.index("nivel_rio_m")] == 1.23 and rows[0][cols.index("situacao_rio")] == "Alerta", "resumo: nível 1.23 e situação 'Alerta'")
ok(LOG[-1][2]["query"] == core.TAGS_QUERY, "consulta enviada é exatamente TAGS_QUERY")
d.panel.filter_var.set("00002"); d.panel.apply_filter(); ok(len(d.panel.view) == 1, "filtro de tabela funciona")
d.select_spec(1); d.on_run()
ok(d.panel.result.ok and d.panel.tables["data.historic.items"][1][0][1] == "DCRS-00001", "historic: tabela de items")
ok(LOG[-1][2]["variables"]["interval"] == "HOUR_1" and LOG[-1][2]["query"] == core.HISTORIC_QUERY, "variáveis e consulta do historic corretas")
d.form._getters["startDate"] = lambda: "2026/11/21"; d.on_run()
ok(MSGS[-1][0] == "erro" and "startDate" in MSGS[-1][1], "data inválida é recusada com mensagem")

# Open-Meteo: parâmetros do forecast e tabelas paralelas via requisição livre
om = tabs["Open-Meteo"]; om.select_spec(0)
p = om.specs[0].build(om.form.values())
ok(p.params["latitude"] == "-30.0346" and p.params["timezone"] == "America/Sao_Paulo" and "apikey" not in p.params, "forecast: parâmetros padrão, sem apikey")
om.key_var.set("SECRET"); p = om.specs[0].build(om.form.values())
ok(p.url.startswith("https://customer-api.open-meteo.com") and p.params["apikey"] == "SECRET", "com chave: host customer-api + apikey")
ok("SECRET" not in core.build_curl(p, "UA"), "curl mascara a apikey")
om.key_var.set("")
om.select_spec(5)
om.form._getters["url"] = lambda: BASE + "/forecast"; om.on_run()
t = om.panel.tables
ok(list(t)[0] == "hourly" and t["hourly"][1] == [["a", 1.0], ["b", ""]], "tabelas paralelas (hourly) extraídas; null vira vazio")
ok("daily" in t and "(chave/valor)" in t, "daily e chave/valor também disponíveis")
try: om.specs[0].build({**om.form.values(), "latitude": "abc"}); ok(False, "latitude inválida deveria falhar")
except (ValueError, KeyError) as e: ok(True, "latitude inválida é recusada")

# ANA: autenticação automática, Bearer, e renovação
an = tabs["ANA HidroWebService"]; an.base_var.set(BASE + "/ana"); an.id_var.set("123"); an.pw_var.set("pw")
an.select_spec(2)
ok(an.specs[2].name.startswith("Invent") and an.form.values()["UnidadeFederativa"] == "RS", "ANA: inventário com filtro de UF")
an.on_run()
ok(an.panel.result.ok and an.token_var.get() == "TOK123", "ANA: token obtido automaticamente e guardado no campo")
ok(an.panel.result.data["items"][0]["Tipo_Estacao"] == "Fluviometrica", "ANA: consulta autenticada com Bearer funcionou")
an.token_var.set("VENCIDO"); an.on_run()
ok(an.panel.result.ok and an.token_var.get() == "TOK123", "ANA: 401 com token vencido -> renova e repete")
an.token_var.set(""); an.pw_var.set("errada"); an.on_run()
ok(not an.panel.result.ok, "ANA: senha errada mostra erro: " + an.panel.result.summary())
an.pw_var.set("pw"); an.get_token()
ok(an.panel.result.new_token is None or an.token_var.get() == "TOK123", "ANA: botão 'Obter token'")
ok("Senha: ***" in an.panel.req_text.text and "pw" not in an.panel.req_text.text.split("--- Headers")[0], "curl da ANA mascara a senha")

# OpenAPI: carregar rotas e usar
an.select_spec(1); an.on_run()
ok(any(s.name.startswith("[OpenAPI]") for s in an.specs), "ANA: rotas do OpenAPI adicionadas: " + str([s.name for s in an.specs if s.name.startswith("[OpenAPI]")]))
oz = tabs["OpenZenith"]; oz.base_var.set(BASE + "/api")
oz.install_openapi(SPEC)
dyn = [s for s in oz.specs if s.name.startswith("[OpenAPI]")]
oz.select_spec(oz.specs.index(dyn[0]))
oz.form._getters["query:lat"] = lambda: "1"; oz.form._getters["query:lon"] = lambda: "2"
pp = dyn[0].build(oz.form.values())
ok(pp.url == BASE + "/api/elevation" and pp.params == {"lat": "1", "lon": "2"}, "OpenAPI: prefixo /api não duplica: " + pp.url)
pp2 = dyn[1].build({"path:id": "a b", "__body__": '{"x":1}'})
ok(pp2.url.endswith("/items/a%20b") and pp2.json_body == {"x": 1}, "OpenAPI: parâmetro de path e corpo JSON")
ad = [s for s in an.specs if s.name.startswith("Série telemétrica adotada")]
ok(len(ad) == 1, "ANA: rota Adotada presente")
an.select_spec(an.specs.index(ad[0]))
pa = ad[0].build({**an.form.values(), "CodigoDaEstacao": "123"})
ok(pa.url.endswith("/EstacoesTelemetricas/HidroinfoanaSerieTelemetricaAdotada/v1") and pa.params["RangeIntervaloDeBusca"] == "DIAS_30", "ANA: Adotada monta URL e parâmetros do manual")
print("\nTODOS OS TESTES PASSARAM")
