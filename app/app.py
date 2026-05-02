import os
import shutil
import tempfile
from pathlib import Path

import boto3
import uvicorn
import logging
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from strands import Agent
from strands.models import BedrockModel
from strands_tools import file_read

from helpers.aws_credentials import AWS_ACCESS_KEY, AWS_REGION, AWS_SECRET_KEY
from helpers.outputs import CVEvaluationOutput, JobDescriptionOutput
from prompts.CVEvaluationPrompt import CVEvaluationPrompt
from prompts.JobDescriptionPrompt import JOBDescriptionPrompt
from prompts.BasePromptModel import BasePromptModel
from strands import Agent, ModelRetryStrategy
from helpers.model_definitions import MODEL_ID, calculate_cost_for_model
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

app = FastAPI(title="CV Evaluation Agent")
FastAPIInstrumentor().instrument_app(app)

logging.getLogger().setLevel(logging.INFO)

UPLOADS_DIR = Path("/app/uploads")

# ── Locale → language name mapping ────────────────────────────────────────────

_LOCALE_MAP: dict[str, str] = {
    "en":    "English",
    "en-US": "English",
    "en-GB": "English",
    "pt-BR": "Brazilian Portuguese",
    "pt":    "Portuguese",
    "es":    "Spanish",
    "es-ES": "Spanish",
    "fr":    "French",
    "de":    "German",
    "it":    "Italian",
    "nl":    "Dutch",
    "pl":    "Polish",
    "ru":    "Russian",
    "zh":    "Simplified Chinese",
    "ja":    "Japanese",
    "ko":    "Korean",
    "ar":    "Arabic",
}

_LOCALE  = os.environ.get("LOCALE", "en")
_LANGUAGE = _LOCALE_MAP.get(_LOCALE, "English")


# ── Agent factory ─────────────────────────────────────────────────────────────

def _bedrock_model() -> BedrockModel:
    session = boto3.Session(
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
        aws_session_token=None,
        region_name=AWS_REGION,
    )
    return BedrockModel(
        model_id=MODEL_ID,
        temperature=0.3,
        boto_session=session,
    )


def build_agent(agent_name: str, prompt: BasePromptModel, language: str = _LANGUAGE) -> Agent:
    """Construct a fresh Bedrock-backed Strands agent for CV evaluation."""
    system_prompt = prompt.SYSTEM + prompt.LANGUAGE_INSTRUCTION.format(language=language)
    if agent_name == "CVEvaluationAgent":
        return Agent(
            model=_bedrock_model(),
            tools=[file_read],
            system_prompt=system_prompt,
            structured_output_model=CVEvaluationOutput,
            retry_strategy=ModelRetryStrategy(
                max_attempts=3,
                initial_delay=2,
                max_delay=60,
            )
        )
    elif agent_name == "JOBDescriptionAgent":
        return Agent(
            model=_bedrock_model(),
            system_prompt=system_prompt,
            structured_output_model=JobDescriptionOutput,
            retry_strategy=ModelRetryStrategy(
                max_attempts=3,
                initial_delay=2,
                max_delay=60,
            )
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.post("/cv-evaluation")
async def cv_evaluation(
    cv_file: UploadFile = File(..., description="CV file (PDF, TXT, DOCX, etc.)"),
    job_description: str = Form(...),
    job_requirements: str = Form(...),
    job_responsibilities: str = Form(...),
):
    """
    Evaluate a candidate CV against a job posting.

    Accepts multipart/form-data with:
    - cv_file: the candidate's CV file
    - job_description, job_requirements, job_responsibilities: job details as text
    """
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(cv_file.filename).suffix if cv_file.filename else ".bin"
    tmp_path = None

    try:
        # Save the upload to a named temp file
        with tempfile.NamedTemporaryFile(
            dir=UPLOADS_DIR, suffix=suffix, delete=False
        ) as tmp:
            shutil.copyfileobj(cv_file.file, tmp)
            tmp_path = tmp.name

        agent = build_agent("CVEvaluationAgent", CVEvaluationPrompt)
        candidate_cv = agent.tool.file_read(tmp_path)
        user_prompt = CVEvaluationPrompt.USER.format(
            job_description=job_description,
            job_requirements=job_requirements,
            job_responsibilities=job_responsibilities,
            candidate_cv=candidate_cv,
        )

        response = agent(user_prompt)
        usage = result.metrics.accumulated_usage
        costs = calculate_cost_for_model(
            model=MODEL_ID,
            input_tokens=usage["inputTokens"],
            output_tokens=usage["outputTokens"]
        )
        result: CVEvaluationOutput = response.structured_output
        return {"result": result.model_dump(mode="json"), "cost": costs}

    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        print(f"Exception in cv_evaluation: {exc}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

@app.post("/job-description")
async def job_description(
    seed_job_description: str = Form(...),
    seed_job_requirements: str = Form(...),
    seed_job_responsibilities: str = Form(...),
):
    """
    Generate a job description based on a seed description.

    Accepts form data with:
    - seed_job_description: the initial job description to expand
    """
    try:
        agent = build_agent("JOBDescriptionAgent", JOBDescriptionPrompt)
        user_prompt = JOBDescriptionPrompt.USER.format(
            seed_job_description=seed_job_description,
            seed_job_requirements=seed_job_requirements,
            seed_job_responsibilities=seed_job_responsibilities,
        )

        response = agent(user_prompt)
        print(f"JD Response type: {type(response)}")
        print(f"JD Response attributes: {dir(response)}")
        usage = response.metrics.accumulated_usage
        costs = calculate_cost_for_model(
            model=MODEL_ID,
            input_tokens=usage["inputTokens"],
            output_tokens=usage["outputTokens"]
        )
        result: JobDescriptionOutput = response.structured_output
        return {"result": result.model_dump(mode="json"), "cost": costs}

    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        print(f"Exception in job_description: {exc}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(exc)) from exc

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
