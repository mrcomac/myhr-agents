"""
consumer.py — RabbitMQ consumer for CV evaluation messages.

Routing:
  hr.events (topic exchange)  →  cv_evaluation queue
  on nack (>3 retries)        →  cv_evaluation.dlq
  on rate-limit               →  cv_evaluation.delay  (30 s TTL, re-routes back)

Idempotency (Redis):
  cv_eval:processing:{loopId}  — set while in-flight (TTL 5 min)
  cv_eval:done:{loopId}        — set after success (TTL 24 h)

Daily rate limiting (Redis):
  cv_eval:daily:{YYYY-MM-DD}   — incrementing counter (TTL 25 h)
"""

import json
import logging
import os
import tempfile
import time
from datetime import date, timedelta

import pika
import redis
import requests
from minio import Minio
from minio.error import S3Error

from app import build_agent, CVEvaluationOutput
from helpers.aws_credentials import AWS_ACCESS_KEY, AWS_REGION, AWS_SECRET_KEY
from helpers.model_definitions import MODEL_ID, calculate_cost_for_model
from prompts.CVEvaluationPrompt import CVEvaluationPrompt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("consumer")

# ── Configuration ──────────────────────────────────────────────────────────────

RABBITMQ_URL        = os.environ["RABBITMQ_URL"]
REDIS_URL           = os.environ.get("REDIS_URL", "redis://localhost:6379")
MINIO_ENDPOINT      = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY    = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY    = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
MINIO_USE_SSL       = os.environ.get("MINIO_USE_SSL", "false").lower() == "true"
MINIO_BUCKET        = os.environ.get("STORAGE_BUCKET", "myhr")
JOB_SERVICE_URL     = os.environ.get("JOB_SERVICE_URL", "http://localhost:8082")
LOOP_SERVICE_URL    = os.environ.get("LOOP_SERVICE_URL", "http://localhost:8083")
CV_EVAL_DAILY_LIMIT = int(os.environ.get("CV_EVAL_DAILY_LIMIT", "100"))

# ── Topology constants (must match publisher.go) ───────────────────────────────

HR_EXCHANGE       = "hr.events"
CV_EVAL_QUEUE     = "cv_evaluation"
CV_EVAL_DLQ       = "cv_evaluation.dlq"
CV_EVAL_DELAY_Q   = "cv_evaluation.delay"
DLQ_EXCHANGE      = "hr.events.dlq"
CV_EVAL_ROUTING_KEY = "application.cv_evaluation"
MAX_RETRIES       = 3

# Redis key prefixes / TTLs
KEY_PROCESSING = "cv_eval:processing:{loop_id}"
KEY_DONE       = "cv_eval:done:{loop_id}"
KEY_DAILY      = "cv_eval:daily:{date}"
TTL_PROCESSING = 5 * 60        # 5 minutes
TTL_DONE       = 24 * 60 * 60  # 24 hours
TTL_DAILY      = 25 * 60 * 60  # 25 hours (covers the full day + buffer)


# ── Clients ────────────────────────────────────────────────────────────────────

def make_redis() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def make_minio() -> Minio:
    return Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_USE_SSL,
    )


# ── Job service helpers ────────────────────────────────────────────────────────

