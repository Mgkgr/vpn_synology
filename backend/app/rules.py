"""Safe local management of the mounted Mihomo DIRECT rule provider."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import os
import re
import tempfile
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypeVar

from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent


class DirectRuleValidationError(ValueError):
    """Raised for a local rule that is outside the intentionally small allowlist."""


class MihomoReloadClient(Protocol):
    def reload(self) -> object: ...


@dataclass(frozen=True, slots=True)
class DirectRulesPreview:
    """Validated, normalized content that is safe to write to the DIRECT provider."""

    text: str
    rules: tuple[str, ...]

    @property
    def rule_count(self) -> int:
        return len(self.rules)


@dataclass(frozen=True, slots=True)
class RuleRevision:
    """An immutable local copy written before a controller reload is requested."""

    number: int
    created_at: datetime
    path: Path
    sha256: str


_ALLOWED_TYPES = frozenset({"DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD", "IP-CIDR", "GEOIP", "GEOSITE"})
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,252}\Z")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_REVISION_NAME = re.compile(r"(?P<number>[0-9]{6,})-[0-9]{8}T[0-9]{6}[0-9]{6}Z\.txt\Z")
_RULE_FILE_LOCKS: dict[Path, object] = {}
_RULE_FILE_LOCKS_GUARD = threading.Lock()


class RuleService:
    """Validate, version and atomically apply only terminal DIRECT rules.

    The controller object is injected, making this service entirely offline-testable.
    It has no import-time side effects and never writes controller configuration itself.
    """

    def __init__(
        self,
        direct_rules_path: Path,
        revision_dir: Path | None = None,
        mihomo: MihomoReloadClient | None = None,
        *,
        audit_session_factory: sessionmaker[Session] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if mihomo is None:
            raise ValueError("a Mihomo reload client is required")
        self._direct_rules_path = Path(direct_rules_path)
        self._revision_dir = Path("/data/rule-revisions") if revision_dir is None else Path(revision_dir)
        self._mihomo = mihomo
        self._audit_session_factory = audit_session_factory
        self._now = now or (lambda: datetime.now(UTC))
        self._apply_lock = _rule_file_lock(self._direct_rules_path)

    def preview_direct_rules(self, text: str) -> DirectRulesPreview:
        """Return only strict DIRECT-provider forms, preserving harmless comments and blanks."""

        if not isinstance(text, str):
            raise DirectRuleValidationError("rule content must be text")

        lines: list[str] = []
        rules: list[str] = []
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                lines.append("")
                continue
            if line.startswith("#"):
                lines.append(line)
                continue
            normalized = _validate_rule_line(line, line_number)
            lines.append(normalized)
            rules.append(normalized)

        normalized_text = "\n".join(lines)
        if lines and (text.endswith("\n") or text.endswith("\r")):
            normalized_text += "\n"
        return DirectRulesPreview(text=normalized_text, rules=tuple(rules))

    def read_direct_rules(self) -> str:
        """Return the mounted provider file verbatim for an authenticated editor.

        A missing or non-UTF-8 file is deliberately an operational failure rather
        than an empty draft: the UI must never mistake an unavailable source for
        a user-approved empty DIRECT list.
        """

        return self._direct_rules_path.read_text(encoding="utf-8")

    def apply_direct_rules(self, text: str, actor: str) -> RuleRevision:
        """Apply a validated revision from synchronous code.

        For the real async ``MihomoClient`` use ``apply_direct_rules_async``
        from an async request handler; this method deliberately avoids starting
        an event loop at import time or from a running loop.
        """

        with self._apply_lock:  # type: ignore[union-attr]
            prepared = self._prepare_apply(text, actor)
            try:
                outcome = self._mihomo.reload()
                if inspect.isawaitable(outcome):
                    _run_awaitable(outcome)
            except BaseException:
                self._restore_after_reload_failure(prepared)
                raise
            self._audit_success(prepared)
            return prepared.revision

    async def apply_direct_rules_async(self, text: str, actor: str) -> RuleRevision:
        """Async counterpart used with the native asynchronous Mihomo client."""

        await asyncio.to_thread(self._apply_lock.acquire)  # type: ignore[union-attr]
        try:
            prepared = self._prepare_apply(text, actor)
            try:
                outcome = self._mihomo.reload()
                if inspect.isawaitable(outcome):
                    await outcome
            except BaseException:
                self._restore_after_reload_failure(prepared)
                raise
            self._audit_success(prepared)
            return prepared.revision
        finally:
            self._apply_lock.release()  # type: ignore[union-attr]

    def _prepare_apply(self, text: str, actor: str) -> "_PreparedApply":
        preview = self.preview_direct_rules(text)
        normalized_actor = _actor(actor)
        content = preview.text.encode("utf-8")
        existed = self._direct_rules_path.exists()
        previous = self._direct_rules_path.read_bytes() if existed else b""
        revision = self._write_revision(content)
        _atomic_replace(self._direct_rules_path, content)
        return _PreparedApply(revision, normalized_actor, previous, existed, content)

    def _write_revision(self, content: bytes) -> RuleRevision:
        created_at = _utc(self._now())
        self._revision_dir.mkdir(parents=True, exist_ok=True)
        number = _next_revision_number(self._revision_dir)
        filename = f"{number:06d}-{created_at:%Y%m%dT%H%M%S%fZ}.txt"
        path = self._revision_dir / filename
        _atomic_replace(path, content)
        return RuleRevision(number, created_at, path, hashlib.sha256(content).hexdigest())

    def _restore_after_reload_failure(self, prepared: "_PreparedApply") -> None:
        restored = False
        try:
            if _current_file_content(self._direct_rules_path) == prepared.applied_content:
                if prepared.previous_existed:
                    _atomic_replace(self._direct_rules_path, prepared.previous_content)
                else:
                    self._direct_rules_path.unlink(missing_ok=True)
                restored = True
        finally:
            self._audit(
                actor=prepared.actor,
                action="direct_rules_apply_failed",
                revision=prepared.revision,
                succeeded=False,
                error_text="Mihomo reload failed",
                restored=restored,
            )

    def _audit_success(self, prepared: "_PreparedApply") -> None:
        self._audit(
            actor=prepared.actor,
            action="direct_rules_apply",
            revision=prepared.revision,
            succeeded=True,
            error_text=None,
            restored=None,
        )

    def _audit(
        self,
        *,
        actor: str,
        action: str,
        revision: RuleRevision,
        succeeded: bool,
        error_text: str | None,
        restored: bool | None,
    ) -> None:
        if self._audit_session_factory is None:
            return
        with self._audit_session_factory.begin() as session:
            session.add(
                AuditEvent(
                    observed_at=_utc(self._now()),
                    actor=actor,
                    action=action,
                    revision_number=revision.number,
                    succeeded=succeeded,
                    error_text=error_text,
                    restored=restored,
                )
            )


@dataclass(frozen=True, slots=True)
class _PreparedApply:
    revision: RuleRevision
    actor: str
    previous_content: bytes
    previous_existed: bool
    applied_content: bytes


def _validate_rule_line(line: str, line_number: int) -> str:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) != 3 or parts[0] not in _ALLOWED_TYPES or parts[2] != "DIRECT":
        raise DirectRuleValidationError(f"rule line {line_number} is not an allowed terminal DIRECT rule")
    rule_type, payload, _action = parts
    if not payload:
        raise DirectRuleValidationError(f"rule line {line_number} has an empty payload")

    if rule_type in {"DOMAIN", "DOMAIN-SUFFIX"}:
        payload = _validated_domain(payload, line_number)
    elif rule_type == "IP-CIDR":
        payload = _validated_cidr(payload, line_number)
    elif not _SAFE_TOKEN.fullmatch(payload):
        raise DirectRuleValidationError(f"rule line {line_number} has an invalid payload")
    return f"{rule_type},{payload},DIRECT"


def _validated_domain(value: str, line_number: int) -> str:
    try:
        domain = value.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise DirectRuleValidationError(f"rule line {line_number} has an invalid domain") from error
    if len(domain) > 253 or domain.endswith(".") or not domain or any(
        not _DOMAIN_LABEL.fullmatch(label) for label in domain.split(".")
    ):
        raise DirectRuleValidationError(f"rule line {line_number} has an invalid domain")
    return domain


def _validated_cidr(value: str, line_number: int) -> str:
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError as error:
        raise DirectRuleValidationError(f"rule line {line_number} has an invalid CIDR") from error


def _actor(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("actor must be text")
    actor = value.strip()
    if not actor or len(actor) > 255 or any(character.isspace() and character not in {" ", "-"} for character in actor):
        raise ValueError("actor is invalid")
    return actor


def _next_revision_number(revision_dir: Path) -> int:
    numbers = [int(match.group("number")) for path in revision_dir.glob("*.txt") if (match := _REVISION_NAME.fullmatch(path.name))]
    return max(numbers, default=0) + 1


def _rule_file_lock(path: Path):
    """Return the one non-reentrant lock shared by every service for this file."""

    key = path.resolve()
    with _RULE_FILE_LOCKS_GUARD:
        lock = _RULE_FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _RULE_FILE_LOCKS[key] = lock
        return lock


def _current_file_content(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _atomic_replace(destination: Path, content: bytes) -> None:
    """Replace a local file without exposing a partially-written provider file."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


T = TypeVar("T")


def _run_awaitable(awaitable: Awaitable[T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    raise RuntimeError("use apply_direct_rules_async from an active event loop")
