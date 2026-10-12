import json

from app.adapters.dcim import HttpDcimCatalog, UnconfiguredDcimCatalog, build_dcim_catalog


def test_unconfigured_dcim_is_explicitly_empty(monkeypatch):
    monkeypatch.delenv("NETCI_DCIM_BASE_URL", raising=False)

    catalog = build_dcim_catalog()
    result = catalog.search_services("payments")

    assert isinstance(catalog, UnconfiguredDcimCatalog)
    assert result.status == "not_configured"
    assert result.items == []
    assert catalog.list_servers("payments", "billing").status == "not_configured"


def test_http_dcim_adapts_items_without_inventing_fields(monkeypatch):
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return json.dumps({"items": [{"id": "billing", "name": "Billing"}]}).encode()

    def urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr("app.adapters.dcim.urllib.request.urlopen", urlopen)
    catalog = HttpDcimCatalog("https://dcim.example/api", "secret")

    result = catalog.search_services("bill ing")

    assert result.status == "ready"
    assert result.items == [{"id": "billing", "name": "Billing"}]
    assert requests[0][0].full_url == "https://dcim.example/api/services?query=bill+ing"
    assert requests[0][0].get_header("Authorization") == "Bearer secret"

    servers = catalog.list_servers("pay ments", "billing/api")
    assert servers.items == [{"id": "billing", "name": "Billing"}]
    assert requests[1][0].full_url == (
        "https://dcim.example/api/systems/pay%20ments/servers?moduleId=billing%2Fapi"
    )
