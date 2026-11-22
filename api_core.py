"""api_core.py - lógica do API Explorer (sem dependência de interface gráfica).

Contém:
  * dataclasses de requisição/resposta;
  * execução HTTP (requests) e geração de comando curl;
  * conversão de JSON em tabelas;
  * definição das requisições de cada API (Defesa Civil RS, Open-Meteo,
    OpenZenith, ANA HidroWebService e requisição livre).
"""
from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote, unquote_plus, urlparse

import requests

BRT = timezone(timedelta(hours=-3))  # horário de Brasília
DATE_FMT = "%Y-%m-%d %H:%M:%S"
HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}
SECRET_HEADERS = {"authorization", "proxy-authorization", "senha", "identificador",
                  "accesstoken", "x-api-key", "api-key", "cookie", "set-cookie"}
SECRET_QUERY = {"apikey", "api_key", "api-key", "access_token", "token", "senha"}
SECRET_QUERY_VALUE = re.compile(r"([?&])([^=&\s?#]+)=[^&#\s\"']*")
ANA_TOKEN_KEYS = {"tokenautenticacao", "access_token", "accesstoken", "token"}


# --------------------------------------------------------------------------
# Estruturas
# --------------------------------------------------------------------------
@dataclass
class Field:
    """Campo de formulário de uma requisição."""
    key: str
    label: str
    default: Any = ""
    kind: str = "entry"          # entry | combo | text
    choices: tuple = ()
    help: str = ""
    required: bool = False

    def initial(self) -> str:
        d = self.default() if callable(self.default) else self.default
        return "" if d is None else str(d)


@dataclass
class PreparedRequest:
    method: str
    url: str
    params: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    json_body: Any = None
    data: Optional[str] = None
    postprocess: Optional[Callable[[Any], dict]] = None   # JSON -> tabelas extras
    on_done: Optional[Callable[["HttpResult"], None]] = None
    is_auth: bool = False
    is_openapi: bool = False
    archive_batch: bool = False


@dataclass
class RequestSpec:
    name: str
    description: str
    fields: list
    build: Callable[[dict], PreparedRequest]
    button: str = ""
    quick_fields: tuple[str, ...] | None = None


@dataclass
class HttpResult:
    status: Optional[int] = None
    reason: str = ""
    elapsed: float = 0.0
    size: int = 0
    resp_headers: dict = field(default_factory=dict)
    text: str = ""
    data: Any = None
    error: str = ""
    final_url: str = ""
    new_token: Optional[str] = None
    note: str = ""
    tables: dict = field(default_factory=dict)

    def graphql_errors(self) -> str:
        if isinstance(self.data, dict) and self.data.get("errors"):
            errs = self.data["errors"]
            if isinstance(errs, list):
                msgs = [e.get("message", str(e)) if isinstance(e, dict) else str(e) for e in errs]
            else:
                msgs = [str(errs)]
            return "; ".join(msgs)
        return ""

    def summary(self) -> str:
        if self.error:
            return f"ERRO: {self.error}"
        kb = self.size / 1024
        s = f"HTTP {self.status} {self.reason} · {self.elapsed:.2f} s · {kb:.1f} KB"
        gql = self.graphql_errors()
        if gql:
            s += f" · Erro GraphQL: {gql}"
        if self.note:
            s += f" · {self.note}"
        return s

    @property
    def ok(self) -> bool:
        return (not self.error and self.status is not None and 200 <= self.status < 300
                and not self.graphql_errors())


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
_warned_ssl = False


def mask_query_secrets(text: str) -> str:
    def redact(match: re.Match) -> str:
        separator, name = match.groups()
        if unquote_plus(name).lower() in SECRET_QUERY:
            return f"{separator}{name}=***"
        return match[0]
    return SECRET_QUERY_VALUE.sub(redact, text)


def do_request(prep: PreparedRequest, verify: bool = True, timeout: float = 60.0,
               user_agent: str = "ApiExplorer/1.0") -> HttpResult:
    global _warned_ssl
    if not verify and not _warned_ssl:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _warned_ssl = True

    headers = {"User-Agent": user_agent, "Accept": "application/json, text/plain, */*"}
    headers.update({k: v for k, v in prep.headers.items() if v is not None})
    params = {k: v for k, v in prep.params.items() if v not in (None, "")}
    kwargs: dict = dict(headers=headers, params=params or None, verify=verify, timeout=timeout)
    if any(k.lower() in SECRET_HEADERS - {"authorization"} for k in headers):
        kwargs["allow_redirects"] = False
    if prep.json_body is not None:
        kwargs["json"] = prep.json_body
    elif prep.data is not None:
        kwargs["data"] = prep.data.encode("utf-8")

    t0 = time.perf_counter()
    try:
        r = requests.request(prep.method.upper(), prep.url, **kwargs)
    except requests.exceptions.SSLError as e:
        return HttpResult(error=f"Falha de SSL ({mask_query_secrets(str(e))}).",
                          elapsed=time.perf_counter() - t0)
    except requests.exceptions.Timeout:
        return HttpResult(error=f"Tempo esgotado ({timeout:g} s).", elapsed=time.perf_counter() - t0)
    except requests.RequestException as e:
        return HttpResult(error=f"{type(e).__name__}: {mask_query_secrets(str(e))}",
                          elapsed=time.perf_counter() - t0)

    elapsed = time.perf_counter() - t0
    data = None
    try:
        data = r.json()
    except ValueError:
        pass
    note = ""
    if r.status_code == 429:
        note = "limite de requisições atingido: aguarde cerca de 90 s antes de tentar de novo"
    error = ""
    if prep.is_openapi and 200 <= r.status_code < 300:
        if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
            error = "A resposta não contém uma especificação OpenAPI com rotas."
    return HttpResult(status=r.status_code, reason=r.reason or "", elapsed=elapsed,
                      size=len(r.content), resp_headers=dict(r.headers), text=r.text,
                      data=data, error=error, final_url=mask_query_secrets(r.url or ""), note=note)


