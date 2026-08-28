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
    assert body['kpis']['modules'] == 3
    assert len(body['pipelineActivity']) == 7
    assert {item['id'] for item in body['systems']} == {'hello-container', 'hello-kubernetes', 'hello-systemd-go'}

    dora = client.get('/modules/hello-container/dora')
    assert dora.status_code == 200
    assert [metric['label'] for metric in dora.json()['metrics']] == [
        'Deployment Frequency',
        'Lead Time for Changes',
        'Change Failure Rate',
        'Time to Restore Service',
    ]


def test_portal_system_detail_and_module_overview_are_hierarchical():
    system = client.get('/systems/hello-container')
    assert system.status_code == 200
    assert system.json()['moduleCount'] == 1
    assert {item['id'] for item in system.json()['modules']} == {'hello-container'}

    overview = client.get('/modules/hello-container/overview')
    assert overview.status_code == 200
    assert overview.json()['module']['name'] == 'Hello Container'
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
            'servers': ['localhost'],
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
        'servers': ['localhost'],
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
            'servers': ['localhost'],
            'tasks': ['Health check'],
        }],
    })

    assert response.status_code == 404
    assert len(main.platform.list_applications()) == before


def test_module_environment_contract_rejects_mixed_runtime_and_incomplete_targets():
    created_system = client.post('/systems', json={
        'id': 'validation-system',
        'unit': 'Technology Platform Center',
        'description': 'Target validation system',
        'owner': 'Admin',
    })
    assert created_system.status_code == 201

    mixed_runtime = client.post('/systems/validation-system/modules', json={
        'name': 'mixed-runtime-api',
        'displayName': 'Mixed Runtime API',
        'repositoryUrl': 'https://github.com/example/mixed-runtime-api',
        'pipelineTemplate': 'container-ci-cd-v1',
        'runtime': 'docker',
        'moduleType': 'Backend',
        'description': 'Mixed runtime targets',
        'defaultEnvironment': 'dev',
        'deploymentEnvironments': [{
            'displayName': 'Development',
            'environment': 'dev',
            'runtime': 'kubernetes',
            'servers': [],
            'tasks': ['Health check'],
        }],
    })
    assert mixed_runtime.status_code in (400, 422)
    assert mixed_runtime.json()['code'] in ('VALIDATION_ERROR', 'INVALID_MODULE_ENVIRONMENT')

    missing_servers = client.post('/systems/validation-system/modules', json={
        'name': 'missing-servers-api',
        'displayName': 'Missing Servers API',
        'repositoryUrl': 'https://github.com/example/missing-servers-api',
        'pipelineTemplate': 'container-ci-cd-v1',
        'runtime': 'docker',
        'moduleType': 'Backend',
        'description': 'Missing server targets',
        'defaultEnvironment': 'dev',
        'deploymentEnvironments': [{
            'displayName': 'Development',
            'environment': 'dev',
            'runtime': 'docker',
            'servers': [],
            'tasks': ['Health check'],
        }],
    })
    assert missing_servers.status_code in (400, 422)
    assert missing_servers.json()['code'] in ('VALIDATION_ERROR', 'INVALID_MODULE_ENVIRONMENT')


