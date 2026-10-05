import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict, deque

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.models import ChatRequest, FeedbackRequest
from app.router import classify_question
from app.services.demo_service import demo_answer, demo_retrieve


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("nsp-api")
settings = get_settings()

app = FastAPI(title="NSP Conversational AI API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

histories: dict[str, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=12))
retriever = None
answerer = None

if not settings.demo_mode:
    from app.services.bigquery_service import BigQueryRetriever
    from app.services.gemini_service import GeminiAnswerer

    retriever = BigQueryRetriever(settings)
    answerer = GeminiAnswerer(settings)


def sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/api/health")
async def health():
    if settings.demo_mode:
        return {
            "status": "ok",
            "mode": "demo",
            "bigquery_ok": False,
            "vertex_ok": False,
            "embedded_record_count": 8674,
        }
    bigquery_ok, count = await retriever.health()
    return {
        "status": "ok" if bigquery_ok else "degraded",
        "mode": "gcp",
        "bigquery_ok": bigquery_ok,
        "vertex_ok": True,
        "embedded_record_count": count,
    }


@app.get("/api/suggestions")
async def suggestions():
    return {
        "suggestions": [
            "Find young football athletes in Saudi Arabia",
            "Show me active female judo players",
            "What information is available about referees?",
            "Tell me about athletes in Riyadh",
        ]
    }


@app.post("/api/chat")
async def chat(request: ChatRequest):
    conversation_id = request.conversation_id or str(uuid.uuid4())
    top_k = request.top_k or settings.top_k

    async def event_stream():
        started = time.perf_counter()
        try:
            if classify_question(request.message) == "aggregate":
                message = (
                    "That's a counting question. Semantic search returns the most "
                    "relevant sample records, not totals. Aggregate query support is "
                    "planned, so I won't give you a potentially incorrect number."
                )
                yield sse("retrieval", {"records": [], "count": 0, "latency_ms": 0})
                yield sse("token", {"text": message})
                yield sse(
                    "done",
                    {"conversation_id": conversation_id, "total_latency_ms": 0},
                )
                return

            if settings.demo_mode:
                records = demo_retrieve(request.message, top_k)
                retrieval_ms = 180
                bytes_billed = 0
            else:
                records, retrieval_ms, bytes_billed = await retriever.retrieve(
                    request.message, top_k
                )

            yield sse(
                "retrieval",
                {
                    "records": [record.model_dump() for record in records],
                    "count": len(records),
                    "latency_ms": retrieval_ms,
                },
            )

            full_answer = ""
            if settings.demo_mode:
                answer = demo_answer(request.message, records)
                for word in answer.split(" "):
                    token = word + " "
                    full_answer += token
                    yield sse("token", {"text": token})
                    await asyncio.sleep(0.025)
            else:
                async for token in answerer.stream_answer(
                    request.message,
                    records,
                    list(histories[conversation_id]),
                ):
                    full_answer += token
                    yield sse("token", {"text": token})

            histories[conversation_id].append({"role": "user", "text": request.message})
            histories[conversation_id].append({"role": "assistant", "text": full_answer})
            total_ms = round((time.perf_counter() - started) * 1000)
            logger.info(
                json.dumps(
                    {
                        "conversation_id": conversation_id,
                        "question": request.message,
                        "record_ids": [record.record_id for record in records],
                        "distances": [record.distance for record in records],
                        "total_bytes_billed": bytes_billed,
                        "latency_ms": total_ms,
                    },
                    ensure_ascii=False,
                )
            )
            yield sse(
                "done",
                {"conversation_id": conversation_id, "total_latency_ms": total_ms},
            )
        except Exception as exc:
            logger.exception("Chat request failed")
            yield sse(
                "error",
                {"message": "I couldn't complete that request. Please try again."},
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/feedback")
async def feedback(request: FeedbackRequest):
    logger.info(json.dumps({"feedback": request.model_dump()}, ensure_ascii=False))
    return {"status": "recorded"}

