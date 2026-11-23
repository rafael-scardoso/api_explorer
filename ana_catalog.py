"""Download explícito e leitura local do inventário público ANA/SNIRH."""

import csv
import os
import tempfile
from pathlib import Path

import requests


SOURCE = (
    "https://portal1.snirh.gov.br/server/rest/services/"
    "Esta%C3%A7%C3%B5es_Hidrometeorol%C3%B3gicas_SNIRH/FeatureServer/0"
)
CATALOG = Path(__file__).with_name("ana_estacoes.csv")
FIELDS = {
    "codigo": "Codigo",
    "estacao": "Nome",
    "codigo_bacia": "BaciaCodigo",
    "bacia": "Bacia",
    "sub_bacia": "SubBacia",
    "rio": "Rio",
    "municipio": "Municipio",
    "uf": "UF",
    "tipo": "TipoEstacao",
    "latitude": "Latitude",
    "longitude": "Longitude",
}
ALL_BASINS = "Todas as bacias"


def load_catalog(path=CATALOG):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(FIELDS):
            raise ValueError("O arquivo ana_estacoes.csv não tem as colunas esperadas.")
        rows = list(reader)
    if not rows or any(not row["codigo"] or None in row.values() for row in rows):
        raise ValueError("O arquivo ana_estacoes.csv está vazio ou incompleto.")
    return rows


def basin_label(row):
    if row["codigo_bacia"]:
        return f'{row["codigo_bacia"]} - {row["bacia"]}' if row["bacia"] else row["codigo_bacia"]
    return row["bacia"] or "Bacia não informada"


def download_catalog(path=CATALOG):
    """Consulta o geosserviço público e troca o CSV somente após validar a coleta."""
    rows = []
    seen = set()
    with requests.Session() as session:
        def query(**params):
            response = session.get(f"{SOURCE}/query", params={"where": "1=1", "f": "json", **params}, timeout=45)
            response.raise_for_status()
            result = response.json()
            if "error" in result:
                raise ValueError(result["error"].get("message", "O SNIRH retornou um erro."))
            return result

        total = query(returnCountOnly="true").get("count")
        if not isinstance(total, int) or total <= 0:
            raise ValueError("O SNIRH não informou a quantidade de estações.")
        while len(rows) < total:
            page = query(outFields=",".join(FIELDS.values()), returnGeometry="false",
                         orderByFields="Codigo ASC", resultOffset=len(rows), resultRecordCount=1000)
            entries = page.get("features")
            if not entries:
                raise ValueError(f"Coleta incompleta do SNIRH: {len(rows)} de {total} estações.")
            for entry in entries:
                attrs = entry["attributes"]
                code = attrs["Codigo"]
                if not isinstance(code, int) or code < 0 or code in seen:
                    raise ValueError("O SNIRH retornou um código de estação inválido ou repetido.")
                seen.add(code)
                row = {key: "" if attrs[field] is None else str(attrs[field]).strip()
                       for key, field in FIELDS.items()}
                row["codigo"] = f"{code:08d}"
                rows.append(row)
        if len(rows) != total or page.get("exceededTransferLimit"):
            raise ValueError("A quantidade de estações mudou durante a coleta. Tente novamente.")

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return rows


if __name__ == "__main__":
    stations = download_catalog()
    print(f"{len(stations)} estações salvas em {CATALOG}")
