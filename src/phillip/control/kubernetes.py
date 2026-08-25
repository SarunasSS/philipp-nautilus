from __future__ import annotations

import json
import logging
import ssl

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request
from urllib.request import urlopen

from phillip.control.models import StrategyConfigResponse
from phillip.control.models import StrategyConfigUpdate
from phillip.control.models import StrategyStatus
from phillip.control.models import StrategyTarget


LOGGER = logging.getLogger(__name__)
RESTART_ANNOTATION = "philipp.1d.works/config-restarted-at"


class KubernetesAPIError(RuntimeError):
    def __init__(self, http_status: int, detail: str) -> None:
        super().__init__(detail)
        self.http_status = http_status
        self.detail = detail


@dataclass(frozen=True)
class KubernetesSettings:
    api_url: str
    namespace: str
    token_path: str
    ca_path: str


class KubernetesClient:
    def __init__(self, settings: KubernetesSettings) -> None:
        self.settings = settings
        self._ssl_context = ssl.create_default_context(cafile=settings.ca_path)

    def read_config(self, target: StrategyTarget) -> StrategyConfigResponse:
        config_map = self._request("GET", self._config_map_path(target))
        data = config_map.get("data", {})
        metadata = config_map.get("metadata", {})
        missing = [field.key for field in target.fields if field.key not in data]
        if missing or not metadata.get("resourceVersion"):
            fields = ", ".join(missing) if missing else "metadata.resourceVersion"
            raise KubernetesAPIError(500, f"ConfigMap is missing required field(s): {fields}")

        return StrategyConfigResponse(
            strategy_id=target.id,
            display_name=target.display_name,
            resource_version=metadata["resourceVersion"],
            values={field.key: data[field.key] for field in target.fields},
            fields=target.fields,
        )

    def update_config(
        self,
        target: StrategyTarget,
        requested: StrategyConfigUpdate,
    ) -> StrategyConfigResponse:
        try:
            values = target.validate_values(requested.values)
        except ValueError as error:
            raise KubernetesAPIError(422, str(error)) from error

        current = self._request("GET", self._config_map_path(target))
        metadata = current.get("metadata", {})
        current_version = metadata.get("resourceVersion")
        if current_version != requested.resource_version:
            raise KubernetesAPIError(
                409,
                "The strategy configuration changed after the dashboard loaded. Reload it and try again.",
            )

        data = current.get("data", {}).copy()
        data.update(values)
        config_map = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": target.config_map,
                "namespace": self.settings.namespace,
                "resourceVersion": current_version,
                "labels": metadata.get("labels", {}),
                "annotations": metadata.get("annotations", {}),
            },
            "data": data,
        }
        updated = self._request("PUT", self._config_map_path(target), config_map)
        updated_version = updated.get("metadata", {}).get("resourceVersion")
        if not updated_version:
            raise KubernetesAPIError(502, "Kubernetes updated the ConfigMap without a resourceVersion")

        restarted_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        deployment_patch = {
            "spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: restarted_at}}}},
        }
        try:
            self._request(
                "PATCH",
                self._deployment_path(target),
                deployment_patch,
                content_type="application/strategic-merge-patch+json",
            )
        except KubernetesAPIError as error:
            raise KubernetesAPIError(
                502,
                f"The ConfigMap was updated, but restarting {target.deployment} failed.",
            ) from error

        LOGGER.info(
            "Updated %s and restarted %s at resourceVersion %s",
            target.config_map,
            target.deployment,
            updated_version,
        )
        return StrategyConfigResponse(
            strategy_id=target.id,
            display_name=target.display_name,
            resource_version=updated_version,
            values=values,
            fields=target.fields,
            restarted_at=restarted_at,
        )

    def read_status(self, target: StrategyTarget) -> StrategyStatus:
        deployment = self._request("GET", self._deployment_path(target))
        metadata = deployment.get("metadata", {})
        spec = deployment.get("spec", {})
        status = deployment.get("status", {})
        desired = spec.get("replicas", 1)
        ready = status.get("readyReplicas", 0)
        available = status.get("availableReplicas", 0)
        updated = status.get("updatedReplicas", 0)
        generation = metadata.get("generation", 0)
        observed = status.get("observedGeneration", 0)

        conditions = status.get("conditions", [])
        available_condition = next(
            (condition for condition in conditions if condition.get("type") == "Available"),
            {},
        )
        progressing_condition = next(
            (condition for condition in conditions if condition.get("type") == "Progressing"),
            {},
        )
        if desired == 0:
            state = "Stopped"
        elif observed >= generation and ready == desired and available == desired and updated == desired:
            state = "Ready"
        elif available_condition.get("status") == "False":
            state = "Degraded"
        else:
            state = "Progressing"

        containers = spec.get("template", {}).get("spec", {}).get("containers", [])
        image = containers[0].get("image", "") if containers else ""
        annotations = spec.get("template", {}).get("metadata", {}).get("annotations", {})
        restarted_at = annotations.get(RESTART_ANNOTATION)
        restarted_at_unix_ms = None
        if restarted_at:
            try:
                restarted_at_unix_ms = int(datetime.fromisoformat(restarted_at.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                LOGGER.warning("Deployment %s has an invalid restart timestamp: %s", target.deployment, restarted_at)
        condition = available_condition.get("message") or progressing_condition.get("message") or ""
        return StrategyStatus(
            strategy_id=target.id,
            display_name=target.display_name,
            deployment=target.deployment,
            state=state,
            state_code={"Degraded": 0, "Stopped": 1, "Progressing": 2, "Ready": 3}[state],
            replicas=f"{ready} / {desired}",
            desired_replicas=desired,
            ready_replicas=ready,
            available_replicas=available,
            updated_replicas=updated,
            generation=generation,
            observed_generation=observed,
            image=image,
            restarted_at=restarted_at,
            restarted_at_unix_ms=restarted_at_unix_ms,
            condition=condition,
        )

    def _config_map_path(self, target: StrategyTarget) -> str:
        namespace = quote(self.settings.namespace, safe="")
        name = quote(target.config_map, safe="")
        return f"/api/v1/namespaces/{namespace}/configmaps/{name}"

    def _deployment_path(self, target: StrategyTarget) -> str:
        namespace = quote(self.settings.namespace, safe="")
        name = quote(target.deployment, safe="")
        return f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}"

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        content_type: str = "application/json",
    ) -> dict[str, object]:
        with open(self.settings.token_path, encoding="utf-8") as token_file:
            token = token_file.read().strip()
        if not token:
            raise KubernetesAPIError(503, "The Kubernetes ServiceAccount token is empty")

        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            f"{self.settings.api_url}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
        )
        try:
            with urlopen(request, context=self._ssl_context, timeout=10) as response:
                response_body = response.read()
        except HTTPError as error:
            error_body = error.read().decode(errors="replace")[:1000]
            try:
                detail = json.loads(error_body).get("message", error_body)
            except json.JSONDecodeError:
                detail = error_body
            raise KubernetesAPIError(error.code, detail or error.reason) from error
        except OSError as error:
            raise KubernetesAPIError(503, f"Kubernetes API request failed: {error}") from error

        try:
            return json.loads(response_body)
        except json.JSONDecodeError as error:
            raise KubernetesAPIError(502, "Kubernetes API returned invalid JSON") from error
