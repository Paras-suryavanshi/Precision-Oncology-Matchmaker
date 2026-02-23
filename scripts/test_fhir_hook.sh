#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8001}"
RPC_URL="${BASE_URL%/}/"
API_KEY="${API_KEY:-my-secret-key-123}"

post_json() {
  local label="$1"
  local with_key="$2"
  local payload="$3"

  echo
  echo "===== ${label} ====="
  if [[ "$with_key" == "yes" ]]; then
    curl -sS -i -X POST "$RPC_URL" \
      -H 'Content-Type: application/json' \
      -H "X-API-Key: ${API_KEY}" \
      --data "$payload"
  else
    curl -sS -i -X POST "$RPC_URL" \
      -H 'Content-Type: application/json' \
      --data "$payload"
  fi
  echo
}

payload_no_metadata='{
  "jsonrpc": "2.0",
  "id": "case-b",
  "method": "message/send",
  "params": {
    "message": {
      "kind": "message",
      "message_id": "case-b-message",
      "role": "user",
      "parts": [
        {"kind": "text", "text": "What is the weather in New York?"}
      ]
    }
  }
}'

payload_wrong_key='{
  "jsonrpc": "2.0",
  "id": "case-c",
  "method": "message/send",
  "params": {
    "metadata": {
      "custom-context": {
        "fhirUrl": "https://fhir.example.org",
        "fhirToken": "token-should-not-be-used",
        "patientId": "patient-wrong-key"
      }
    },
    "message": {
      "kind": "message",
      "message_id": "case-c-message",
      "role": "user",
      "parts": [
        {"kind": "text", "text": "What time is it in New York?"}
      ]
    }
  }
}'

payload_valid_fhir='{
  "jsonrpc": "2.0",
  "id": "case-d",
  "method": "message/send",
  "params": {
    "metadata": {
      "http://localhost:5139/schemas/a2a/v1/fhir-context": {
        "fhirUrl": "https://fhir.example.org",
        "fhirToken": "token-sensitive-123456",
        "patientId": "patient-42"
      }
    },
    "message": {
      "kind": "message",
      "message_id": "case-d-message",
      "role": "user",
      "parts": [
        {"kind": "text", "text": "What is the weather in New York?"}
      ]
    }
  }
}'

payload_malformed_fhir='{
  "jsonrpc": "2.0",
  "id": "case-e",
  "method": "message/send",
  "params": {
    "metadata": {
      "http://localhost:5139/schemas/a2a/v1/fhir-context": "this-is-not-a-json-object"
    },
    "message": {
      "kind": "message",
      "message_id": "case-e-message",
      "role": "user",
      "parts": [
        {"kind": "text", "text": "What time is it in New York?"}
      ]
    }
  }
}'

echo "Target RPC endpoint: ${RPC_URL}"
echo "Using API key prefix: ${API_KEY:0:6}..."
echo "Run your server separately, for example: uvicorn multi_tool_agent.agent:a2a_app --host 127.0.0.1 --port 8001 --log-level info"

# Case A: no API key
post_json "Case A - Missing API key (expect 401, hook not called)" "no" "$payload_no_metadata"

# Case B: valid key, no metadata
post_json "Case B - Valid API key + no metadata (expect hook_called_no_metadata)" "yes" "$payload_no_metadata"

# Case C: valid key, wrong metadata key
post_json "Case C - Valid API key + wrong metadata key (expect hook_called_fhir_not_found)" "yes" "$payload_wrong_key"

# Case D: valid key, proper FHIR metadata
post_json "Case D - Valid API key + FHIR metadata (expect hook_called_fhir_found + patient-42 in tool log)" "yes" "$payload_valid_fhir"

# Case E: malformed fhir payload
post_json "Case E - Valid API key + malformed FHIR metadata (expect hook_called_fhir_malformed)" "yes" "$payload_malformed_fhir"

echo
echo "Expected server log markers to check in terminal:"
echo "  security_rejected_missing_api_key"
echo "  hook_called_enter"
echo "  hook_called_no_metadata"
echo "  hook_called_fhir_not_found"
echo "  hook_called_fhir_found"
echo "  hook_called_fhir_malformed"
echo "  tool_get_weather_called / tool_get_current_time_called with patient_id"
