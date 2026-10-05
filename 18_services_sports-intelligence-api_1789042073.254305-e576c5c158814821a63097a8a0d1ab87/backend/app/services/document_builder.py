import hashlib
import math
from datetime import date, datetime
from typing import Any


NSP_FIELDS = [
    ("individual_type", "Individual type"),
    ("gender", "Gender"),
    ("age", "Age"),
    ("status", "Status"),
    ("nationality", "Nationality"),
    ("ministry_office_en", "Ministry office"),
    ("federation", "Federation"),
    ("sports_type_en", "Sport (English)"),
    ("sports_type", "Sport (Arabic)"),
    ("clubacadmeyname_en", "Club/academy (English)"),
    ("clubacadmeyname_ar", "Club/academy (Arabic)"),
    ("institutionalclub_en", "Institutional club"),
    ("athletetype_en", "Athlete type"),
    ("AgeGroup_en", "Age group"),
    ("city", "City"),
    ("region", "Region"),
    ("registration_date", "Registration date"),
    ("modified_date", "Last modified date"),
]

SOPC_FIELDS = [
    ("competition", "Competition"),
    ("year", "Year"),
    ("sport", "Sport"),
    ("discipline", "Discipline"),
    ("phase", "Competition phase"),
    ("class", "Class"),
    ("rank", "Rank"),
    ("Position", "Position"),
    ("medal", "Medal"),
    ("result", "Result"),
    ("person_team", "Participant type"),
    ("person", "Athlete/person"),
    ("team", "Team"),
    ("gender", "Event gender"),
    ("persongender", "Person gender"),
    ("nationality", "Nationality"),
    ("age", "Age"),
    ("age_group", "Age group"),
    ("country", "Participating country"),
    ("host_city", "Host city"),
    ("host_country", "Host country"),
    ("host_continent", "Host continent"),
    ("world_ranking", "World ranking"),
    ("rank_within_country", "Rank within country"),
    ("is_olympic_discipline", "Olympic discipline"),
]


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    text = str(value).strip()
    return "" if text.casefold() in {"nan", "none", "null"} else text


def stable_hash(*parts: Any) -> str:
    text = "|".join(clean(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def build_document(row: dict[str, Any], source: str, row_number: int) -> tuple[str, str, dict[str, str], str]:
    if source == "NSP":
        fields = NSP_FIELDS
        record_id = clean(row.get("ldv_contactaffiliationrecordid")) or stable_hash(
            "nsp", row.get("contactid"), row_number
        )
        title = " · ".join(
            value
            for value in [
                clean(row.get("individual_type")),
                clean(row.get("sports_type_en")) or clean(row.get("sports_type")),
                clean(row.get("region")),
            ]
            if value
        ) or "NSP affiliation record"
        metadata = {
            "source": "NSP",
            "individual_type": clean(row.get("individual_type")),
            "sport": clean(row.get("sports_type_en")) or clean(row.get("sports_type")),
            "federation": clean(row.get("federation")),
            "region": clean(row.get("region")),
            "city": clean(row.get("city")),
            "status": clean(row.get("status")),
        }
    else:
        fields = SOPC_FIELDS
        record_id = stable_hash(
            "sopc",
            row.get("source_file_name"),
            row.get("competition"),
            row.get("date"),
            row.get("person"),
            row.get("team"),
            row.get("discipline"),
            row.get("phase"),
            row_number,
        )
        title = " · ".join(
            value
            for value in [
                clean(row.get("competition")),
                clean(row.get("sport")),
                clean(row.get("year")),
            ]
            if value
        ) or "SOPC competition result"
        metadata = {
            "source": "SOPC",
            "sport": clean(row.get("sport")),
            "competition": clean(row.get("competition")),
            "year": clean(row.get("year")),
            "medal": clean(row.get("medal")),
            "nationality": clean(row.get("nationality")),
        }

    lines = [f"Source: {source}"]
    lines.extend(
        f"{label}: {value}"
        for key, label in fields
        if (value := clean(row.get(key)))
    )
    return record_id, "\n".join(lines), metadata, title
