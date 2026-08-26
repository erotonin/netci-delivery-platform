import pytest
from fastapi.testclient import TestClient

from app.main import app, platform


client = TestClient(app)


def application_payload(name: str = 'hello-netci', runtime: str = 'docker', template: str = 'container-ci-cd-v1'):
    return {
        'name': name,
        'repositoryUrl': f'https://github.com/example/{name}',
        'pipelineTemplate': template,
        'runtime': runtime,
    }


def setup_function():
    platform.reset()


def test_healthz():
    response = client.get('/healthz', headers={'X-Correlation-Id': 'health-check-1'})
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'
    assert response.headers['X-Correlation-Id'] == 'health-check-1'


def test_stage_catalog_contains_three_templates():
    response = client.get('/stage-catalog')
    assert response.status_code == 200
    template_ids = {item['id'] for item in response.json()['templates']}
    assert template_ids == {
        'container-ci-cd-v1',
        'kubernetes-ci-cd-v1',
        'systemd-ansible-ci-cd-v1',
    }


def test_validation_errors_use_the_public_error_contract_and_generated_correlation_id():
    response = client.post('/applications', json={'name': 'INVALID NAME'})

    assert response.status_code == 422
    assert response.json()['code'] == 'VALIDATION_ERROR'
    assert response.json()['message'] == 'request validation failed'
    assert response.json()['correlationId'] == response.headers['X-Correlation-Id']


def test_oversized_correlation_id_is_rejected_with_a_fresh_valid_id():
    response = client.get('/healthz', headers={'X-Correlation-Id': 'x' * 129})

    assert response.status_code == 422
    assert response.json()['code'] == 'VALIDATION_ERROR'
    assert response.json()['correlationId'] == response.headers['X-Correlation-Id']
    assert len(response.headers['X-Correlation-Id']) <= 128


def test_create_application_and_start_pipeline():
    create = client.post('/applications', json={
        'name': 'hello-netci',
        'repositoryUrl': 'https://github.com/example/hello-netci',
        'pipelineTemplate': 'container-ci-cd-v1',
        'runtime': 'docker',
        'stages': ['checkout', 'unit-test', 'build'],
    })
    assert create.status_code == 201
    application_id = create.json()['id']

    run = client.post(f'/applications/{application_id}/pipeline-runs', headers={'X-Correlation-Id': 'test-correlation'}, json={
        'commitSha': 'abcdef1234567',
        'environment': 'dev',
    })
    assert run.status_code == 202
    assert run.json()['status'] == 'queued'


def test_duplicate_application_is_rejected():
    payload = {
        'name': 'hello-netci',
        'repositoryUrl': 'https://github.com/example/hello-netci',
        'pipelineTemplate': 'container-ci-cd-v1',
        'runtime': 'docker',
    }
    assert client.post('/applications', json=payload).status_code == 201
    assert client.post('/applications', json=payload).status_code == 409


def test_create_application_replays_the_original_result_for_the_same_idempotency_key():
    headers = {'Idempotency-Key': 'create-application-1'}

    first = client.post('/applications', headers=headers, json=application_payload())
    replay = client.post('/applications', headers=headers, json=application_payload())

    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert len(client.get('/applications').json()) == 1


def test_idempotency_keys_are_scoped_to_the_operation_and_resource():
    headers = {'Idempotency-Key': 'shared-retry-key'}
    application = client.post('/applications', headers=headers, json=application_payload()).json()

    run = client.post(
        f"/applications/{application['id']}/pipeline-runs",
        headers=headers,
        json={'commitSha': 'abcdef1234567', 'environment': 'dev'},
    )

    assert run.status_code == 202
    assert run.json()['applicationId'] == application['id']


def test_start_pipeline_replays_once_and_rejects_key_reuse_with_a_different_request():
    application = client.post('/applications', json=application_payload()).json()
    path = f"/applications/{application['id']}/pipeline-runs"
    headers = {'Idempotency-Key': 'pipeline-retry-1'}
    payload = {'commitSha': 'abcdef1234567', 'environment': 'dev'}

    first = client.post(path, headers=headers, json=payload)
    replay = client.post(path, headers=headers, json=payload)
    conflict = client.post(path, headers=headers, json={**payload, 'commitSha': 'fffffff1234567'})

    assert first.status_code == replay.status_code == 202
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json()['code'] == 'IDEMPOTENCY_KEY_REUSED'


def test_idempotency_key_length_is_enforced_by_the_http_contract():
    response = client.post(
        '/applications',
        headers={'Idempotency-Key': 'x' * 129},
        json=application_payload(),
    )

    assert response.status_code == 422
    assert response.json()['code'] == 'VALIDATION_ERROR'


def test_pipeline_run_preserves_the_correlation_id_in_status_and_logs():
    application = client.post('/applications', json=application_payload()).json()
    response = client.post(
        f"/applications/{application['id']}/pipeline-runs",
        headers={'X-Correlation-Id': 'pipeline-correlation-1'},
        json={'commitSha': 'abcdef1234567', 'environment': 'staging'},
    )

    assert response.status_code == 202
    assert response.json()['correlationId'] == 'pipeline-correlation-1'
    assert response.json()['environment'] == 'staging'
    logs = client.get(f"/pipeline-runs/{response.json()['id']}/logs")
    assert logs.json()['correlationId'] == 'pipeline-correlation-1'
    assert 'correlationId=pipeline-correlation-1' in logs.json()['lines'][0]