def fetch_job(job_id: str) -> dict:
    """Fetch job details from job-service (internal, unauthenticated)."""
    resp = requests.get(f"{JOB_SERVICE_URL}/jobs/{job_id}", timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── Loop service helpers ───────────────────────────────────────────────────────

def store_cv_evaluation(loop_id: str, result: CVEvaluationOutput, cost: str = "") -> None:
    """POST evaluation result to loop-service internal endpoint."""
    payload = {
        "classification": result.classification,
        "score": result.score,
        "summary": result.summary,
        "strengths": result.strengths,
        "gaps": result.gaps,
        "reasoning": result.reasoning,
    }
    if cost:
        payload["cost"] = cost
    resp = requests.post(
        f"{LOOP_SERVICE_URL}/internal/loops/{loop_id}/cv-evaluation",
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()


# ── Retry counting ─────────────────────────────────────────────────────────────

def retry_count(properties: pika.BasicProperties) -> int:
    """Return the number of times this message has been dead-lettered."""
    headers = properties.headers or {}
    deaths = headers.get("x-death", [])
    if not deaths:
        return 0
    return sum(d.get("count", 0) for d in deaths)


# ── Rate-limit helpers ─────────────────────────────────────────────────────────

def check_rate_limit(rdb: redis.Redis) -> bool:
    """
    Increment daily counter. Returns True if within limit, False if exceeded.
    Uses a pipeline + INCR + EXPIRE to be atomic enough for our needs.
    """
    key = KEY_DAILY.format(date=date.today().isoformat())
    pipe = rdb.pipeline()
    pipe.incr(key)
    pipe.expire(key, TTL_DAILY)
    count, _ = pipe.execute()
    return int(count) <= CV_EVAL_DAILY_LIMIT


def requeue_after_delay(channel: pika.channel.Channel, body: bytes, properties: pika.BasicProperties) -> None:
    """Publish the message to the delay queue so it re-enters after 30 s."""
    channel.basic_publish(
        exchange="",
        routing_key=CV_EVAL_DELAY_Q,
        body=body,
        properties=pika.BasicProperties(
            delivery_mode=2,
            headers=properties.headers,
            content_type=properties.content_type,
        ),
    )


# ── Core processing ────────────────────────────────────────────────────────────

def process_cv_evaluation(
    channel: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    properties: pika.BasicProperties,
    body: bytes,
    rdb: redis.Redis,
    minio: Minio,
) -> None:
    tag = method.delivery_tag

    # 1. Parse message
    try:
        msg = json.loads(body)
    except json.JSONDecodeError:
        log.error("invalid JSON body — discarding")
        channel.basic_nack(delivery_tag=tag, requeue=False)
        return

    msg_type = msg.get("type", "")
    if msg_type != "application.cv_evaluation":
        log.warning("unknown message type %s — discarding", msg_type)
        channel.basic_nack(delivery_tag=tag, requeue=False)
        return

    loop_id    = msg.get("loopId", "")
    job_id     = msg.get("jobId", "")
    cv_path    = msg.get("cvMinioPath", "")
    force      = msg.get("force", False)

    if not (loop_id and job_id and cv_path):
        log.error("missing required fields in message — discarding: %s", msg)
        channel.basic_nack(delivery_tag=tag, requeue=False)
        return

    log.info("processing CV evaluation loopId=%s jobId=%s force=%s", loop_id, job_id, force)

    # 2. Idempotency — skip if already done (bypassed when force=True)
    done_key = KEY_DONE.format(loop_id=loop_id)
    if not force and rdb.exists(done_key):
        log.info("loopId=%s already evaluated — acking duplicate", loop_id)
        channel.basic_ack(delivery_tag=tag)
        return

    # 3. Idempotency — skip if another consumer is processing it
    processing_key = KEY_PROCESSING.format(loop_id=loop_id)
    acquired = rdb.set(processing_key, "1", nx=True, ex=TTL_PROCESSING)
    if not acquired:
        log.info("loopId=%s already in-flight — nacking for requeue", loop_id)
        channel.basic_nack(delivery_tag=tag, requeue=True)
        return

    try:
        # 4. Daily rate limit
        if not check_rate_limit(rdb):
            log.warning("daily rate limit reached — delaying loopId=%s", loop_id)
            requeue_after_delay(channel, body, properties)
            channel.basic_ack(delivery_tag=tag)
            return

        # 5. Download CV from MinIO
        try:
            response = minio.get_object(MINIO_BUCKET, cv_path)
            cv_bytes = response.read()
        except S3Error as exc:
            log.error("MinIO error for loopId=%s: %s", loop_id, exc)
            _handle_failure(channel, tag, properties, loop_id)
            return

        # 6. Save to temp file so the agent can read it
        suffix = "." + cv_path.rsplit(".", 1)[-1] if "." in cv_path else ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(cv_bytes)
            tmp_path = tmp.name

        try:
            # 7. Fetch job details
            try:
                job = fetch_job(job_id)
            except Exception as exc:
                log.error("failed to fetch job %s: %s", job_id, exc)
                _handle_failure(channel, tag, properties, loop_id)
                return

            # 8. Run agent evaluation
            try:
                agent = build_agent("CVEvaluationAgent", CVEvaluationPrompt)
                cv_content = agent.tool.file_read(tmp_path)
                user_prompt = CVEvaluationPrompt.USER.format(
                    job_description=job.get("description", ""),
                    job_requirements=job.get("requirements", ""),
                    job_responsibilities=job.get("responsibilities", ""),
                    candidate_cv=cv_content,
                )
                agent_response = agent(user_prompt)
                result: CVEvaluationOutput = agent_response.structured_output
                usage = agent_response.metrics.accumulated_usage
                cost = calculate_cost_for_model(
                    model=MODEL_ID,
                    input_tokens=usage["inputTokens"],
                    output_tokens=usage["outputTokens"]
                )
            except Exception as exc:
                log.error("agent evaluation failed loopId=%s: %s", loop_id, exc)
                _handle_failure(channel, tag, properties, loop_id)
                return

            # 9. Store result in loop-service
            try:
                store_cv_evaluation(loop_id, result, cost)
            except Exception as exc:
                log.error("failed to store evaluation loopId=%s: %s", loop_id, exc)
                _handle_failure(channel, tag, properties, loop_id)
                return

        finally:
            import os
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        # 10. Mark done and ack
        rdb.set(done_key, "1", ex=TTL_DONE)
        channel.basic_ack(delivery_tag=tag)
        log.info("CV evaluation complete loopId=%s score=%d", loop_id, result.score)

    finally:
        rdb.delete(processing_key)


def _mark_evaluation_failed(loop_id: str) -> None:
    """Notify loop-service that CV evaluation has permanently failed for this loop."""
    try:
        resp = requests.post(
            f"{LOOP_SERVICE_URL}/internal/loops/{loop_id}/cv-evaluation/failed",
            timeout=10,
        )
        if resp.status_code not in (200, 204):
            log.error("failed to mark evaluation as failed loopId=%s status=%d", loop_id, resp.status_code)
        else:
            log.info("marked evaluation as failed loopId=%s", loop_id)
    except Exception as exc:
        log.error("error calling failed endpoint loopId=%s: %s", loop_id, exc)


def _handle_failure(
    channel: pika.channel.Channel,
    tag: int,
    properties: pika.BasicProperties,
    loop_id: str,
) -> None:
    """Nack. After MAX_RETRIES the broker will dead-letter to cv_evaluation.dlq."""
    retries = retry_count(properties)
    if retries >= MAX_RETRIES:
        log.error("loopId=%s exceeded max retries (%d) — sending to DLQ", loop_id, MAX_RETRIES)
        _mark_evaluation_failed(loop_id)
        channel.basic_nack(delivery_tag=tag, requeue=False)
    else:
        log.warning("loopId=%s retry %d/%d", loop_id, retries + 1, MAX_RETRIES)
        channel.basic_nack(delivery_tag=tag, requeue=False)  # broker dead-letters then re-routes


# ── Entry point ────────────────────────────────────────────────────────────────

def connect_rabbitmq() -> pika.BlockingConnection:
    """Connect to RabbitMQ with exponential backoff retry."""
    params = pika.URLParameters(RABBITMQ_URL)
    for attempt in range(1, 11):
        try:
            return pika.BlockingConnection(params)
        except pika.exceptions.AMQPConnectionError as exc:
            if attempt == 10:
                raise
            wait = min(2 ** attempt, 30)
            log.warning("RabbitMQ not ready, retrying in %ds (attempt %d/10): %s", wait, attempt, exc)
            time.sleep(wait)


def main() -> None:
    rdb = make_redis()
    minio = make_minio()

    connection = connect_rabbitmq()
    channel = connection.channel()

    # Declare topology (idempotent — safe to call on consumer startup too)
    channel.exchange_declare(DLQ_EXCHANGE, "fanout", durable=True)
    channel.queue_declare(CV_EVAL_DLQ, durable=True)
    channel.queue_bind(CV_EVAL_DLQ, DLQ_EXCHANGE, routing_key="#")

    channel.exchange_declare(HR_EXCHANGE, "topic", durable=True)

    channel.queue_declare(
        CV_EVAL_QUEUE,
        durable=True,
        arguments={"x-dead-letter-exchange": DLQ_EXCHANGE},
    )
    channel.queue_bind(CV_EVAL_QUEUE, HR_EXCHANGE, routing_key=CV_EVAL_ROUTING_KEY)

    channel.queue_declare(
        CV_EVAL_DELAY_Q,
        durable=True,
        arguments={
            "x-message-ttl": 30_000,
            "x-dead-letter-exchange": HR_EXCHANGE,
            "x-dead-letter-routing-key": CV_EVAL_ROUTING_KEY,
        },
    )

    # One message at a time per consumer instance
    channel.basic_qos(prefetch_count=1)

    def on_message(ch, method, properties, body):
        process_cv_evaluation(ch, method, properties, body, rdb, minio)

    channel.basic_consume(queue=CV_EVAL_QUEUE, on_message_callback=on_message)
    log.info("consumer started — waiting for CV evaluation messages")
    channel.start_consuming()


if __name__ == "__main__":
    main()
