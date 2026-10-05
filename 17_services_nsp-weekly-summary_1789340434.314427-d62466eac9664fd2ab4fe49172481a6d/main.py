import json
import logging
import time
from datetime import datetime, timedelta
from google.cloud import bigquery, monitoring_v3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PROJECT_ID = "mos-gcp-host-dev-prj"
CONFIG_DATASET = "mos_config"

# Approved active-only Cloud Run services + Gen2 functions (functions are
# Cloud Run-backed, so both appear under resource.type=cloud_run_revision).
# Kept in sync with the 5xx alert policy filter in
# monitoring/EXTEND_MONITORING_gcloud.md. Excludes POCs / AI apps.
MONITORED_SERVICE_REGEX = (
    r"(.*-entity|bq-nsp-api|metadata-nsp-api|nsp-weekly-summary|"
    r"nsp-athlete-age-classifications|ora-nic-metadata|oracle-to-bq-load-v3|"
    r"webook-.*|appdynamics-api-gcs|dataset-sync|absa-orchestrator|"
    r"absa-gemini-enricher|gemini-absa-enricher|gmaps-multi-collector|"
    r"sharepoint-dataroom-extractor)"
)
# Log-based metric created via monitoring/EXTEND_MONITORING_gcloud.md (§2a).
DATAFUSION_FAILURE_METRIC = "logging.googleapis.com/user/nsp_datafusion_pipeline_failures"
CLOUD_RUN_5XX_METRIC = "run.googleapis.com/request_count"
SUMMARY_WINDOW_DAYS = 7


def get_pipeline_stats():
    """Get current pipeline status and last 7 days stats from watermark table."""
    bq = bigquery.Client(project=PROJECT_ID)

    # Current status of all pipelines
    status_sql = f"""
    SELECT
        table_name,
        primary_key_columns,
        run_status,
        total_runs,
        total_failures,
        rows_extracted,
        rows_inserted,
        rows_closed,
        last_successful_ts,
        run_start_time,
        run_end_time,
        error_message,
        TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), last_successful_ts, HOUR) as hours_since_last_run
    FROM `{PROJECT_ID}.{CONFIG_DATASET}.nsp_cdc_watermark`
    WHERE is_active = TRUE
    ORDER BY table_name
    """
    status_rows = list(bq.query(status_sql).result())

    # Get history table row counts
    count_sql = """
    SELECT 'ldv_nationalities' as entity, COUNT(*) as total, COUNTIF(is_current) as current_rows FROM dt_nsp_api.ldv_nationalities UNION ALL
    SELECT 'ldv_sportseasons', COUNT(*), COUNTIF(is_current) FROM dt_nsp_api.ldv_sportseasons UNION ALL
    SELECT 'ldv_clubsportses', COUNT(*), COUNTIF(is_current) FROM dt_nsp_api.ldv_clubsportses UNION ALL
    SELECT 'ldv_athleteageclassifications', COUNT(*), COUNTIF(is_current) FROM dt_nsp_api.ldv_athleteageclassifications UNION ALL
    SELECT 'ldv_staffdirectories', COUNT(*), COUNTIF(is_current) FROM dt_nsp_api.ldv_staffdirectories UNION ALL
    SELECT 'ldv_recurringregistrationwindows', COUNT(*), COUNTIF(is_current) FROM dt_nsp_api.ldv_recurringregistrationwindows UNION ALL
    SELECT 'ldv_nationalteams', COUNT(*), COUNTIF(is_current) FROM dt_nsp_api.ldv_nationalteams UNION ALL
    SELECT 'ldv_sportdisciplines', COUNT(*), COUNTIF(is_current) FROM dt_nsp_api.ldv_sportdisciplines UNION ALL
    SELECT 'ldv_contactaffiliationrecords', COUNT(*), COUNTIF(is_current) FROM dt_nsp_api.ldv_contactaffiliationrecords UNION ALL
    SELECT 'business_units', COUNT(*), COUNTIF(is_current) FROM dt_nsp_api.business_units UNION ALL
    SELECT 'accounts', COUNT(*), COUNTIF(is_current) FROM dt_nsp_api.accounts UNION ALL
    SELECT 'ldv_clubs', COUNT(*), COUNTIF(is_current) FROM dt_nsp_api.ldv_clubs UNION ALL
    SELECT 'ldv_countries', COUNT(*), COUNTIF(is_current) FROM dt_nsp_api.ldv_countries UNION ALL
    SELECT 'ldv_federations', COUNT(*), COUNTIF(is_current) FROM dt_nsp_api.ldv_federations UNION ALL
    SELECT 'contacts', COUNT(*), COUNTIF(is_current) FROM dt_nsp_api.contacts
    """
    try:
        count_rows = {row.entity: {'total': row.total, 'current': row.current_rows}
                      for row in bq.query(count_sql).result()}
    except Exception as e:
        logger.warning(f"Could not get all counts: {e}")
        count_rows = {}

    return status_rows, count_rows


