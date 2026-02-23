from dotenv import load_dotenv
load_dotenv()  # this reads your .env file and loads GOOGLE_API_KEY into the environment
import datetime
import hashlib
import json
import logging
import os
import ctypes
import warnings
from zoneinfo import ZoneInfo
from google.adk.agents import Agent
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.tools import ToolContext
from a2a.types import AgentCard, AgentCapabilities, APIKeySecurityScheme, AgentExtension, SecurityScheme, In
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)
LOG_FULL_PAYLOAD = os.getenv("LOG_FULL_PAYLOAD", "true").lower() == "true"
LOG_HOOK_RAW_OBJECTS = os.getenv("LOG_HOOK_RAW_OBJECTS", "false").lower() == "true"


class _AnsiColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: "\x1b[36m",   # cyan
        logging.INFO: "\x1b[32m",    # green
        logging.WARNING: "\x1b[33m", # yellow
        logging.ERROR: "\x1b[31m",   # red
        logging.CRITICAL: "\x1b[35m" # magenta
    }
    RESET = "\x1b[0m"

    def format(self, record):
        color = self.LEVEL_COLORS.get(record.levelno, "")
        original_levelname = record.levelname
        record.levelname = f"{color}{original_levelname}{self.RESET}" if color else original_levelname
        try:
            return super().format(record)
        finally:
            record.levelname = original_levelname


def _configure_logger():
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        _AnsiColorFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False


def _enable_windows_ansi():
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if handle == 0:
            return
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            return
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        return


def _safe_pretty_json(value):
    try:
        return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _serialize_for_log(value):
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, str, int, float, bool)):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")
        except TypeError:
            return model_dump()
        except Exception:
            return str(value)
    return str(value)


def _redact_headers(headers):
    if not isinstance(headers, dict):
        return headers
    redacted = dict(headers)
    for key in list(redacted.keys()):
        key_lower = str(key).lower()
        if key_lower in {"x-api-key", "authorization", "cookie", "set-cookie"}:
            header_value = str(redacted[key])
            redacted[key] = f"[REDACTED len={len(header_value)}]"
    return redacted