def test_unknown_template_returns_a_correlated_domain_error():
    response = client.post(
        '/applications',
        json=application_payload(template='does-not-exist'),
    )

    assert response.status_code == 422
    assert response.json() == {
        'code': 'TEMPLATE_NOT_FOUND',
        'message': 'pipeline template does not exist',
        'correlationId': response.headers['X-Correlation-Id'],
    }


@pytest.mark.parametrize('stages', [
    ['checkout', 'unknown-stage'],
    ['checkout', 'build', 'build'],
    ['build', 'checkout'],
])
def test_application_rejects_unknown_duplicate_or_out_of_order_stages(stages):
    response = client.post('/applications', json={**application_payload(), 'stages': stages})

    assert response.status_code == 422
    assert response.json()['code'] == 'INVALID_STAGES'


def test_successful_production_ci_creates_a_deployment_waiting_for_approval():
    application = client.post(
        '/applications',
        json={**application_payload(), 'defaultEnvironment': 'prod'},
    ).json()
    run = client.post(
        f"/applications/{application['id']}/pipeline-runs",
        json={'commitSha': 'abcdef1234567', 'environment': 'prod'},
    ).json()
    path = f"/pipeline-runs/{run['id']}/ci-result"

    started = client.post(path, json={'status': 'running', 'logLines': ['agent allocated']})
    completed = client.post(
        path,
        json={
            'status': 'succeeded',
            'artifactDigest': f"sha256:{'a' * 64}",
            'logLines': ['artifact published'],
        },
    )

    assert started.status_code == completed.status_code == 202
    assert completed.json()['pipelineRun']['status'] == 'waiting_approval'
    assert completed.json()['pipelineRun']['artifactDigest'] == f"sha256:{'a' * 64}"
    assert completed.json()['deployment']['status'] == 'pending_approval'
    assert completed.json()['deployment']['pipelineRunId'] == run['id']


def test_approval_resumes_the_waiting_production_pipeline():
    application = client.post('/applications', json=application_payload()).json()
    run = client.post(
        f"/applications/{application['id']}/pipeline-runs",
        json={'commitSha': 'abcdef1234567', 'environment': 'prod'},
    ).json()
    client.post(f"/pipeline-runs/{run['id']}/ci-result", json={'status': 'running'})
    completed = client.post(
        f"/pipeline-runs/{run['id']}/ci-result",
        json={'status': 'succeeded', 'artifactDigest': f"sha256:{'b' * 64}"},
    ).json()

    approved = client.post(
        f"/deployments/{completed['deployment']['id']}/approve",
        json={'actor': 'mentor-reviewer', 'comment': 'approved for production'},
    )

    assert approved.status_code == 202
    assert approved.json()['status'] == 'deploying'
    assert approved.json()['approvedBy'] == 'mentor-reviewer'
    assert client.get(f"/pipeline-runs/{run['id']}").json()['status'] == 'running'


def test_deployment_result_and_rollback_complete_the_public_lifecycle():
    application = client.post('/applications', json=application_payload()).json()
    run = client.post(
        f"/applications/{application['id']}/pipeline-runs",
        json={'commitSha': 'abcdef1234567', 'environment': 'staging'},
    ).json()
    client.post(f"/pipeline-runs/{run['id']}/ci-result", json={'status': 'running'})
    ci_result = client.post(
        f"/pipeline-runs/{run['id']}/ci-result",
        json={'status': 'succeeded', 'artifactDigest': f"sha256:{'c' * 64}"},
    ).json()
    deployment_id = ci_result['deployment']['id']

    healthy = client.post(
        f'/deployments/{deployment_id}/result',
        json={'status': 'healthy', 'message': 'health check passed'},
    )
    rolled_back = client.post(
        f'/deployments/{deployment_id}/rollback',
        json={'targetArtifactDigest': f"sha256:{'d' * 64}", 'reason': 'mentor drill'},
    )

    assert healthy.status_code == rolled_back.status_code == 202
    assert healthy.json()['status'] == 'healthy'
    assert client.get(f"/pipeline-runs/{run['id']}").json()['status'] == 'rolled_back'
    assert client.get(f'/deployments/{deployment_id}').json()['status'] == 'rolled_back'
    assert rolled_back.json()['status'] == 'rolled_back'
    assert rolled_back.json()['artifactDigest'] == f"sha256:{'d' * 64}"
    assert rolled_back.json()['previousArtifactDigest'] == f"sha256:{'c' * 64}"


def test_successful_ci_requires_an_exact_immutable_digest():
    application = client.post('/applications', json=application_payload()).json()
    run = client.post(
        f"/applications/{application['id']}/pipeline-runs",
        json={'commitSha': 'abcdef1234567', 'environment': 'dev'},
    ).json()
    path = f"/pipeline-runs/{run['id']}/ci-result"
    client.post(path, json={'status': 'running'})

    response = client.post(path, json={'status': 'succeeded', 'artifactDigest': 'sha256:not-a-digest'})

    assert response.status_code == 422
    assert response.json()['code'] == 'IMMUTABLE_ARTIFACT_REQUIRED'
    assert client.get(f"/pipeline-runs/{run['id']}").json()['status'] == 'running'