def test_kubernetes_module_keeps_secret_reference_and_explicit_namespace():
    response = client.post('/systems/hello-kubernetes/modules', json={
        'name': 'notification-worker',
        'displayName': 'Notification Worker',
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
    created = client.post('/systems/hello-container/modules', headers={'Idempotency-Key': 'module-trigger-1'}, json={
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
            'servers': ['localhost'],
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
    triggered = client.post('/modules/hello-container/pipeline-runs', json={
        'commitSha': 'a1c4e2f',
        'branch': 'main',
        'environment': 'dev',
        'parameters': {'portalPipeline': 'ci'},
    })

    assert triggered.status_code == 202
    assert triggered.json()['status'] == 'queued'
    assert triggered.json()['applicationId'] == client.get('/modules/hello-container').json()['applicationId']
    assert triggered.json()['parameters'] == {'portalPipeline': 'ci'}


def test_production_requests_are_queryable_and_approval_is_a_portal_command():
    client.post('/modules/hello-container/versions', json={
        'tag': 'v1.0.0',
        'gitTagUrl': 'https://github.com/example/hello-container/tags/v1.0.0',
        'artifactUrl': 'https://github.com/example/hello-container/releases/v1.0.0',
    })
    req = client.post('/production-requests', headers={'Idempotency-Key': 'prod-request-test'}, json={
        'scheduledFor': '2026-08-30T03:00:00+07:00',
        'rollbackStrategy': 'automatic',
        'runAutomationTests': True,
        'modules': [{'moduleId': 'hello-container', 'version': 'v1.0.0', 'deploymentOrder': 1}],
    })
    assert req.status_code == 201
    request_id = req.json()['id']

    approved = client.post(
        f'/production-requests/{request_id}/approve',
        json={'actor': 'mentor-reviewer', 'comment': 'approved for demo'},
    )
    assert approved.status_code == 202
    assert approved.json()['status'] == 'approved'
    assert approved.json()['comment'] == 'approved for demo'


def test_portal_can_create_a_scheduled_multi_module_production_request():
    client.post('/modules/hello-container/versions', json={
        'tag': 'v1.0.0',
        'gitTagUrl': 'https://github.com/example/hello-container/tags/v1.0.0',
        'artifactUrl': 'https://github.com/example/hello-container/releases/v1.0.0',
    })
    client.post('/modules/hello-kubernetes/versions', json={
        'tag': 'v1.0.0',
        'gitTagUrl': 'https://github.com/example/hello-kubernetes/tags/v1.0.0',
        'artifactUrl': 'https://github.com/example/hello-kubernetes/releases/v1.0.0',
    })
    response = client.post('/production-requests', headers={'Idempotency-Key': 'prod-request-1'}, json={
        'scheduledFor': '2026-08-30T03:00:00+07:00',
        'rollbackStrategy': 'automatic',
        'runAutomationTests': True,
        'modules': [
            {'moduleId': 'hello-container', 'version': 'v1.0.0', 'deploymentOrder': 1},
            {'moduleId': 'hello-kubernetes', 'version': 'v1.0.0', 'deploymentOrder': 2},
        ],
    })

    assert response.status_code == 201
    body = response.json()
    assert body['status'] == 'waiting_approval'
    assert body['requestedBy'] == 'anonymous'
    assert body['scheduledFor'] == '2026-08-30T03:00:00+07:00'
    assert body['rollbackStrategy'] == 'automatic'
    assert body['runAutomationTests'] is True
    assert body['modules'] == [
        {'moduleId': 'hello-container', 'moduleName': 'Hello Container', 'version': 'v1.0.0', 'deploymentOrder': 1},
        {'moduleId': 'hello-kubernetes', 'moduleName': 'Hello Kubernetes', 'version': 'v1.0.0', 'deploymentOrder': 2},
    ]
    assert any(item['id'] == body['id'] for item in client.get('/production-requests').json())


def test_production_request_creation_is_idempotent_for_the_same_key():
    client.post('/modules/hello-container/versions', json={
        'tag': 'v1.0.0',
        'gitTagUrl': 'https://github.com/example/hello-container/tags/v1.0.0',
        'artifactUrl': 'https://github.com/example/hello-container/releases/v1.0.0',
    })
    payload = {
        'requestedBy': 'Admin',
        'scheduledFor': '2026-08-30T03:00:00+07:00',
        'rollbackStrategy': 'automatic',
        'runAutomationTests': True,
        'modules': [{'moduleId': 'hello-container', 'version': 'v1.0.0', 'deploymentOrder': 1}],
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
    assert {'localhost', 'jenkins-local', 'kind-local'} <= {item['id'] for item in servers.json()}

    audit = client.get('/audit-events?moduleId=hello-container')
    assert audit.status_code == 200
    assert all(item['target'] == 'hello-container' for item in audit.json())


def test_dcim_lookup_exposes_system_modules_and_deployment_targets():
    services = client.get('/dcim/services?query=hello')
    assert services.status_code == 200
    assert services.json()['items'][0]['code'] == 'VTN_HELLO-CONTAINER'

    created = client.post('/systems', json={
        'id': 'eOffice',
        'unit': 'Digital Office',
        'description': 'Enterprise office',
        'owner': 'Admin',
    })
    assert created.status_code == 201
    assert client.get('/systems/eOffice').json()['moduleCount'] == 0

    modules = client.get('/dcim/modules?systemId=hello-container')
    assert modules.status_code == 200
    assert {'hello-container'} <= {item['id'] for item in modules.json()['items']}

    servers = client.get('/servers')
    assert any(item['hostname'] == 'localhost' for item in servers.json())
    assert all({'ipAddress', 'environment', 'systemId'} <= item.keys() for item in servers.json())


def test_pipeline_can_publish_ci_report_for_a_module_version(monkeypatch):
    client.post('/modules/hello-container/versions', json={
        'tag': 'v1.0.0',
        'gitTagUrl': 'https://github.com/example/hello-container/tags/v1.0.0',
        'artifactUrl': 'https://github.com/example/hello-container/releases/v1.0.0',
    })
    monkeypatch.setenv('NETCI_PIPELINE_API_KEY', 'test-pipeline-key')
    payload = {
        'coverage': 87,
        'autoTest': 'passed',
        'sast': 'passed',
        'sastIssues': 0,
        'vulnerabilities': {'critical': 0, 'high': 0, 'medium': 2},
        'commit': 'a1c4e2f',
    }

    unauthorized = client.post('/modules/hello-container/versions/v1.0.0/ci-report', json=payload)
    assert unauthorized.status_code == 401

    response = client.post(
        '/modules/hello-container/versions/v1.0.0/ci-report',
        headers={'Authorization': 'Bearer test-pipeline-key'},
        json=payload,
    )
    assert response.status_code == 202
    assert response.json()['coverage'] == 87
    assert response.json()['vulnerabilities']['medium'] == 2

    versions = client.get('/modules/hello-container/versions').json()['items']
    version = next(item for item in versions if item['version'] == 'v1.0.0')
    assert version['ciReport']['commit'] == 'a1c4e2f'


def test_pipeline_api_key_has_no_implicit_production_default(monkeypatch):
    client.post('/modules/hello-container/versions', json={
        'tag': 'v1.0.0',
        'gitTagUrl': 'https://github.com/example/hello-container/tags/v1.0.0',
        'artifactUrl': 'https://github.com/example/hello-container/releases/v1.0.0',
    })
    monkeypatch.delenv('NETCI_PIPELINE_API_KEY', raising=False)
    monkeypatch.setenv('NETCI_ENVIRONMENT', 'production')

    response = client.post(
        '/modules/hello-container/versions/v1.0.0/ci-report',
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
    created = client.post('/modules/hello-container/versions', json={
        'tag': 'v2.5.0',
        'gitTagUrl': 'https://github.com/example/hello-container/tags/v2.5.0',
        'artifactUrl': 'https://github.com/example/hello-container/releases/v2.5.0',
    })

    assert created.status_code == 201
    assert created.json()['version'] == 'v2.5.0'
    assert client.get('/modules/hello-container/versions').json()['items'][0]['version'] == 'v2.5.0'


def test_version_registration_fails_closed_without_leaving_a_ghost_version(monkeypatch):
    class FailingPortalStore:
        def upsert_version(self, *_args):
            raise RuntimeError('database offline')

    monkeypatch.setattr(main.portal, 'store', FailingPortalStore())

    response = client.post('/modules/hello-container/versions', json={
        'tag': 'v9.9.9',
        'gitTagUrl': 'https://github.com/example/hello-container/tags/v9.9.9',
        'artifactUrl': 'https://github.com/example/hello-container/releases/v9.9.9',
    })

    assert response.status_code == 503
    assert response.json()['code'] == 'PERSISTENCE_UNAVAILABLE'
    assert 'v9.9.9' not in [item['version'] for item in client.get('/modules/hello-container/versions').json()['items']]


def test_ci_report_fails_closed_without_mutating_the_projection(monkeypatch):
    client.post('/modules/hello-container/versions', json={
        'tag': 'v1.0.0',
        'gitTagUrl': 'https://github.com/example/hello-container/tags/v1.0.0',
        'artifactUrl': 'https://github.com/example/hello-container/releases/v1.0.0',
    })
    class FailingPortalStore:
        def upsert_version(self, *_args):
            raise RuntimeError('database offline')

    monkeypatch.setenv('NETCI_PIPELINE_API_KEY', 'test-pipeline-key')
    monkeypatch.setattr(main.portal, 'store', FailingPortalStore())

    response = client.post(
        '/modules/hello-container/versions/v1.0.0/ci-report',
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
