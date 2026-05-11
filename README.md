# Omni-Match A2A: Oncology Orchestrator 🧬🤖

Omni-Match A2A is a secure, Agent-to-Agent (A2A) orchestration system designed for precision oncology clinical trial matching. Unlike traditional LLM wrappers that are prone to hallucinating medical data, Omni-Match employs a strict **Hybrid Algorithmic + AI Architecture**. It pre-hydrates unstructured medical data via FHIR APIs and uses deterministic Python guardrails to ensure HIPAA-compliant, hallucination-free evaluations.

## 🌟 Key Features

* **Zero-Hallucination Architecture:** The AI is completely stripped of native tool-calling autonomy. Instead, a deterministic Python backend fetches, scrubs, and formats the patient data *before* feeding it to the LLM.
* **Agent-to-Agent (A2A) Routing:** Features a "Blind Router" front-end (Po) that refuses to give medical advice, strictly forwarding clinical evaluation requests to the secure backend evaluator.
* **PII Scrubbing & Compliance:** Extracts raw FHIR data and completely scrubs Direct Identifiers (PII). Patient age is calculated dynamically to avoid transmitting exact Dates of Birth (DOB) to external AI models.
* **Dynamic Missing Data Resolution:** If critical clinical variables (e.g., PD-L1 biomarkers, ECOG status) are missing, the backend halts the evaluation and triggers a `HALT_AND_ASK` command, prompting the front-end to request the specific missing data from the user.

## ⚙️ How it Works (The Flow)

1. **User Request:** User asks the Front-end Agent to evaluate a patient for oncology trials.
2. **Strict Routing:** Front-end blindly routes the request to the Python Backend via Webhooks/Ngrok.
3. **Data Hydration:** Backend extracts the FHIR token, pulls patient demographics, conditions, and labs, and scrubs the PII.
4. **Deterministic Filtering:** Hard programmatic rules (e.g., Age > 18, Pregnancy status) are applied.
5. **LLM Evaluation:** The sanitized, *pre-hydrated* data is sent to the LLM solely for complex unstructured clinical reasoning.
6. **Verdict Delivery:** The LLM returns an Eligibility status, which the Front-end formats into a clean, professional summary.

## 🛠️ Tech Stack
* **Language:** Python
* **Backend Framework:** FastAPI / Flask (Dockerized)
* **AI/LLM:** Google Gemini Flash (Via AI Studio)
* **Data Standard:** FHIR (Fast Healthcare Interoperability Resources) API
* **Orchestration:** Prompt Opinion A2A Platform

## 🚀 Local Installation & Setup

Follow these steps to run the Omni-Match backend locally and connect it to the Prompt Opinion front-end agent.

### 📋 Prerequisites
Before you begin, ensure you have the following installed on your machine:
* **Docker & Docker Compose** (Recommended for isolated environment)
* **Python 3.10+** (If running without Docker)
* **Ngrok** (To expose your local server to the external Prompt Opinion platform)
* A Free API Key from [Google AI Studio](https://aistudio.google.com/app/apikey)

---

### Step 1: Clone the Repository
Open your terminal and clone this project to your local machine:
```bash
git clone https://github.com/Paras-suryavanshi/Precision-Oncology-Matchmaker.git
cd po-adk-python
