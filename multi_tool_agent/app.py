"""
Application entry point — wires the ADK agent into an A2A ASGI server.

Start the server with:
    uvicorn multi_tool_agent.app:a2a_app --host 0.0.0.0 --port 8001

The agent card is served publicly at:
    GET http://localhost:8001/.well-known/agent-card.json

All other endpoints require an X-API-Key header (see middleware.py).
"""
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    APIKeySecurityScheme,
    In,
    SecurityScheme,
)
from google.adk.a2a.utils.agent_to_a2a import to_a2a

from .agent import root_agent
from .middleware import ApiKeyMiddleware


# ── Agent Card ─────────────────────────────────────────────────────────────────
# Served publicly so callers can discover the agent's capabilities and learn
# that an API key is required before sending any messages.

agent_card = AgentCard(
    name="healthcare_fhir_agent",
    description=(
        "A clinical assistant that queries a patient's FHIR health record to answer "
        "questions about demographics, active medications, conditions, and observations."
    ),
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
                # This URI is the agreed-upon key under which callers send
                # FHIR credentials in the A2A message metadata.
                uri="http://localhost:5139/schemas/a2a/v1/fhir-context",
                description="FHIR context allowing the agent to query a FHIR server securely.",
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
                in_=In.header,
                description="API key required to access this agent.",
            )
        )
    },
    security=[{"apiKey": []}],
)


# ── ASGI App ───────────────────────────────────────────────────────────────────

a2a_app = to_a2a(root_agent, port=8001, agent_card=agent_card)
a2a_app.add_middleware(ApiKeyMiddleware)