def build_curl(prep: PreparedRequest, user_agent: str = "", mask: bool = True) -> str:
    """Equivalente em curl (bash), com segredos mascarados."""
    params = {k: v for k, v in prep.params.items() if v not in (None, "")}
    url = requests.Request(prep.method.upper(), prep.url, params=params or None).prepare().url
    if mask:
        url = mask_query_secrets(url)
    parts = ["curl", "-X", prep.method.upper(), shlex.quote(url)]
    headers = {}
    if user_agent:
        headers["User-Agent"] = user_agent
    headers.update(prep.headers)
    if prep.json_body is not None:
        headers.setdefault("Content-Type", "application/json")
    for k, v in headers.items():
        if mask and k.lower() in SECRET_HEADERS:
            v = "***"
        parts += ["-H", shlex.quote(f"{k}: {v}")]
    if prep.json_body is not None:
        parts += ["-d", shlex.quote(json.dumps(prep.json_body, ensure_ascii=False))]
    elif prep.data is not None:
        parts += ["-d", shlex.quote(prep.data)]
    return " \\\n  ".join(parts)


# --------------------------------------------------------------------------
# JSON -> tabelas
# --------------------------------------------------------------------------
def is_scalar(x: Any) -> bool:
    return x is None or isinstance(x, (str, int, float, bool))


def flatten(obj: Any, prefix: str = "", out: Optional[dict] = None) -> dict:
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, list):
        if all(is_scalar(i) for i in obj):
            out[prefix or "valor"] = ", ".join("" if i is None else str(i) for i in obj)
        else:
            out[prefix or "valor"] = json.dumps(obj, ensure_ascii=False)
    else:
        out[prefix or "valor"] = obj
    return out


def records_to_table(records: list) -> tuple:
    rows = [flatten(r) if isinstance(r, dict) else {"valor": r} for r in records]
    cols: list = []
    seen: set = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    return cols, [["" if r.get(c) is None else r.get(c) for c in cols] for r in rows]


def is_parallel(d: Any) -> bool:
    """dict cujos valores são listas de escalares do mesmo tamanho (Open-Meteo)."""
    if not isinstance(d, dict) or not d:
        return False
    vals = list(d.values())
    if not all(isinstance(v, list) and v and all(is_scalar(i) for i in v) for v in vals):
        return False
    return len({len(v) for v in vals}) == 1


def parallel_to_table(d: dict) -> tuple:
    cols = list(d.keys())
    n = len(d[cols[0]])
    return cols, [["" if d[c][i] is None else d[c][i] for c in cols] for i in range(n)]


def extract_tables(data: Any, max_tables: int = 30) -> dict:
    """Devolve {nome: (colunas, linhas)}, da maior para a menor, mais chave/valor."""
    found: dict = {}

    def visit(node: Any, path: str) -> None:
        if len(found) >= max_tables:
            return
        if isinstance(node, list):
            if node and all(isinstance(i, dict) for i in node):
                found[path or "(raiz)"] = records_to_table(node)
        elif isinstance(node, dict):
            if is_parallel(node):
                found[path or "(raiz)"] = parallel_to_table(node)
                return
            for k, v in node.items():
                visit(v, f"{path}.{k}" if path else str(k))

    visit(data, "")
    tables = dict(sorted(found.items(), key=lambda kv: len(kv[1][1]), reverse=True))
    if isinstance(data, (dict, list)):
        kv = flatten(data)
    else:
        kv = {"valor": data}
    tables["(chave/valor)"] = (["campo", "valor"], [[k, "" if v is None else v] for k, v in kv.items()])
    return tables


def dig(d: Any, path: str, default: Any = None) -> Any:
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    if isinstance(cur, dict) and "value" in cur:
        cur = cur["value"]
    return default if cur is None else cur


def find_key(data: Any, key: str) -> Any:
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for v in data.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(data, list):
        for v in data:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


# --------------------------------------------------------------------------
# Utilidades de formulário
# --------------------------------------------------------------------------
def parse_kv_lines(text: str, sep: str) -> dict:
    out: dict = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if sep not in line:
            raise ValueError(f"Linha inválida (esperado '{sep}'): {line}")
        k, v = line.split(sep, 1)
        out[k.strip()] = v.strip()
    return out


_COORD = re.compile(r"^\s*-?\d+(\.\d+)?(\s*,\s*-?\d+(\.\d+)?)*\s*$")


def _coord(v: str, label: str) -> str:
    if not _COORD.match(v or ""):
        raise ValueError(f"{label} inválida: use número decimal com ponto (ex.: -30.03). "
                         "Para vários pontos, separe por vírgula.")
    return v.replace(" ", "")


def _date(v: str, label: str, fmt: str) -> str:
    try:
        datetime.strptime(v.strip(), fmt)
    except ValueError:
        human = (fmt.replace("%Y", "AAAA").replace("%m", "MM").replace("%d", "DD")
                 .replace("%H", "hh").replace("%M", "mm").replace("%S", "ss"))
        raise ValueError(f"{label} inválida. Formato esperado: {human}")
    return v.strip()


def join_base(base: str, path: str) -> str:
    base = base.strip().rstrip("/")
    bp = urlparse(base).path
    if bp and path.startswith(bp + "/"):
        path = path[len(bp):]
    return base + "/" + path.lstrip("/")


def now_brt() -> datetime:
    return datetime.now(BRT)


