import json
import time
import requests
import io
import re
import logging
from datetime import datetime, timezone, timedelta
from google.cloud import storage, bigquery

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_ID          = "mos-gcp-host-dev-prj"
STAGING_DATASET     = "ds_nsp_api"
HISTORY_DATASET     = "dt_nsp_api"
CONFIG_DATASET      = "mos_config"
GCS_BUCKET          = "mos-dataroom-gcp-dev-storage"
GCS_BASE_PATH       = "ldv_recurringregistrationwindows_scd2"

ENTITY_NAME         = "ldv_recurringregistrationwindows"
TABLE_NAME          = "ldv_recurringregistrationwindows"
PRIMARY_KEY         = "ldv_recurringregistrationwindowid"
DELTA_COLUMN        = "modifiedon"
ODATA_PAGE_SIZE     = 5_000
MAX_PAGES           = 5_000
MAX_RETRIES         = 3
RETRY_BACKOFF_SEC   = 5
TOKEN_REFRESH_PAGES = 50

# Change detection columns (from Data Fusion SCD2_Merge stage)
CHANGE_DETECT_COLUMNS = [
    'versionnumber'
]

# ── CRM Credentials (from Secret Manager) ─────────────────────────────────────
def _get_secret(secret_id):
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8").strip()

_credentials_cache = {}

def get_crm_credentials():
    """Get CRM credentials from Secret Manager (cached)."""
    if not _credentials_cache:
        _credentials_cache['tenant_id'] = _get_secret("TENANT_ID_PROD")
        _credentials_cache['client_id'] = _get_secret("CLIENT_ID_PROD")
        _credentials_cache['client_secret'] = _get_secret("CLIENT_SECRET_PROD")
        _credentials_cache['dynamics_url'] = _get_secret("DYNAMICS_URL_PROD")
        logger.info("CRM credentials loaded from Secret Manager")
    return _credentials_cache

# ── TYPED_SCHEMA ─────────────────────────────────────────────────────────────
# 44 columns from D365 ldv_recurringregistrationwindows entity
TYPED_SCHEMA = [
    ('odata_etag', 'STRING', 'string'),
    ('ldv_perioddescription', 'STRING', 'string'),
    ('ldv_allowedclubclassificationcode', 'INTEGER', 'int'),
    ('ldv_maximumnumberofforeignathletesperclub', 'INTEGER', 'int'),
    ('_modifiedby_value', 'STRING', 'string'),
    ('overriddencreatedon', 'STRING', 'string'),
    ('ldv_enddate', 'STRING', 'string'),
    ('ldv_recurringregistrationwindowname', 'STRING', 'string'),
    ('ldv_origin', 'STRING', 'string'),
    ('ldv_startdate', 'STRING', 'string'),
    ('statecode', 'INTEGER', 'int'),
    ('createdon', 'STRING', 'string'),
    ('ldv_perioddescriptionarabic', 'STRING', 'string'),
    ('ldv_tmp_age_classificationcode', 'STRING', 'string'),
    ('_ldv_registrationwindowid_value', 'STRING', 'string'),
    ('_ownerid_value', 'STRING', 'string'),
    ('modifiedon', 'STRING', 'string'),
    ('_createdonbehalfby_value', 'STRING', 'string'),
    ('_owninguser_value', 'STRING', 'string'),
    ('importsequencenumber', 'INTEGER', 'int'),
    ('ldv_minimumnumofsaudibornnonnationalperclub', 'INTEGER', 'int'),
    ('_ldv_sportdisciplineid_value', 'STRING', 'string'),
    ('_owningteam_value', 'STRING', 'string'),
    ('ldv_professionalamateurathletecode', 'INTEGER', 'int'),
    ('ldv_minimumnumberofforeignathletesperclub', 'INTEGER', 'int'),
    ('timezoneruleversionnumber', 'INTEGER', 'int'),
    ('ldv_allowedclubtypecode', 'STRING', 'string'),
    ('ldv_maximumnumofsaudibornnonnationalperclub', 'INTEGER', 'int'),
    ('_createdby_value', 'STRING', 'string'),
    ('ldv_allowedaffiliationtypecode', 'STRING', 'string'),
    ('ldv_isautomaticclassificationupdate', 'BOOLEAN', 'bool'),
    ('_ldv_athletesageclassificationid_value', 'STRING', 'string'),
    ('_ldv_federationid_value', 'STRING', 'string'),
    ('_owningbusinessunit_value', 'STRING', 'string'),
    ('ldv_name', 'STRING', 'string'),
    ('ldv_minimumnumberofnationalathletesperclub', 'INTEGER', 'int'),
    ('_modifiedonbehalfby_value', 'STRING', 'string'),
    ('versionnumber', 'INTEGER', 'int'),
    ('ldv_maximumnumberofnationalathletesperclub', 'INTEGER', 'int'),
    ('ldv_recurringregistrationwindowid', 'STRING', 'string'),
    ('ldv_isautomaticathleterelease', 'BOOLEAN', 'bool'),
    ('utcconversiontimezonecode', 'INTEGER', 'int'),
    ('statuscode', 'INTEGER', 'int'),
    ('_ldv_sportseasonid_value', 'STRING', 'string'),
]

