"""OpenAPI schema federation for downstream service proxies.

The main app exposes generic catch-all proxies (`/api/v1/instagram-scraper/{path}`,
`/api/v1/cp/{path}`) that forward to standalone services with their own
OpenAPI specs. Without help, the main app's Swagger UI shows just the
proxy stub — admins can't see what endpoints the downstream actually
offers or what their response schemas look like.

This module fetches each downstream's `/api/v1/openapi.json` at app-
startup time, transforms the paths so they appear under the proxy
prefix, namespaces the schemas to avoid collisions, and merges
everything into the main app's spec. After that, `/docs` and
`/openapi.json` on port 8000 expose the full surface.

Failures are non-fatal: if a downstream is unreachable, the main spec
is returned unchanged with a logged warning. Admins can still browse
the main app's own routes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import httpx
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.core.logging import logger


# (mount_prefix, downstream_base_url, schema_namespace)
# Schema namespace prefixes downstream model names so e.g. content_pipeline's
# `PostDetail` and ig_scraper's `PostDetail` don't collide in the merged spec.
DownstreamConfig = Tuple[str, str, str]


def _fetch_downstream(base_url: str) -> Dict[str, Any] | None:
    """Pull `/api/v1/openapi.json` from a downstream service. Returns None
    on any error — federation is best-effort.
    """
    url = f"{base_url.rstrip('/')}/api/v1/openapi.json"
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("openapi_federation_fetch_failed", url=url, error=str(exc))
        return None


def _rename_schema_refs(node: Any, namespace: str) -> Any:
    """Walk a JSON dict/list tree and rewrite every `$ref` like
    `#/components/schemas/X` to `#/components/schemas/<namespace>X`.

    Returns a new tree; doesn't mutate the input.
    """
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str) and v.startswith("#/components/schemas/"):
                schema_name = v.rsplit("/", 1)[-1]
                out[k] = f"#/components/schemas/{namespace}{schema_name}"
            else:
                out[k] = _rename_schema_refs(v, namespace)
        return out
    if isinstance(node, list):
        return [_rename_schema_refs(x, namespace) for x in node]
    return node


def _merge_downstream(
    main_spec: Dict[str, Any],
    downstream: Dict[str, Any],
    *,
    mount_prefix: str,
    namespace: str,
    api_prefix: str,
) -> None:
    """Merge `downstream`'s paths + schemas into `main_spec` in-place.

    - Paths are rewritten from `/api/v1/<path>` to
      `/api/v1/<mount_prefix>/<path>` so they show up under the gateway.
    - Schemas (`components/schemas/X`) become `<namespace>X` to dodge
      name collisions across services.
    - Tags are namespaced too so the Swagger UI groups them cleanly.
    """
    paths = downstream.get("paths", {})
    schemas = downstream.get("components", {}).get("schemas", {})

    # Tag the operations so Swagger UI groups them under the service name.
    tag_name = mount_prefix
    main_spec.setdefault("tags", [])
    if not any(t.get("name") == tag_name for t in main_spec["tags"]):
        main_spec["tags"].append(
            {"name": tag_name, "description": f"Proxied to {namespace} downstream service."}
        )

    for path, ops in paths.items():
        # Strip the downstream's own /api/v1 prefix; the main app already
        # exposes the gateway under /api/v1/<mount_prefix>/.
        stripped = path[len(api_prefix):] if path.startswith(api_prefix) else path
        new_path = f"{api_prefix}/{mount_prefix}{stripped}"

        rewritten = _rename_schema_refs(ops, namespace)
        # Drop the downstream's auth requirement display (we authenticate
        # at the gateway with JWT; downstream API-key is server-to-server).
        for method_op in rewritten.values():
            if isinstance(method_op, dict):
                method_op["security"] = [{"BearerAuth": []}]
                method_op["tags"] = [tag_name]
        main_spec.setdefault("paths", {})[new_path] = rewritten

    main_spec.setdefault("components", {}).setdefault("schemas", {})
    for name, body in schemas.items():
        main_spec["components"]["schemas"][f"{namespace}{name}"] = _rename_schema_refs(body, namespace)


def install(app: FastAPI, downstreams: List[DownstreamConfig], *, api_prefix: str = "/api/v1") -> None:
    """Wire a custom `app.openapi` that lazily federates the listed
    downstreams. The merge is cached after the first call.

    The cache is on `app.openapi_schema`, which is FastAPI's standard
    location, so `/docs` and `/openapi.json` both benefit. To force a
    refetch (e.g. after a downstream redeploy), set
    `app.openapi_schema = None`.
    """

    def custom_openapi() -> Dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema

        spec = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        # Ensure a BearerAuth security scheme exists once so federated
        # operations reference it correctly.
        spec.setdefault("components", {}).setdefault("securitySchemes", {})[
            "BearerAuth"
        ] = {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}

        for mount_prefix, base_url, namespace in downstreams:
            ds = _fetch_downstream(base_url)
            if ds is None:
                continue
            try:
                _merge_downstream(
                    spec,
                    ds,
                    mount_prefix=mount_prefix,
                    namespace=namespace,
                    api_prefix=api_prefix,
                )
                logger.info(
                    "openapi_federation_merged",
                    mount_prefix=mount_prefix,
                    paths_count=len(ds.get("paths", {})),
                    schemas_count=len(ds.get("components", {}).get("schemas", {})),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "openapi_federation_merge_failed",
                    mount_prefix=mount_prefix,
                    error=str(exc),
                )

        app.openapi_schema = spec
        return spec

    app.openapi = custom_openapi  # type: ignore[method-assign]
