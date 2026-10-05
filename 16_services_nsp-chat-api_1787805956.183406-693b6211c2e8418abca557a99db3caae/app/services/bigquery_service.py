import asyncio
import time

from google.cloud import bigquery

from app.config import Settings
from app.models import SourceRecord


class BigQueryRetriever:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = bigquery.Client(project=settings.gcp_project)

    async def retrieve(self, question: str, top_k: int) -> tuple[list[SourceRecord], int, int]:
        started = time.perf_counter()
        rows, bytes_billed = await asyncio.wait_for(
            asyncio.to_thread(self._query, question, top_k), timeout=30
        )
        latency_ms = round((time.perf_counter() - started) * 1000)
        return rows, latency_ms, bytes_billed

    def _query(self, question: str, top_k: int) -> tuple[list[SourceRecord], int]:
        vector = "e.embedding.result" if self.settings.bq_vector_mode == "legacy_struct" else "e.embedding"
        legacy_projection = self.settings.bq_vector_mode == "legacy_struct"

        if legacy_projection:
            projection = """
              COALESCE(e.contactid, TO_HEX(MD5(e.document_text))) AS record_id,
              e.contactid,
              e.document_text,
              CAST(NULL AS STRING) AS individual_type,
              CAST(NULL AS STRING) AS federation,
              CAST(NULL AS STRING) AS city,
              CAST(NULL AS STRING) AS region,
            """
        else:
            projection = """
              e.record_id,
              e.contactid,
              e.document_text,
              e.individual_type,
              e.federation,
              e.city,
              e.region,
            """

        sql = f"""
        WITH question AS (
          SELECT AI.EMBED(
            @user_question,
            model => '{self.settings.embedding_model}'
          ).result AS question_vector
        )
        SELECT
          {projection}
          ML.DISTANCE({vector}, q.question_vector, 'COSINE') AS distance
        FROM `{self.settings.table_path}` AS e
        CROSS JOIN question AS q
        WHERE {vector} IS NOT NULL
        ORDER BY distance ASC
        LIMIT @top_k
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("user_question", "STRING", question),
                bigquery.ScalarQueryParameter("top_k", "INT64", top_k),
            ],
            maximum_bytes_billed=10 * 1024**3,
        )
        job = self.client.query(
            sql,
            job_config=job_config,
            location=self.settings.bq_location,
        )
        result = job.result(timeout=30)
        records = [
            SourceRecord(
                record_id=row.record_id,
                contactid=row.contactid,
                document_text=row.document_text,
                individual_type=row.individual_type,
                federation=row.federation,
                city=row.city,
                region=row.region,
                distance=float(row.distance),
            )
            for row in result
        ]
        return records, int(job.total_bytes_billed or 0)

    async def health(self) -> tuple[bool, int]:
        def check() -> int:
            sql = f"SELECT COUNT(*) AS count FROM `{self.settings.table_path}`"
            row = next(iter(self.client.query(sql, location=self.settings.bq_location).result(timeout=15)))
            return int(row.count)

        try:
            return True, await asyncio.wait_for(asyncio.to_thread(check), timeout=15)
        except Exception:
            return False, 0

