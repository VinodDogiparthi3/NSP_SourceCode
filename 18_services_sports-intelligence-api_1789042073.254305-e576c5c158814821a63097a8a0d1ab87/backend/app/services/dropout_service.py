import hashlib
import json
from pathlib import Path

import duckdb
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score
from sklearn.model_selection import train_test_split

from app.config import Settings


FEATURES = [
    "days_since_activity",
    "tenure_days",
    "recency_tenure_ratio",
    "club_change_frequency",
    "age_group_transitions",
    "affiliation_count",
]


def anonymise(contact_id: str) -> str:
    digest = hashlib.sha256(contact_id.encode("utf-8")).hexdigest()[:10].upper()
    return f"NSP-{digest}"


def signal_list(row: pd.Series, thresholds: dict[str, float]) -> list[str]:
    signals: list[str] = []
    if row.days_since_activity >= thresholds["days_since_activity"]:
        signals.append(f"No affiliation activity for {int(row.days_since_activity)} days")
    if row.recency_tenure_ratio >= thresholds["recency_tenure_ratio"]:
        signals.append("Long inactivity relative to observed NSP tenure")
    if row.distinct_clubs > 1:
        signals.append(f"Affiliations across {int(row.distinct_clubs)} clubs/academies")
    if row.distinct_age_groups > 1:
        signals.append("Age-group transition appears in affiliation history")
    if row.affiliation_count > 1:
        signals.append(f"{int(row.affiliation_count)} affiliation records require review")
    return signals or ["No strong proxy signal; included for monitoring"]


def recommended_action(row: pd.Series) -> str:
    if row.dropout_probability >= 0.70:
        if row.distinct_clubs > 1:
            return "Federation outreach and club-transfer support within 7 days"
        return "Federation welfare check and participation-plan review within 7 days"
    if row.dropout_probability >= 0.35:
        return "Club check-in and activity review within 30 days"
    return "Continue routine monitoring"