def _sum_metric_by_service(metric_type, extra_filter, days=SUMMARY_WINDOW_DAYS):
    """
    Query Cloud Monitoring time series over the trailing window and return
    {resource_label_value: summed_value}. Used for Cloud Run 5xx counts.
    Returns {} on any error so the summary never fails because of monitoring.
    """
    try:
        client = monitoring_v3.MetricServiceClient()
        now = time.time()
        interval = monitoring_v3.TimeInterval({
            "end_time": {"seconds": int(now)},
            "start_time": {"seconds": int(now - days * 86400)},
        })
        flt = f'metric.type = "{metric_type}"'
        if extra_filter:
            flt += f" AND {extra_filter}"
        agg = monitoring_v3.Aggregation({
            "alignment_period": {"seconds": days * 86400},
            "per_series_aligner": monitoring_v3.Aggregation.Aligner.ALIGN_SUM,
            "cross_series_reducer": monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
            "group_by_fields": ["resource.labels.service_name"],
        })
        results = client.list_time_series(request={
            "name": f"projects/{PROJECT_ID}",
            "filter": flt,
            "interval": interval,
            "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            "aggregation": agg,
        })
        out = {}
        for ts in results:
            svc = ts.resource.labels.get("service_name", "unknown")
            total = 0
            for p in ts.points:
                total += int(p.value.int64_value or int(p.value.double_value or 0))
            out[svc] = out.get(svc, 0) + total
        return out
    except Exception as e:
        logger.warning(f"Monitoring query failed for {metric_type}: {e}")
        return {}


def get_cloud_run_health():
    """
    Return 5xx error counts per monitored Cloud Run service / Gen2 function over
    the trailing window. Only services matching the approved regex are kept.
    """
    import re
    counts = _sum_metric_by_service(
        CLOUD_RUN_5XX_METRIC,
        'resource.type = "cloud_run_revision" '
        'AND metric.labels.response_code_class = "5xx"',
    )
    pattern = re.compile(f"^{MONITORED_SERVICE_REGEX}$")
    return {svc: n for svc, n in counts.items() if n and pattern.match(svc)}


def get_datafusion_failures():
    """
    Return total Data Fusion pipeline failure events over the trailing window,
    from the log-based metric nsp_datafusion_pipeline_failures.
    """
    counts = _sum_metric_by_service(DATAFUSION_FAILURE_METRIC, "")
    # Log-based metric may not group by service_name; sum everything.
    return sum(counts.values()) if counts else 0


