"""
Healthcare Agent — a FHIR-connected clinical assistant.

This agent demonstrates how to build a healthcare-aware ADK agent that:
  - Receives FHIR context (server URL, bearer token, patient ID) via A2A metadata
  - Uses that context in tool functions to query the FHIR server securely
  - Returns structured clinical data that the LLM can reason over and summarise

The FHIR credentials arrive via fhir_hook.py (before_model_callback) and are
stored in session state.  Tool functions read them from tool_context.state —
they never appear in the prompt text and never need to be passed by the caller.

Tools provided:
  get_patient_demographics    Patient name, date of birth, gender, contact info
  get_active_medications      Active MedicationRequest resources
  get_active_conditions       Active Condition resources (problem list / diagnoses)
  get_recent_observations     Observation resources — vitals, labs, etc.

To build your own agent on top of this template:
  1. Replace or extend the tool functions below with your own FHIR queries.
  2. Update root_agent: change model, description, instruction, and tools list.
  3. The FHIR credentials are already in tool_context.state — no extra setup.

Runtime dependency: httpx (already installed as part of google-adk / a2a).
"""
import logging

import httpx
from google.adk.agents import Agent
from google.adk.tools import ToolContext

from .fhir_hook import extract_fhir_context

logger = logging.getLogger(__name__)

_FHIR_TIMEOUT = 15  # seconds


# ── Internal helpers ───────────────────────────────────────────────────────────

def _get_fhir_context(tool_context: ToolContext):
    """
    Read FHIR credentials from session state.

    Returns (fhir_url, fhir_token, patient_id) on success, or an error dict
    if any credential is missing (i.e. the caller did not send fhir-context).
    """
    fhir_url   = tool_context.state.get("fhir_url",   "").rstrip("/")
    fhir_token = tool_context.state.get("fhir_token", "")
    patient_id = tool_context.state.get("patient_id", "")

    if not fhir_url or not fhir_token or not patient_id:
        missing = [
            name for name, val in [
                ("fhir_url", fhir_url),
                ("fhir_token", fhir_token),
                ("patient_id", patient_id),
            ]
            if not val
        ]
        return {
            "status": "error",
            "error_message": (
                f"FHIR context is not available. Missing: {', '.join(missing)}. "
                "Ensure the caller includes 'fhir-context' in the A2A message metadata."
            ),
        }
    return fhir_url, fhir_token, patient_id


def _fhir_get(fhir_url: str, token: str, path: str, params: dict | None = None) -> dict:
    """
    Perform a FHIR REST GET request and return the parsed JSON body.

    Raises httpx.HTTPStatusError on 4xx/5xx responses.
    """
    response = httpx.get(
        f"{fhir_url}/{path}",
        params=params,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept":        "application/fhir+json",
        },
        timeout=_FHIR_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _http_error_result(exc: httpx.HTTPStatusError) -> dict:
    return {
        "status":        "error",
        "http_status":   exc.response.status_code,
        "error_message": f"FHIR server returned HTTP {exc.response.status_code}: {exc.response.text[:200]}",
    }


def _connection_error_result(exc: Exception) -> dict:
    return {
        "status":        "error",
        "error_message": f"Could not reach FHIR server: {exc}",
    }


def _coding_display(codings: list) -> str:
    """Return the first human-readable display text from a list of FHIR codings."""
    for c in codings:
        if c.get("display"):
            return c["display"]
    return "Unknown"


# ── Tool 1: Patient demographics ───────────────────────────────────────────────