assert len(TYPED_SCHEMA) == 44, f"Expected 44 columns, got {len(TYPED_SCHEMA)}"
_TYPED_SCHEMA_COLS = {c for c, _, _ in TYPED_SCHEMA}


# ── OAuth Token ───────────────────────────────────────────────────────────────
def get_access_token(tenant_id, client_id, client_secret, dynamics_url):
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    payload = {
        "client_id": client_id,
        "scope": f"{dynamics_url}/.default",
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }
    resp = requests.post(url, data=payload,
                         headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    resp.raise_for_status()
    logger.info("CRM authentication successful")
    return resp.json()["access_token"]


# ── Schema helpers ────────────────────────────────────────────────────────────
def sanitize_bq_field_name(name):
    if name is None:
        name = ""
    n = re.sub(r"[^A-Za-z0-9_]", "_", str(name).strip().lower())
    n = re.sub(r"_+", "_", n).lstrip("_")
    if not n or n[0].isdigit():
        n = f"c_{n}" if n else "c"
    if len(n) > 300:
        n = n[:300]
    if not re.search(r"[A-Za-z0-9]", n):
        n = "c"
    return n


def normalize_record(record):
    out = {}
    seen_sanitized = {}
    for k, v in record.items():
        if k == "@odata.etag":
            sk = "odata_etag"
        elif k.startswith("@"):
            continue
        else:
            sk = sanitize_bq_field_name(k)
            if sk in seen_sanitized and seen_sanitized[sk] != k:
                i = 2
                while f"{sk}_{i}" in seen_sanitized:
                    i += 1
                sk = f"{sk}_{i}"
        seen_sanitized[sk] = k
        if v is None:
            out[sk] = None
        elif isinstance(v, (dict, list)):
            out[sk] = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, bool):
            out[sk] = "true" if v else "false"
        else:
            out[sk] = str(v)
    return out


# ── GCS helpers ───────────────────────────────────────────────────────────────
def upload_page_to_gcs(records, bucket_name, blob_path):
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    ndjson = "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    blob.upload_from_string(ndjson, content_type="application/x-ndjson")


def compose_parts_to_single_file(bucket_name, folder_path, final_blob_path):
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    CHUNK = 32
    parts = sorted(
        client.list_blobs(bucket_name, prefix=f"{folder_path}/part_"),
        key=lambda b: b.name)
    if not parts:
        raise RuntimeError("No part files to compose")
    logger.info(f"  Composing {len(parts)} part files...")

    if len(parts) <= CHUNK:
        final_blob = bucket.blob(final_blob_path)
        final_blob.compose(parts)
    else:
        tmp_blobs = []
        for i, start in enumerate(range(0, len(parts), CHUNK)):
            chunk = parts[start:start + CHUNK]
            tmp_path = f"{folder_path}/tmp_compose_{i:05d}.ndjson"
            tmp_blob = bucket.blob(tmp_path)
            tmp_blob.compose(chunk)
            tmp_blobs.append(tmp_blob)
        if len(tmp_blobs) <= CHUNK:
            final_blob = bucket.blob(final_blob_path)
            final_blob.compose(tmp_blobs)
        else:
            tmp2_blobs = []
            for i, start in enumerate(range(0, len(tmp_blobs), CHUNK)):
                chunk = tmp_blobs[start:start + CHUNK]
                tmp_path = f"{folder_path}/tmp2_compose_{i:05d}.ndjson"
                tmp_blob = bucket.blob(tmp_path)
                tmp_blob.compose(chunk)
                tmp2_blobs.append(tmp_blob)
            final_blob = bucket.blob(final_blob_path)
            final_blob.compose(tmp2_blobs)
            for b in tmp2_blobs:
                b.delete()
        for b in tmp_blobs:
            b.delete()

    for part in parts:
        part.delete()
    logger.info(f"  Composed → gs://{bucket_name}/{final_blob_path}")