def build_summary_html(status_rows, count_rows,
                       cloud_run_5xx=None, datafusion_failures=0):
    """Build HTML email summary."""
    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)

    # Categorize pipelines
    healthy = []
    failed = []
    stale = []
    running = []

    for row in status_rows:
        if row.run_status == 'FAILED':
            failed.append(row)
        elif row.run_status == 'RUNNING':
            running.append(row)
        elif row.hours_since_last_run and row.hours_since_last_run > 25:
            stale.append(row)
        else:
            healthy.append(row)

    total_pipelines = len(status_rows)
    total_runs = sum(row.total_runs for row in status_rows)
    total_failures = sum(row.total_failures for row in status_rows)

    # Build summary text
    lines = []
    lines.append("=" * 60)
    lines.append(f"  NSP SCD2 PIPELINE - WEEKLY SUMMARY")
    lines.append(f"  Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"  Project: {PROJECT_ID}")
    lines.append("=" * 60)
    lines.append("")

    # Overview
    lines.append("📊 OVERVIEW")
    lines.append(f"  Total Pipelines: {total_pipelines}")
    lines.append(f"  ✅ Healthy: {len(healthy)}")
    lines.append(f"  ❌ Failed: {len(failed)}")
    lines.append(f"  ⚠️  Stale (>25h since last run): {len(stale)}")
    lines.append(f"  🔄 Running: {len(running)}")
    lines.append(f"  Total Runs (lifetime): {total_runs}")
    lines.append(f"  Total Failures (lifetime): {total_failures}")
    lines.append("")

    # Failed pipelines detail
    if failed:
        lines.append("❌ FAILED PIPELINES")
        lines.append("-" * 40)
        for row in failed:
            lines.append(f"  {row.table_name}")
            lines.append(f"    Error: {(row.error_message or 'Unknown')[:100]}")
            lines.append(f"    Last success: {row.last_successful_ts}")
            lines.append("")

    # Stale pipelines
    if stale:
        lines.append("⚠️  STALE PIPELINES (not run in >25 hours)")
        lines.append("-" * 40)
        for row in stale:
            lines.append(f"  {row.table_name} — {row.hours_since_last_run:.0f}h since last run")
        lines.append("")

    # Pipeline Status Table
    lines.append("📋 PIPELINE STATUS")
    lines.append("-" * 80)
    lines.append(f"  {'Entity':<35} {'Status':<10} {'Runs':<6} {'Fails':<6} {'Last Run':<20}")
    lines.append(f"  {'-'*35} {'-'*10} {'-'*6} {'-'*6} {'-'*20}")
    for row in status_rows:
        last_run = row.last_successful_ts.strftime('%Y-%m-%d %H:%M') if row.last_successful_ts else 'Never'
        status_icon = '✅' if row.run_status == 'COMPLETED' else '❌' if row.run_status == 'FAILED' else '🔄'
        lines.append(f"  {row.table_name:<35} {status_icon:<10} {row.total_runs:<6} {row.total_failures:<6} {last_run:<20}")
    lines.append("")

    # Data Volume
    if count_rows:
        lines.append("📦 DATA VOLUME (History Tables)")
        lines.append("-" * 60)
        lines.append(f"  {'Entity':<35} {'Total Rows':<12} {'Current':<12}")
        lines.append(f"  {'-'*35} {'-'*12} {'-'*12}")
        total_all = 0
        for entity in sorted(count_rows.keys()):
            data = count_rows[entity]
            total_all += data['total']
            lines.append(f"  {entity:<35} {data['total']:<12,} {data['current']:<12,}")
        lines.append(f"  {'TOTAL':<35} {total_all:<12,}")
        lines.append("")

    # Cloud Run / Gen2 Functions 5xx health (last 7 days)
    lines.append("🌐 CLOUD RUN / FUNCTIONS HEALTH (5xx, last 7 days)")
    lines.append("-" * 60)
    if cloud_run_5xx:
        lines.append(f"  {'Service / Function':<45} {'5xx Errors':<10}")
        lines.append(f"  {'-'*45} {'-'*10}")
        for svc in sorted(cloud_run_5xx, key=lambda s: -cloud_run_5xx[s]):
            lines.append(f"  {svc:<45} {cloud_run_5xx[svc]:<10}")
        lines.append(f"  Total services with 5xx: {len(cloud_run_5xx)}")
    else:
        lines.append("  ✅ No 5xx errors on monitored services/functions.")
    lines.append("")

    # Data Fusion pipeline failures (last 7 days, log-based metric)
    lines.append("🔀 DATA FUSION PIPELINE FAILURES (last 7 days)")
    lines.append("-" * 60)
    if datafusion_failures:
        lines.append(f"  ❌ {datafusion_failures} Data Fusion failure event(s) detected.")
        lines.append("     Check CDF run history and Dataproc job logs.")
    else:
        lines.append("  ✅ No Data Fusion pipeline failures detected.")
    lines.append("")

    lines.append("=" * 60)
    lines.append("  End of Weekly Summary")
    lines.append(f"  Next report: {(now + timedelta(days=7)).strftime('%Y-%m-%d')}")
    lines.append("=" * 60)

    return "\n".join(lines)


def send_summary_via_log(summary_text):
    """
    Log the summary as a structured log entry.
    The alert policy on log-based metric will trigger email.
    Alternative: we write to a BQ table for historical tracking.
    """
    bq = bigquery.Client(project=PROJECT_ID)

    # Store summary in a tracking table
    sql = f"""
    CREATE TABLE IF NOT EXISTS `{PROJECT_ID}.{CONFIG_DATASET}.nsp_weekly_summaries` (
        report_date DATE,
        summary_text STRING,
        total_pipelines INT64,
        healthy_count INT64,
        failed_count INT64,
        stale_count INT64,
        created_at TIMESTAMP
    )
    """
    bq.query(sql).result()

    logger.info("=" * 60)
    logger.info("NSP SCD2 WEEKLY SUMMARY REPORT")
    logger.info("=" * 60)
    for line in summary_text.split("\n"):
        logger.info(line)

    return summary_text


def main(request):
    """
    Cloud Run HTTP entry point for weekly summary.
    Called by Cloud Scheduler every Sunday at 8:00 AM KSA.
    """
    try:
        logger.info("Generating weekly pipeline summary...")

        # Get stats
        status_rows, count_rows = get_pipeline_stats()

        # New: cross-layer health (Cloud Run/Functions 5xx + Data Fusion failures)
        cloud_run_5xx = get_cloud_run_health()
        datafusion_failures = get_datafusion_failures()

        # Build summary
        summary_text = build_summary_html(
            status_rows, count_rows,
            cloud_run_5xx=cloud_run_5xx,
            datafusion_failures=datafusion_failures,
        )

        # Log it (visible in Cloud Run logs)
        send_summary_via_log(summary_text)

        # Return summary as response
        return json.dumps({
            "status": "SUCCESS",
            "summary": summary_text,
            "pipelines_total": len(status_rows),
            "pipelines_healthy": len([r for r in status_rows if r.run_status == 'COMPLETED']),
            "pipelines_failed": len([r for r in status_rows if r.run_status == 'FAILED']),
            "cloud_run_services_with_5xx": len(cloud_run_5xx),
            "datafusion_failures": datafusion_failures,
        }), 200, {"Content-Type": "application/json"}

    except Exception as e:
        logger.error(f"Weekly summary failed: {e}", exc_info=True)
        return json.dumps({
            "status": "FAILED",
            "error": str(e),
        }), 500, {"Content-Type": "application/json"}
