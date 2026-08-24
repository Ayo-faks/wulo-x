from __future__ import annotations

from utils.telemetry_config import _build_resource_attributes


def test_resource_attributes_include_only_bounded_opaque_identity(monkeypatch) -> None:
    values = {
        "SERVICE_NAME": "clinic-recall-api",
        "SERVICE_NAMESPACE": "wulo-x",
        "CONTAINER_APP_REPLICA_NAME": "replica-1",
        "ENVIRONMENT": "staging",
        "SERVICE_VERSION": "1.2.3",
        "GIT_SHA": "a" * 40,
        "IMAGE_TAG": "azd-deploy-123",
        "CONTAINER_APP_REVISION": "backend--revision-1",
        "CONFIG_HASH": "b" * 64,
        "PROGRAMME_UUID": "programme-1",
        "EXPERIMENT_ARM": "B",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    attributes = _build_resource_attributes()

    assert attributes == {
        "service.name": "clinic-recall-api",
        "service.namespace": "wulo-x",
        "service.instance.id": "replica-1",
        "service.environment": "staging",
        "service.version": "1.2.3",
        "deployment.id": "a" * 40,
        "container.image.tag": "azd-deploy-123",
        "container.app.revision": "backend--revision-1",
        "config.hash": "b" * 64,
        "programme.uuid": "programme-1",
        "experiment.arm": "B",
    }


def test_resource_attributes_omit_blanks_and_bound_values(monkeypatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_ID", " " * 10)
    monkeypatch.setenv("IMAGE_TAG", "x" * 200)
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("CONFIG_HASH", raising=False)
    monkeypatch.delenv("PROGRAMME_UUID", raising=False)
    monkeypatch.delenv("EXPERIMENT_ARM", raising=False)

    attributes = _build_resource_attributes()

    assert "deployment.id" not in attributes
    assert attributes["container.image.tag"] == "x" * 128


def test_resource_attributes_fall_back_to_container_app_identity(monkeypatch) -> None:
    for name in (
        "SERVICE_NAME",
        "ENVIRONMENT",
        "SERVICE_VERSION",
        "APP_VERSION",
        "DEPLOYMENT_ID",
        "GIT_SHA",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONTAINER_APP_NAME", "artagent-backend-9i3s6fse")
    monkeypatch.setenv("CONTAINER_APP_REVISION", "backend--observability")
    monkeypatch.setenv("AZURE_APPCONFIG_LABEL", "staging")

    attributes = _build_resource_attributes()

    assert attributes["service.name"] == "artagent-backend-9i3s6fse"
    assert attributes["service.environment"] == "staging"
    assert attributes["service.version"] == "backend--observability"
    assert attributes["deployment.id"] == "backend--observability"