def cleanup_gcs_folder(bucket_name, folder_path):
    """Delete all files in the GCS folder after successful load."""
    client = storage.Client()
    blobs = list(client.list_blobs(bucket_name, prefix=folder_path))
    for blob in blobs:
        blob.delete()
    logger.info(f"  Cleaned up GCS folder: gs://{bucket_name}/{folder_path} ({len(blobs)} files)")


# ── Watermark functions ───────────────────────────────────────────────────────
def get_watermark():
    """Read last_successful_ts from watermark table. Returns None if no watermark."""
    bq = bigquery.Client(project=PROJECT_ID)
    sql = f"""
    SELECT last_successful_ts, primary_key_columns, change_detect_columns
    FROM `{PROJECT_ID}.{CONFIG_DATASET}.nsp_cdc_watermark`
    WHERE table_name = '{TABLE_NAME}' AND is_active = TRUE
    """
    rows = list(bq.query(sql).result())
    if not rows:
        return None
    row = rows[0]
    return row.last_successful_ts


def check_already_running():
    """Check if pipeline is already running (concurrent execution protection)."""
    bq = bigquery.Client(project=PROJECT_ID)
    sql = f"""
    SELECT run_status FROM `{PROJECT_ID}.{CONFIG_DATASET}.nsp_cdc_watermark`
    WHERE table_name = '{TABLE_NAME}' AND run_status = 'RUNNING'
    """
    rows = list(bq.query(sql).result())
    return len(rows) > 0


def set_watermark_running():
    """Set watermark status to RUNNING."""
    bq = bigquery.Client(project=PROJECT_ID)
    sql = f"""
    UPDATE `{PROJECT_ID}.{CONFIG_DATASET}.nsp_cdc_watermark`
    SET run_status = 'RUNNING', run_start_time = CURRENT_TIMESTAMP(),
        run_end_time = NULL, rows_extracted = 0, rows_inserted = 0, rows_closed = 0,
        error_message = NULL, updated_at = CURRENT_TIMESTAMP()
    WHERE table_name = '{TABLE_NAME}'
    """
    bq.query(sql).result()
    logger.info("  Watermark set to RUNNING")


def set_watermark_completed(rows_extracted, rows_inserted, rows_closed):
    """Set watermark status to COMPLETED with stats."""
    bq = bigquery.Client(project=PROJECT_ID)
    sql = f"""
    UPDATE `{PROJECT_ID}.{CONFIG_DATASET}.nsp_cdc_watermark`
    SET run_status = 'COMPLETED', run_end_time = CURRENT_TIMESTAMP(),
        last_successful_ts = CURRENT_TIMESTAMP(),
        rows_extracted = {rows_extracted}, rows_inserted = {rows_inserted},
        rows_closed = {rows_closed},
        updated_at = CURRENT_TIMESTAMP(), total_runs = total_runs + 1, error_message = NULL
    WHERE table_name = '{TABLE_NAME}'
    """
    bq.query(sql).result()
    logger.info(f"  Watermark set to COMPLETED (extracted={rows_extracted}, inserted={rows_inserted}, closed={rows_closed})")


