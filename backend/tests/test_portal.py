from fastapi.testclient import TestClient

import app.main as main
from app.main import app


client = TestClient(app)


def setup_function():
    main.platform.reset()
    main.portal.reset()


def test_portal_dashboard_has_reference_systems_and_four_dora_metrics():
    response = client.get('/portal/dashboard')

    assert response.status_code == 200
    body = response.json()
    assert body['kpis']['systems'] == 3
    assert body['kpis']['modules'] == 5
    assert len(body['pipelineActivity']) == 7
    assert {item['id'] for item in body['systems']} == {'netChat', 'PCTT', 'NocPro5'}

    dora = client.get('/modules/backend-api/dora')
    assert dora.status_code == 200
    assert [metric['label'] for metric in dora.json()['metrics']] == [
        'Deployment Frequency',
        'Lead Time for Changes',
        'Change Failure Rate',
        'Time to Restore Service',
    ]


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
        'displayName': 'Billing Service API',
        'repositoryUrl': 'https://github.com/example/billing-api',
        'pipelineTemplate': 'container-ci-cd-v1',
        'runtime': 'docker',
        'moduleType': 'Backend',
        'description': 'Billing API',
        'defaultEnvironment': 'dev',
        'stages': ['checkout', 'unit-test', 'build', 'publish'],
        'pipelineConfig': {
            'runner': 'docker-linux',
            'strategy': 'Gitflow',
            'pipelines': {'CI': {'branch': 'main', 'coverageReportPath': 'coverage/lcov.info', 'stages': ['checkout', 'unit-test']}},
        },
        'deploymentEnvironments': [{
            'displayName': 'Development',
            'environment': 'dev',
            'runtime': 'docker',
            'servers': ['srv-dev-01'],
            'tasks': ['Restart service', 'Health check'],
            'taskSettings': {'healthCheck': {'script': 'curl -f http://localhost/health', 'retries': 3, 'delay': '10s'}},
        }],
    })
    assert created_module.status_code == 201
    assert created_module.json()['systemId'] == 'billing-platform'
    assert created_module.json()['name'] == 'Billing Service API'
    assert created_module.json()['applicationId']
    assert created_module.json()['deploymentEnvironments'] == [{
        'displayName': 'Development',
        'environment': 'dev',
        'runtime': 'docker',
        'servers': ['srv-dev-01'],
        'tasks': ['Restart service', 'Health check'],
        'taskSettings': {'healthCheck': {'script': 'curl -f http://localhost/health', 'retries': 3, 'delay': '10s'}},
        'kubeconfigRef': None,
        'namespace': None,
    }]
    assert created_module.json()['pipelineConfig']['runner'] == 'docker-linux'
    assert client.get('/systems/billing-platform').json()['moduleCount'] == 1
    application = next(item for item in client.get('/applications').json() if item['id'] == created_module.json()['applicationId'])
    assert application['stages'] == ['checkout', 'unit-test', 'build', 'publish']


def test_invalid_system_does_not_provision_an_orphan_delivery_application():
    before = len(main.platform.list_applications())

    response = client.post('/systems/missing-system/modules', json={
        'name': 'orphan-api',
        'displayName': 'Orphan API',
        'repositoryUrl': 'https://github.com/example/orphan-api',
        'pipelineTemplate': 'container-ci-cd-v1',
        'runtime': 'docker',
        'moduleType': 'Backend',
        'description': 'Must not be provisioned',
        'defaultEnvironment': 'dev',
        'stages': ['checkout', 'build'],
        'deploymentEnvironments': [{
            'displayName': 'Development',
            'environment': 'dev',
            'runtime': 'docker',
            'servers': ['srv-dev-01'],
            'tasks': ['Health check'],
        }],
    })

    assert response.status_code == 404
    assert len(main.platform.list_applications()) == before


