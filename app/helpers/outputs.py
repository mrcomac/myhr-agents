from pydantic import BaseModel, Field, conint, model_validator
from typing import List, Literal

class JobDescriptionOutput(BaseModel):
    description: str = Field(description="Generated job description based on the seed input")
    requirements: str = Field(description="List of key requirements for the job")
    responsibilities: str = Field(description="List of key responsibilities for the job")

class CVEvaluationOutput(BaseModel):
    classification: Literal[
        "Strong Candidate",
        "Potential Candidate",
        "Candidate",
    ] = Field(description="Final classification of the candidate based on job fit")

    score: conint(ge=0, le=100) = Field(
        description="Score from 0 to 100 representing overall fit"
    )

    summary: str = Field(
        description="Short paragraph summarizing how well the candidate fits the role"
    )

    strengths: List[str] = Field(
        description="List of key strengths aligned with the job requirements"
    )

    gaps: List[str] = Field(
        description="List of missing skills, experience, or mismatches"
    )

    reasoning: str = Field(
        description="Clear explanation of why this classification was chosen"
    )

    @model_validator(mode="after")
    def validate_score_vs_classification(self):
        if self.score >= 85 and self.classification != "Strong Candidate":
            raise ValueError("Score suggests Strong Candidate")
        if 65 <= self.score < 85 and self.classification != "Potential Candidate":
            raise ValueError("Score suggests Potential Candidate")
        if self.score < 65 and self.classification != "Candidate":
            raise ValueError("Score suggests Candidate")
        return self

