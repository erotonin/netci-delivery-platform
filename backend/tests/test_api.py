from fastapi.testclient import TestClient

from app.main import app, applications, pipeline_runs


client = TestClient(app)


def setup_function():
    applications.clear()
    pipeline_runs.clear()


def test_healthz():
    response = client.get('/healthz')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'


def test_stage_catalog_contains_three_templates():
    response = client.get('/stage-catalog')
    assert response.status_code == 200
    template_ids = {item['id'] for item in response.json()['templates']}
    assert template_ids == {
        'container-ci-cd-v1',
        'kubernetes-ci-cd-v1',
        'systemd-ansible-ci-cd-v1',
    }


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
