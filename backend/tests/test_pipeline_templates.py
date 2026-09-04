"""Tests for Golden Path Pipeline Templates."""

import pytest
from app.catalog.templates import (
    PipelineTemplateEngine,
    TemplateValidationError,
    seed_builtin_templates,
)
from app.store.memory import InMemoryDatabase


@pytest.fixture()
def template_engine():
    db = InMemoryDatabase()
    with db.transaction() as session:
        seed_builtin_templates(session)
        yield PipelineTemplateEngine(session), session


def test_seed_and_list_builtin_templates(template_engine):
    engine, session = template_engine
    templates = session.list_catalog_templates()
    assert len(templates) >= 3
    ids = {t.id for t in templates}
    assert "fastapi-service" in ids
    assert "go-microservice" in ids
    assert "react-spa" in ids


def test_register_and_instantiate_template(template_engine):
    engine, session = template_engine
    engine.register_template(
        template_id="custom-worker",
        version="v1.0.0",
        name="Custom Worker",
        description="Async task worker",
        category="worker",
        parameters_schema={
            "type": "object",
            "required": ["concurrency"],
            "properties": {
                "concurrency": {"type": "integer", "default": 4},
                "queue_name": {"type": "string", "default": "default-queue"},
            },
        },
        pipeline_definition={
            "runtime": "docker",
            "stages": [
                {"name": "test", "command": "pytest", "stage_id": "test"},
                {"name": "build", "command": "docker build -t worker --build-arg CONC={{ concurrency }} .", "stage_id": "build"},
            ],
        },
    )

    inst = engine.instantiate(
        template_id="custom-worker",
        version="v1.0.0",
        application_name="order-worker",
        owning_team="order-team",
        parameters={"concurrency": 8, "queue_name": "order-tasks"},
    )
    assert inst.application_name == "order-worker"
    assert inst.stages[1]["command"] == "docker build -t worker --build-arg CONC=8 ."
    assert inst.pipeline_config["parameters"]["queue_name"] == "order-tasks"


def test_instantiate_missing_required_parameter(template_engine):
    engine, session = template_engine
    with pytest.raises(TemplateValidationError, match="missing required parameter: 'port'"):
        engine.instantiate(
            template_id="fastapi-service",
            version="v1.0.0",
            application_name="payments-api",
            owning_team="pay-team",
            parameters={},
        )


def test_instantiate_invalid_parameter_type_or_enum(template_engine):
    engine, session = template_engine
    with pytest.raises(TemplateValidationError, match="must be an integer"):
        engine.instantiate(
            template_id="fastapi-service",
            version="v1.0.0",
            application_name="payments-api",
            owning_team="pay-team",
            parameters={"port": "not-a-number"},
        )

    with pytest.raises(TemplateValidationError, match="not in allowed enum"):
        engine.instantiate(
            template_id="fastapi-service",
            version="v1.0.0",
            application_name="payments-api",
            owning_team="pay-team",
            parameters={"port": 8000, "python_version": "2.7"},
        )
