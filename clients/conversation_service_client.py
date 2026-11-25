"""Pure-stdlib loopback client for Antigravity `ConversationService` (`conversation_service_client.py`).

Uses ONLY the documented `ANTIGRAVITY_LS_ADDRESS` and `ANTIGRAVITY_CSRF_TOKEN`
environment variables to invoke Connect HTTP+JSON RPCs against the local
Antigravity language server (`product.v1.ConversationService/{ListConversations,
GetConversation,UpdateConversation,DeleteConversation}`).

Security & Scope Guardrails:
  - Pure Python 3 standard library only (Santa Lockdown compliant).
  - Strictly enforces loopback targets (`127.0.0.1`, `localhost`, `::1`).
  - Never reads undocumented internal daemon discovery files.
  - Never includes raw `ANTIGRAVITY_CSRF_TOKEN` in serialized diagnostics or errors.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import contextlib
import ipaddress
import json
import os
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

CONVERSATION_SERVICE_NAME = "product.v1.ConversationService"
DEFAULT_CONVERSATION_SERVICE_TIMEOUT_SECONDS = 10.0
DEFAULT_LIST_PAGE_SIZE = 100
MAX_LIST_PAGES = 50


class ConversationServiceError(RuntimeError):
    """Structured error raised when a `ConversationService` RPC fails or is misconfigured."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "conversation_service_error",
        status_code: int | None = None,
        method: str = "",
    ) -> None:
        super().__init__(message)
        self.code = str(code or "conversation_service_error")
        self.status_code = status_code
        self.method = str(method or "")
        self.message = str(message)


def _is_loopback_host(host: str) -> bool:
    """Return True only when `host` resolves syntactically to a loopback address or `localhost`."""
    clean = str(host or "").strip().strip("[]").lower()
    if not clean:
        return False
    if clean == "localhost":
        return True
    try:
        return bool(ipaddress.ip_address(clean).is_loopback)
    except ValueError:
        return False


def _parse_loopback_address(raw_address: str) -> tuple[str, str, str]:
    """Validate and normalize `ANTIGRAVITY_LS_ADDRESS` into `(norm_address, base_url, reason)`."""
    raw = str(raw_address or "").strip()
    if not raw:
        return "", "", "missing_ls_address"

    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return "", "", "invalid_ls_address"

    if parsed.scheme.lower() != "http":
        return "", "", "invalid_ls_address"
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return "", "", "invalid_ls_address"
    if parsed.path not in ("", "/"):
        return "", "", "invalid_ls_address"

    host = parsed.hostname or ""
    if not host:
        return "", "", "invalid_ls_address"
    if not _is_loopback_host(host):
        return "", "", "non_loopback_ls_address"

    try:
        port = parsed.port
    except ValueError:
        return "", "", "invalid_ls_address"
    if port is None or port < 1 or port > 65535:
        return "", "", "invalid_ls_address"

    host_fmt = f"[{host}]" if ":" in host else host
    norm_addr = f"{host_fmt}:{port}"
    return norm_addr, f"http://{norm_addr}", "ok"


