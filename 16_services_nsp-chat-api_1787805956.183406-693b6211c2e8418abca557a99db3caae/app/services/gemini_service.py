from google import genai
from google.genai import types

from app.config import Settings
from app.models import SourceRecord


SYSTEM_PROMPT = """You are an analyst assistant for NSP, the Saudi National Sports
Platform. Answer only from the retrieved affiliation records supplied to you.
Never use outside knowledge and never guess. If the records are insufficient, say
so clearly. Never calculate a total, count, or statistic from retrieved top-K
records. Reproduce Arabic values exactly unless asked to translate. Respond in the
language used by the user. Age Group is a business classification. Athlete Type
'Not applicable' is valid for non-athletes. One person can have multiple affiliation
records. Be concise and refer to evidence as Record 1, Record 2, and so on."""


class GeminiAnswerer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = genai.Client(
            vertexai=True,
            project=settings.gcp_project,
            location=settings.vertex_location,
        )

    async def stream_answer(
        self,
        question: str,
        records: list[SourceRecord],
        history: list[dict[str, str]],
    ):
        record_block = "\n\n".join(
            f"Record {index}:\n{record.document_text}"
            for index, record in enumerate(records, start=1)
        )
        history_block = "\n".join(
            f"{item['role'].title()}: {item['text']}" for item in history[-6:]
        )
        prompt = (
            f"Conversation history:\n{history_block or 'None'}\n\n"
            f"Retrieved NSP records:\n{record_block or 'No records retrieved.'}\n\n"
            f"User question: {question}"
        )
        stream = await self.client.aio.models.generate_content_stream(
            model=self.settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.2,
                max_output_tokens=800,
            ),
        )
        async for chunk in stream:
            if chunk.text:
                yield chunk.text

