"""Inventory public API references exposed by the Shizixi frontend bundle.

This is a read-only audit helper.  It downloads public HTML/JavaScript assets,
extracts endpoint-like strings, and probes only the conventional FastAPI
schema/documentation URLs.  It does not authenticate, crawl private pages, or
call business endpoints.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from urllib.parse import urljoin

import httpx


SITE = "https://www.shizixi.com/"
API_SITE = "https://api.shizixi.com/"


def _get(client: httpx.Client, url: str) -> httpx.Response:
    return client.get(url, timeout=30, follow_redirects=True)


def main() -> None:
    with httpx.Client(headers={"User-Agent": "QuantiAgent-public-api-audit/1.0"}) as client:
        home = _get(client, SITE)
        home.raise_for_status()
        scripts = sorted(set(re.findall(r'<script[^>]+src="([^"]+)"', home.text)))
        assets = [urljoin(SITE, path) for path in scripts if not path.startswith("/cdn-cgi/")]

        bundles: dict[str, str] = {}
        for url in assets:
            response = _get(client, url)
            response.raise_for_status()
            bundles[url] = response.text

        text = "\n".join(bundles.values())
        absolute_urls = sorted(set(re.findall(r"https?://[^\"'`\\s)]+", text)))
        api_paths = sorted(set(re.findall(
            r"[\"'`]((?:/api/|/v\d+/)[A-Za-z0-9_?=&./:{}$-]+)", text
        )))
        route_fragments = sorted(set(re.findall(
            r"[\"'`](/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_?=&.{}:$-]+){1,})[\"'`]", text
        )))

        conventional_docs = {}
        for base in (SITE, API_SITE):
            for path in ("openapi.json", "docs", "redoc"):
                url = urljoin(base, path)
                try:
                    response = _get(client, url)
                    conventional_docs[url] = {
                        "status": response.status_code,
                        "content_type": response.headers.get("content-type", ""),
                        "bytes": len(response.content),
                    }
                except Exception as exc:  # diagnostic output only
                    conventional_docs[url] = {"error": f"{type(exc).__name__}: {exc}"}

        schema_response = _get(client, urljoin(API_SITE, "openapi.json"))
        schema_response.raise_for_status()
        schema = schema_response.json()
        operations = []
        tag_counts: Counter[str] = Counter()
        for path, path_item in schema.get("paths", {}).items():
            for method, operation in path_item.items():
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                tags = operation.get("tags") or ["untagged"]
                tag_counts[tags[0]] += 1
                operations.append({
                    "method": method.upper(), "path": path,
                    "summary": operation.get("summary", ""), "tags": tags,
                    "openapi_security": bool(operation.get("security")),
                })

        print(json.dumps({
            "homepage": {
                "status": home.status_code,
                "server": home.headers.get("server", ""),
                "assets": assets,
            },
            "bundle_bytes": {url: len(body.encode("utf-8")) for url, body in bundles.items()},
            "absolute_urls": absolute_urls,
            "api_paths": api_paths,
            "route_fragments": route_fragments,
            "conventional_docs": conventional_docs,
            "openapi": {
                "title": schema.get("info", {}).get("title", ""),
                "version": schema.get("info", {}).get("version", ""),
                "path_count": len(schema.get("paths", {})),
                "operation_count": len(operations),
                "tag_counts": dict(tag_counts.most_common()),
                "operations": operations,
            },
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
