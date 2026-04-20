from pydantic import BaseModel

class CVEvaluationRequest(BaseModel):
    prompt: str
    job_description: str
    job_requirements: str
    job_responsibilities: str