def test_module_environment_contract_rejects_mixed_runtime_and_incomplete_targets():
    base_payload = {
        'name': 'invalid-target-module',
        'repositoryUrl': 'https://github.com/example/invalid-target-module',
        'pipelineTemplate': 'container-ci-cd-v1',
        'runtime': 'docker',
        'moduleType': 'Backend',
        'description': 'Invalid environment contract test',
        'defaultEnvironment': 'dev',
    }

    mixed_runtime = client.post('/systems/netChat/modules', json={
        **base_payload,
        'deploymentEnvironments': [{
            'displayName': 'Development',
            'environment': 'dev',
            'runtime': 'kubernetes',
            'servers': [],
            'tasks': [],
            'kubeconfigRef': 'netci-dev-kubeconfig',
            'namespace': 'dev',
        }],
    })
    assert mixed_runtime.status_code == 422
    assert mixed_runtime.json()['code'] == 'VALIDATION_ERROR'

    missing_servers = client.post('/systems/netChat/modules', json={
        **base_payload,
        'deploymentEnvironments': [{
            'displayName': 'Development',
            'environment': 'dev',
            'runtime': 'docker',
            'servers': [],
            'tasks': [],
        }],
    })
    assert missing_servers.status_code == 422
    assert missing_servers.json()['code'] == 'VALIDATION_ERROR'


def test_kubernetes_module_keeps_secret_reference_and_explicit_namespace():
    response = client.post('/systems/netChat/modules', json={
        'name': 'notification-worker',
        'repositoryUrl': 'https://github.com/example/notification-worker',
        'pipelineTemplate': 'kubernetes-ci-cd-v1',
        'runtime': 'kubernetes',
        'moduleType': 'Worker',
        'description': 'Notification worker',
        'defaultEnvironment': 'staging',
        'deploymentEnvironments': [{
            'displayName': 'Pre-production',
            'environment': 'staging',
            'runtime': 'kubernetes',
            'servers': [],
            'tasks': ['Health check'],
            'kubeconfigRef': 'netci-staging-kubeconfig',
            'namespace': 'staging',
        }],
    })

    assert response.status_code == 201
    assert response.json()['runtime'] == 'kubernetes'
    assert response.json()['deploymentEnvironments'][0]['kubeconfigRef'] == 'netci-staging-kubeconfig'
    assert response.json()['deploymentEnvironments'][0]['namespace'] == 'staging'


def test_module_pipeline_trigger_uses_the_delivery_application_contract():
    created = client.post('/systems/netChat/modules', headers={'Idempotency-Key': 'module-trigger-1'}, json={
        'name': 'trigger-api',
        'repositoryUrl': 'https://github.com/example/trigger-api',
        'pipelineTemplate': 'container-ci-cd-v1',
        'runtime': 'docker',
        'moduleType': 'Backend',
        'description': 'Trigger test module',
        'defaultEnvironment': 'dev',
        'deploymentEnvironments': [{
            'displayName': 'Development',
            'environment': 'dev',
            'runtime': 'docker',
            'servers': ['srv-dev-01'],
            'tasks': [],
        }],
    })
    assert created.status_code == 201
    triggered = client.post('/modules/trigger-api/pipeline-runs', json={
        'commitSha': 'abcdef1234567',
        'environment': 'dev',
    })
    assert triggered.status_code == 202
    assert triggered.json()['status'] == 'queued'
    assert triggered.json()['applicationId'] == created.json()['applicationId']


def test_reference_module_is_provisioned_and_can_trigger_the_demo_pipeline():
    triggered = client.post('/modules/backend-api/pipeline-runs', json={
        'commitSha': 'a1c4e2f',
        'branch': 'main',
        'environment': 'dev',
        'parameters': {'portalPipeline': 'ci'},
    })

    assert triggered.status_code == 202
    assert triggered.json()['status'] == 'queued'
    assert triggered.json()['applicationId'] == client.get('/modules/backend-api').json()['applicationId']
    assert triggered.json()['parameters'] == {'portalPipeline': 'ci'}


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


def test_portal_can_create_a_scheduled_multi_module_production_request():
    response = client.post('/production-requests', headers={'Idempotency-Key': 'prod-request-1'}, json={
        'scheduledFor': '2026-08-30T03:00:00+07:00',
        'rollbackStrategy': 'automatic',
        'runAutomationTests': True,
        'modules': [
            {'moduleId': 'backend-api', 'version': 'v2.4.1', 'deploymentOrder': 1},
            {'moduleId': 'web-client', 'version': 'v1.9.2', 'deploymentOrder': 2},
        ],
    })

    assert response.status_code == 201
    body = response.json()
    assert body['status'] == 'waiting_approval'
    assert body['requestedBy'] == 'anonymous'  # from the credential, not the body
    assert body['scheduledFor'] == '2026-08-30T03:00:00+07:00'
    assert body['rollbackStrategy'] == 'automatic'
    assert body['runAutomationTests'] is True
    assert body['modules'] == [
        {'moduleId': 'backend-api', 'moduleName': 'Backend API', 'version': 'v2.4.1', 'deploymentOrder': 1},
        {'moduleId': 'web-client', 'moduleName': 'Web Client', 'version': 'v1.9.2', 'deploymentOrder': 2},
    ]
    assert any(item['id'] == body['id'] for item in client.get('/production-requests').json())