def get_patient_demographics(tool_context: ToolContext) -> dict:
    """
    Fetches the demographic information for the current patient from the FHIR server.

    Returns name, date of birth, gender, and primary contact details.
    No arguments required — the patient identity comes from the session context.
    """
    ctx = _get_fhir_context(tool_context)
    if isinstance(ctx, dict):          # error dict
        return ctx
    fhir_url, fhir_token, patient_id = ctx

    logger.info("tool_get_patient_demographics patient_id=%s", patient_id)

    try:
        patient = _fhir_get(fhir_url, fhir_token, f"Patient/{patient_id}")
    except httpx.HTTPStatusError as e:
        return _http_error_result(e)
    except Exception as e:
        return _connection_error_result(e)

    # --- Parse name ---
    names = patient.get("name", [])
    official = next((n for n in names if n.get("use") == "official"), names[0] if names else {})
    given   = " ".join(official.get("given", []))
    family  = official.get("family", "")
    full_name = f"{given} {family}".strip() or "Unknown"

    # --- Parse telecom ---
    contacts = [
        {"system": t.get("system"), "value": t.get("value"), "use": t.get("use")}
        for t in patient.get("telecom", [])
    ]

    # --- Parse address ---
    addrs = patient.get("address", [])
    address = None
    if addrs:
        a = addrs[0]
        address = ", ".join(filter(None, [
            " ".join(a.get("line", [])),
            a.get("city"),
            a.get("state"),
            a.get("postalCode"),
            a.get("country"),
        ]))

    return {
        "status":       "success",
        "patient_id":   patient_id,
        "name":         full_name,
        "birth_date":   patient.get("birthDate"),
        "gender":       patient.get("gender"),
        "active":       patient.get("active"),
        "contacts":     contacts,
        "address":      address,
        "marital_status": (patient.get("maritalStatus") or {}).get("text"),
    }


# ── Tool 2: Active medications ─────────────────────────────────────────────────

def get_active_medications(tool_context: ToolContext) -> dict:
    """
    Retrieves the patient's current active medication list from the FHIR server.

    Queries MedicationRequest resources with status=active and returns medication
    names, dosage instructions, and prescribing dates.
    No arguments required.
    """
    ctx = _get_fhir_context(tool_context)
    if isinstance(ctx, dict):
        return ctx
    fhir_url, fhir_token, patient_id = ctx

    logger.info("tool_get_active_medications patient_id=%s", patient_id)

    try:
        bundle = _fhir_get(
            fhir_url, fhir_token, "MedicationRequest",
            params={"patient": patient_id, "status": "active", "_count": "50"},
        )
    except httpx.HTTPStatusError as e:
        return _http_error_result(e)
    except Exception as e:
        return _connection_error_result(e)

    medications = []
    for entry in bundle.get("entry", []):
        res = entry.get("resource", {})

        # Medication name: prefer medicationCodeableConcept, fall back to reference display
        med_concept = res.get("medicationCodeableConcept", {})
        med_name = (
            med_concept.get("text")
            or _coding_display(med_concept.get("coding", []))
            or res.get("medicationReference", {}).get("display", "Unknown")
        )

        dosage_instructions = [
            d.get("text", "No dosage text")
            for d in res.get("dosageInstruction", [])
        ]

        medications.append({
            "medication":   med_name,
            "status":       res.get("status"),
            "dosage":       dosage_instructions[0] if dosage_instructions else "Not specified",
            "authored_on":  res.get("authoredOn"),
            "requester":    (res.get("requester") or {}).get("display"),
        })

    return {
        "status":      "success",
        "patient_id":  patient_id,
        "count":       len(medications),
        "medications": medications,
    }


# ── Tool 3: Active conditions (problem list) ───────────────────────────────────

def get_active_conditions(tool_context: ToolContext) -> dict:
    """
    Retrieves the patient's active conditions / diagnoses from the FHIR server.

    Queries Condition resources with clinical-status=active and returns the
    problem list with diagnosis codes, severity, and onset dates.
    No arguments required.
    """
    ctx = _get_fhir_context(tool_context)
    if isinstance(ctx, dict):
        return ctx
    fhir_url, fhir_token, patient_id = ctx

    logger.info("tool_get_active_conditions patient_id=%s", patient_id)

    try:
        bundle = _fhir_get(
            fhir_url, fhir_token, "Condition",
            params={
                "patient":          patient_id,
                "clinical-status":  "active",
                "_count":           "50",
            },
        )
    except httpx.HTTPStatusError as e:
        return _http_error_result(e)
    except Exception as e:
        return _connection_error_result(e)

    conditions = []
    for entry in bundle.get("entry", []):
        res  = entry.get("resource", {})
        code = res.get("code", {})

        # ICD-10 / SNOMED display name
        condition_name = (
            code.get("text")
            or _coding_display(code.get("coding", []))
        )

        # Severity
        severity = (res.get("severity") or {}).get("text")

        # Onset — could be a date string or a period
        onset = res.get("onsetDateTime") or (res.get("onsetPeriod") or {}).get("start")

        conditions.append({
            "condition":     condition_name,
            "clinical_status": (
                (res.get("clinicalStatus") or {})
                .get("coding", [{}])[0]
                .get("code")
            ),
            "severity":      severity,
            "onset":         onset,
            "recorded_date": res.get("recordedDate"),
        })

    return {
        "status":     "success",
        "patient_id": patient_id,
        "count":      len(conditions),
        "conditions": conditions,
    }