def set_watermark_failed(error_msg):
    """Set watermark status to FAILED."""
    bq = bigquery.Client(project=PROJECT_ID)
    safe_msg = str(error_msg)[:500].replace("'", "").replace("\\", "").replace("\n", " ")
    sql = f"""
    UPDATE `{PROJECT_ID}.{CONFIG_DATASET}.nsp_cdc_watermark`
    SET run_status = 'FAILED', run_end_time = CURRENT_TIMESTAMP(),
        updated_at = CURRENT_TIMESTAMP(), total_failures = total_failures + 1,
        error_message = '{safe_msg}'
    WHERE table_name = '{TABLE_NAME}'
    """
    try:
        bq.query(sql).result()
    except Exception:
        pass
    logger.info(f"  Watermark set to FAILED: {error_msg}")


# ── Extract + stream to GCS ───────────────────────────────────────────────────
def extract_and_stream(entity_name, dynamics_url, tenant_id, client_id, client_secret,
                       bucket_name, folder_path, delta_filter=None):
    """
    Stream each OData page directly to a GCS part file as NDJSON.
    Returns (total_records, pages, discovered_fields_set).
    """
    access_token = get_access_token(tenant_id, client_id, client_secret, dynamics_url)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Prefer": f"odata.maxpagesize={ODATA_PAGE_SIZE}",
    }

    url = f"{dynamics_url}/api/data/v9.0/{entity_name}?$count=true"
    if delta_filter:
        url += f"&$filter={delta_filter}"
        logger.info(f"  Delta filter applied: {delta_filter}")

    page_num = 0
    total_records = 0
    discovered_keys = set()

    logger.info(f"Starting extraction for entity: {entity_name}")

    while url:
        page_num += 1
        if page_num > 1 and page_num % TOKEN_REFRESH_PAGES == 0:
            access_token = get_access_token(tenant_id, client_id, client_secret, dynamics_url)
            headers["Authorization"] = f"Bearer {access_token}"

        last_exc = None
        records = []
        next_link = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = requests.get(url, headers=headers, timeout=300)
                response.raise_for_status()
                data = response.json()
                records = data.get("value", [])
                next_link = data.get("@odata.nextLink")
                last_exc = None
                if page_num == 1:
                    logger.info(f"  Server total: {data.get('@odata.count', 'N/A')}")
                logger.info(f"  Page {page_num}: {len(records)} records | total: {total_records + len(records):,}")
                break
            except Exception as exc:
                last_exc = exc
                wait = RETRY_BACKOFF_SEC * (2 ** (attempt - 1))
                logger.warning(f"  Page {page_num} attempt {attempt} failed: {exc}. Retry in {wait}s...")
                time.sleep(wait)

        if last_exc is not None:
            raise RuntimeError(f"Extraction failed on page {page_num} after {MAX_RETRIES} retries: {last_exc}")

        if not records:
            url = next_link
            if page_num >= MAX_PAGES:
                break
            continue

        normalized = [normalize_record(r) for r in records]
        for rec in normalized:
            discovered_keys.update(rec.keys())

        blob_path = f"{folder_path}/part_{page_num:05d}.ndjson"
        upload_page_to_gcs(normalized, bucket_name, blob_path)
        total_records += len(normalized)
        records = None
        normalized = None
        url = next_link
        if page_num >= MAX_PAGES:
            break

    logger.info(f"[{entity_name}] Extraction complete: {total_records:,} records, {page_num} pages")
    return total_records, page_num, discovered_keys