def test_production_request_creation_is_idempotent_for_the_same_key():
    payload = {
        'requestedBy': 'Admin',
        'scheduledFor': '2026-08-30T03:00:00+07:00',
        'rollbackStrategy': 'automatic',
        'runAutomationTests': True,
        'modules': [{'moduleId': 'backend-api', 'version': 'v2.4.1', 'deploymentOrder': 1}],
    }

    first = client.post('/production-requests', headers={'Idempotency-Key': 'prod-request-repeat'}, json=payload)
    repeated = client.post('/production-requests', headers={'Idempotency-Key': 'prod-request-repeat'}, json=payload)

    assert first.status_code == 201
    assert repeated.status_code == 201
    assert repeated.json()['id'] == first.json()['id']
    assert sum(item['id'] == first.json()['id'] for item in client.get('/production-requests').json()) == 1


def test_portal_write_fails_closed_when_configured_persistence_is_unavailable(monkeypatch):
    class FailingPortalStore:
        def insert_system(self, record):
            raise OSError('database connection refused')

    monkeypatch.setattr(main.portal, 'store', FailingPortalStore())

    response = client.post('/systems', json={
        'id': 'persistence-check',
        'unit': 'Technology Platform Center',
        'description': 'Must not survive a failed write',
        'owner': 'Admin',
    })

    assert response.status_code == 503
    assert response.json()['code'] == 'PERSISTENCE_UNAVAILABLE'
    assert client.get('/systems/persistence-check').status_code == 404


def test_portal_persistence_bootstrap_failure_is_visible_in_health(monkeypatch):
    class FailingPortalStore:
        last_error = 'database connection refused'

        def bootstrap(self):
            return False

    monkeypatch.setattr(main.portal, 'store', FailingPortalStore())
    main.portal.reset()

    health = client.get('/healthz')

    # A configured store that cannot be reached must not read as healthy: the status
    # code has to carry the failure too, or a load balancer keeps sending writes here.
    assert health.status_code == 503
    assert health.json()['status'] == 'degraded'
    assert health.json()['dependencies']['portalPersistence'] == {
        'mode': 'postgresql',
        'status': 'degraded',
        'message': 'database connection refused',
    }


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


def test_version_registration_fails_closed_without_leaving_a_ghost_version(monkeypatch):
    class FailingPortalStore:
        def upsert_version(self, *_args):
            raise RuntimeError('database offline')

    monkeypatch.setattr(main.portal, 'store', FailingPortalStore())

    response = client.post('/modules/backend-api/versions', json={
        'tag': 'v9.9.9',
        'gitTagUrl': 'https://git.example.net/netchat/backend-api/-/tags/v9.9.9',
        'artifactUrl': 'https://artifacts.example.net/netchat/backend-api/v9.9.9',
    })

    assert response.status_code == 503
    assert response.json()['code'] == 'PERSISTENCE_UNAVAILABLE'
    assert 'v9.9.9' not in [item['version'] for item in client.get('/modules/backend-api/versions').json()['items']]


def test_ci_report_fails_closed_without_mutating_the_projection(monkeypatch):
    class FailingPortalStore:
        def upsert_version(self, *_args):
            raise RuntimeError('database offline')

    monkeypatch.setenv('NETCI_PIPELINE_API_KEY', 'test-pipeline-key')
    monkeypatch.setattr(main.portal, 'store', FailingPortalStore())

    response = client.post(
        '/modules/backend-api/versions/v2.4.1/ci-report',
        headers={'Authorization': 'Bearer test-pipeline-key'},
        json={
            'coverage': 10,
            'autoTest': 'failed',
            'sast': 'failed',
            'sastIssues': 2,
            'vulnerabilities': {'critical': 1, 'high': 2, 'medium': 3},
            'commit': 'deadbeef',
        },
    )

    assert response.status_code == 503
    version = next(item for item in client.get('/modules/backend-api/versions').json()['items'] if item['version'] == 'v2.4.1')
    assert version['ciReport']['commit'] == 'a1c4e2f'
