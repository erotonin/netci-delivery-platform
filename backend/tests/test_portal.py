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