# ── BigQuery: load NDJSON to staging ──────────────────────────────────────────
def load_to_staging(bucket_name, blob_path, fieldnames):
    """Load NDJSON from GCS into staging table with typed schema via SAFE_CAST."""
    bq = bigquery.Client(project=PROJECT_ID)
    temp_table_id = f"{PROJECT_ID}.{STAGING_DATASET}.temp_{TABLE_NAME}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    staging_table_id = f"{PROJECT_ID}.{STAGING_DATASET}.{TABLE_NAME}"

    # Step 1: Load to temp table (all STRING)
    all_fields = set(fieldnames) | _TYPED_SCHEMA_COLS
    schema = [bigquery.SchemaField(f, "STRING", mode="NULLABLE") for f in sorted(all_fields)]

    bq.delete_table(temp_table_id, not_found_ok=True)
    bq.create_table(bigquery.Table(temp_table_id, schema=schema))

    gcs_uri = f"gs://{bucket_name}/{blob_path}"
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        autodetect=False, schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        ignore_unknown_values=True, max_bad_records=0,
    )
    load_job = bq.load_table_from_uri(gcs_uri, temp_table_id, job_config=job_config)
    load_job.result()
    loaded_rows = load_job.output_rows or 0
    logger.info(f"  Temp table loaded: {loaded_rows:,} rows")

    # Step 2: SAFE_CAST into typed staging table
    sql = build_typed_select_sql(temp_table_id, staging_table_id)
    bq.query(sql).result()

    # Step 3: Drop temp
    bq.delete_table(temp_table_id, not_found_ok=True)

    # Get final count
    result = list(bq.query(f"SELECT COUNT(*) as cnt FROM `{staging_table_id}`").result())
    final_count = result[0].cnt
    logger.info(f"  Staging table loaded: {final_count:,} rows (typed)")
    return final_count


def build_typed_select_sql(temp_table_id, final_table_id):
    """Generate CREATE OR REPLACE TABLE with SAFE_CAST for typed columns."""
    bidi_pattern = r'[\x{200E}\x{200F}\x{202A}\x{202B}\x{202C}\x{202D}\x{202E}]'
    select_parts = []
    for col, _bq_type, parser in TYPED_SCHEMA:
        clean = (f"NULLIF(TRIM(REGEXP_REPLACE(NORMALIZE(`{col}`, NFKC), "
                 f"r'{bidi_pattern}', '')), '')")
        if parser == "string":
            select_parts.append(f"  {clean} AS `{col}`")
        elif parser == "int":
            select_parts.append(f"  SAFE_CAST({clean} AS INT64) AS `{col}`")
        elif parser == "float":
            select_parts.append(f"  SAFE_CAST(REPLACE({clean}, ',', '') AS FLOAT64) AS `{col}`")
        elif parser == "bool":
            select_parts.append(
                f"  CASE LOWER({clean}) WHEN 'true' THEN TRUE WHEN '1' THEN TRUE "
                f"WHEN 'false' THEN FALSE WHEN '0' THEN FALSE ELSE NULL END AS `{col}`")
    select_sql = ",\n".join(select_parts)
    return f"CREATE OR REPLACE TABLE `{final_table_id}` AS\nSELECT\n{select_sql}\nFROM `{temp_table_id}`"