_configure_logger()
_enable_windows_ansi()
warnings.filterwarnings(
    "ignore",
    message=r".*\[EXPERIMENTAL\].*",
    category=UserWarning,
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────

# In real life load this from environment variable or secrets manager
# Think of it like reading from appsettings.json / Azure Key Vault
VALID_API_KEYS = {
    "my-secret-key-123",   # your .NET app's key
    "another-valid-key",   # any other trusted callers
}

FHIR_CONTEXT_KEY = "fhir-context"


# ─── SECURITY MIDDLEWARE ───────────────────────────────────────────────────────

class ApiKeyMiddleware(BaseHTTPMiddleware):
    """
    Validates API key on every request EXCEPT the agent card endpoint.
    Think of this like an ASP.NET AuthorizationMiddleware / API key filter.
    
    Your .NET app must send this header on every call:
        X-API-Key: my-secret-key-123
    """
    async def dispatch(self, request: Request, call_next):
        body_bytes = await request.body()
        body_text = body_bytes.decode("utf-8", errors="replace")
        parsed = {}
        try:
            parsed = json.loads(body_text) if body_text else {}
            pretty_body = _safe_pretty_json(parsed)
        except json.JSONDecodeError:
            pretty_body = body_text

        if LOG_FULL_PAYLOAD:
            logger.info(
                "incoming_http_request path=%s method=%s headers=%s\npayload=\n%s",
                request.url.path,
                request.method,
                _safe_pretty_json(_redact_headers(dict(request.headers))),
                pretty_body,
            )

        # If caller sends FHIR metadata under params.message.metadata, mirror it
        # into params.metadata so ADK callback path can consume it.
        fhir_key, fhir_data = _extract_fhir_from_payload(parsed)
        if isinstance(parsed, dict):
            params = parsed.get("params")
            if isinstance(params, dict):
                if fhir_key and fhir_data and not params.get("metadata"):
                    params["metadata"] = {fhir_key: fhir_data}
                    body_bytes = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
                    logger.info(
                        "FHIR_METADATA_BRIDGED source=message.metadata target=params.metadata key=%s",
                        fhir_key,
                    )
                if fhir_data:
                    logger.info("FHIR_URL_FOUND value=%s", fhir_data.get("fhirUrl", "[EMPTY]"))
                    logger.info(
                        "FHIR_TOKEN_FOUND fingerprint=%s",
                        _token_fingerprint(fhir_data.get("fhirToken", "")),
                    )
                    logger.info(
                        "FHIR_PATIENT_FOUND value=%s",
                        fhir_data.get("patientId", "[EMPTY]"),
                    )
                else:
                    logger.info("FHIR_NOT_FOUND_IN_PAYLOAD keys_checked=params.metadata,message.metadata")

        request = _clone_request_with_body(request, body_bytes)
        
        # Always allow the agent card through — it's public by design
        # This is how callers discover your agent and know it needs a key
        if request.url.path == "/.well-known/agent-card.json":
            return await call_next(request)

        # Extract the API key from the request header
        api_key = request.headers.get("X-API-Key")

        if not api_key:
            logger.warning(
                "security_rejected_missing_api_key path=%s method=%s",
                request.url.path,
                request.method,
            )
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized", "detail": "X-API-Key header is required"}
            )

        if api_key not in VALID_API_KEYS:
            logger.warning(
                "security_rejected_invalid_api_key path=%s method=%s key_prefix=%s",
                request.url.path,
                request.method,
                api_key[:6],
            )
            return JSONResponse(
                status_code=403,
                content={"error": "Forbidden", "detail": "Invalid API key"}
            )

        logger.info(
            "security_authorized path=%s method=%s key_prefix=%s",
            request.url.path,
            request.method,
            api_key[:6],
        )
        return await call_next(request)


# ─── FHIR CONTEXT MIDDLEWARE ───────────────────────────────────────────────────

