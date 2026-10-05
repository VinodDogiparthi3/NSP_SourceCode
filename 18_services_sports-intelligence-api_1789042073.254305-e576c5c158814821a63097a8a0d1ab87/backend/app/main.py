import asyncio
import json
import logging
import re
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.models import ChatRequest, FeedbackRequest, QueryPlan, QueryTask, SourceRecord
from app.services.data_service import LocalDataService
from app.services.gemini_service import GeminiService


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("sports-intelligence")
settings = get_settings()
data_service = LocalDataService(settings)
gemini = GeminiService(settings) if settings.gemini_enabled else None
vector_store: Any | None = None
histories: dict[str, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=12))

app = FastAPI(title="NSP & SOPC Intelligence API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def get_vector_store():
    global vector_store
    if not settings.enable_vector_search:
        raise RuntimeError("Vector search is disabled in this deployment")
    if vector_store is None:
        from app.services.vector_service import LocalVectorStore

        vector_store = LocalVectorStore(settings)
    return vector_store


def vector_counts() -> dict[str, int]:
    if not settings.enable_vector_search:
        return {"nsp": 0, "sopc": 0}
    try:
        from app.services.vector_service import LocalVectorStore

        return LocalVectorStore(settings, load_embedding_model=False).counts()
    except Exception:
        return {"nsp": 0, "sopc": 0}


def contains_arabic(text: str) -> bool:
    return any("\u0600" <= char <= "\u06ff" for char in text)


def plain_message(route: str, question: str) -> str:
    arabic = contains_arabic(question)
    messages = {
        "greeting": (
            "مرحباً! يمكنني تحليل بيانات NSP وSOPC، بما في ذلك الرياضيين والاتحادات والنتائج والميداليات وتنبيهات التسرب المبكر. ما الذي تريد معرفته؟"
            if arabic else
            "Hello! I can analyse NSP and SOPC data—including athletes, federations, competition results, medals, and early-warning dropout risk. What would you like to know?"
        ),
        "out_of_scope": (
            "أفهم سؤالك، لكنه خارج نطاق بيانات NSP وSOPC المتاحة. يمكنني المساعدة في بيانات الرياضة والرياضيين والاتحادات والبطولات والنتائج والمخاطر المبكرة."
            if arabic else
            "I understand the question, but it is outside the available NSP and SOPC datasets. I can help with sports participants, federations, competitions, results, medals, and early-warning risk."
        ),
        "clarification": (
            "يرجى توضيح المجموعة أو الرياضة أو الفترة الزمنية أو المقياس المطلوب حتى أقدم نتيجة دقيقة."
            if arabic else
            "Please clarify the population, sport, time period, or metric you want so I can return an accurate result."
        ),
    }
    return messages.get(route, messages["clarification"])


def is_top_dropout_question(question: str) -> bool:
    """Keep the primary early-warning demo reliable even when the LLM is busy."""
    normalized = " ".join(question.casefold().replace("-", " ").split())
    asks_about_risk = any(
        phrase in normalized
        for phrase in ("dropout", "drop out", "early warning", "at risk", "risk of quitting")
    )
    asks_for_ranking = any(
        phrase in normalized
        for phrase in ("highest", "top", "most", "greatest", "high risk")
    )
    asks_about_athletes = any(
        phrase in normalized for phrase in ("athlete", "player", "participant")
    )
    return asks_about_risk and asks_for_ranking and asks_about_athletes


def top_dropout_answer(rows: list[dict], question: str) -> str:
    if not rows:
        return "No active athletes with complete federation information were available for ranking."

    arabic = contains_arabic(question)
    if arabic:
        intro = (
            "تُظهر البيانات المحلية الرياضيين النشطين اسمياً التاليين كأعلى حالات الإنذار المبكر. "
            "النتيجة مؤشر احتمالي للمراجعة وليست دليلاً على التسرب الفعلي."
        )
        headers = ["معرّف الرياضي", "الرياضة", "الاتحاد", "المنطقة", "درجة الإنذار", "عدم النشاط"]
    else:
        intro = (
            "These nominally active athletes have the highest early-warning proxy scores in the local NSP data. "
            "The score prioritises human review; it is not proof that an athlete will drop out."
        )
        headers = ["Athlete ID", "Sport", "Federation", "Region", "Risk score", "Inactivity"]

    table = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows[:5]:
        values = [
            str(row.get("athlete_id") or "—"),
            str(row.get("sport") or "—"),
            str(row.get("federation") or "—"),
            str(row.get("region") or "—"),
            f"{float(row.get('dropout_probability') or 0) * 100:.1f}%",
            f"{int(row.get('days_since_activity') or 0)} days",
        ]
        table.append("| " + " | ".join(value.replace("|", "/") for value in values) + " |")

    first = rows[0]
    signals = first.get("signals") or []
    signal_text = "; ".join(str(item) for item in signals)
    action = str(first.get("recommended_action") or "Federation review is recommended")
    if arabic:
        closing = f"\n\nأبرز الإشارات للحالة الأولى: {signal_text}. الإجراء المقترح: {action}."
    else:
        closing = f"\n\nFor the first-ranked case, the main signals are {signal_text.lower()}. Recommended action: {action}."
    return intro + "\n\n" + "\n".join(table) + closing


NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def referenced_dropout_ids(history: list[dict[str, str]]) -> list[str]:
    """Recover anonymised IDs from the latest grounded assistant answer."""
    for turn in reversed(history):
        if turn.get("role") != "assistant":
            continue
        ids = list(dict.fromkeys(re.findall(r"\bNSP-[A-F0-9]{10}\b", turn.get("text", ""))))
        if ids:
            return ids
    return []


def requested_reference_count(question: str, available: int) -> int:
    normalized = question.casefold()
    digit = re.search(r"\b(?:first|top)\s+(\d{1,2})\b", normalized)
    if digit:
        return min(max(int(digit.group(1)), 1), available)
    for word, value in NUMBER_WORDS.items():
        if re.search(rf"\b(?:first|top)\s+{word}\b", normalized):
            return min(value, available)
    if re.search(r"\b(first|1st)\b", normalized):
        return min(1, available)
    return min(available, 5)


def dropout_context_followup(question: str, history: list[dict[str, str]]) -> tuple[str, dict] | None:
    """Answer referential follow-ups from grounded rows instead of regenerating SQL."""
    normalized = " ".join(question.casefold().replace("-", " ").split())
    requested_fields = {
        "federation": any(term in normalized for term in ("federation", "federations", "fed ")),
        "action": any(term in normalized for term in ("action", "intervention", "recommend", "what should we do")),
        "signals": any(term in normalized for term in ("signal", "reason", "why", "risk factor")),
        "region": "region" in normalized or "where" in normalized,
        "sport": "sport" in normalized,
        "score": any(term in normalized for term in ("score", "probability", "risk level")),
    }
    referential = any(
        term in normalized
        for term in ("first", "second", "third", "they", "them", "those", "these", "above", "athlete")
    )
    ids = referenced_dropout_ids(history)
    if not ids or not referential or not any(requested_fields.values()):
        return None

    selected_ids = ids[:requested_reference_count(question, len(ids))]
    quoted_ids = ", ".join(f"'{item}'" for item in selected_ids)
    result = data_service.execute(
        f"""
        SELECT athlete_id, sport, federation, region, dropout_probability,
               risk_level, days_since_activity, signals, recommended_action
        FROM dropout_alerts
        WHERE athlete_id IN ({quoted_ids})
        """
    )
    by_id = {row["athlete_id"]: row for row in result["rows"]}
    rows = [by_id[item] for item in selected_ids if item in by_id]
    result["rows"] = rows
    result["row_count"] = len(rows)
    if not rows:
        return None

    if requested_fields["action"] or requested_fields["signals"]:
        sections = []
        for row in rows:
            signals = "; ".join(str(item) for item in (row.get("signals") or []))
            sections.append(
                f"**{row['athlete_id']} — {row.get('federation') or 'Federation unavailable'}**\n\n"
                f"Reason for review: {signals}.\n\n"
                f"Recommended action: {row.get('recommended_action') or 'Human review is recommended'}."
            )
        answer = "\n\n".join(sections)
    else:
        columns = ["Athlete ID"]
        keys = ["athlete_id"]
        for key, label in (
            ("federation", "Federation"), ("sport", "Sport"), ("region", "Region"),
            ("dropout_probability", "Risk score"), ("risk_level", "Risk level"),
        ):
            if requested_fields["score"] and key in {"dropout_probability", "risk_level"}:
                columns.append(label); keys.append(key)
            elif requested_fields.get(key, False):
                columns.append(label); keys.append(key)
        table = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
        for row in rows:
            values = []
            for key in keys:
                value = row.get(key)
                if key == "dropout_probability":
                    value = f"{float(value or 0) * 100:.1f}%"
                values.append(str(value or "—").replace("|", "/"))
            table.append("| " + " | ".join(values) + " |")
        answer = "Here is the requested information from the athletes in the previous result:\n\n" + "\n".join(table)
    return answer, result


def local_fallback_plan(question: str) -> QueryPlan:
    """Safe coverage for the demonstration's common intents when Gemini is unavailable."""
    q = " ".join(question.casefold().replace("-", " ").split())
    if re.fullmatch(r"(?:hi|hello|hey|thanks|thank you)[!. ]*", q):
        return QueryPlan(route="greeting", dataset="both", normalized_question=question)
    if any(term in q for term in ("weather", "stock price", "write code", "recipe", "capital of")):
        return QueryPlan(route="out_of_scope", dataset="both", normalized_question=question)
    if "basketball" in q and any(term in q for term in ("compare", "nsp", "sopc")) and "performance" in q:
        return QueryPlan(
            route="multi", dataset="both", normalized_question=question,
            tasks=[
                QueryTask(
                    label="Active basketball participation in NSP", route="analytics", dataset="nsp",
                    sql="SELECT coalesce(sports_type_en, sports_type, 'Unknown') AS sport, count(DISTINCT contactid) AS active_athletes FROM nsp WHERE individual_type ILIKE 'Athlete' AND status ILIKE 'Active' AND (sports_type_en ILIKE '%basketball%' OR sports_type ILIKE '%السلة%') GROUP BY sport ORDER BY active_athletes DESC LIMIT 50",
                ),
                QueryTask(
                    label="Historical basketball results in SOPC", route="analytics", dataset="sopc",
                    sql="SELECT year, sport, count(*) AS result_records, count(*) FILTER (WHERE medal IS NOT NULL AND trim(cast(medal AS VARCHAR)) NOT IN ('', '.', 'No Medal')) AS medal_records FROM sopc WHERE sport ILIKE '%basketball%' OR discipline ILIKE '%basketball%' GROUP BY year, sport ORDER BY year DESC, result_records DESC LIMIT 50",
                ),
            ],
        )
    if "taekwondo" in q or "taekwon" in q:
        return QueryPlan(
            route="analytics", dataset="sopc", normalized_question="Taekwondo competition results and performance",
            sql="SELECT year, competition, sport, discipline, phase, rank, Position, medal, result, host_country FROM sopc WHERE sport ILIKE '%Taekwondo%' OR discipline ILIKE '%Taekwondo%' ORDER BY year DESC LIMIT 20",
        )
    if "medal" in q or "ميدال" in q:
        if "trend" in q or "year" in q or "سنة" in q:
            sql = "SELECT year, sport, medal, count(*) AS medal_count FROM sopc WHERE nationality ILIKE 'KSA' AND medal IS NOT NULL AND trim(cast(medal AS VARCHAR)) NOT IN ('', '.', 'No Medal') GROUP BY year, sport, medal ORDER BY year, medal_count DESC LIMIT 50"
        else:
            sql = "SELECT sport, medal, count(*) AS medal_count FROM sopc WHERE nationality ILIKE 'KSA' AND medal IS NOT NULL AND trim(cast(medal AS VARCHAR)) NOT IN ('', '.', 'No Medal') GROUP BY sport, medal ORDER BY medal_count DESC LIMIT 50"
        return QueryPlan(route="analytics", dataset="sopc", normalized_question=question, sql=sql)
    if "federation" in q and any(term in q for term in ("highest number", "most active", "top")):
        return QueryPlan(
            route="analytics", dataset="nsp", normalized_question=question,
            sql="SELECT federation, count(DISTINCT contactid) AS active_athletes FROM nsp WHERE individual_type ILIKE 'Athlete' AND status ILIKE 'Active' AND federation IS NOT NULL AND trim(federation) NOT IN ('', '.', 'Unknown') GROUP BY federation ORDER BY active_athletes DESC LIMIT 10",
        )
    if "female" in q and "basketball" in q and ("riyadh" in q or "الرياض" in q):
        return QueryPlan(
            route="analytics", dataset="nsp", normalized_question=question,
            sql="SELECT count(DISTINCT contactid) AS active_female_basketball_athletes FROM nsp WHERE individual_type ILIKE 'Athlete' AND status ILIKE 'Active' AND gender ILIKE 'Female' AND (sports_type_en ILIKE '%basketball%' OR sports_type ILIKE '%السلة%') AND (region ILIKE '%Riyadh%' OR region ILIKE '%الرياض%' OR city ILIKE '%Riyadh%' OR city ILIKE '%الرياض%')",
        )
    if any(term in q for term in ("volleyball", "vollyball", "voleyball")) and any(term in q for term in ("eastern", "provnce", "الشرقية")):
        return QueryPlan(
            route="analytics", dataset="nsp", normalized_question="Active volleyball athletes in the Eastern Province",
            sql="SELECT coalesce(sports_type_en, sports_type, 'Unknown') AS sport, federation, region, city, count(DISTINCT contactid) AS active_athletes FROM nsp WHERE individual_type ILIKE 'Athlete' AND status ILIKE 'Active' AND (sports_type_en ILIKE '%volleyball%' OR sports_type ILIKE '%الطائرة%') AND (region ILIKE '%Eastern%' OR region ILIKE '%الشرقية%') GROUP BY sport, federation, region, city ORDER BY active_athletes DESC LIMIT 50",
        )
    if "active athlete" in q and "sport" in q and "region" in q:
        return QueryPlan(
            route="analytics", dataset="nsp", normalized_question=question,
            sql="SELECT coalesce(sports_type_en, sports_type, 'Unknown') AS sport, coalesce(region, 'Unknown') AS region, count(DISTINCT contactid) AS active_athletes FROM nsp WHERE individual_type ILIKE 'Athlete' AND status ILIKE 'Active' GROUP BY sport, region ORDER BY active_athletes DESC LIMIT 50",
        )
    return QueryPlan(route="semantic_search", dataset="both", normalized_question=question, search_query=question)


def compact_result_answer(sections: list[tuple[str, dict]], records: list[SourceRecord]) -> str:
    """Readable grounded fallback when final LLM wording is temporarily unavailable."""
    output: list[str] = []
    for label, result in sections:
        rows = result.get("rows", [])
        output.append(f"### {label}")
        if not rows:
            output.append("No matching local records were found.")
            continue
        columns = list(rows[0].keys())
        output.append("| " + " | ".join(column.replace("_", " ").title() for column in columns) + " |")
        output.append("| " + " | ".join("---" for _ in columns) + " |")
        for row in rows[:10]:
            values = []
            for column in columns:
                value = row.get(column)
                if isinstance(value, list):
                    value = "; ".join(str(item) for item in value)
                values.append(str(value if value not in (None, "") else "—").replace("|", "/"))
            output.append("| " + " | ".join(values) + " |")
        if len(rows) > 10:
            output.append(f"Showing 10 of {len(rows)} returned rows.")
    if not output and records:
        output.append("I found these relevant local records:")
        for record in records[:5]:
            output.append(f"- **{record.title}** — {record.document_text}")
    return "\n\n".join(output) if output else "I could not find enough relevant local evidence for that question."


def result_sources(result: dict, source: str) -> list[SourceRecord]:
    records: list[SourceRecord] = []
    source_name = "EARLY_WARNING" if source == "dropout" else source.upper()
    for index, row in enumerate(result["rows"][:8], start=1):
        title = (
            str(row.get("athlete_id"))
            if source == "dropout"
            else f"Exact {source.upper()} query result {index}"
        )
        records.append(
            SourceRecord(
                record_id=f"{source}-{index}-{abs(hash(json.dumps(row, default=str))) }",
                source=source_name,
                title=title,
                document_text="\n".join(f"{key}: {value}" for key, value in row.items()),
                metadata={"evidence_type": "exact_query"},
                distance=0,
            )
        )
    return records


@app.get("/api/health")
async def health():
    counts = data_service.counts()
    indexed = await asyncio.to_thread(vector_counts)
    return {
        "status": "ok" if counts["nsp"] and counts["sopc"] else "degraded",
        "mode": "local",
        "gemini_configured": gemini is not None,
        "nsp_record_count": counts["nsp"],
        "sopc_record_count": counts["sopc"],
        "high_risk_count": counts["high_risk"],
        "indexed_record_count": indexed["nsp"] + indexed["sopc"],
        "indexed": indexed,
        "dropout_model_ready": data_service.alert_path.exists(),
    }


@app.get("/api/dashboard")
async def dashboard():
    counts = data_service.counts()
    return {"counts": counts, "dropout": data_service.dropout_summary(limit=8)}


@app.get("/api/dropout/alerts")
async def dropout_alerts(limit: int = Query(default=20, ge=1, le=100)):
    return data_service.dropout_summary(limit=limit)


@app.get("/api/suggestions")
async def suggestions():
    return {
        "suggestions": [
            "Which active athletes show the highest early-warning dropout risk?",
            "How many active athletes are registered by sport and region?",
            "Show Saudi medal trends by sport and year from SOPC",
            "Find Taekwondo competition results and explain the performance",
        ]
    }


@app.post("/api/chat")
async def chat(request: ChatRequest):
    conversation_id = request.conversation_id or str(uuid.uuid4())
    top_k = request.top_k or settings.top_k

    async def event_stream():
        started = time.perf_counter()
        full_answer = ""
        try:
            client_history = [turn.model_dump() for turn in request.history]
            history = client_history or list(histories[conversation_id])

            context_answer = await asyncio.to_thread(dropout_context_followup, request.message, history)
            if context_answer:
                answer, result = context_answer
                sources = result_sources(result, "dropout")
                yield sse("route", {"route": "dropout_risk", "dataset": "dropout"})
                yield sse("retrieval", {"records": [item.model_dump() for item in sources], "count": len(sources), "latency_ms": 0})
                yield sse("analysis", {"sql": result["sql"], "row_count": result["row_count"]})
                yield sse("token", {"text": answer})
                histories[conversation_id].append({"role": "user", "text": request.message})
                histories[conversation_id].append({"role": "assistant", "text": answer})
                yield sse("done", {"conversation_id": conversation_id, "total_latency_ms": round((time.perf_counter() - started) * 1000)})
                return

            # The flagship use case is an exact ranking over the local model output.
            # It must remain available even if an external LLM has a transient quota,
            # network, or SQL-generation failure.
            if is_top_dropout_question(request.message):
                result = await asyncio.to_thread(
                    data_service.execute,
                    """
                    SELECT athlete_id, sport, federation, region, city, age, age_group,
                           dropout_probability, risk_level, days_since_activity,
                           signals, recommended_action
                    FROM dropout_alerts
                    WHERE federation IS NOT NULL
                      AND trim(federation) NOT IN ('', '.', 'Unknown')
                      AND region IS NOT NULL
                      AND trim(region) NOT IN ('', '.', 'Unknown')
                    ORDER BY dropout_probability DESC, days_since_activity DESC
                    LIMIT 5
                    """,
                )
                sources = result_sources(result, "dropout")
                answer = top_dropout_answer(result["rows"], request.message)
                yield sse("route", {"route": "dropout_risk", "dataset": "dropout"})
                yield sse(
                    "retrieval",
                    {"records": [item.model_dump() for item in sources], "count": len(sources), "latency_ms": 0},
                )
                yield sse("analysis", {"sql": result["sql"], "row_count": result["row_count"]})
                yield sse("token", {"text": answer})
                histories[conversation_id].append({"role": "user", "text": request.message})
                histories[conversation_id].append({"role": "assistant", "text": answer})
                yield sse(
                    "done",
                    {
                        "conversation_id": conversation_id,
                        "total_latency_ms": round((time.perf_counter() - started) * 1000),
                    },
                )
                return

            catalog = await asyncio.to_thread(data_service.catalog)
            if gemini is None:
                plan = local_fallback_plan(request.message)
            else:
                try:
                    plan = await gemini.plan(request.message, history, catalog)
                except Exception:
                    logger.exception("Gemini planning failed; using the local safe planner")
                    plan = local_fallback_plan(request.message)
            yield sse("route", {"route": plan.route, "dataset": plan.dataset})

            if plan.route in {"greeting", "out_of_scope", "clarification"}:
                message = plain_message(plan.route, request.message)
                yield sse("retrieval", {"records": [], "count": 0, "latency_ms": 0})
                yield sse("token", {"text": message})
                full_answer = message

            else:
                tasks = plan.tasks if plan.route == "multi" else [
                    QueryTask(
                        label=plan.normalized_question or request.message,
                        route=plan.route,
                        dataset=plan.dataset,
                        search_query=plan.search_query,
                        sql=plan.sql,
                    )
                ]
                all_sources: list[SourceRecord] = []
                evidence_sections: list[str] = []
                result_sections: list[tuple[str, dict]] = []
                sql_parts: list[str] = []
                total_retrieval_ms = 0

                for index, task in enumerate(tasks, start=1):
                    label = task.label or f"Part {index}"
                    if task.route in {"analytics", "dropout_risk"}:
                        if not task.sql:
                            raise ValueError(f"The planner omitted SQL for: {label}")
                        try:
                            result = await asyncio.to_thread(data_service.execute, task.sql)
                        except Exception as first_error:
                            try:
                                if gemini is None:
                                    raise first_error
                                repaired = await gemini.repair_sql(label, task.sql, str(first_error))
                                result = await asyncio.to_thread(data_service.execute, repaired)
                            except Exception:
                                local_plan = local_fallback_plan(label)
                                if local_plan.route not in {"analytics", "dropout_risk"} or not local_plan.sql:
                                    raise first_error
                                result = await asyncio.to_thread(data_service.execute, local_plan.sql)
                        if result["row_count"] == 0 and gemini is not None:
                            try:
                                refined = await gemini.refine_empty(label, result["sql"], catalog)
                                if refined and refined != result["sql"]:
                                    result = await asyncio.to_thread(data_service.execute, refined)
                            except Exception:
                                logger.exception("Empty-result refinement failed; returning the grounded empty result")
                        source_dataset = "dropout" if task.route == "dropout_risk" else task.dataset
                        source_dataset = "nsp" if source_dataset == "both" else source_dataset
                        all_sources.extend(result_sources(result, source_dataset))
                        sql_parts.append(f"-- {label}\n{result['sql']}")
                        evidence_sections.append(
                            f"PART {index} — {label}\n{data_service.query_result_text(result)}"
                        )
                        result_sections.append((label, result))
                    else:
                        if not settings.enable_vector_search:
                            evidence_sections.append(
                                f"PART {index} — {label}\n"
                                "No exact analytical query was produced, and semantic vector search "
                                "is disabled in this lightweight cloud deployment."
                            )
                            continue
                        dataset = task.dataset if task.dataset in {"nsp", "sopc"} else "both"
                        records, retrieval_ms = await asyncio.to_thread(
                            get_vector_store().retrieve,
                            task.search_query or label,
                            dataset,
                            top_k,
                        )
                        records = [item for item in records if item.distance <= settings.max_distance]
                        all_sources.extend(records)
                        total_retrieval_ms += retrieval_ms
                        evidence_sections.append(
                            f"PART {index} — {label}\n{GeminiService.record_evidence(records) if records else 'No sufficiently relevant local record was retrieved.'}"
                        )

                all_sources = all_sources[:12]
                yield sse(
                    "retrieval",
                    {"records": [item.model_dump() for item in all_sources], "count": len(all_sources), "latency_ms": total_retrieval_ms},
                )
                if sql_parts:
                    yield sse("analysis", {"sql": "\n\n".join(sql_parts), "row_count": len(all_sources)})
                if not evidence_sections:
                    message = (
                        "I understood the question, but could not find enough local evidence. Try adding a sport, federation, competition, city, region, or year."
                    )
                    full_answer = message
                    yield sse("token", {"text": message})
                else:
                    normalized = plan.normalized_question or request.message
                    evidence = f"INTERPRETED REQUEST: {normalized}\n\n" + "\n\n".join(evidence_sections)
                    fallback_answer = compact_result_answer(result_sections, all_sources)
                    if gemini is None:
                        full_answer = fallback_answer
                        yield sse("token", {"text": fallback_answer})
                    else:
                        try:
                            generated_parts: list[str] = []
                            async for token in gemini.stream_answer(request.message, evidence, history):
                                generated_parts.append(token)
                            full_answer = "".join(generated_parts)
                            yield sse("token", {"text": full_answer})
                        except Exception:
                            logger.exception("Gemini answer generation failed; using grounded local formatting")
                            full_answer = fallback_answer
                            yield sse("token", {"text": fallback_answer})

            histories[conversation_id].append({"role": "user", "text": request.message})
            histories[conversation_id].append({"role": "assistant", "text": full_answer})
            yield sse(
                "done",
                {
                    "conversation_id": conversation_id,
                    "total_latency_ms": round((time.perf_counter() - started) * 1000),
                },
            )
        except Exception as exc:
            logger.exception("Chat request failed")
            error_text = str(exc).upper()
            if "429" in error_text or "RESOURCE_EXHAUSTED" in error_text:
                message = "The AI service is temporarily at capacity. Your local data is ready—please try again in a moment."
            elif "503" in error_text or "UNAVAILABLE" in error_text:
                message = "The AI service is temporarily unavailable. Please retry in a moment."
            else:
                message = "I couldn't complete that request safely. Please rephrase it or add a sport, region, federation, competition, or year."
            yield sse("error", {"message": message})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/feedback")
async def feedback(request: FeedbackRequest):
    logger.info(json.dumps({"feedback": request.model_dump()}, ensure_ascii=False))
    return {"status": "recorded"}
