import pytest
from apps.artagent.backend.api.v1.endpoints.calls import router
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.state.acs_caller = None
    app.include_router(router, prefix="/api/v1/calls")
    return TestClient(app)


def test_answer_endpoint_handles_eventgrid_validation_before_acs_init(client: TestClient) -> None:
    response = client.post(
        "/api/v1/calls/answer",
        json=[
            {
                "eventType": "Microsoft.EventGrid.SubscriptionValidationEvent",
                "data": {"validationCode": "abc-123"},
            }
        ],
    )

    assert response.status_code == 200
    assert response.json() == {"validationResponse": "abc-123"}


def test_answer_endpoint_still_fails_closed_without_acs_for_call_events(client: TestClient) -> None:
    response = client.post(
        "/api/v1/calls/answer",
        json=[{"eventType": "Microsoft.Communication.IncomingCall", "data": {}}],
    )

    assert response.status_code == 503
    assert response.json() == {"error": "ACS not initialised"}