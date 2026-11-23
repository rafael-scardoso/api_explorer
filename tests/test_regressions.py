import json
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import requests

import api_core as core
from app import AnaTab


class HttpRegressionTests(unittest.TestCase):
    def test_key_is_sent_but_masked_in_curl_and_final_url(self):
        req = core.PreparedRequest("GET", "https://example.test/?other=1",
                                   params={"apikey": "private-value"})
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"

        def send(_session, prepared, **kwargs):
            self.assertIn("apikey=private-value", prepared.url)
            response.url = prepared.url
            return response

        with patch.object(requests.Session, "send", send):
            out = core.do_request(req)
        self.assertTrue(out.ok)
        self.assertTrue(out.final_url.endswith("other=1&apikey=***"))
        self.assertNotIn("private-value", core.build_curl(req))

    @patch("api_core.requests.request",
           side_effect=requests.ConnectionError("Failed: /test?ApiKey=private-value&limit=1"))
    def test_error_does_not_expose_key(self, _):
        result = core.do_request(core.PreparedRequest("GET", "https://example.test"))
        self.assertIn("ApiKey=***&limit=1", result.error)
        self.assertNotIn("private-value", result.error)

    def test_graphql_error_is_not_success(self):
        result = core.HttpResult(status=200, data={"errors": [{"message": "Operação bloqueada"}]})
        self.assertFalse(result.ok)
        self.assertIn("Operação bloqueada", result.summary())

    def test_openapi_rejects_html_with_http_200(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b"<html></html>"
        with patch("api_core.requests.request", return_value=response):
            out = core.do_request(core.PreparedRequest("GET", "https://example.test", is_openapi=True))
        self.assertFalse(out.ok)
        self.assertIn("OpenAPI", out.error)

    def test_ana_inventory_lists_by_uf_only(self):
        spec = core.make_ana_specs(lambda: core.ANA_BASE, lambda spec: None, lambda: ())[2]
        self.assertEqual([field.key for field in spec.fields], ["UnidadeFederativa"])
        self.assertEqual(spec.fields[0].initial(), "RS")
        self.assertEqual(spec.build({"UnidadeFederativa": " sc "}).params, {"UnidadeFederativa": "SC"})
        with self.assertRaisesRegex(ValueError, "UF"):
            spec.build({"UnidadeFederativa": "XX"})

    def test_ana_filters_required(self):
        specs = core.make_ana_specs(lambda: core.ANA_BASE, lambda spec: None, lambda: ())
        for index, field in ((2, "UnidadeFederativa"), (3, "CodigoDaEstacao"), (4, "CodigoDaEstacao")):
            spec = specs[index]
            values = {field.key: field.initial() for field in spec.fields}
            with self.subTest(spec=spec.name), self.assertRaises(ValueError):
                spec.build({**values, field: ""})

    def test_reindesc_uses_selected_base(self):
        for spec in core.make_reindesc_specs(lambda: "https://example.test/base"):
            values = {field.key: field.initial() for field in spec.fields}
            self.assertEqual(spec.build(values).url, "https://example.test/base/")


class AnaRegressionTests(unittest.TestCase):
    settings = {"base": core.ANA_BASE, "auth_path": core.ANA_AUTH_PATH, "id": "test",
                "pw": "test", "token": "private-token", "verify": True, "timeout": 5, "ua": "test"}

    @patch("app.core.do_request")
    def test_does_not_attach_ana_token_to_external_host_or_openapi(self, send):
        for req in (core.PreparedRequest("GET", "https://example.test/"),
                    core.PreparedRequest("GET", core.ANA_BASE + core.ANA_OPENAPI_PATH, is_openapi=True)):
            send.reset_mock()
            AnaTab.execute(None, req, self.settings)
            send.assert_called_once_with(req, verify=True, timeout=5, user_agent="test")

    @patch("app.core.do_request")
    def test_auth_payload_masks_token_but_keeps_it_for_session(self, send):
        payload = {"token": "outer-token",
                   "items": {"token": "other-token", "tokenautenticacao": " private-token ", "tokenType": "Bearer"}}
        send.return_value = core.HttpResult(status=200, data=payload, text=json.dumps(payload))
        result = AnaTab._fetch_token(None, self.settings)
        self.assertEqual(result.new_token, "private-token")
        self.assertNotIn("private-token", result.text)
        self.assertNotIn("other-token", result.text)
        self.assertNotIn("outer-token", result.text)
        self.assertEqual(result.data["items"]["tokenautenticacao"], "***")
        self.assertEqual(result.data["items"]["tokenType"], "Bearer")

    @patch("app.core.do_request")
    def test_inventory_sends_tokenautenticacao_as_bearer(self, send):
        tab = object.__new__(AnaTab)
        send.side_effect = [
            core.HttpResult(status=200, data={"items": {"token": "other-token", "tokenautenticacao": "ana-token"}}),
            core.HttpResult(status=200, data={"items": []}),
        ]
        spec = core.make_ana_specs(lambda: core.ANA_BASE, lambda spec: None, lambda: ())[2]
        req = spec.build({"UnidadeFederativa": "RS"})
        result = tab.execute(req, {**self.settings, "token": ""})
        self.assertTrue(result.ok)
        self.assertEqual(result.new_token, "ana-token")
        sent = send.call_args_list[1].args[0]
        self.assertEqual(sent.headers["Authorization"], "Bearer ana-token")
        self.assertEqual(sent.params, {"UnidadeFederativa": "RS"})


class StationCatalogueTests(unittest.TestCase):
    @patch("api_core.do_request")
    def test_dcrs_matches_municipal_boundaries_and_keeps_missing_stations(self, send):
        outer = [[-52, -31], [-51, -31], [-51, -29], [-52, -29], [-52, -31]]
        hole = [[-51.4, -30.7], [-51.2, -30.7], [-51.2, -30.5], [-51.4, -30.5], [-51.4, -30.7]]
        send.side_effect = [
            core.HttpResult(status=200, data={"features": [
                {"properties": {"codarea": "4304606"},
                 "geometry": {"type": "MultiPolygon", "coordinates": [[outer, hole]]}},
            ]}),
            core.HttpResult(status=200, data=[{"id": 4304606, "nome": "Canoas"}]),
        ]
        boundaries = core.ibge_municipal_boundaries(timeout=1)
        stations = [
            {"codigo": "A", "name": {"general": "Ponte"}, "position": {"latitude": -30, "longitude": -51.1},
             "data": {"rio": {"rio_nome": {"value": "Rio dos Sinos"}}}},
            {"codigo": "B", "position": {"latitude": -30.6, "longitude": -51.3}},
            {"codigo": "C"},
        ]
        data = {"data": {"tags_data": {"qualle_meteorologia": stations}}}
        cols, rows = core.dcrs_station_table(data, boundaries)["Cadastro de estações"]
        self.assertEqual(cols, ["codigo", "estacao", "cidade", "rio", "latitude", "longitude"])
        self.assertEqual(rows[0], ["A", "Ponte", "Canoas", "Rio dos Sinos", -30, -51.1])
        self.assertEqual([row[2] for row in rows], ["Canoas", "", ""])

    def test_ana_maps_city_coordinates_and_basin(self):
        data = {"items": [{"codigoestacao": "15400000", "Estacao_Nome": "PORTO VELHO",
                           "Municipio_Nome": "PORTO VELHO", "UF_Estacao": "RO",
                           "Latitude": "-8.7483", "Longitude": "-63.9169",
                           "codigobacia": "1", "Bacia_Nome": "RIO AMAZONAS"}]}
        cols, rows = core.ana_inventory_table(data)["Estações ANA"]
        self.assertEqual(dict(zip(cols, rows[0]))["codigo_bacia"], "1")
        self.assertEqual(rows[0][2:6], ["PORTO VELHO", "RO", "-8.7483", "-63.9169"])


class ArchiveBatchTests(unittest.TestCase):
    def archive(self, **overrides):
        spec = core.make_open_meteo_specs(lambda: "")[1]
        defaults = {field.key: field.initial() for field in spec.fields}
        return spec.build({**defaults, "start_date": "2020-01-01", "end_date": "2020-07-01",
                           "hourly": "", "daily": "precipitation_sum", **overrides})

    @staticmethod
    def response(part, **_):
        start = date.fromisoformat(part.params["start_date"])
        end = date.fromisoformat(part.params["end_date"])
        times = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
        data = {"daily": {"time": times, "precipitation_sum": [1] * len(times)}}
        return core.HttpResult(status=200, data=data)

    @patch("api_core.do_request")
    def test_merges_all_dates_including_leap_day(self, send):
        send.side_effect = self.response
        prep = self.archive()
        result = core.fetch_open_meteo_archive(prep, timeout=1)
        self.assertTrue(result.ok)
        self.assertEqual(send.call_count, 3)
        commands = core.build_request_preview(prep, "test").splitlines()
        self.assertEqual(sum(line.startswith("curl ") for line in commands), 3)
        times = result.data["daily"]["time"]
        self.assertEqual((times[0], times[-1], len(times)), ("2020-01-01", "2020-07-01", 183))
        self.assertIn("2020-02-29", times)
        self.assertEqual(len(result.tables["Histórico diário"][1]), 183)

    @patch("api_core.do_request")
    def test_rejects_a_missing_day_after_a_successful_block(self, send):
        def incomplete(part, **_):
            result = self.response(part)
            if part.params["start_date"] != "2020-01-01":
                for values in result.data["daily"].values():
                    del values[0]
            return result

        send.side_effect = incomplete
        result = core.fetch_open_meteo_archive(self.archive())
        self.assertFalse(result.ok)
        self.assertIsNone(result.data)
        self.assertIn("2020-03-31", result.error)

    def test_date_before_era5_coverage_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "1940"):
            self.archive(start_date="1939-12-31")

    @patch("api_core.do_request")
    def test_twelve_year_export_data_preserves_all_hours(self, send):
        def hourly_response(part, **kwargs):
            result = self.response(part, **kwargs)
            times = [f"{day}T{hour:02}:00" for day in result.data["daily"]["time"] for hour in range(24)]
            result.data["hourly"] = {"time": times, "temperature_2m": [20] * len(times)}
            return result

        send.side_effect = hourly_response
        progress = []
        result = core.fetch_open_meteo_archive(
            self.archive(start_date="2008-01-01", end_date="2019-12-31", hourly="temperature_2m"),
            progress=lambda done, total: progress.append((done, total)),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(send.call_count, 49)
        self.assertEqual(progress, [(i, 49) for i in range(50)])
        times = result.data["hourly"]["time"]
        self.assertEqual((times[0], times[-1]), ("2008-01-01T00:00", "2019-12-31T23:00"))
        self.assertEqual(len(set(times)), 105192)
        self.assertEqual(len(result.tables["Histórico horário"][1]), 105192)
        self.assertEqual(len(result.tables["Histórico diário"][1]), 4383)

    @patch("api_core.time.sleep")
    @patch("api_core.do_request")
    def test_retries_only_the_failed_block(self, send, sleep):
        calls = []

        def temporary_failure(part, **kwargs):
            calls.append(part.params["start_date"])
            if len(calls) == 2:
                return core.HttpResult(status=429, resp_headers={"retry-after": "2"})
            return self.response(part, **kwargs)

        send.side_effect = temporary_failure
        result = core.fetch_open_meteo_archive(self.archive())
        self.assertTrue(result.ok, result.error)
        self.assertEqual(calls, ["2020-01-01", "2020-03-31", "2020-03-31", "2020-06-29"])
        sleep.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