# --------------------------------------------------------------------------
# Requisição livre
# --------------------------------------------------------------------------
def make_free_spec(resolve: Callable[[str], str] = lambda u: u, default_url: str = "",
                   field_help: Optional[dict] = None) -> RequestSpec:
    fields = [
        Field("method", "Método", "GET", "combo", ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")),
        Field("url", "URL", default_url, help="URL completa, ou caminho começando com / (usa a URL base da aba)"),
        Field("params", "Parâmetros (query)", "", "text", help="um por linha: chave=valor"),
        Field("headers", "Headers", "", "text", help="um por linha: Chave: valor"),
        Field("body", "Corpo", "", "text", help="JSON ou texto livre"),
    ]
    if field_help:
        fields = [replace(f, help=field_help.get(f.key, f.help)) for f in fields]

    def build(v: dict) -> PreparedRequest:
        url = resolve(v["url"].strip())
        if not re.match(r"^https?://", url):
            raise ValueError("Informe uma URL começando com http:// ou https://")
        prep = PreparedRequest(v["method"].strip().upper() or "GET", url,
                               params=parse_kv_lines(v["params"], "="),
                               headers=parse_kv_lines(v["headers"], ":"))
        body = v["body"].strip()
        if body:
            try:
                prep.json_body = json.loads(body)
            except ValueError:
                prep.data = body
        return prep

    return RequestSpec("Requisição livre", "Monte qualquer requisição HTTP manualmente.", fields, build,
                       button="Consulta manual")


# --------------------------------------------------------------------------
# Defesa Civil RS (GraphQL)
# --------------------------------------------------------------------------
DCRS_ENDPOINT = "https://redehidrometeorologica.defesacivil.rs.gov.br/graphql"

# Texto das consultas conforme o guia oficial. A API só aceita estas consultas
# (espaços e quebras de linha não importam; campos e ordem importam).
TAGS_QUERY = """query Tags_data {
  tags_data(clients: ["casa-militar-defesa-civil-rs"]) {
    qualle_meteorologia {
      codigo
      name { prefix general local }
      show
      timestamp
      position {
        bacia latitude longitude regiao altitude
        filtros { outros { crepdec { nome { value } codigo { value } } } }
      }
      data {
        rio {
          rio_alarmes { inundacao { ativo { value } status { value } } }
          rio_nome { value }
          rio_nivel { value }
          rio_nivel_tendencia { value }
          rio_area_drenagem { value }
          homologacao { relacao { valido { value } } }
        }
        chuva {
          acumulado {
            min005 { value } h001 { value } h003 { value } h006 { value }
            h012 { value } h024 { value } h048 { value } h168 { value }
          }
          homologacao { relacao { valido { value } } }
        }
        pressaoatmos {
          atual { value }
          historico { diaanterior { media { value } } diaatual { media { value } } }
          tendencia { value }
          homologacao { relacao { valido { value } } }
        }
        senstermica {
          atual { value }
          historico { diaatual { media { value } } diaanterior { media { value } } }
          tendencia { value }
          homologacao { relacao { valido { value } } }
        }
        radiacaosolar {
          atual { value }
          homologacao { relacao { valido { value } } }
        }
        temperatura {
          atual { value }
          historico {
            diaanterior { media { value } }
            diaatual { media { value } maxima { value } minima { value } }
          }
          tendencia { value }
          homologacao { relacao { valido { value } } }
        }
        umidade {
          atual { value }
          historico { diaanterior { media { value } } diaatual { media { value } } }
          tendencia { value }
          homologacao { relacao { valido { value } } }
        }
        vento {
          direcao { value }
          velocidade_maxima { value }
          velocidade_media { value }
          homologacao { relacao { valido { value } } }
        }
      }
      type
      filter {
        relacao {
          tem_chuva_acumulada tem_nivel_do_rio tem_pressao_atmosferica
          tem_sensacao_termica tem_umidade tem_vazao_do_rio tem_vento
        }
      }
      resource { estacao { estado { manutencao { value } } } }
    }
    proximaAtualizacaoEm
  }
}"""

HISTORIC_QUERY = """query Historic($stationCode: String!, $startDate: String!, $endDate: String!, $interval: QueryInterval) {
  historic(
    system: Qualle_Hidrometeorologia
    client: "casa-militar-defesa-civil-rs"
    stationCode: $stationCode
    startDate: $startDate
    endDate: $endDate
    interval: $interval
    opts: { ordenacao: ASC }
  )
}"""

DCRS_INTERVALS = ("MIN_5", "MIN_10", "HOUR_1", "HOUR_3", "HOUR_6", "HOUR_12")
DCRS_SITUACAO = {0: "Normalidade", 1: "Atenção", 2: "Alerta", 3: "Inundação"}
IBGE_RS_MESH = "https://servicodados.ibge.gov.br/api/v3/malhas/estados/43"
IBGE_RS_MUNICIPALITIES = "https://servicodados.ibge.gov.br/api/v1/localidades/estados/43/municipios"


def ibge_municipal_boundaries(**http) -> list:
    mesh = do_request(PreparedRequest("GET", IBGE_RS_MESH, params={
        "formato": "application/vnd.geo+json", "qualidade": "intermediaria", "intrarregiao": "municipio",
    }), **http)
    if not mesh.ok or not isinstance(mesh.data, dict) or not mesh.data.get("features"):
        return []
    places = do_request(PreparedRequest("GET", IBGE_RS_MUNICIPALITIES), **http)
    if not places.ok or not isinstance(places.data, list):
        return []
    names = {str(place["id"]): place["nome"] for place in places.data}
    boundaries = []
    for item in mesh.data["features"]:
        name = names.get(str(item["properties"].get("codarea")))
        geometry = item.get("geometry") or {}
        if not name or geometry.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        coords = geometry["coordinates"]
        for polygon in [coords] if geometry["type"] == "Polygon" else coords:
            if polygon and polygon[0]:
                xs, ys = zip(*(point[:2] for point in polygon[0]))
                boundaries.append((name, (min(xs), min(ys), max(xs), max(ys)), polygon))
    return boundaries


def _point_in_ring(x: float, y: float, ring: list) -> bool:
    inside = False
    ax, ay = ring[-1][:2]
    for bx, by, *_ in ring:
        if (ay > y) != (by > y) and x < ax + (bx - ax) * (y - ay) / (by - ay):
            inside = not inside
        ax, ay = bx, by
    return inside


def dcrs_station_table(data: Any, boundaries=()) -> dict:
    stations = dig(data, "data.tags_data.qualle_meteorologia")
    if not isinstance(stations, list):
        return {}
    columns = ["codigo", "estacao", "cidade", "rio", "latitude", "longitude"]
    rows = []
    for station in stations:
        lat = dig(station, "position.latitude", "")
        lon = dig(station, "position.longitude", "")
        city = ""
        if lat != "" and lon != "":
            try:
                x, y = float(lon), float(lat)
                for name, (xmin, ymin, xmax, ymax), polygon in boundaries:
                    if xmin <= x <= xmax and ymin <= y <= ymax and _point_in_ring(x, y, polygon[0]):
                        if not any(_point_in_ring(x, y, hole) for hole in polygon[1:]):
                            city = name
                            break
            except (TypeError, ValueError):
                pass
        station_name = " - ".join(str(part) for path in ("name.general", "name.local")
                                  if (part := dig(station, path, "")))
        rows.append([dig(station, "codigo", ""), station_name, city,
                     dig(station, "data.rio.rio_nome", ""), lat, lon])
    return {"Cadastro de estações": (columns, rows)}

_DCRS_COLUMNS = [
    ("codigo", "codigo"), ("prefixo", "name.prefix"), ("nome", "name.general"), ("local", "name.local"),
    ("regiao", "position.regiao"), ("bacia", "position.bacia"),
    ("latitude", "position.latitude"), ("longitude", "position.longitude"), ("altitude", "position.altitude"),
    ("timestamp", "timestamp"),
    ("rio", "data.rio.rio_nome"), ("nivel_rio_m", "data.rio.rio_nivel"),
    ("tendencia_nivel", "data.rio.rio_nivel_tendencia"),
    ("chuva_5min", "data.chuva.acumulado.min005"), ("chuva_1h", "data.chuva.acumulado.h001"),
    ("chuva_3h", "data.chuva.acumulado.h003"), ("chuva_6h", "data.chuva.acumulado.h006"),
    ("chuva_12h", "data.chuva.acumulado.h012"), ("chuva_24h", "data.chuva.acumulado.h024"),
    ("chuva_48h", "data.chuva.acumulado.h048"), ("chuva_7d", "data.chuva.acumulado.h168"),
    ("temp_C", "data.temperatura.atual"), ("umidade_%", "data.umidade.atual"),
    ("pressao_hPa", "data.pressaoatmos.atual"), ("vento_med_kmh", "data.vento.velocidade_media"),
    ("vento_max_kmh", "data.vento.velocidade_maxima"), ("vento_dir", "data.vento.direcao"),
    ("radiacao_Wm2", "data.radiacaosolar.atual"),
    ("manutencao", "resource.estacao.estado.manutencao"),
]


def dcrs_summary_tables(data: Any) -> dict:
    stations = find_key(data, "qualle_meteorologia")
    if not isinstance(stations, list) or not stations:
        return {}
    cols = [c for c, _ in _DCRS_COLUMNS] + ["alerta_ativo", "situacao_rio"]
    rows = []
    for st in stations:
        row = [dig(st, p, "") for _, p in _DCRS_COLUMNS]
        ativo = dig(st, "data.rio.rio_alarmes.inundacao.ativo", "")
        status = dig(st, "data.rio.rio_alarmes.inundacao.status", None)
        if ativo is False or ativo == "":
            sit = "sem limiar" if ativo is False else ""
        elif status is None:
            sit = "sem leitura recente"
        else:
            sit = DCRS_SITUACAO.get(status, str(status))
        rows.append(row + [ativo, sit])
    return {"Resumo das estações": (cols, rows)}


def make_dcrs_specs(get_endpoint: Callable[[], str]) -> list:
    def build_tags(v: dict) -> PreparedRequest:
        return PreparedRequest("POST", get_endpoint(), headers={"Content-Type": "application/json"},
                               json_body={"query": TAGS_QUERY}, postprocess=dcrs_summary_tables)

    hist_fields = [
        Field("stationCode", "Código da estação", "DCRS-00001", required=True, help="veja o código em Estações agora"),
        Field("startDate", "Início", lambda: (now_brt() - timedelta(hours=6)).strftime(DATE_FMT),
              required=True, help="AAAA-MM-DD HH:MM:SS, horário de Brasília, até 3 meses atrás"),
        Field("endDate", "Fim", lambda: now_brt().strftime(DATE_FMT), required=True, help="mesmo formato"),
        Field("interval", "Intervalo", "HOUR_1", "combo", DCRS_INTERVALS, help="vazio = padrão da API"),
    ]

    def build_hist(v: dict) -> PreparedRequest:
        variables = {
            "stationCode": v["stationCode"].strip(),
            "startDate": _date(v["startDate"], "startDate", DATE_FMT),
            "endDate": _date(v["endDate"], "endDate", DATE_FMT),
        }
        if v["interval"].strip():
            variables["interval"] = v["interval"].strip()
        return PreparedRequest("POST", get_endpoint(), headers={"Content-Type": "application/json"},
                               json_body={"query": HISTORIC_QUERY, "variables": variables})

    return [
        RequestSpec("tags_data - situação atual de todas as estações",
                    "Uma única requisição traz as ~130 estações (nível de rio, chuva acumulada, clima, "
                    "vento, alerta de inundação). Não há filtro: use o campo 'Filtrar' da tabela. "
                    "Atualiza a cada 5 min.", [], build_tags, button="Estações agora"),
        RequestSpec("historic - série histórica de uma estação",
                    "Série de uma estação nos últimos 3 meses, agrupada no intervalo escolhido.",
                    hist_fields, build_hist, button="Histórico por estação"),
        RequestSpec("Listar estações", "Lista as estações da rede com município, rio e coordenadas. "
                    "O município é identificado pelas coordenadas na malha municipal do IBGE.",
                    [], lambda values: replace(build_tags(values), postprocess=dcrs_station_table),
                    button="Listar estações"),
        make_free_spec(default_url=DCRS_ENDPOINT),
    ]


# --------------------------------------------------------------------------
# Open-Meteo
# --------------------------------------------------------------------------
def om_host(sub: str, key: str) -> str:
    return f"https://{'customer-' if key else ''}{sub}.open-meteo.com"


ARCHIVE_CHUNK_DAYS = 90


def archive_requests(prep: PreparedRequest):
    start = date.fromisoformat(prep.params["start_date"])
    end = date.fromisoformat(prep.params["end_date"])
    while start <= end:
        stop = min(start + timedelta(days=ARCHIVE_CHUNK_DAYS - 1), end)
        yield replace(prep, archive_batch=False, params={
            **prep.params, "start_date": start.isoformat(), "end_date": stop.isoformat(),
        })
        start = stop + timedelta(days=1)


def build_request_preview(prep: PreparedRequest, user_agent: str) -> str:
    if not prep.archive_batch:
        return build_curl(prep, user_agent)
    return "\n\n".join(build_curl(part, user_agent) for part in archive_requests(prep))


def _request_archive_block(part: PreparedRequest, **http) -> HttpResult:
    for attempt in range(3):
        result = do_request(part, **http)
        if result.ok or attempt == 2:
            return result
        if result.status == 429:
            retry_after = requests.structures.CaseInsensitiveDict(result.resp_headers).get("Retry-After", "")
            if not retry_after.isdecimal() or int(retry_after) > 90:
                return result
            delay = max(1, int(retry_after))
        elif result.status in (None, 408, 500, 502, 503, 504):
            delay = 2 ** attempt
        else:
            return result
        time.sleep(delay)


def fetch_open_meteo_archive(prep: PreparedRequest, *,
                             progress: Callable[[int, int], None] | None = None,
                             **http) -> HttpResult:
    points = len(prep.params["latitude"].split(","))
    periods = {key: [v.strip() for v in prep.params[key].split(",") if v.strip()]
               for key in ("hourly", "daily") if prep.params[key]}
    merged = []
    total_size = blocks = 0
    start_date = date.fromisoformat(prep.params["start_date"])
    end_date = date.fromisoformat(prep.params["end_date"])
    total_blocks = (end_date - start_date).days // ARCHIVE_CHUNK_DAYS + 1
    started_at = time.perf_counter()
    if progress:
        progress(0, total_blocks)
    for part in archive_requests(prep):
        start, end = part.params["start_date"], part.params["end_date"]
        result = _request_archive_block(part, **http)
        if not result.ok:
            reason = result.data.get("reason") if isinstance(result.data, dict) else None
            reason = reason or result.summary().removeprefix("ERRO: ")
            detail = mask_query_secrets(str(reason))
            return replace(result, error=f"Histórico incompleto no bloco {start} a {end}. {detail}", data=None)

        locations = result.data if isinstance(result.data, list) else [result.data]
        if len(locations) != points or not all(isinstance(location, dict) for location in locations):
            return replace(result, error=f"Resposta de locais inválida no bloco {start} a {end}.", data=None)
        locations = sorted(locations, key=lambda item: item.get("location_id", 0))
        first_day = date.fromisoformat(start)
        days = (date.fromisoformat(end) - first_day).days + 1
        expected_days = {(first_day + timedelta(days=i)).isoformat() for i in range(days)}
        for index, location in enumerate(locations):
            for key, variables in periods.items():
                series = location.get(key)
                if not is_parallel(series) or not {"time", *variables}.issubset(series):
                    return replace(result, error=f"Série {key} inválida no bloco {start} a {end}.", data=None)
                times = series["time"]
                if (not all(isinstance(t, str) for t in times)
                        or {t[:10] for t in times} != expected_days
                        or (key == "daily" and set(times) != expected_days)
                        or times != sorted(times) or len(set(times)) != len(times)):
                    return replace(result, error=f"Datas ausentes ou inválidas em {key}, bloco {start} a {end}.", data=None)
                if blocks:
                    previous = merged[index]
                    if (any(previous.get(k) != location.get(k) for k in ("latitude", "longitude", "location_id", "timezone"))
                            or previous.get(key + "_units") != location.get(key + "_units")
                            or previous[key].keys() != series.keys()):
                        return replace(result, error=f"Local ou formato diferente no bloco {start} a {end}.", data=None)
                    for name, values in series.items():
                        previous[key][name].extend(values)
        if not blocks:
            merged = locations
        blocks += 1
        total_size += result.size
        if progress:
            progress(blocks, total_blocks)

    tables = {}
    for index, location in enumerate(merged, 1):
        prefix = f"Local {index} · " if points > 1 else ""
        for key, label in (("hourly", "Histórico horário"), ("daily", "Histórico diário")):
            if key in periods:
                tables[prefix + label] = parallel_to_table(location[key])
    return replace(result, data=merged if points > 1 else merged[0], text="", tables=tables,
                   elapsed=time.perf_counter() - started_at, size=total_size,
                   note=f"{blocks} bloco(s) · {prep.params['start_date']} a {prep.params['end_date']}")


def make_open_meteo_specs(get_key: Callable[[], str]) -> list:
    def with_key(params: dict) -> dict:
        key = get_key().strip()
        if key:
            params["apikey"] = key
        return params

    def host(sub: str) -> str:
        return om_host(sub, get_key().strip())

    forecast_fields = [
        Field("latitude", "latitude", "-30.0346", required=True, help="vários pontos: separe por vírgula"),
        Field("longitude", "longitude", "-51.2177", required=True),
        Field("current", "current", "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m", help="variáveis atuais"),
        Field("hourly", "hourly", "temperature_2m,precipitation,precipitation_probability,wind_speed_10m", help="variáveis horárias"),
        Field("daily", "daily", "temperature_2m_max,temperature_2m_min,precipitation_sum", help="variáveis diárias"),
        Field("forecast_days", "Dias de previsão", "3", help="0 a 16"),
        Field("past_days", "past_days", "0", help="0 a 92"),
        Field("timezone", "timezone", "America/Sao_Paulo"),
    ]

    def build_forecast(v: dict) -> PreparedRequest:
        p = {"latitude": _coord(v["latitude"], "latitude"), "longitude": _coord(v["longitude"], "longitude"),
             "current": v["current"].strip(), "hourly": v["hourly"].strip(), "daily": v["daily"].strip(),
             "forecast_days": v["forecast_days"].strip(), "past_days": v["past_days"].strip(),
             "timezone": v["timezone"].strip()}
        return PreparedRequest("GET", host("api") + "/v1/forecast", params=with_key(p))

    archive_fields = [
        Field("latitude", "latitude", "-30.0346", required=True),
        Field("longitude", "longitude", "-51.2177", required=True),
        Field("start_date", "Data inicial", lambda: (now_brt() - timedelta(days=14)).strftime("%Y-%m-%d"), required=True, help="AAAA-MM-DD"),
        Field("end_date", "Data final", lambda: (now_brt() - timedelta(days=7)).strftime("%Y-%m-%d"), required=True,
              help="AAAA-MM-DD. O ERA5 tem atraso de cerca de 5 dias."),
        Field("hourly", "hourly", "temperature_2m,precipitation"),
        Field("daily", "daily", "precipitation_sum,temperature_2m_max"),
        Field("timezone", "timezone", "America/Sao_Paulo"),
    ]

    def build_archive(v: dict) -> PreparedRequest:
        p = {"latitude": _coord(v["latitude"], "latitude"), "longitude": _coord(v["longitude"], "longitude"),
             "start_date": _date(v["start_date"], "start_date", "%Y-%m-%d"),
             "end_date": _date(v["end_date"], "end_date", "%Y-%m-%d"),
             "hourly": v["hourly"].strip(), "daily": v["daily"].strip(), "timezone": v["timezone"].strip()}
        p["start_date"] = datetime.strptime(p["start_date"], "%Y-%m-%d").date().isoformat()
        p["end_date"] = datetime.strptime(p["end_date"], "%Y-%m-%d").date().isoformat()
        p["models"] = "era5"
        for key in ("hourly", "daily"):
            p[key] = ",".join(var.strip() for var in p[key].split(",") if var.strip())
        if p["latitude"].count(",") != p["longitude"].count(","):
            raise ValueError("Informe a mesma quantidade de latitudes e longitudes.")
        if p["start_date"] > p["end_date"]:
            raise ValueError("A data inicial deve ser anterior ou igual à data final.")
        if p["start_date"] < "1940-01-01" or p["end_date"] > (now_brt().date() - timedelta(days=5)).isoformat():
            raise ValueError("O ERA5 cobre datas a partir de 1940-01-01 e tem atraso de cerca de 5 dias.")
        if not p["hourly"] and not p["daily"]:
            raise ValueError("Informe ao menos uma variável horária ou diária.")
        return PreparedRequest("GET", host("archive-api") + "/v1/archive", params=with_key(p), archive_batch=True)

    flood_fields = [
        Field("latitude", "latitude", "-30.0346", required=True),
        Field("longitude", "longitude", "-51.2177", required=True),
        Field("daily", "daily", "river_discharge,river_discharge_mean,river_discharge_max"),
        Field("forecast_days", "Dias de previsão", "7", help="0 a 210"),
        Field("past_days", "past_days", "0", help="as estatísticas de ensemble (mean, max...) só existem na previsão"),
    ]

    def build_flood(v: dict) -> PreparedRequest:
        p = {"latitude": _coord(v["latitude"], "latitude"), "longitude": _coord(v["longitude"], "longitude"),
             "daily": v["daily"].strip(), "forecast_days": v["forecast_days"].strip(),
             "past_days": v["past_days"].strip()}
        return PreparedRequest("GET", host("flood-api") + "/v1/flood", params=with_key(p))

    geo_fields = [
        Field("name", "Local ou CEP", "Porto Alegre", required=True),
        Field("count", "count", "5", help="1 a 100"),
        Field("language", "language", "pt"),
        Field("countryCode", "countryCode", "BR", help="opcional (ISO-3166)"),
    ]

    def build_geo(v: dict) -> PreparedRequest:
        p = {"name": v["name"].strip(), "count": v["count"].strip(), "language": v["language"].strip(),
             "countryCode": v["countryCode"].strip(), "format": "json"}
        return PreparedRequest("GET", host("geocoding-api") + "/v1/search", params=with_key(p))

    elev_fields = [
        Field("latitude", "latitude", "-30.0346", required=True, help="aceita lista separada por vírgula"),
        Field("longitude", "longitude", "-51.2177", required=True),
    ]

    def build_elev(v: dict) -> PreparedRequest:
        p = {"latitude": _coord(v["latitude"], "latitude"), "longitude": _coord(v["longitude"], "longitude")}
        return PreparedRequest("GET", host("api") + "/v1/elevation", params=with_key(p))

    return [
        RequestSpec("Previsão (forecast)", "Condições atuais, previsão horária e diária num único GET. "
                    "Variáveis separadas por vírgula; campos vazios são omitidos.", forecast_fields, build_forecast,
                    "Previsão", ("latitude", "longitude", "forecast_days")),
        RequestSpec("Histórico (archive / ERA5)", "Consulte 10 anos ou mais dentro da cobertura do ERA5. "
                    "O aplicativo busca blocos de até 90 dias e consolida o período para exportação em CSV.",
                    archive_fields, build_archive, "Histórico", ("latitude", "longitude", "start_date", "end_date")),
        RequestSpec("Vazão de rios (flood)", "Vazão modelada (GloFAS). É dado de modelo, não de sensor.",
                    flood_fields, build_flood, "Vazão de rios", ("latitude", "longitude", "forecast_days")),
        RequestSpec("Geocoding (buscar coordenadas)", "Busca coordenadas e elevação pelo nome do local.",
                    geo_fields, build_geo, "Buscar local", ("name",)),
        RequestSpec("Elevação", "Elevação por coordenadas.", elev_fields, build_elev, button="Elevação"),
        make_free_spec(default_url="https://api.open-meteo.com/v1/forecast"),
    ]


# --------------------------------------------------------------------------
# OpenAPI / Swagger (usado por OpenZenith e ANA)
# --------------------------------------------------------------------------
def deref(spec: dict, obj: Any) -> Any:
    if isinstance(obj, dict) and "$ref" in obj and isinstance(obj["$ref"], str) and obj["$ref"].startswith("#/"):
        cur: Any = spec
        for part in obj["$ref"][2:].split("/"):
            cur = cur.get(part, {}) if isinstance(cur, dict) else {}
        return cur
    return obj


def openapi_endpoints(spec: dict) -> dict:
    rows = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                continue
            params = []
            for p in op.get("parameters", []) or []:
                p = deref(spec, p)
                params.append(f"{p.get('name')}({p.get('in')}{'*' if p.get('required') else ''})")
            rows.append([method.upper(), path, op.get("summary", ""), (op.get("tags") or [""])[0], ", ".join(params)])
    return {"Endpoints (OpenAPI)": (["método", "caminho", "resumo", "tag", "parâmetros (* obrigatório)"], rows)}


def _make_openapi_build(method: str, path: str, fields: list, get_base: Callable[[], str]):
    def build(v: dict) -> PreparedRequest:
        url_path, params, headers = path, {}, {}
        body = None
        for f in fields:
            val = v.get(f.key, "").strip()
            if f.required and not val:
                raise ValueError(f"Campo obrigatório: {f.label}")
            if not val:
                continue
            if f.key == "__body__":
                try:
                    body = json.loads(val)
                except ValueError:
                    raise ValueError("Corpo: JSON inválido.")
                continue
            loc, name = f.key.split(":", 1)
            if loc == "path":
                url_path = url_path.replace("{" + name + "}", quote(val, safe=""))
            elif loc == "query":
                params[name] = val
            elif loc == "header":
                headers[name] = val
        return PreparedRequest(method, join_base(get_base(), url_path), params=params, headers=headers, json_body=body)
    return build


def specs_from_openapi(spec: dict, get_base: Callable[[], str], limit: int = 300) -> list:
    out = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters", []) or []
        for method, op in item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                continue
            fields = []
            for p in list(shared) + list(op.get("parameters", []) or []):
                p = deref(spec, p)
                loc, name = p.get("in"), p.get("name")
                if loc not in ("path", "query", "header") or not name:
                    continue
                schema = deref(spec, p.get("schema", p)) or {}
                enum = schema.get("enum")
                default = schema.get("default", "")
                desc = (p.get("description") or "").replace("\n", " ")[:90]
                fields.append(Field(f"{loc}:{name}", f"{name} ({loc})",
                                    "" if default in (None, "") else str(default),
                                    "combo" if enum else "entry", tuple(str(e) for e in (enum or ())),
                                    help=desc, required=bool(p.get("required")) or loc == "path"))
            if "requestBody" in op:
                fields.append(Field("__body__", "Corpo (JSON)", "", "text"))
            title = f"[OpenAPI] {method.upper()} {path}"
            desc = op.get("summary") or op.get("description") or ""
            out.append(RequestSpec(title, desc, fields,
                                   _make_openapi_build(method.upper(), path, fields, get_base)))
            if len(out) >= limit:
                return out
    return out


def make_openapi_loader_spec(get_base: Callable[[], str], default_path: str, candidates: tuple,
                             on_loaded: Callable[[dict], None]) -> RequestSpec:
    fields = [Field("path", "Caminho do JSON OpenAPI", default_path, "combo", candidates,
                    help="se um falhar, tente outro; a lista de requisições da aba será preenchida")]

    def build(v: dict) -> PreparedRequest:
        def done(res: HttpResult) -> None:
            if res.ok and isinstance(res.data, dict) and isinstance(res.data.get("paths"), dict):
                on_loaded(res.data)
        return PreparedRequest("GET", join_base(get_base(), v["path"].strip() or default_path),
                               postprocess=lambda d: openapi_endpoints(d) if isinstance(d, dict) else {},
                               on_done=done, is_openapi=True)

    return RequestSpec("Carregar rotas da documentação (OpenAPI)",
                       "Baixa a especificação OpenAPI/Swagger e adiciona cada rota como uma requisição "
                       "desta aba, com seus parâmetros.", fields, build, "Carregar rotas", ())


# --------------------------------------------------------------------------
# OpenZenith
# --------------------------------------------------------------------------
OZ_BASES = ("https://openzenith.org/api", "https://openzenith.cyopsys.com/api")


def make_openzenith_specs(get_base: Callable[[], str], on_loaded: Callable[[dict], None]) -> list:
    elev_fields = [Field("lat", "lat", "-30.0346", required=True), Field("lon", "lon", "-51.2177", required=True)]

    def build_elev(v: dict) -> PreparedRequest:
        return PreparedRequest("GET", join_base(get_base(), "/elevation"),
                               params={"lat": _coord(v["lat"], "lat"), "lon": _coord(v["lon"], "lon")})

    def build_health(v: dict) -> PreparedRequest:
        return PreparedRequest("GET", join_base(get_base(), "/health"))

    return [
        RequestSpec("Elevação (lat, lon)", "Elevação em metros, tipo de superfície e tile.",
                    elev_fields, build_elev, button="Elevação"),
        RequestSpec("Health (status do serviço)", "Verifica se a API está respondendo.", [], build_health,
                    button="Status do serviço"),
        make_openapi_loader_spec(get_base, "/openapi.json", ("/openapi.json", "/v3/api-docs"), on_loaded),
        make_free_spec(resolve=lambda u: join_base(get_base(), u) if u.startswith("/") else u,
                       default_url="/elevation?lat=-30.03&lon=-51.21"),
    ]


# --------------------------------------------------------------------------
# ANA HidroWebService
# --------------------------------------------------------------------------
ANA_BASE = "https://www.ana.gov.br/hidrowebservice"
ANA_AUTH_PATH = "/EstacoesTelemetricas/OAUth/v1"
ANA_OPENAPI_PATH = "/api-docs/Vers%C3%A3o%20-%20v1.0.3984.2"


def ana_inventory_table(data: Any) -> dict:
    stations = dig(data, "items")
    if not isinstance(stations, list):
        return {}
    columns = [
        ("codigo", "codigoestacao"), ("estacao", "Estacao_Nome"), ("cidade", "Municipio_Nome"),
        ("uf", "UF_Estacao"), ("latitude", "Latitude"), ("longitude", "Longitude"),
        ("codigo_bacia", "codigobacia"), ("bacia", "Bacia_Nome"),
    ]
    return {"Estações ANA": ([header for header, _ in columns],
                            [[dig(st, key, "") for _, key in columns] for st in stations])}


def find_token(data: Any) -> Optional[str]:
    if isinstance(data, dict):
        for k, v in data.items():
            if k.lower() in ANA_TOKEN_KEYS and isinstance(v, str) and v.strip():
                return v
        for v in data.values():
            r = find_token(v)
            if r:
                return r
    elif isinstance(data, list):
        for v in data:
            r = find_token(v)
            if r:
                return r
    return None


def redact_ana_tokens(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: "***" if k.lower() in ANA_TOKEN_KEYS else redact_ana_tokens(v) for k, v in data.items()}
    if isinstance(data, list):
        return [redact_ana_tokens(v) for v in data]
    return data


def ana_auth_request(base: str, auth_path: str, identificador: str, senha: str, method: str = "GET") -> PreparedRequest:
    return PreparedRequest(method, join_base(base, auth_path),
                           headers={"Identificador": identificador, "Senha": senha, "accept": "*/*"},
                           is_auth=True)


def make_ana_specs(get_base: Callable[[], str], on_loaded: Callable[[dict], None],
                   get_auth_args: Callable[[], tuple]) -> list:
    def build_auth(v: dict) -> PreparedRequest:
        base, path, ident, senha = get_auth_args()
        if not ident or not senha:
            raise ValueError("Preencha Identificador e Senha na área de autenticação.")
        return ana_auth_request(base, path, ident, senha)

    inventory_fields = [
        Field("UnidadeFederativa", "UF", "RS", "combo",
              tuple("AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO".split())),
        Field("CodigoDaEstacao", "Código da estação"),
        Field("CodigoDaBacia", "Código da bacia"),
    ]

    def build_inventory(values: dict) -> PreparedRequest:
        params = {f.key: values[f.key].strip() for f in inventory_fields if values[f.key].strip()}
        if not params:
            raise ValueError("Informe a UF, o código da estação ou o código da bacia.")
        if "UnidadeFederativa" in params:
            params["UnidadeFederativa"] = params["UnidadeFederativa"].upper()
        return PreparedRequest("GET", join_base(get_base(), "/EstacoesTelemetricas/HidroInventarioEstacoes/v1"),
                               params=params, postprocess=ana_inventory_table)

    def series_spec(label: str, description: str, suffix: str, interval: str) -> RequestSpec:
        intervals = ("MINUTO_5", "MINUTO_10", "MINUTO_15", "MINUTO_30",
                     *(f"HORA_{hour}" for hour in range(1, 25)), "DIAS_2", "DIAS_7", "DIAS_14", "DIAS_21", "DIAS_30")
        fields = [
            Field("CodigoDaEstacao", "Código da estação", required=True),
            Field("RangeIntervaloDeBusca", "Intervalo", interval, "combo", intervals, required=True),
            Field("TipoFiltroData", "Tipo de data", "DATA_LEITURA", "combo",
                  ("DATA_LEITURA", "DATA_ULTIMA_ATUALIZACAO"), required=True),
            Field("DataDeBusca", "Data de busca", help="AAAA-MM-DD"),
        ]

        def build(values: dict) -> PreparedRequest:
            params = {f.key: values[f.key].strip() for f in fields if values[f.key].strip()}
            for field in fields:
                if field.required and field.key not in params:
                    raise ValueError(f"Preencha o campo {field.label}.")
            if "DataDeBusca" in params:
                params["DataDeBusca"] = _date(params["DataDeBusca"], "Data de busca", "%Y-%m-%d")
            path = f"/EstacoesTelemetricas/HidroinfoanaSerieTelemetrica{suffix}/v1"
            return PreparedRequest("GET", join_base(get_base(), path), params=params)

        return RequestSpec(f"Série telemétrica {label.lower()}", description, fields, build,
                           f"Série {label.lower()}", ("CodigoDaEstacao", "RangeIntervaloDeBusca", "DataDeBusca"))

    return [
        RequestSpec("Autenticação - obter token", "Chama a rota de autenticação com Identificador e Senha "
                    "(headers) e guarda o tokenautenticacao. As demais requisições usam o token automaticamente.",
                    [], build_auth, button="Autenticar"),
        make_openapi_loader_spec(get_base, ANA_OPENAPI_PATH, (ANA_OPENAPI_PATH, "/v3/api-docs", "/v2/api-docs"), on_loaded),
        RequestSpec("Inventário de estações", "Lista cidade, coordenadas e código de bacia das estações. "
                    "Informe UF, código de estação ou código de bacia.",
                    inventory_fields, build_inventory, button="Listar estações"),
        series_spec("Adotada", "Consulta os dados adotados da estação.", "Adotada", "DIAS_30"),
        series_spec("Detalhada", "Inclui dados brutos e adotados. O período máximo é de 30 dias.",
                    "Detalhada", "DIAS_7"),
        make_free_spec(resolve=lambda u: join_base(get_base(), u) if u.startswith("/") else u,
                       default_url="/EstacoesTelemetricas/HidroInventarioEstacoes/v1"),
    ]


# --------------------------------------------------------------------------
# REINDESC
# --------------------------------------------------------------------------
REINDESC_BASE = "https://reindesc.cemaden.gov.br"


def make_reindesc_specs(get_base: Callable[[], str]) -> list:
    return [
        RequestSpec("Consultar portal", "Consulta a página pública do REINDESC.", [],
                    lambda _: PreparedRequest("GET", join_base(get_base(), "/")),
                    button="Consultar portal"),
        make_free_spec(resolve=lambda u: join_base(get_base(), u) if u.startswith("/") else u, default_url="/",
                       field_help={
                           "method": "Use GET para consultas; use o método indicado pela rota conhecida.",
                           "url": "Endereço HTTP(S) completo ou caminho iniciado por /, relativo à URL base.",
                           "params": "Uma linha chave=valor. Pode ser texto, código, número ou data AAAA-MM-DD, "
                                     "se a rota aceitar. Use os nomes esperados pela rota.",
                           "headers": "Uma linha Nome: valor. Ex.: Accept: application/json.",
                           "body": "Para rotas que recebem corpo: JSON (texto entre aspas, números, true/false, "
                                   "listas ou objetos) ou texto livre. Deixe vazio em consultas GET.",
                       }),
    ]