def build_dropout_model(settings: Settings) -> dict:
    derived_dir = settings.data_path / "derived"
    settings.model_path.mkdir(parents=True, exist_ok=True)
    derived_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    nsp_path = settings.nsp_glob.replace("'", "''")
    con.execute(f"CREATE VIEW nsp AS SELECT * FROM read_parquet('{nsp_path}')")
    snapshot_date = con.execute("SELECT max(modified_date) FROM nsp").fetchone()[0]
    frame = con.execute(
        """
        SELECT
          contactid,
          min(registration_date) AS first_registration,
          max(modified_date) AS last_activity,
          arg_max(coalesce(sports_type_en, sports_type, 'Unknown'), modified_date) AS sport,
          arg_max(coalesce(federation, 'Unknown'), modified_date) AS federation,
          arg_max(coalesce(region, 'Unknown'), modified_date) AS region,
          arg_max(coalesce(city, 'Unknown'), modified_date) AS city,
          arg_max(coalesce(AgeGroup_en, 'Unknown'), modified_date) AS age_group,
          arg_max(age, modified_date) AS age,
          count(*) AS affiliation_count,
          count(DISTINCT nullif(clubacadmeyname_en, '')) AS distinct_clubs,
          count(DISTINCT nullif(AgeGroup_en, '')) AS distinct_age_groups
        FROM nsp
        WHERE contactid IS NOT NULL
          AND lower(coalesce(individual_type, '')) = 'athlete'
          AND lower(coalesce(status, '')) = 'active'
        GROUP BY contactid
        """
    ).fetch_df()

    frame["days_since_activity"] = (
        pd.Timestamp(snapshot_date) - pd.to_datetime(frame["last_activity"])
    ).dt.days.clip(lower=0)
    frame["tenure_days"] = (
        pd.Timestamp(snapshot_date) - pd.to_datetime(frame["first_registration"])
    ).dt.days.clip(lower=1)
    frame["recency_tenure_ratio"] = (
        frame["days_since_activity"] / frame["tenure_days"].clip(lower=1)
    ).clip(upper=5)
    frame["club_change_frequency"] = (
        (frame["distinct_clubs"] - 1).clip(lower=0)
        / (frame["tenure_days"] / 365.25).clip(lower=0.25)
    )
    frame["age_group_transitions"] = (frame["distinct_age_groups"] - 1).clip(lower=0)

    thresholds = {
        "days_since_activity": float(frame["days_since_activity"].quantile(0.75)),
        "recency_tenure_ratio": float(frame["recency_tenure_ratio"].quantile(0.75)),
    }
    proxy_points = (
        2 * (frame["days_since_activity"] >= thresholds["days_since_activity"]).astype(int)
        + 2 * (frame["recency_tenure_ratio"] >= thresholds["recency_tenure_ratio"]).astype(int)
        + (frame["distinct_clubs"] > 1).astype(int)
        + (frame["distinct_age_groups"] > 1).astype(int)
        + (frame["affiliation_count"] > 1).astype(int)
    )
    # A proxy positive requires multiple concurrent warning signals. A single
    # old activity date is not enough to label an athlete as at-risk.
    labels = (proxy_points >= 4).astype(int)

    x_train, x_test, y_train, y_test = train_test_split(
        frame[FEATURES], labels, test_size=0.2, random_state=42, stratify=labels
    )
    model = RandomForestClassifier(
        n_estimators=220,
        max_depth=7,
        min_samples_leaf=30,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
    )
    model.fit(x_train, y_train)
    test_probability = model.predict_proba(x_test)[:, 1]
    test_prediction = (test_probability >= 0.5).astype(int)
    model_probability = model.predict_proba(frame[FEATURES])[:, 1]
    continuous_severity = (
        0.50 * frame["days_since_activity"].rank(pct=True)
        + 0.35 * frame["recency_tenure_ratio"].rank(pct=True)
        + 0.10 * (frame["distinct_clubs"] > 1).astype(float)
        + 0.05 * (frame["distinct_age_groups"] > 1).astype(float)
    ).clip(0, 1)
    # The classifier supplies the proxy class signal; the continuous component
    # preserves severity differences inside each proxy class for useful ranking.
    probability = 0.35 * model_probability + 0.65 * continuous_severity

    frame["dropout_probability"] = np.round(probability, 4)
    frame["risk_level"] = np.select(
        [frame["dropout_probability"] >= 0.70, frame["dropout_probability"] >= 0.35],
        ["HIGH", "MEDIUM"],
        default="LOW",
    )
    frame["athlete_id"] = frame["contactid"].map(anonymise)
    frame["signals"] = frame.apply(lambda row: signal_list(row, thresholds), axis=1)
    frame["recommended_action"] = frame.apply(recommended_action, axis=1)
    frame["snapshot_date"] = snapshot_date.isoformat()

    output_columns = [
        "athlete_id", "sport", "federation", "region", "city", "age", "age_group",
        "dropout_probability", "risk_level", "days_since_activity", "tenure_days",
        "recency_tenure_ratio", "club_change_frequency", "age_group_transitions",
        "affiliation_count", "distinct_clubs", "distinct_age_groups", "signals",
        "recommended_action", "snapshot_date",
    ]
    alert_path = derived_dir / "dropout_alerts.parquet"
    frame[output_columns].to_parquet(alert_path, index=False)
    joblib.dump(model, settings.model_path / "dropout_random_forest.joblib")

    metrics = {
        "model_type": "Random Forest classifier",
        "label_type": "rule-based proxy label (not observed dropout)",
        "snapshot_date": snapshot_date.isoformat(),
        "athletes_monitored": int(len(frame)),
        "proxy_positive_rate": round(float(labels.mean()), 4),
        "high_risk": int((frame["risk_level"] == "HIGH").sum()),
        "medium_risk": int((frame["risk_level"] == "MEDIUM").sum()),
        "thresholds": thresholds,
        "test_metrics_against_proxy_label": {
            "roc_auc": round(float(roc_auc_score(y_test, test_probability)), 4),
            "accuracy": round(float(accuracy_score(y_test, test_prediction)), 4),
            "precision": round(float(precision_score(y_test, test_prediction)), 4),
            "recall": round(float(recall_score(y_test, test_prediction)), 4),
            "f1": round(float(f1_score(y_test, test_prediction)), 4),
        },
        "feature_importance": {
            feature: round(float(value), 4)
            for feature, value in sorted(
                zip(FEATURES, model.feature_importances_), key=lambda item: item[1], reverse=True
            )
        },
    }
    (settings.model_path / "dropout_metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )
    return metrics