def _first_non_empty(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _safe_correlation_ids(callback_context, llm_request):
    return {
        "task_id": _first_non_empty(
            getattr(llm_request, "task_id", None),
            getattr(callback_context, "task_id", None),
        ),
        "context_id": _first_non_empty(
            getattr(llm_request, "context_id", None),
            getattr(callback_context, "context_id", None),
        ),
        "message_id": _first_non_empty(
            getattr(llm_request, "message_id", None),
            getattr(callback_context, "message_id", None),
        ),
    }


def _token_fingerprint(token: str) -> str:
    if not token:
        return "empty"
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return f"len={len(token)} sha256={token_hash}"


def _coerce_fhir_data(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
        return None
    return None


def _extract_metadata_sources(callback_context, llm_request):
    callback_metadata = getattr(callback_context, "metadata", None)
    run_config = getattr(callback_context, "run_config", None)
    custom_metadata = getattr(run_config, "custom_metadata", None) if run_config else None
    a2a_metadata = custom_metadata.get("a2a_metadata") if isinstance(custom_metadata, dict) else None

    llm_payload = _serialize_for_log(llm_request)
    request_contents = llm_payload.get("contents", []) if isinstance(llm_payload, dict) else []
    content_metadata = None
    if request_contents and isinstance(request_contents, list):
        first_content = request_contents[-1] if request_contents else {}
        if isinstance(first_content, dict):
            content_metadata = first_content.get("metadata")

    sources = [
        ("callback_context.metadata", callback_metadata),
        ("callback_context.run_config.custom_metadata.a2a_metadata", a2a_metadata),
        ("llm_request.contents[-1].metadata", content_metadata),
    ]
    return sources


def _extract_fhir_from_payload(payload):
    if not isinstance(payload, dict):
        return None, None
    params = payload.get("params")
    if not isinstance(params, dict):
        return None, None
    params_metadata = params.get("metadata")
    if isinstance(params_metadata, dict):
        for key, value in params_metadata.items():
            if FHIR_CONTEXT_KEY in str(key):
                return key, _coerce_fhir_data(value)
    message = params.get("message")
    if isinstance(message, dict):
        message_metadata = message.get("metadata")
        if isinstance(message_metadata, dict):
            for key, value in message_metadata.items():
                if FHIR_CONTEXT_KEY in str(key):
                    return key, _coerce_fhir_data(value)
    return None, None


def _clone_request_with_body(request: Request, body_bytes: bytes) -> Request:
    async def receive():
        return {
            "type": "http.request",
            "body": body_bytes,
            "more_body": False,
        }
    return Request(request.scope, receive)


def extract_fhir_context(callback_context, llm_request):
    """Extracts FHIR metadata from A2A message and stores in session state."""
    correlation = _safe_correlation_ids(callback_context, llm_request)
    metadata_sources = _extract_metadata_sources(callback_context, llm_request)
    selected_source = "none"
    metadata = {}
    for source_name, candidate in metadata_sources:
        if isinstance(candidate, dict) and candidate:
            metadata = candidate
            selected_source = source_name
            break
    metadata_keys = list(metadata.keys()) if isinstance(metadata, dict) else []

    if LOG_HOOK_RAW_OBJECTS:
        logger.info(
            "hook_raw_llm_request=\n%s",
            _safe_pretty_json(_serialize_for_log(llm_request)),
        )
        logger.info(
            "hook_raw_callback_context=\n%s",
            _safe_pretty_json(
                {
                    "task_id": getattr(callback_context, "task_id", None),
                    "context_id": getattr(callback_context, "context_id", None),
                    "message_id": getattr(callback_context, "message_id", None),
                    "metadata": _serialize_for_log(getattr(callback_context, "metadata", None)),
                    "state": _serialize_for_log(getattr(callback_context, "state", None)),
                }
            ),
        )

    logger.info(
        "hook_called_enter task_id=%s context_id=%s message_id=%s metadata_source=%s metadata_keys=%s",
        correlation["task_id"],
        correlation["context_id"],
        correlation["message_id"],
        selected_source,
        metadata_keys,
    )

    if not metadata:
        logger.info(
            "hook_called_no_metadata task_id=%s context_id=%s message_id=%s",
            correlation["task_id"],
            correlation["context_id"],
            correlation["message_id"],
        )
        return None
    if not isinstance(metadata, dict):
        logger.warning(
            "hook_called_metadata_invalid_shape task_id=%s context_id=%s message_id=%s metadata_type=%s",
            correlation["task_id"],
            correlation["context_id"],
            correlation["message_id"],
            type(metadata).__name__,
        )
        return None

    fhir_data = None
    for key, value in metadata.items():
        if FHIR_CONTEXT_KEY in str(key):
            fhir_data = _coerce_fhir_data(value)
            if fhir_data is None:
                logger.warning(
                    "hook_called_fhir_malformed task_id=%s context_id=%s message_id=%s metadata_key=%s value_type=%s",
                    correlation["task_id"],
                    correlation["context_id"],
                    correlation["message_id"],
                    key,
                    type(value).__name__,
                )
            break

    if fhir_data:
        callback_context.state["fhir_url"]   = fhir_data.get("fhirUrl", "")
        callback_context.state["fhir_token"] = fhir_data.get("fhirToken", "")
        callback_context.state["patient_id"] = fhir_data.get("patientId", "")
        logger.info("FHIR_URL_FOUND value=%s", callback_context.state["fhir_url"] or "[EMPTY]")
        logger.info(
            "FHIR_TOKEN_FOUND fingerprint=%s",
            _token_fingerprint(callback_context.state["fhir_token"]),
        )
        logger.info(
            "FHIR_PATIENT_FOUND value=%s",
            callback_context.state["patient_id"] or "[EMPTY]",
        )
        logger.info(
            "hook_called_fhir_found task_id=%s context_id=%s message_id=%s patient_id=%s fhir_url_set=%s fhir_token=%s",
            correlation["task_id"],
            correlation["context_id"],
            correlation["message_id"],
            callback_context.state["patient_id"],
            bool(callback_context.state["fhir_url"]),
            _token_fingerprint(callback_context.state["fhir_token"]),
        )
    else:
        logger.info(
            "hook_called_fhir_not_found task_id=%s context_id=%s message_id=%s metadata_keys=%s",
            correlation["task_id"],
            correlation["context_id"],
            correlation["message_id"],
            metadata_keys,
        )

    logger.info(
        "hook_called_exit task_id=%s context_id=%s message_id=%s patient_id=%s",
        correlation["task_id"],
        correlation["context_id"],
        correlation["message_id"],
        callback_context.state.get("patient_id", ""),
    )

    return None


# ─── TOOLS ────────────────────────────────────────────────────────────────────

def get_weather(city: str, tool_context: ToolContext) -> dict:
    """Retrieves the current weather report for a specified city."""
    patient_id = tool_context.state.get("patient_id", "unknown")
    logger.info("tool_get_weather_called city=%s patient_id=%s", city, patient_id)

    if city.lower() == "new york":
        return {
            "status": "success",
            "report": (
                f"The weather in New York is sunny with a temperature of 25 degrees "
                f"Celsius (77 degrees Fahrenheit). Patient context: {patient_id}"
            ),
        }
    return {
        "status": "error",
        "error_message": f"Weather information for '{city}' is not available.",
    }


def get_current_time(city: str, tool_context: ToolContext) -> dict:
    """Returns the current time in a specified city."""
    patient_id = tool_context.state.get("patient_id", "unknown")
    logger.info("tool_get_current_time_called city=%s patient_id=%s", city, patient_id)

    if city.lower() == "new york":
        tz  = ZoneInfo("America/New_York")
        now = datetime.datetime.now(tz)
        return {
            "status": "success",
            "report": f'The current time in {city} is {now.strftime("%Y-%m-%d %H:%M:%S %Z%z")}',
        }
    return {
        "status": "error",
        "error_message": f"Sorry, I don't have timezone information for {city}.",
    }


# ─── AGENT ────────────────────────────────────────────────────────────────────

root_agent = Agent(
    name="weather_time_agent",
    model="gemini-2.0-flash",
    description="Agent to answer questions about the time and weather in a city.",
    instruction="You are a helpful agent who can answer user questions about the time and weather in a city.",
    tools=[get_weather, get_current_time],
    before_model_callback=extract_fhir_context,
)


# ─── AGENT CARD — tells callers this agent requires an API key ─────────────────

agent_card = AgentCard(
    name="weather_time_agent",
    description="Agent to answer questions about the time and weather in a city.",
    url="http://localhost:8001",
    version="1.0.0",
    defaultInputModes=["text/plain"],
    defaultOutputModes=["text/plain"],
      capabilities=AgentCapabilities(
        streaming=True,
        pushNotifications=False,
        stateTransitionHistory=True,
        extensions=[
            AgentExtension(
                uri="http://localhost:5139/schemas/a2a/v1/fhir-context",
                description="FHIR context allowing the agent to query a FHIR server securely",
                required=False,
            )
        ],
    ),
    skills=[],
    
    securitySchemes={
        "apiKey": SecurityScheme(
            root=APIKeySecurityScheme(
                type="apiKey",
                name="X-API-Key",
                in_=In.header,        # ← correct field name and enum value
                description="API key required to access this agent."
            )
        )
    },
    security=[{"apiKey": []}],
)


# ─── WIRE IT ALL TOGETHER ──────────────────────────────────────────────────────

a2a_app = to_a2a(root_agent, port=8001, agent_card=agent_card)

# Add security middleware — this is what actually enforces the key check
a2a_app.add_middleware(ApiKeyMiddleware)
