"""Official release metadata is informational; root-approved artifacts authorize updates."""

import json
import re
import ssl
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .catalog import ComponentDefinition, DIGEST_RE

MAX_BODY = 2 * 1024 * 1024
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,95}\Z")


class ReleaseError(ValueError):
    pass


@dataclass(frozen=True)
class ReleaseCandidate:
    component: str
    release_id: str
    version: str
    published_at: datetime
    digest: Optional[str]
    compatibility: str
    release_notes_url: Optional[str]
    image: Optional[str] = None


@dataclass(frozen=True)
class ReleaseCheck:
    releases: Tuple[ReleaseCandidate, ...] = ()
    checked_at: Optional[datetime] = None
    attempted_at: Optional[datetime] = None
    freshness_error: Optional[str] = None
    etag: Optional[str] = None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_official(url: str, etag: Optional[str]):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "api.github.com" or not re.fullmatch(r"/repos/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+/releases", parsed.path) or parsed.query != "per_page=20":
        raise ReleaseError("untrusted_release_source")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "vpn-dashboard-maintenance"}
    if etag and len(etag) <= 256 and not any(ord(char) < 32 for char in etag):
        headers["If-None-Match"] = etag
    opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(Request(url, headers=headers), timeout=10) as response:
            body = response.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise ReleaseError("release_metadata_too_large")
            return response.status, response.headers.get("ETag"), body
    except HTTPError as error:
        return error.code, None, b""


def _parse(component: ComponentDefinition, raw: bytes) -> Tuple[ReleaseCandidate, ...]:
    rows = json.loads(raw.decode("utf-8"))
    if not isinstance(rows, list) or len(rows) > 20:
        raise ReleaseError("invalid_metadata")
    output = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get("draft")) is not bool or type(row.get("prerelease")) is not bool:
            raise ReleaseError("invalid_metadata")
        if row["draft"] or row["prerelease"]:
            continue
        version, release_id = row.get("tag_name"), row.get("id")
        if not isinstance(version, str) or not TAG.fullmatch(version) or type(release_id) is not int or release_id <= 0:
            raise ReleaseError("invalid_metadata")
        notes = "https://github.com/" + component.repository + "/releases/tag/" + quote(version, safe="")
        if row.get("html_url") != notes:
            raise ReleaseError("invalid_metadata")
        published = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
        if published.tzinfo is None:
            raise ReleaseError("invalid_metadata")
        output.append(ReleaseCandidate(component.id, str(release_id), version, published, None, "unverified", notes))
    return tuple(sorted(output, key=lambda item: item.published_at, reverse=True))


def _local_check(component: ComponentDefinition, record: dict, now: datetime) -> ReleaseCheck:
    commit = record.get("git_commit")
    if (component.id != "dashboard" or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or record.get("version") != commit or record.get("release_id") != commit):
        raise ReleaseError("local_release_not_prepared")
    published = datetime.fromisoformat(record["published_at"].replace("Z", "+00:00"))
    if published.tzinfo is None:
        raise ReleaseError("invalid_metadata")
    candidate = ReleaseCandidate(component.id, commit, commit, published, None, "unverified", None)
    check = ReleaseCheck((candidate,), now, now)
    approved = resolve_approved_release(component, check, commit, {commit: record}, "linux/amd64")
    return replace(check, releases=(approved,))


def check_releases(catalog, previous: Dict[str, ReleaseCheck], now: datetime, fetch=fetch_official, local_artifacts=None) -> Dict[str, ReleaseCheck]:
    def check(component):
        old = previous.get(component.id, ReleaseCheck())
        if component.source_kind != "github" and (not local_artifacts or component.id not in local_artifacts):
            return component.id, replace(old, attempted_at=now, freshness_error="local_release_not_prepared" if component.source_kind == "local" else "engine_not_selected")
        try:
            if component.source_kind == "local":
                return component.id, _local_check(component, local_artifacts[component.id], now)
            if component.source_kind != "github":
                raise ReleaseError("engine_not_selected")
            url = "https://api.github.com/repos/" + component.repository + "/releases?per_page=20"
            status, etag, body = fetch(url, old.etag)
            if status == 304 and old.checked_at is not None:
                return component.id, replace(old, checked_at=now, attempted_at=now, freshness_error=None)
            if status != 200:
                raise ReleaseError("rate_limited" if status in (403, 429) else "upstream_unavailable")
            if len(body) > MAX_BODY:
                raise ReleaseError("release_metadata_too_large")
            if etag is not None and (not isinstance(etag, str) or len(etag) > 256 or any(ord(char) < 32 for char in etag)):
                raise ReleaseError("invalid_metadata")
            result = ReleaseCheck(_parse(component, body), now, now, None, etag)
        except ReleaseError as error:
            result = replace(old, attempted_at=now, freshness_error=str(error))
        except (TimeoutError, OSError, URLError):
            result = replace(old, attempted_at=now, freshness_error="upstream_unavailable")
        except (ValueError, TypeError, KeyError, AttributeError):
            result = replace(old, attempted_at=now, freshness_error="invalid_metadata")
        return component.id, result
    with ThreadPoolExecutor(max_workers=2) as executor:
        return dict(executor.map(check, catalog))


def resolve_approved_release(component: ComponentDefinition, check: ReleaseCheck, release_id: str, approvals: dict, platform: str) -> ReleaseCandidate:
    """Approvals are read by the root worker from its private catalogue, never the API."""
    candidate = next((item for item in check.releases if item.release_id == release_id and item.component == component.id), None)
    record = approvals.get(release_id)
    if candidate is None or check.freshness_error or not isinstance(record, dict):
        raise ReleaseError("release_not_approved")
    digest, image = record.get("digest"), record.get("image")
    allowed_images = (digest,) if component.source_kind == "local" else tuple(repo + "@" + str(digest) for repo in component.image_repositories)
    if (record.get("component") != component.id or record.get("release_id") != release_id
            or record.get("repository") != component.repository or record.get("version") != candidate.version
            or record.get("compatibility") != "approved" or record.get("platform") != platform
            or platform != "linux/amd64" or not record.get("validation_id") or record.get("rollback_verified") is not True
            or record.get("signature") not in ("verified", "not_published")
            or not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest)
            or image not in allowed_images):
        raise ReleaseError("release_not_approved")
    return replace(candidate, compatibility="approved", digest=digest, image=image)
