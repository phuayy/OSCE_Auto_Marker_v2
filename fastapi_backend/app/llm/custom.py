"""Operator-defined scoring providers — the connection half of "add a vendor".

Six providers ship in ``app/llm/registry.py``. Adding a seventh used to mean a
module, a release and a restart, which is the wrong cost for something an
operator does when their institution signs with a new vendor, stands up a local
vLLM box, or is handed an Azure deployment. This module is the description of a
provider that did *not* ship in the build: everything needed to authenticate to
a platform and reach its inference endpoint, stored in the database and applied
to the next run everywhere.

**What is deliberately not here: the model id.** A platform and a checkpoint are
different decisions with different lifetimes — one key authorises a whole
catalogue, and the model changes far more often than the endpoint does. The
model stays in Settings -> Scoring model, where it already is; this record
answers only "how do I talk to this platform at all".

**The fields are a union, not a profile.** Surveying what the current market
actually requires to open a connection:

===========================  ==================================================
Requirement                  Who needs it
===========================  ==================================================
Base URL                     Everyone. Self-hosted (vLLM, Ollama, LM Studio,
                             TGI), gateways (LiteLLM, Portkey, Cloudflare AI
                             Gateway) and regional endpoints differ per install
API key                      Everyone except an unauthenticated local server
Authorization: Bearer        OpenAI, Together, Groq, Fireworks, Mistral, xAI,
                             Perplexity, Cerebras, SambaNova, DeepInfra,
                             Nebius, Novita, vLLM, OpenRouter
x-api-key header             Anthropic, Voyage, some gateway deployments
api-key header               Azure OpenAI
Key as a query parameter     Google's native REST surface, some appliances
API version                  Azure (api-version query), Anthropic
                             (anthropic-version header)
Organisation / project id    OpenAI (OpenAI-Organization, OpenAI-Project),
                             IBM watsonx
Account id / region          Cloudflare Workers AI, Azure, Vertex, Bedrock —
                             all of which put it *in the URL*
Arbitrary extra headers      OpenRouter's HTTP-Referer / X-Title, corporate
                             gateways with their own auth token
Arbitrary query parameters   Azure, appliance-specific switches
Arbitrary body fields        NVIDIA's chat_template_kwargs, OpenRouter's
                             reasoning — vendor switches plain OpenAI 400s on
Wire format                  OpenAI /chat/completions (the overwhelming
                             majority) or Anthropic /messages
===========================  ==================================================

So the union is offered and **everything is optional except the identity, the
base URL and the key**. Those three are not preferences: a provider with no
endpoint has nothing to call, and one with no key cannot authenticate to any
commercial platform. Every other field is dead weight for most vendors and
load-bearing for one, which is exactly the shape a union should have.

The API key is **not** stored here. It goes to ``provider_credentials`` like
every other provider's, so a custom vendor inherits the encryption, the
write-only API, the narrow subprocess forwarding and the rotation-evicts-every-
cache behaviour without a second implementation of any of it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Mapping
from urllib.parse import urlparse

from app.llm.base import ProviderDescriptor

# Wire formats this build can speak. Both already have an adapter, so a custom
# provider reuses one rather than shipping a third HTTP client.
API_FORMAT_OPENAI = "openai"
API_FORMAT_ANTHROPIC = "anthropic"
API_FORMATS = (API_FORMAT_OPENAI, API_FORMAT_ANTHROPIC)

# Where the credential goes on the wire.
AUTH_BEARER = "bearer"        # Authorization: Bearer <key>
AUTH_HEADER = "header"        # <headerName>: <prefix><key>
AUTH_QUERY = "query"          # ?<param>=<key>
AUTH_SCHEMES = (AUTH_BEARER, AUTH_HEADER, AUTH_QUERY)

# Ids are used in URLs, environment variable names and settings rows, so they
# are restricted to something safe in all three.
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")

# Only these schemes are accepted for a base URL. A provider definition is
# operator-supplied and is fetched by the server, so file:// and friends would
# be a request-forgery primitive rather than a feature.
ALLOWED_URL_SCHEMES = ("https", "http")

MAX_LABEL_LENGTH = 120
MAX_TEXT_LENGTH = 500
MAX_URL_LENGTH = 2048
MAX_HEADER_ENTRIES = 20
MAX_EXTRA_BODY_BYTES = 4096

# Header names a definition may not set: the transport owns them, and letting a
# definition override one turns a settings form into a way to corrupt requests
# for reasons that would be very hard to diagnose.
RESERVED_HEADERS = frozenset({"content-length", "host", "transfer-encoding", "connection"})

# Placeholders substituted into the base URL, so the deployment-shaped parts of
# an endpoint stay in their own fields instead of being pasted into a URL the
# operator then has to re-edit when the region changes.
URL_PLACEHOLDERS = ("region", "accountId", "organizationId", "projectId", "apiVersion")


class CustomProviderError(ValueError):
    """A definition that cannot be stored. The message is shown to the operator."""


def key_env_name(provider_id: str) -> str:
    """The environment variable a custom provider's key travels to a subprocess in.

    Generated rather than operator-chosen so two custom providers can never be
    given the same variable name and silently share a credential.
    """
    slug = re.sub(r"[^A-Z0-9]+", "_", str(provider_id or "").upper()).strip("_")
    return f"OSCE_LLM_KEY_{slug or 'CUSTOM'}"


def _text(value: Any, *, limit: int = MAX_TEXT_LENGTH) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _verbatim(value: Any, *, limit: int, label: str) -> str:
    """Like :func:`_text`, but without trimming surrounding whitespace.

    Only for values where whitespace carries meaning. Line breaks are still
    refused, because these end up in a header.
    """
    text = str(value if value is not None else "")[:limit]
    if re.search(r"[\r\n]", text):
        raise CustomProviderError(f"{label} cannot contain line breaks.")
    return text


def _string_map(raw: Any, *, label: str) -> dict[str, str]:
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, Mapping):
        raise CustomProviderError(f"{label} must be a set of name/value pairs.")
    if len(raw) > MAX_HEADER_ENTRIES:
        raise CustomProviderError(f"{label} cannot have more than {MAX_HEADER_ENTRIES} entries.")
    cleaned: dict[str, str] = {}
    for name, value in raw.items():
        key = _text(name, limit=120)
        if not key:
            continue
        if re.search(r"[\r\n]", f"{key}{value}"):
            # Header injection: a newline in a header value splits the request.
            raise CustomProviderError(f"{label} cannot contain line breaks.")
        cleaned[key] = _text(value, limit=MAX_URL_LENGTH)
    return cleaned


@dataclass(frozen=True)
class CustomProviderSpec:
    """One operator-defined platform: identity plus how to connect to it.

    Frozen, and carries no secret. The key lives in ``provider_credentials``;
    this object is safe to serialise into a subprocess environment variable, a
    log line or an HTTP response.
    """

    # --- identity ---------------------------------------------------------
    id: str
    label: str
    vendor: str = ""
    description: str = ""
    documentation_url: str = ""

    # --- endpoint ---------------------------------------------------------
    base_url: str = ""
    api_format: str = API_FORMAT_OPENAI

    # --- authentication ---------------------------------------------------
    auth_scheme: str = AUTH_BEARER
    auth_header_name: str = ""
    auth_value_prefix: str = ""
    auth_query_param: str = ""

    # --- deployment identifiers -------------------------------------------
    api_version: str = ""
    api_version_header: str = ""
    api_version_query_param: str = ""
    organization_id: str = ""
    organization_header: str = "OpenAI-Organization"
    project_id: str = ""
    project_header: str = "OpenAI-Project"
    account_id: str = ""
    region: str = ""

    # --- free-form escape hatches -----------------------------------------
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    extra_query: Mapping[str, str] = field(default_factory=dict)
    extra_body: Mapping[str, Any] = field(default_factory=dict)

    # --- behaviour --------------------------------------------------------
    # 0 means "whatever the caller asked for" — a scoring call and a settings
    # connection test want very different ceilings.
    request_timeout_seconds: float = 0.0
    supports_json_mode: bool = True
    supports_reasoning_control: bool = False
    enabled: bool = True

    # --- provenance (not operator-supplied) -------------------------------
    created_at: str = ""
    updated_at: str = ""
    updated_by: str = ""

    # -- derived ------------------------------------------------------------

    @property
    def key_env(self) -> str:
        return key_env_name(self.id)

    def resolved_base_url(self) -> str:
        """The base URL with its deployment placeholders filled in."""
        url = self.base_url
        for name in URL_PLACEHOLDERS:
            url = url.replace("{" + name + "}", getattr(self, _snake(name)) or "")
        return url.strip()

    def headers(self, api_key: str = "") -> dict[str, str]:
        """Every header this platform needs, credential included.

        Order is deliberate: the auth header is written before ``extra_headers``
        so an operator who has to override it (a gateway with a second layer of
        auth) can, and the version/organisation headers come after the auth one
        so they cannot displace it by accident.
        """
        headers: dict[str, str] = {}
        if self.auth_scheme == AUTH_HEADER and self.auth_header_name and api_key:
            headers[self.auth_header_name] = f"{self.auth_value_prefix}{api_key}"
        if self.api_version and self.api_version_header:
            headers[self.api_version_header] = self.api_version
        if self.organization_id and self.organization_header:
            headers[self.organization_header] = self.organization_id
        if self.project_id and self.project_header:
            headers[self.project_header] = self.project_id
        headers.update(dict(self.extra_headers or {}))
        return headers

    def query(self, api_key: str = "") -> dict[str, str]:
        """Query parameters appended to every request."""
        params: dict[str, str] = {}
        if self.auth_scheme == AUTH_QUERY and self.auth_query_param and api_key:
            params[self.auth_query_param] = api_key
        if self.api_version and self.api_version_query_param:
            params[self.api_version_query_param] = self.api_version
        params.update(dict(self.extra_query or {}))
        return params

    def sends_bearer_header(self) -> bool:
        """Whether the OpenAI SDK's own ``Authorization: Bearer`` is wanted."""
        return self.auth_scheme == AUTH_BEARER

    def to_descriptor(self) -> ProviderDescriptor:
        """The shape the settings screen, the router and the credential resolver
        already understand.

        The model shortlist is empty on purpose — this record describes a
        platform, not a catalogue — and ``allows_custom_model`` is therefore
        always true, so the routing card asks for a model id instead of offering
        a list it could not have.
        """
        return ProviderDescriptor(
            id=self.id,
            label=self.label,
            vendor=self.vendor or self.label,
            description=self.description or f"Operator-defined provider at {self.resolved_base_url()}.",
            api_key_env=(self.key_env,),
            base_url_env="",
            default_base_url=self.resolved_base_url(),
            models=(),
            allows_custom_model=True,
            requirements=(
                "Save an API key for this provider below. It is stored encrypted and never "
                "returned to this screen."
            ),
            is_custom=True,
            connection=self.to_public(),
        )

    # -- serialisation ------------------------------------------------------

    def to_public(self) -> dict[str, Any]:
        """The definition as the API and the subprocess handoff carry it.

        Contains no credential, which is what makes it safe to put in an
        environment variable and in a log line.
        """
        return {
            "id": self.id,
            "label": self.label,
            "vendor": self.vendor,
            "description": self.description,
            "documentationUrl": self.documentation_url,
            "baseUrl": self.base_url,
            "resolvedBaseUrl": self.resolved_base_url(),
            "apiFormat": self.api_format,
            "authScheme": self.auth_scheme,
            "authHeaderName": self.auth_header_name,
            "authValuePrefix": self.auth_value_prefix,
            "authQueryParam": self.auth_query_param,
            "apiVersion": self.api_version,
            "apiVersionHeader": self.api_version_header,
            "apiVersionQueryParam": self.api_version_query_param,
            "organizationId": self.organization_id,
            "organizationHeader": self.organization_header,
            "projectId": self.project_id,
            "projectHeader": self.project_header,
            "accountId": self.account_id,
            "region": self.region,
            "extraHeaders": dict(self.extra_headers or {}),
            "extraQuery": dict(self.extra_query or {}),
            "extraBody": dict(self.extra_body or {}),
            "requestTimeoutSeconds": self.request_timeout_seconds,
            "supportsJsonMode": self.supports_json_mode,
            "supportsReasoningControl": self.supports_reasoning_control,
            "enabled": self.enabled,
            "keyEnv": self.key_env,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "updatedBy": self.updated_by,
        }

    def to_storage(self) -> dict[str, Any]:
        """The JSON blob persisted on the row.

        Identity and provenance live in their own columns, so they are stripped
        here rather than stored twice and allowed to disagree.
        """
        payload = self.to_public()
        for key in (
            "id",
            "label",
            "vendor",
            "description",
            "resolvedBaseUrl",
            "keyEnv",
            "createdAt",
            "updatedAt",
            "updatedBy",
            "enabled",
        ):
            payload.pop(key, None)
        return payload

    def with_provenance(self, **fields: str) -> "CustomProviderSpec":
        return replace(self, **fields)

    @classmethod
    def from_raw(cls, raw: Any, *, provider_id: str = "") -> "CustomProviderSpec":
        """Parse and validate an operator's definition.

        Everything that can be wrong with a definition is rejected *here*, at
        one boundary, so the HTTP route, the subprocess loader and a future CLI
        importer all enforce the identical contract rather than three drifting
        approximations of it.
        """
        payload = raw if isinstance(raw, Mapping) else {}

        resolved_id = _text(provider_id or payload.get("id"), limit=64).lower()
        if not ID_PATTERN.match(resolved_id):
            raise CustomProviderError(
                "The provider id must be 2-64 characters of lowercase letters, digits, "
                "'.', '-' or '_', and start with a letter or digit."
            )

        label = _text(payload.get("label"), limit=MAX_LABEL_LENGTH)
        if not label:
            raise CustomProviderError(
                "Give the provider a name so it is recognisable in the dropdowns."
            )

        api_format = (
            _text(payload.get("apiFormat") or payload.get("api_format"), limit=32).lower()
            or API_FORMAT_OPENAI
        )
        if api_format not in API_FORMATS:
            raise CustomProviderError(
                f"'{api_format}' is not a supported API format. "
                f"Choose one of: {', '.join(API_FORMATS)}."
            )

        base_url = _text(payload.get("baseUrl") or payload.get("base_url"), limit=MAX_URL_LENGTH)
        if not base_url:
            raise CustomProviderError(
                "Enter the inference endpoint's base URL — without one there is nothing to call."
            )
        _validate_url(base_url)

        auth_scheme = (
            _text(payload.get("authScheme") or payload.get("auth_scheme"), limit=32).lower()
            or AUTH_BEARER
        )
        if auth_scheme not in AUTH_SCHEMES:
            raise CustomProviderError(
                f"'{auth_scheme}' is not a supported authentication scheme. "
                f"Choose one of: {', '.join(AUTH_SCHEMES)}."
            )
        auth_header_name = _text(
            payload.get("authHeaderName") or payload.get("auth_header_name"), limit=120
        )
        auth_query_param = _text(
            payload.get("authQueryParam") or payload.get("auth_query_param"), limit=120
        )
        if auth_scheme == AUTH_HEADER and not auth_header_name:
            raise CustomProviderError(
                "A header authentication scheme needs the header name to send the key in "
                "(for example 'x-api-key' or 'api-key')."
            )
        if auth_scheme == AUTH_QUERY and not auth_query_param:
            raise CustomProviderError(
                "A query authentication scheme needs the parameter name to send the key in "
                "(for example 'key')."
            )
        if auth_header_name.lower() in RESERVED_HEADERS:
            raise CustomProviderError(
                f"'{auth_header_name}' is a transport header and cannot be set here."
            )

        extra_headers = _string_map(
            payload.get("extraHeaders") or payload.get("extra_headers"), label="Extra headers"
        )
        for name in extra_headers:
            if name.lower() in RESERVED_HEADERS:
                raise CustomProviderError(
                    f"'{name}' is a transport header and cannot be set here."
                )
        extra_query = _string_map(
            payload.get("extraQuery") or payload.get("extra_query"),
            label="Extra query parameters",
        )

        extra_body = payload.get("extraBody") or payload.get("extra_body") or {}
        if extra_body and not isinstance(extra_body, Mapping):
            raise CustomProviderError("Extra request body fields must be a JSON object.")
        extra_body = dict(extra_body or {})
        if len(json.dumps(extra_body, default=str)) > MAX_EXTRA_BODY_BYTES:
            raise CustomProviderError(
                f"Extra request body fields cannot exceed {MAX_EXTRA_BODY_BYTES} characters of JSON."
            )

        documentation_url = _text(
            payload.get("documentationUrl") or payload.get("documentation_url"), limit=MAX_URL_LENGTH
        )
        if documentation_url:
            _validate_url(documentation_url, field="documentation URL")

        return cls(
            id=resolved_id,
            label=label,
            vendor=_text(payload.get("vendor"), limit=MAX_LABEL_LENGTH),
            description=_text(payload.get("description")),
            documentation_url=documentation_url,
            base_url=base_url,
            api_format=api_format,
            auth_scheme=auth_scheme,
            auth_header_name=auth_header_name,
            # Kept verbatim rather than stripped: the trailing space in
            # "Api-Key " is the difference between a header that authenticates
            # and one that does not, and no operator would guess that the
            # settings form had eaten it.
            auth_value_prefix=_verbatim(
                payload.get("authValuePrefix") or payload.get("auth_value_prefix"),
                limit=64,
                label="Authentication value prefix",
            ),
            auth_query_param=auth_query_param,
            api_version=_text(payload.get("apiVersion") or payload.get("api_version"), limit=64),
            api_version_header=_text(
                payload.get("apiVersionHeader") or payload.get("api_version_header"), limit=120
            ),
            api_version_query_param=_text(
                payload.get("apiVersionQueryParam") or payload.get("api_version_query_param"),
                limit=120,
            ),
            organization_id=_text(
                payload.get("organizationId") or payload.get("organization_id"), limit=200
            ),
            organization_header=_text(
                payload.get("organizationHeader") or payload.get("organization_header"), limit=120
            )
            or "OpenAI-Organization",
            project_id=_text(payload.get("projectId") or payload.get("project_id"), limit=200),
            project_header=_text(
                payload.get("projectHeader") or payload.get("project_header"), limit=120
            )
            or "OpenAI-Project",
            account_id=_text(payload.get("accountId") or payload.get("account_id"), limit=200),
            region=_text(payload.get("region"), limit=120),
            extra_headers=extra_headers,
            extra_query=extra_query,
            extra_body=extra_body,
            request_timeout_seconds=_positive_number(
                payload.get("requestTimeoutSeconds", payload.get("request_timeout_seconds")),
                label="Request timeout",
                maximum=3600.0,
            ),
            supports_json_mode=_flag(
                payload.get("supportsJsonMode", payload.get("supports_json_mode")), True
            ),
            supports_reasoning_control=_flag(
                payload.get("supportsReasoningControl", payload.get("supports_reasoning_control")),
                False,
            ),
            enabled=_flag(payload.get("enabled"), True),
            created_at=_text(payload.get("createdAt") or payload.get("created_at"), limit=64),
            updated_at=_text(payload.get("updatedAt") or payload.get("updated_at"), limit=64),
            updated_by=_text(payload.get("updatedBy") or payload.get("updated_by"), limit=120),
        )


def _snake(camel: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()


def _validate_url(url: str, *, field: str = "base URL") -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise CustomProviderError(
            f"The {field} must start with https:// or http:// "
            "(https unless the endpoint is on this machine)."
        )
    if not parsed.netloc:
        raise CustomProviderError(f"The {field} is missing a host name.")


def _flag(value: Any, fallback: bool) -> bool:
    if value is None or value == "":
        return fallback
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _positive_number(value: Any, *, label: str, maximum: float) -> float:
    if value is None or value == "":
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise CustomProviderError(f"{label} must be a number of seconds.") from None
    if number < 0 or number > maximum:
        raise CustomProviderError(f"{label} must be between 0 and {int(maximum)} seconds.")
    return number
