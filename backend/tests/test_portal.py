from fastapi.testclient import TestClient

from app.main import app, platform, portal


client = TestClient(app)


def setup_function():
    platform.reset()
    portal.reset()


def test_portal_dashboard_has_reference_systems_and_five_dora_ready_shape():
    response = client.get('/portal/dashboard')

    assert response.status_code == 200
    body = response.json()
    assert body['kpis']['systems'] == 3
    assert body['kpis']['modules'] == 5
    assert len(body['pipelineActivity']) == 7
    assert {item['id'] for item in body['systems']} == {'netChat', 'PCTT', 'NocPro5'}

    dora = client.get('/modules/backend-api/dora')
    assert dora.status_code == 200
    assert len(dora.json()['metrics']) == 5


def test_portal_system_detail_and_module_overview_are_hierarchical():
    system = client.get('/systems/netChat')
    assert system.status_code == 200
    assert system.json()['moduleCount'] == 2
    assert {item['id'] for item in system.json()['modules']} == {'backend-api', 'web-client'}

    overview = client.get('/modules/backend-api/overview')
    assert overview.status_code == 200
    assert overview.json()['module']['name'] == 'Backend API'
    assert overview.json()['trends']['securityFindings']['critical'] == 0


def test_portal_can_create_system_and_attach_a_delivery_application_as_module():
    created_system = client.post('/systems', json={
        'id': 'billing-platform',
        'unit': 'Technology Platform Center',
        'description': 'Billing delivery system',
        'owner': 'Admin',
    })
    assert created_system.status_code == 201

    created_module = client.post('/systems/billing-platform/modules', headers={'Idempotency-Key': 'module-1'}, json={
        'name': 'billing-api',
        'repositoryUrl': 'https://github.com/example/billing-api',
        'pipelineTemplate': 'container-ci-cd-v1',
        'runtime': 'docker',
        'moduleType': 'Backend',
        'description': 'Billing API',
        'defaultEnvironment': 'dev',
    })
    assert created_module.status_code == 201
    assert created_module.json()['systemId'] == 'billing-platform'
    assert created_module.json()['applicationId']
    assert client.get('/systems/billing-platform').json()['moduleCount'] == 1


def test_module_pipeline_trigger_uses_the_delivery_application_contract():
    created = client.post('/systems/netChat/modules', headers={'Idempotency-Key': 'module-trigger-1'}, json={
        'name': 'trigger-api',
        'repositoryUrl': 'https://github.com/example/trigger-api',
        'pipelineTemplate': 'container-ci-cd-v1',
        'runtime': 'docker',
        'moduleType': 'Backend',
        'description': 'Trigger test module',
        'defaultEnvironment': 'dev',
    })
    assert created.status_code == 201
    triggered = client.post('/modules/trigger-api/pipeline-runs', json={
        'commitSha': 'abcdef1234567',
        'environment': 'dev',
    })
    assert triggered.status_code == 202
    assert triggered.json()['status'] == 'queued'
    assert triggered.json()['applicationId'] == created.json()['applicationId']


def test_production_requests_are_queryable_and_approval_is_a_portal_command():
    requests = client.get('/production-requests')
    assert requests.status_code == 200
    request_id = requests.json()[0]['id']

    approved = client.post(
        f'/production-requests/{request_id}/approve',
        json={'actor': 'mentor-reviewer', 'comment': 'approved for demo'},
    )
    assert approved.status_code == 202
    assert approved.json()['status'] == 'approved'
    assert approved.json()['comment'] == 'approved for demo'


def test_portal_returns_servers_and_scoped_audit_events():
    servers = client.get('/servers')
    assert servers.status_code == 200
    assert {'jenkins-a', 'jenkins-b', 'kind-local'} <= {item['id'] for item in servers.json()}

    audit = client.get('/audit-events?moduleId=backend-api')
    assert audit.status_code == 200
    assert all(item['target'] == 'backend-api' for item in audit.json())


def test_dcim_lookup_exposes_system_modules_and_deployment_targets():
    services = client.get('/dcim/services?query=netchat')
    assert services.status_code == 200
    assert services.json()['items'][0]['code'] == 'VTN_CNTT_MSS_686'

    available = client.get('/dcim/services?query=eoffice')
    assert available.status_code == 200
    assert available.json()['items'][0]['name'] == 'eOffice'

    created = client.post('/systems', json={
        'id': available.json()['items'][0]['name'],
        'unit': available.json()['items'][0]['tenant'],
        'description': available.json()['items'][0]['description'],
        'owner': 'Admin',
    })
    assert created.status_code == 201
    assert client.get('/systems/eOffice').json()['moduleCount'] == 0

    modules = client.get('/dcim/modules?systemId=netChat')
    assert modules.status_code == 200
    assert {'backend-api', 'web-client', 'notification-worker'} <= {
        item['id'] for item in modules.json()['items']
    }

    servers = client.get('/servers')
    assert any(item['hostname'] == 'srv-prod-01' for item in servers.json())
    assert all({'ipAddress', 'environment', 'systemId'} <= item.keys() for item in servers.json())


def test_pipeline_can_publish_ci_report_for_a_module_version(monkeypatch):
    monkeypatch.setenv('NETCI_PIPELINE_API_KEY', 'test-pipeline-key')
    payload = {
        'coverage': 87,
        'autoTest': 'passed',
        'sast': 'passed',
        'sastIssues': 0,
        'vulnerabilities': {'critical': 0, 'high': 0, 'medium': 2},
        'commit': 'a1c4e2f',
    }

    unauthorized = client.post('/modules/backend-api/versions/v2.4.1/ci-report', json=payload)
    assert unauthorized.status_code == 401

    response = client.post(
        '/modules/backend-api/versions/v2.4.1/ci-report',
        headers={'Authorization': 'Bearer test-pipeline-key'},
        json=payload,
    )
    assert response.status_code == 202
    assert response.json()['coverage'] == 87
    assert response.json()['vulnerabilities']['medium'] == 2

    versions = client.get('/modules/backend-api/versions').json()['items']
    version = next(item for item in versions if item['version'] == 'v2.4.1')
    assert version['ciReport']['commit'] == 'a1c4e2f'


def test_pipeline_api_key_has_no_implicit_production_default(monkeypatch):
    monkeypatch.delenv('NETCI_PIPELINE_API_KEY', raising=False)
    monkeypatch.setenv('NETCI_ENVIRONMENT', 'production')

    response = client.post(
        '/modules/backend-api/versions/v2.4.1/ci-report',
        headers={'Authorization': 'Bearer netci-local-pipeline-key'},
        json={
            'coverage': 87,
            'autoTest': 'passed',
            'sast': 'passed',
            'sastIssues': 0,
            'vulnerabilities': {'critical': 0, 'high': 0, 'medium': 2},
            'commit': 'a1c4e2f',
        },
    )

    assert response.status_code == 503
    assert response.json()['code'] == 'PIPELINE_KEY_NOT_CONFIGURED'


def test_manual_version_registration_is_visible_to_the_portal():
    created = client.post('/modules/backend-api/versions', json={
        'tag': 'v2.5.0',
        'gitTagUrl': 'https://git.example.net/netchat/backend-api/-/tags/v2.5.0',
        'artifactUrl': 'https://artifacts.example.net/netchat/backend-api/v2.5.0',
    })

    assert created.status_code == 201
    assert created.json()['version'] == 'v2.5.0'
    assert client.get('/modules/backend-api/versions').json()['items'][0]['version'] == 'v2.5.0'