# ── SCD2 Merge ────────────────────────────────────────────────────────────────
def run_scd2_merge():
    """
    Run SCD2 merge: close changed records + insert new versions.
    Returns (rows_inserted, rows_closed).
    """
    bq = bigquery.Client(project=PROJECT_ID)
    staging = f"{PROJECT_ID}.{STAGING_DATASET}.{TABLE_NAME}"
    history = f"{PROJECT_ID}.{HISTORY_DATASET}.{TABLE_NAME}"

    # Build change detection condition
    change_conditions = []
    for col in CHANGE_DETECT_COLUMNS:
        col_type = 'string'
        for c, bq_type, p in TYPED_SCHEMA:
            if c == col:
                col_type = p
                break
        if col_type == 'int':
            change_conditions.append(f"COALESCE(CAST(S.`{col}` AS INT64), 0) != COALESCE(CAST(T.`{col}` AS INT64), 0)")
        elif col_type == 'float':
            change_conditions.append(f"COALESCE(CAST(S.`{col}` AS FLOAT64), 0.0) != COALESCE(CAST(T.`{col}` AS FLOAT64), 0.0)")
        elif col_type == 'bool':
            change_conditions.append(f"COALESCE(S.`{col}`, FALSE) != COALESCE(T.`{col}`, FALSE)")
        else:
            change_conditions.append(f"COALESCE(CAST(S.`{col}` AS STRING), '') != COALESCE(CAST(T.`{col}` AS STRING), '')")

    change_sql = "\n        OR ".join(change_conditions)

    merge_sql = f"""
    DECLARE merge_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP();

    -- Step A: Close old versions for CHANGED records
    UPDATE `{history}` T
    SET T.valid_to = merge_ts, T.is_current = FALSE
    WHERE T.is_current = TRUE
      AND EXISTS (
        SELECT 1 FROM `{staging}` S
        WHERE S.`{PRIMARY_KEY}` = T.`{PRIMARY_KEY}`
          AND (
            {change_sql}
          )
      );

    -- Step B: Insert new versions (new + changed records)
    INSERT INTO `{history}`
    SELECT
      S.*,
      COALESCE((SELECT MAX(T2.row_version) + 1 FROM `{history}` T2
                WHERE T2.`{PRIMARY_KEY}` = S.`{PRIMARY_KEY}`), 1) AS row_version,
      merge_ts AS valid_from,
      CAST(NULL AS TIMESTAMP) AS valid_to,
      TRUE AS is_current,
      CASE WHEN EXISTS (SELECT 1 FROM `{history}` T2 WHERE T2.`{PRIMARY_KEY}` = S.`{PRIMARY_KEY}`)
           THEN 'U' ELSE 'I' END AS last_operation,
      merge_ts AS sync_timestamp
    FROM `{staging}` S
    WHERE
      NOT EXISTS (SELECT 1 FROM `{history}` T WHERE T.`{PRIMARY_KEY}` = S.`{PRIMARY_KEY}` AND T.is_current = TRUE)
      OR EXISTS (SELECT 1 FROM `{history}` T WHERE T.`{PRIMARY_KEY}` = S.`{PRIMARY_KEY}`
                 AND T.is_current = FALSE AND T.valid_to = merge_ts);
    """

    logger.info("  Running SCD2 merge...")
    bq.query(merge_sql).result()

    # Get stats
    stats_sql = f"""
    SELECT
      (SELECT COUNT(*) FROM `{history}` WHERE is_current = TRUE
       AND valid_from >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 10 MINUTE)) AS rows_inserted,
      (SELECT COUNT(*) FROM `{history}` WHERE is_current = FALSE
       AND valid_to >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 10 MINUTE)) AS rows_closed
    """
    stats = list(bq.query(stats_sql).result())[0]
    logger.info(f"  SCD2 merge complete: inserted={stats.rows_inserted}, closed={stats.rows_closed}")
    return stats.rows_inserted, stats.rows_closed


# ── Create Views (no PII masking needed for reference data) ───────────────────
def create_views():
    """Recreate consumption views (no PII masking for reference data)."""
    bq = bigquery.Client(project=PROJECT_ID)
    history = f"{PROJECT_ID}.{HISTORY_DATASET}.{TABLE_NAME}"
    views_dataset = "dc_nsp_api_views"

    current_view_sql = f"""
    CREATE OR REPLACE VIEW `{PROJECT_ID}.{views_dataset}.ldv_recurringregistrationwindows_current` AS
    SELECT * EXCEPT(valid_to, is_current)
    FROM `{history}` WHERE is_current = TRUE
    """

    history_view_sql = f"""
    CREATE OR REPLACE VIEW `{PROJECT_ID}.{views_dataset}.ldv_recurringregistrationwindows_history` AS
    SELECT *
    FROM `{history}`
    ORDER BY ldv_recurringregistrationwindowid, row_version
    """

    logger.info("  Creating views...")
    bq.query(current_view_sql).result()
    logger.info("  ✓ ldv_recurringregistrationwindows_current view created")
    bq.query(history_view_sql).result()
    logger.info("  ✓ ldv_recurringregistrationwindows_history view created")


