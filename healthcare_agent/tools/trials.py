import json
import logging
import os
from google.adk.tools import ToolContext

logger = logging.getLogger(__name__)

def search_clinical_trials(tool_context: ToolContext) -> dict:
    """
    Reads the local trials_database.json and returns the full list of clinical trials,
    including their inclusion and exclusion criteria.
    """
    logger.info("tool_search_clinical_trials called")
    file_path = os.path.join(os.path.dirname(__file__), "..", "trials_database.json")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            trials = json.load(f)
        return {
            "status": "success",
            "count": len(trials),
            "trials": trials
        }
    except Exception as e:
        logger.error("Failed to read trials database: %s", str(e))
        return {
            "status": "error",
            "error_message": f"Could not read trials_database.json: {str(e)}"
        }
