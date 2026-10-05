import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any

import duckdb

from app.config import Settings


FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|copy|attach|detach|install|load|pragma|call|export|import|read_csv|read_json|read_parquet|parquet_scan)\b",
    re.IGNORECASE,
)
ALLOWED_TABLES = {"nsp", "sopc", "dropout_alerts"}


def json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "item"):
        return value.item()
    return value


class LocalDataService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = RLock()
        self.connection = duckdb.connect()
        self._catalog_cache: dict[str, list[str]] | None = None
        nsp_path = settings.nsp_glob.replace("'", "''")
        sopc_path = settings.sopc_glob.replace("'", "''")
        self.connection.execute(
            f"CREATE OR REPLACE VIEW nsp AS SELECT * FROM read_parquet('{nsp_path}')"
        )
        self.connection.execute(
            f"CREATE OR REPLACE VIEW sopc AS SELECT * FROM read_parquet('{sopc_path}')"
        )
        alert_path = settings.data_path / "derived" / "dropout_alerts.parquet"
        self.alert_path = alert_path
        if alert_path.exists():
            safe_path = str(alert_path).replace("'", "''")
            self.connection.execute(
                f"CREATE OR REPLACE VIEW dropout_alerts AS SELECT * FROM read_parquet('{safe_path}')"
            )

    def refresh_alerts(self) -> None:
        if self.alert_path.exists():
            safe_path = str(self.alert_path).replace("'", "''")
            self.connection.execute(
                f"CREATE OR REPLACE VIEW dropout_alerts AS SELECT * FROM read_parquet('{safe_path}')"
            )

    def counts(self) -> dict[str, int]:
        with self.lock:
            row = self.connection.execute(
                "SELECT (SELECT count(*) FROM nsp), (SELECT count(*) FROM sopc)"
            ).fetchone()
            alerts = 0
            if self.alert_path.exists():
                self.refresh_alerts()
                alerts = self.connection.execute(
                    "SELECT count(*) FROM dropout_alerts WHERE risk_level = 'HIGH'"
                ).fetchone()[0]
            return {"nsp": int(row[0]), "sopc": int(row[1]), "high_risk": int(alerts)}

    def schemas(self) -> dict[str, list[tuple[str, str]]]:
        return {
            table: [tuple(row[:2]) for row in self.connection.execute(f"DESCRIBE {table}").fetchall()]
            for table in ("nsp", "sopc")
        }

    def catalog(self) -> dict[str, list[str]]:
        """Small real-value vocabulary used by the planner for typo resolution."""
        if self._catalog_cache is not None:
            return self._catalog_cache
        fields = {
            "nsp_sports": ("nsp", "sports_type_en", 80),
            "nsp_sports_ar": ("nsp", "sports_type", 80),
            "nsp_federations": ("nsp", "federation", 80),
            "nsp_regions": ("nsp", "region", 30),
            "nsp_cities": ("nsp", "city", 60),
            "nsp_statuses": ("nsp", "status", 20),
            "nsp_individual_types": ("nsp", "individual_type", 20),
            "sopc_sports": ("sopc", "sport", 80),
            "sopc_disciplines": ("sopc", "discipline", 80),
            "sopc_competitions": ("sopc", "competition", 80),
            "sopc_medals": ("sopc", "medal", 20),
            "sopc_countries": ("sopc", "country", 50),
        }
        catalog: dict[str, list[str]] = {}
        with self.lock:
            for name, (table, column, limit) in fields.items():
                rows = self.connection.execute(
                    f"""
                    SELECT trim(cast({column} AS VARCHAR)) AS value
                    FROM {table}
                    WHERE {column} IS NOT NULL
                      AND trim(cast({column} AS VARCHAR)) NOT IN ('', '.', '0', 'Unknown')
                    GROUP BY value
                    ORDER BY count(*) DESC, value
                    LIMIT {limit}
                    """
                ).fetchall()
                catalog[name] = [row[0] for row in rows]
        self._catalog_cache = catalog
        return catalog

    def validate_sql(self, sql: str) -> str:
        candidate = sql.strip()
        candidate = re.sub(r"^```(?:sql)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)
        candidate = re.sub(r"^sql\s*(?::|\n)\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"^(?:\s*--[^\n]*(?:\n|$))+", "", candidate)
        candidate = candidate.rstrip(";").strip()
        statement = re.search(r"(?im)^(?:select|with)\b", candidate)
        if statement and statement.start() > 0:
            candidate = candidate[statement.start():].strip()
        if not candidate or ";" in candidate:
            raise ValueError("Only one SQL statement is allowed")
        if not re.match(r"^(select|with)\b", candidate, re.IGNORECASE):
            raise ValueError("Only SELECT queries are allowed")
        if FORBIDDEN_SQL.search(candidate):
            raise ValueError("The query contains a forbidden SQL operation")
        privacy_checked = re.sub(
            r"count\s*\(\s*distinct\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?contactid\s*\)",
            "allowed_person_count",
            candidate,
            flags=re.IGNORECASE,
        )
        if re.search(
            r"\b(contactid|ldv_contactaffiliationrecordid|individual_id|dob|source_file_name)\b",
            privacy_checked,
            re.IGNORECASE,
        ):
            raise ValueError("Raw personal or internal identifiers cannot be selected")
        referenced = {
            match.casefold()
            for match in re.findall(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", candidate, re.IGNORECASE)
        }
        # CTE names are allowed; physical sources are still restricted by the function blocklist.
        physical = referenced & ALLOWED_TABLES
        cte_names = {
            match.casefold()
            for match in re.findall(r"(?:\bwith\b|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", candidate, re.IGNORECASE)
        }
        unknown_sources = referenced - ALLOWED_TABLES - cte_names
        if unknown_sources:
            raise ValueError(f"Unapproved query source: {sorted(unknown_sources)[0]}")
        if not physical:
            raise ValueError("The query must use nsp, sopc, or dropout_alerts")
        if "dropout_alerts" in physical and not self.alert_path.exists():
            raise ValueError("The dropout model has not been built yet")
        return candidate

    def execute(self, sql: str, max_rows: int = 100) -> dict[str, Any]:
        safe_sql = self.validate_sql(sql)
        with self.lock:
            cursor = self.connection.execute(
                f"SELECT * FROM ({safe_sql}) AS answer_query LIMIT {int(max_rows)}"
            )
            columns = [item[0] for item in cursor.description]
            rows = [
                {column: json_value(value) for column, value in zip(columns, row)}
                for row in cursor.fetchall()
            ]
        return {"columns": columns, "rows": rows, "row_count": len(rows), "sql": safe_sql}

    def dropout_summary(self, limit: int = 20) -> dict[str, Any]:
        if not self.alert_path.exists():
            return {"ready": False, "summary": {}, "alerts": []}
        with self.lock:
            self.refresh_alerts()
            summary_row = self.connection.execute(
                """
                SELECT count(*) total_monitored,
                       count(*) FILTER (WHERE risk_level='HIGH') high_risk,
                       count(*) FILTER (WHERE risk_level='MEDIUM') medium_risk,
                       round(avg(dropout_probability) * 100, 1) average_risk_pct,
                       max(snapshot_date) snapshot_date
                FROM dropout_alerts
                """
            ).fetchone()
        columns = ["total_monitored", "high_risk", "medium_risk", "average_risk_pct", "snapshot_date"]
        summary = {key: json_value(value) for key, value in zip(columns, summary_row)}
        alerts = self.execute(
            f"""
            SELECT athlete_id, sport, federation, region, city, age, age_group,
                   dropout_probability, risk_level, days_since_activity,
                   signals, recommended_action
            FROM dropout_alerts
            WHERE federation NOT IN ('Unknown', '.', '')
              AND region NOT IN ('Unknown', '')
            ORDER BY dropout_probability DESC, days_since_activity DESC
            LIMIT {min(max(limit, 1), 100)}
            """,
            max_rows=min(max(limit, 1), 100),
        )["rows"]
        return {"ready": True, "summary": summary, "alerts": alerts}

    def query_result_text(self, result: dict[str, Any]) -> str:
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)