def resolve_conversation_service_config(
    *,
    address: str | None = None,
    csrf_token: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve and validate loopback `ConversationService` config from documented LS env vars."""
    eff_env = os.environ if env is None else env
    raw_addr = str(address if address is not None else eff_env.get("ANTIGRAVITY_LS_ADDRESS") or "").strip()
    raw_tok = str(csrf_token if csrf_token is not None else eff_env.get("ANTIGRAVITY_CSRF_TOKEN") or "").strip()

    norm_addr, base_url, addr_reason = _parse_loopback_address(raw_addr)
    if addr_reason != "ok":
        return {
            "configured": False,
            "address": "",
            "base_url": "",
            "csrf_token": "",
            "has_csrf_token": bool(raw_tok),
            "reason": addr_reason,
        }
    if not raw_tok:
        return {
            "configured": False,
            "address": norm_addr,
            "base_url": base_url,
            "csrf_token": "",
            "has_csrf_token": False,
            "reason": "missing_csrf_token",
        }
    return {
        "configured": True,
        "address": norm_addr,
        "base_url": base_url,
        "csrf_token": raw_tok,
        "has_csrf_token": True,
        "reason": "ok",
    }


def _default_urlopen(req: urllib.request.Request, *, timeout: float) -> Any:
    """Open loopback HTTP request bypassing proxy environment variables."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


class ConversationServiceClient:
    """Pure-stdlib Connect HTTP+JSON client for local Antigravity `ConversationService`."""

    def __init__(
        self,
        *,
        address: str | None = None,
        csrf_token: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float = DEFAULT_CONVERSATION_SERVICE_TIMEOUT_SECONDS,
        urlopen_fn: Callable[..., Any] | None = None,
    ) -> None:
        cfg = resolve_conversation_service_config(
            address=address,
            csrf_token=csrf_token,
            env=env,
        )
        self._configured = bool(cfg["configured"])
        self._address = str(cfg["address"])
        self._base_url = str(cfg["base_url"])
        self._csrf_token = str(cfg["csrf_token"])
        self._has_csrf_token = bool(cfg["has_csrf_token"])
        self._reason = str(cfg["reason"])
        self._timeout_seconds = max(0.5, float(timeout_seconds))
        self._urlopen_fn = urlopen_fn

    def is_configured(self) -> bool:
        """Return True when both valid loopback `ANTIGRAVITY_LS_ADDRESS` and `ANTIGRAVITY_CSRF_TOKEN` are set."""
        return self._configured

    def config_status(self) -> dict[str, Any]:
        """Return sanitized configuration diagnostics without exposing the CSRF token."""
        return {
            "configured": self._configured,
            "address": self._address,
            "base_url": self._base_url,
            "has_csrf_token": self._has_csrf_token,
            "reason": self._reason,
        }

    def call(self, method: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Invoke a `product.v1.ConversationService/<method>` Connect HTTP+JSON POST request."""
        clean_method = str(method or "").strip().lstrip("/")
        if not clean_method:
            raise ConversationServiceError("Method name is required.", code="invalid_argument")
        if not self._configured:
            raise ConversationServiceError(
                f"ConversationService client unconfigured ({self._reason}).",
                code=self._reason,
                method=clean_method,
            )

        url = f"{self._base_url}/{CONVERSATION_SERVICE_NAME}/{clean_method}"
        body_bytes = json.dumps(dict(payload or {}), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body_bytes,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "x-antigravity-csrf-token": self._csrf_token,
            },
        )
        open_fn = self._urlopen_fn if self._urlopen_fn is not None else _default_urlopen
        try:
            with open_fn(req, timeout=self._timeout_seconds) as resp:
                raw_bytes = resp.read()
        except urllib.error.HTTPError as err:
            err_body = b""
            with contextlib.suppress(OSError):
                err_body = err.read()
            err_code = f"http_{int(err.code)}"
            err_msg = f"HTTP {int(err.code)} calling {clean_method}"
            if err_body:
                with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
                    parsed_err = json.loads(err_body.decode("utf-8"))
                    if isinstance(parsed_err, dict):
                        if parsed_err.get("code"):
                            err_code = str(parsed_err["code"])
                        if parsed_err.get("message"):
                            err_msg = str(parsed_err["message"])
            raise ConversationServiceError(
                err_msg,
                code=err_code,
                status_code=int(err.code),
                method=clean_method,
            ) from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise ConversationServiceError(
                f"Transport error calling {clean_method}: {err}",
                code="transport_error",
                method=clean_method,
            ) from err

        if not raw_bytes:
            return {}
        try:
            decoded = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise ConversationServiceError(
                f"Invalid JSON response from {clean_method}: {err}",
                code="invalid_json_response",
                method=clean_method,
            ) from err
        if not isinstance(decoded, dict):
            raise ConversationServiceError(
                f"Unexpected non-object JSON response from {clean_method}.",
                code="invalid_response_shape",
                method=clean_method,
            )
        return decoded

    def list_conversations(
        self,
        *,
        page_size: int = DEFAULT_LIST_PAGE_SIZE,
        page_token: str = "",
        project_id: str | None = None,
        parent_conversation_id: str | None = None,
        tags: Sequence[str] | None = None,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """List conversations via `ConversationService/ListConversations`."""
        clamped_size = max(1, min(100, int(page_size)))
        payload: dict[str, Any] = {
            "pageSize": clamped_size,
            "includeArchived": bool(include_archived),
        }
        if page_token:
            payload["pageToken"] = str(page_token)
        if project_id is not None:
            payload["projectId"] = str(project_id)
        if parent_conversation_id is not None:
            payload["parentConversationId"] = str(parent_conversation_id)
        if tags:
            payload["tags"] = [str(t) for t in tags if str(t).strip()]
        return self.call("ListConversations", payload)

    def list_all_conversations(
        self,
        *,
        page_size: int = DEFAULT_LIST_PAGE_SIZE,
        project_id: str | None = None,
        parent_conversation_id: str | None = None,
        tags: Sequence[str] | None = None,
        include_archived: bool = False,
        max_pages: int = MAX_LIST_PAGES,
    ) -> list[dict[str, Any]]:
        """Paginate `ListConversations` safely up to `max_pages` returning normalized conversation dicts."""
        items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_tokens: set[str] = set()
        page_token = ""
        for _ in range(max(1, int(max_pages))):
            resp = self.list_conversations(
                page_size=page_size,
                page_token=page_token,
                project_id=project_id,
                parent_conversation_id=parent_conversation_id,
                tags=tags,
                include_archived=include_archived,
            )
            batch = resp.get("conversations")
            if isinstance(batch, list):
                for conv in batch:
                    if not isinstance(conv, dict):
                        continue
                    cid = str(conv.get("id") or "").strip().lower()
                    if cid and cid in seen_ids:
                        continue
                    if cid:
                        seen_ids.add(cid)
                    items.append(conv)
            next_tok = str(resp.get("nextPageToken") or "").strip()
            if not next_tok or next_tok in seen_tokens:
                break
            seen_tokens.add(next_tok)
            page_token = next_tok
        return items

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Fetch a single conversation via `ConversationService/GetConversation`."""
        clean_id = str(conversation_id or "").strip()
        if not clean_id:
            raise ConversationServiceError("conversation_id is required.", code="invalid_argument", method="GetConversation")
        return self.call("GetConversation", {"conversationId": clean_id})

    def update_conversation(
        self,
        conversation_id: str,
        *,
        annotations: Mapping[str, Any] | None = None,
        title: str | None = None,
        archived: bool | None = None,
        pinned: bool | None = None,
        starred: bool | None = None,
        tags: Sequence[str] | None = None,
        conversation_group_id: str | None = None,
    ) -> dict[str, Any]:
        """Update conversation annotations via `ConversationService/UpdateConversation`."""
        clean_id = str(conversation_id or "").strip()
        if not clean_id:
            raise ConversationServiceError("conversation_id is required.", code="invalid_argument", method="UpdateConversation")
        ann: dict[str, Any] = dict(annotations or {})
        if title is not None:
            ann["title"] = str(title)
        if archived is not None:
            ann["archived"] = bool(archived)
        if pinned is not None:
            ann["pinned"] = bool(pinned)
        if starred is not None:
            ann["starred"] = bool(starred)
        if tags is not None:
            ann["tags"] = [str(t) for t in tags if str(t).strip()]
        if conversation_group_id is not None:
            ann["conversationGroupId"] = str(conversation_group_id)
        return self.call(
            "UpdateConversation",
            {
                "conversationId": clean_id,
                "annotations": ann,
            },
        )

    def delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Permanently delete a conversation natively via `ConversationService/DeleteConversation`."""
        clean_id = str(conversation_id or "").strip()
        if not clean_id:
            raise ConversationServiceError("conversation_id is required.", code="invalid_argument", method="DeleteConversation")
        return self.call("DeleteConversation", {"conversationId": clean_id})