# ── Tool 4: Recent observations (vitals / labs) ────────────────────────────────

def get_recent_observations(category: str, tool_context: ToolContext) -> dict:
    """
    Retrieves recent clinical observations for the patient from the FHIR server.

    Args:
        category: The FHIR observation category to retrieve. Common values:
                    'vital-signs'    — Blood pressure, heart rate, temperature, SpO2, etc.
                    'laboratory'     — Lab results (CBC, metabolic panel, HbA1c, etc.)
                    'social-history' — Smoking status, alcohol use, etc.
                  Defaults to 'vital-signs' if not specified.

    Returns the 20 most recent observations in the given category, sorted by date.
    """
    ctx = _get_fhir_context(tool_context)
    if isinstance(ctx, dict):
        return ctx
    fhir_url, fhir_token, patient_id = ctx

    # Normalise category; default to vital-signs
    category = (category or "vital-signs").strip().lower()
    logger.info("tool_get_recent_observations patient_id=%s category=%s", patient_id, category)

    try:
        bundle = _fhir_get(
            fhir_url, fhir_token, "Observation",
            params={
                "patient":   patient_id,
                "category":  category,
                "_sort":     "-date",
                "_count":    "20",
            },
        )
    except httpx.HTTPStatusError as e:
        return _http_error_result(e)
    except Exception as e:
        return _connection_error_result(e)

    observations = []
    for entry in bundle.get("entry", []):
        res  = entry.get("resource", {})
        code = res.get("code", {})

        obs_name = code.get("text") or _coding_display(code.get("coding", []))

        # Value — could be a Quantity, CodeableConcept, or string
        value = None
        unit  = None
        if "valueQuantity" in res:
            vq    = res["valueQuantity"]
            value = vq.get("value")
            unit  = vq.get("unit") or vq.get("code")
        elif "valueCodeableConcept" in res:
            value = (res["valueCodeableConcept"].get("text")
                     or _coding_display(res["valueCodeableConcept"].get("coding", [])))
        elif "valueString" in res:
            value = res["valueString"]

        # Components (e.g. systolic/diastolic BP)
        components = []
        for comp in res.get("component", []):
            comp_name = (comp.get("code") or {}).get("text") or _coding_display(
                (comp.get("code") or {}).get("coding", [])
            )
            comp_vq = comp.get("valueQuantity", {})
            components.append({
                "name":  comp_name,
                "value": comp_vq.get("value"),
                "unit":  comp_vq.get("unit") or comp_vq.get("code"),
            })

        observations.append({
            "observation":    obs_name,
            "value":          value,
            "unit":           unit,
            "components":     components or None,
            "effective_date": res.get("effectiveDateTime") or (res.get("effectivePeriod") or {}).get("start"),
            "status":         res.get("status"),
            "interpretation": (
                (res.get("interpretation") or [{}])[0].get("text")
                or _coding_display((res.get("interpretation") or [{}])[0].get("coding", []))
            ),
        })

    return {
        "status":       "success",
        "patient_id":   patient_id,
        "category":     category,
        "count":        len(observations),
        "observations": observations,
    }


# ── Agent ──────────────────────────────────────────────────────────────────────

root_agent = Agent(
    name="healthcare_fhir_agent",
    model="gemini-2.0-flash",
    description=(
        "A clinical assistant that can query a patient's FHIR health record "
        "to answer questions about demographics, medications, conditions, and observations."
    ),
    instruction=(
        "You are a clinical assistant with secure, read-only access to a patient's FHIR health record. "
        "Use the available tools to retrieve real data from the connected FHIR server when answering questions. "
        "Always fetch data using the tools — never make up or guess clinical information. "
        "Present medical information clearly and concisely, as if briefing a clinician. "
        "If a tool returns an error, explain what went wrong and suggest how to resolve it. "
        "If FHIR context is not available, let the caller know they need to include it in their request."
    ),
    tools=[
        get_patient_demographics,
        get_active_medications,
        get_active_conditions,
        get_recent_observations,
    ],
    # Runs before every LLM call. Reads fhir_url, fhir_token, and patient_id
    # from the A2A message metadata and writes them into session state so the
    # tools above can use them without them ever appearing in the prompt.
    before_model_callback=extract_fhir_context,
)
