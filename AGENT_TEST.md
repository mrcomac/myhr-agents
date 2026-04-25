# CV Evaluation Agent — Testing Guide

The agent exposes a single evaluation endpoint and supports two testing modes: **CLI** (no HTTP, no Docker) and **HTTP** (via Docker Compose or standalone container).

---

## Prerequisites

### AWS credentials

Create an `.env` file in the `agents/` directory (never commit this file):

```bash
# agents/.env
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_REGION=us-east-1
```

For CLI and local Python usage, source it:

```bash
set -a && source agents/.env && set +a
```

The model used is `anthropic.claude-sonnet-4-20250514-v1:0` — confirm it is enabled in your Bedrock account in the target region.

---

## Option 1 — CLI (no Docker, no server)

The fastest way to test. Runs the full agent pipeline directly from the command line.

### Setup

```bash
cd agents
pip install -r requirements.txt
```

### Run

```bash
cd agents/app

python cli.py \
  --cv /path/to/resume.pdf \
  --job-description "We are looking for a Senior Go Engineer to join our platform team." \
  --requirements "5+ years of Go, experience with PostgreSQL and Redis, REST API design." \
  --responsibilities "Build and maintain microservices, participate in code reviews, own service reliability."
```

### Pretty output (default)

```
Running CV evaluation...

Classification : Strong Candidate
Score          : 88/100

Summary:
The candidate has 7 years of Go experience with direct PostgreSQL and Redis usage...

Strengths:
  - 7 years of production Go experience
  - Led migration from monolith to microservices at previous employer
  - ...

Gaps:
  - No mention of REST API versioning strategy
  - ...

Reasoning:
The candidate exceeds the minimum experience threshold...
```

### JSON output (pipe-friendly)

```bash
python cli.py \
  --cv /path/to/resume.pdf \
  --job-description "..." \
  --requirements "..." \
  --responsibilities "..." \
  --output json
```

```json
{
  "classification": "Strong Candidate",
  "score": 88,
  "summary": "...",
  "strengths": ["..."],
  "gaps": ["..."],
  "reasoning": "..."
}
```

### Supported file formats

The agent reads the file as raw bytes and decodes as UTF-8 (fallback latin-1). Plain text CVs (`.txt`) and text-layer PDFs work best. Binary PDFs without a text layer will produce garbled input — use a pre-extracted `.txt` file in that case.

---

## Option 2 — Standalone container (no full stack)

Runs just the agent service as a Docker container, accessible on port 8084.

### Build

```bash
cd agents
docker build -t myhr-agent .
```

### Run

```bash
cd agents
docker run --rm -p 8084:8000 --env-file .env myhr-agent
```

### Health check

```powershell
curl.exe http://localhost:8084/health
# {"status":"healthy"}
```

### Evaluate a CV

> **Windows / PowerShell:** use `curl.exe` (not `curl` — that is an alias for `Invoke-WebRequest` and uses different syntax).

```powershell
curl.exe -X POST http://localhost:8084/cv-evaluation `
  -F "cv_file=@frontend_cv.pdf" `
  -F "job_description=We are looking for a talented Frontend Developer to join our team and help build intuitive, high-performance web applications. In this role, you will collaborate closely with designers, backend engineers, and product managers to translate user and business requirements into responsive and accessible interfaces using modern technologies such as HTML, CSS, JavaScript, and frameworks like React or Vue. You will be responsible for implementing clean, maintainable code, optimizing application performance, and ensuring cross-browser compatibility, while contributing to UI/UX improvements and best practices. The ideal candidate has a strong attention to detail, a passion for creating seamless user experiences, and experience working in agile environments with version control tools like Git." `
  -F "job_requirements=The ideal candidate should have solid experience with modern frontend technologies, including HTML5, CSS3, JavaScript (ES6+), and at least one major framework such as React or Vue.js, along with familiarity with TypeScript. A strong understanding of responsive design, cross-browser compatibility, and web performance optimization is essential. Experience working with RESTful APIs, version control systems like Git, and modern build tools (e.g., Webpack, Vite) is expected. Candidates should also demonstrate knowledge of UI/UX principles, accessibility standards, and testing practices. Strong problem-solving skills, attention to detail, and the ability to work collaboratively in an agile development environment are important, along with good communication skills in English." `
  -F "job_responsibilities=The Frontend Developer will be responsible for designing and implementing user-facing features, translating UI/UX designs into high-quality, responsive code, and ensuring the technical feasibility of visual concepts. They will collaborate with backend developers and product teams to integrate APIs and deliver seamless user experiences, while maintaining code quality through best practices, code reviews, and testing. The role also involves optimizing applications for maximum speed and scalability, ensuring cross-browser and cross-device compatibility, and continuously improving usability and accessibility. Additionally, the developer will troubleshoot and debug issues, contribute to technical decisions, and stay up to date with emerging frontend technologies and industry trends."
```

---

## Option 3 — Full Docker Compose stack

The agent runs as part of the full myHR stack, behind Traefik at the `/agent` prefix. A valid JWT is required for all requests.

### Start the stack

```bash
# From the api/ directory
cd api

# AWS credentials must be in the environment or .env file
docker compose -f deployments/docker/docker-compose.yml up -d
```

### Health check (through Traefik)

```bash
curl http://localhost:80/agent/health
# {"status":"healthy"}
```

### Get a JWT

```bash
TOKEN=$(curl -s -X POST http://localhost:80/login \
  -H "Content-Type: application/json" \
  -d '{"email":"recruiter@example.com","password":"yourpassword"}' \
  | jq -r '.token')
```

### Evaluate a CV (through Traefik)

```bash
curl -X POST http://localhost:80/agent/cv-evaluation \
  -H "Authorization: Bearer $TOKEN" \
  -F "cv_file=@/path/to/resume.pdf" \
  -F "job_description=Senior Go Engineer on our platform team." \
  -F "job_requirements=5+ years Go, PostgreSQL, Redis." \
  -F "job_responsibilities=Build microservices, own reliability."
```

### Rebuild the agent image after code changes

```bash
docker compose -f deployments/docker/docker-compose.yml build agent-service
docker compose -f deployments/docker/docker-compose.yml up -d agent-service
```

---

## Response schema

All three testing modes return the same JSON structure:

| Field | Type | Description |
|---|---|---|
| `classification` | `"Strong Candidate"` \| `"Potential Candidate"` \| `"Candidate"` | Overall classification |
| `score` | integer 0–100 | Numerical fit score |
| `summary` | string | 2–4 sentence summary |
| `strengths` | string[] | CV-backed strengths |
| `gaps` | string[] | Missing requirements |
| `reasoning` | string | Explanation of classification |

Score and classification are always consistent:

| Score | Classification |
|---|---|
| 85–100 | Strong Candidate |
| 65–84 | Potential Candidate |
| 0–64 | Candidate |

---

## Troubleshooting

**`KeyError: 'AWS_ACCESS_KEY_ID'`** — credentials not exported. Run the export commands at the top of this guide.

**`ValidationError` on startup** — the agent could not connect to Bedrock. Check that the model `anthropic.claude-sonnet-4-20250514-v1:0` is enabled in the AWS console for region `us-east-1`.

**`422 Unprocessable Entity`** — the request is missing a required form field (`cv_file`, `job_description`, `job_requirements`, or `job_responsibilities`).

**`401 Unauthorized` (Traefik only)** — the `Authorization: Bearer <token>` header is missing or the JWT has expired. Re-authenticate to get a fresh token.

**Garbled CV content** — the uploaded file is a scanned/image PDF with no text layer. Extract the text first (e.g. with `pdftotext`) and upload the resulting `.txt` file instead.
