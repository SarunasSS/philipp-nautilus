from __future__ import annotations

import os
import secrets

from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from fastapi import FastAPI
from fastapi import Header
from fastapi import HTTPException
from fastapi import status

from phillip.control.kubernetes import KubernetesAPIError
from phillip.control.kubernetes import KubernetesClient
from phillip.control.kubernetes import KubernetesSettings
from phillip.control.models import StrategyConfigDashboardResponse
from phillip.control.models import StrategyConfigUpdate
from phillip.control.models import StrategyRegistry
from phillip.control.models import StrategyStatus
from phillip.control.models import StrategySummary
from phillip.control.models import StrategyTarget


SERVICE_ACCOUNT_PATH = "/var/run/secrets/kubernetes.io/serviceaccount"


@lru_cache
def strategy_registry() -> StrategyRegistry:
    path = os.environ.get("STRATEGY_REGISTRY_PATH", "/etc/phillip-control/strategies.json")
    return StrategyRegistry.from_path(path)


@lru_cache
def kubernetes_client() -> KubernetesClient:
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    namespace = os.environ.get("STRATEGY_NAMESPACE")
    if not host or not namespace:
        raise RuntimeError("Kubernetes service host and STRATEGY_NAMESPACE are required")

    return KubernetesClient(
        KubernetesSettings(
            api_url=f"https://{host}:{port}",
            namespace=namespace,
            token_path=f"{SERVICE_ACCOUNT_PATH}/token",
            ca_path=f"{SERVICE_ACCOUNT_PATH}/ca.crt",
        ),
    )


def require_control_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = os.environ.get("CONTROL_API_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Control API authentication is not configured",
        )

    scheme, separator, supplied = (authorization or "").partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_target(strategy_id: str, registry: StrategyRegistry) -> StrategyTarget:
    target = registry.get(strategy_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown strategy")
    return target


app = FastAPI(
    title="Philipp strategy control",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/strategies",
    response_model=list[StrategySummary],
    dependencies=[Depends(require_control_token)],
)
def list_strategies(
    registry: Annotated[StrategyRegistry, Depends(strategy_registry)],
    client: Annotated[KubernetesClient, Depends(kubernetes_client)],
) -> list[StrategySummary]:
    return [
        StrategySummary(
            id=target.id,
            display_name=target.display_name,
            namespace=client.settings.namespace,
            deployment=target.deployment,
            log_app=target.log_app,
        )
        for target in registry.strategies
    ]


@app.get(
    "/strategies/{strategy_id}/config",
    response_model=StrategyConfigDashboardResponse,
    dependencies=[Depends(require_control_token)],
)
def get_config(
    strategy_id: str,
    registry: Annotated[StrategyRegistry, Depends(strategy_registry)],
    client: Annotated[KubernetesClient, Depends(kubernetes_client)],
) -> StrategyConfigDashboardResponse:
    try:
        config = client.read_config(get_target(strategy_id, registry))
        return StrategyConfigDashboardResponse.from_config(config)
    except KubernetesAPIError as error:
        raise HTTPException(status_code=error.http_status, detail=error.detail) from error


@app.post(
    "/strategies/{strategy_id}/config",
    response_model=StrategyConfigDashboardResponse,
    dependencies=[Depends(require_control_token)],
)
def update_config(
    strategy_id: str,
    requested: StrategyConfigUpdate,
    registry: Annotated[StrategyRegistry, Depends(strategy_registry)],
    client: Annotated[KubernetesClient, Depends(kubernetes_client)],
) -> StrategyConfigDashboardResponse:
    try:
        updated = client.update_config(get_target(strategy_id, registry), requested)
        return StrategyConfigDashboardResponse.from_config(updated)
    except KubernetesAPIError as error:
        raise HTTPException(status_code=error.http_status, detail=error.detail) from error


@app.get(
    "/strategies/{strategy_id}/status",
    response_model=StrategyStatus,
    dependencies=[Depends(require_control_token)],
)
def get_status(
    strategy_id: str,
    registry: Annotated[StrategyRegistry, Depends(strategy_registry)],
    client: Annotated[KubernetesClient, Depends(kubernetes_client)],
) -> StrategyStatus:
    try:
        return client.read_status(get_target(strategy_id, registry))
    except KubernetesAPIError as error:
        raise HTTPException(status_code=error.http_status, detail=error.detail) from error