# ── Entry point ───────────────────────────────────────────────────────────────
def main(request):
    """
    Cloud Run HTTP entry point.

    Modes:
      - Delta (default): No body or {} → uses watermark for delta filter
      - Full load: {"force_full": true} → ignores watermark, fetches all records
      - Custom start: {"start_date": "2025-01-01T00:00:00Z"} → fetches from that date
    """
    start_time = time.time()

    try:
        # Parse request body
        body = {}
        if request.is_json:
            body = request.get_json(silent=True) or {}

        force_full = body.get("force_full", False)
        custom_start = body.get("start_date", None)

        # Concurrent execution protection
        if check_already_running():
            logger.warning("Pipeline already RUNNING - aborting to prevent duplicate execution")
            return json.dumps({
                "status": "SKIPPED",
                "reason": "Pipeline already running. Concurrent execution prevented.",
            }), 200, {"Content-Type": "application/json"}

        # Step 1: Read watermark and determine filter
        watermark_ts = get_watermark()

        # Load CRM credentials from Secret Manager
        creds = get_crm_credentials()

        if force_full:
            delta_filter = None
            mode = "FULL_LOAD"
            logger.info("Mode: FULL LOAD (force_full=true)")
        elif custom_start:
            delta_filter = f"{DELTA_COLUMN} gt {custom_start}"
            mode = f"CUSTOM_START ({custom_start})"
            logger.info(f"Mode: CUSTOM START from {custom_start}")
        elif watermark_ts:
            ts_str = watermark_ts.strftime('%Y-%m-%dT%H:%M:%SZ')
            delta_filter = f"{DELTA_COLUMN} gt {ts_str}"
            mode = f"DELTA (watermark: {ts_str})"
            logger.info(f"Mode: DELTA from watermark {ts_str}")
        else:
            delta_filter = None
            mode = "FULL_LOAD (no watermark)"
            logger.info("Mode: FULL LOAD (no watermark found - first run)")

        # Step 2: Set watermark to RUNNING
        set_watermark_running()

        # Step 3: Extract from D365 API → GCS (page by page)
        now = datetime.now()
        folder_path = f"{GCS_BASE_PATH}/{ENTITY_NAME}/{now.strftime('%Y/%m/%d/%H%M%S')}"
        final_blob_path = f"{folder_path}/{ENTITY_NAME}_extract.ndjson"

        total_records, pages, fieldnames = extract_and_stream(
            ENTITY_NAME, creds['dynamics_url'],
            creds['tenant_id'], creds['client_id'], creds['client_secret'],
            GCS_BUCKET, folder_path, delta_filter=delta_filter
        )

        if total_records == 0:
            logger.info("No records extracted - nothing to merge")
            set_watermark_completed(0, 0, 0)
            processing_time = round(time.time() - start_time, 2)
            return json.dumps({
                "status": "SUCCESS", "mode": mode,
                "records_extracted": 0, "records_inserted": 0, "records_closed": 0,
                "processing_time_seconds": processing_time,
            }), 200, {"Content-Type": "application/json"}

        # Step 4: Compose GCS parts into single file
        logger.info("Composing GCS parts...")
        compose_parts_to_single_file(GCS_BUCKET, folder_path, final_blob_path)

        # Step 5: Load into staging table (typed)
        logger.info("Loading to staging table...")
        staging_count = load_to_staging(GCS_BUCKET, final_blob_path, fieldnames)

        # Step 6: Run SCD2 merge (staging → history)
        rows_inserted, rows_closed = run_scd2_merge()

        # Step 7: Update watermark
        set_watermark_completed(staging_count, rows_inserted, rows_closed)

        # Step 8: Cleanup GCS
        cleanup_gcs_folder(GCS_BUCKET, folder_path)

        # Step 9: Recreate views
        create_views()

        processing_time = round(time.time() - start_time, 2)
        result = {
            "status": "SUCCESS",
            "mode": mode,
            "records_extracted": total_records,
            "records_staged": staging_count,
            "records_inserted": rows_inserted,
            "records_closed": rows_closed,
            "pages_processed": pages,
            "processing_time_seconds": processing_time,
        }
        logger.info(f"========== DONE: {mode} | {total_records:,} extracted, "
                    f"{rows_inserted} inserted, {rows_closed} closed in {processing_time}s ==========")
        return json.dumps(result), 200, {"Content-Type": "application/json"}

    except Exception as e:
        processing_time = round(time.time() - start_time, 2)
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        set_watermark_failed(str(e))
        return json.dumps({
            "status": "FAILED",
            "error": str(e),
            "processing_time_seconds": processing_time,
        }), 500, {"Content-Type": "application/json"}
