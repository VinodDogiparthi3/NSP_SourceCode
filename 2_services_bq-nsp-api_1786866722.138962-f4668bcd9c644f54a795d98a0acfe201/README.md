# bq-nsp-api — BigQuery Data API

REST API service providing paginated, filtered access to BigQuery views with OAuth 2.0 authentication.

## Service Details

| Property | Value |
|----------|-------|
| **Project** | `mos-gcp-host-dev-prj` (487990962362) |
| **Region** | me-central2 |
| **Runtime** | Python 3.11 |
| **Function Entry Point** | `query_bigquery` |
| **Service Account** | `mos-gcp-dataroom-cloudrun@mos-gcp-host-dev-prj.iam.gserviceaccount.com` |
| **Active Revision** | bq-nsp-api-00036-r9x |
| **Last Deployed** | 2026-08-15 |
| **Created By** | c.skvasudevan@mos.gov.sa |

## URLs

- https://bq-nsp-api-487990962362.me-central2.run.app
- https://bq-nsp-api-ay7xgyzm7a-wx.a.run.app

## Resource Configuration

| Setting | Value |
|---------|-------|
| CPU | 1 vCPU |
| Memory | 512 Mi |
| Max Instances | 10 |
| Concurrency | 80 requests/container |
| Request Timeout | 300s |
| Startup CPU Boost | Enabled |

## Networking

- **VPC**: `mos-gcp-dev-system-vpc` / `mos-gcp-dev-system-snet`
- **Egress**: Private ranges only
- **Ingress**: All (public)
- **IAM Invoker**: Disabled (unauthenticated access allowed)

## API Endpoints

| Endpoint | Method | Auth Scope | Description |
|----------|--------|------------|-------------|
| `/health` | GET | None | Health check |
| `/oauth/token` | POST | None | Get access token (client_credentials grant) |
| `/api/tables` | GET | read | List available tables |
| `/api/<table_key>/data` | GET | read | Paginated data with filters |
| `/api/<table_key>/count` | GET | count | Record count with filters |
| `/api/data` | GET | read | Shortcut → nsp_base data |
| `/api/count` | GET | count | Shortcut → nsp_base count |

## Available Tables

| Table Key | BigQuery View | Records | Filters |
|-----------|---------------|---------|---------|
| `nsp_base` | `dw_nsp_api_views.nsp_base_view_01` | 132,021 | contactid, region, status_ar, individual_type_ar |
| `fr_base` | `dw_ms_crm_onprem_views.fr_base_view_01` | 34,902 | booking_id, contact_name |
| `appdynamics_base` | `dw_appdynamics_views.appdynamics_base_view_01` | 6,038 | incident_id, affected_entity_name |
| `webook_orders` | `dw_webook_01_views.Events_orders_Attendees_Baseview_01` | 1,588,678 | event_id, order_number |
| `webook_event_details` | `dw_webook_01_views.Events_EventDetails_Baseview_01` | 142 | event_id, venue_id |
| `webook_Attendees_details` | `dw_webook_01_views.webook_Event_Attendees_BaseView_01` | 1,588,678 | event_id, venue_id |
| `nafes_licenses` | `dw_ms_crm_onprem_views.nafes_licenses_view_01` | 21,232 | License_ID, Facility_ID |
| `nafes_requests` | `dw_ms_crm_onprem_views.nafes_requests_view_01` | 10,523 | Request_ID, FacilityId |

## OAuth 2.0 Clients

| Client ID | Name | Scopes |
|-----------|------|--------|
| `client_id_1` | MOS Data Room | read, count |
| `client_id_analytics` | Analytics Team | read, count |
| `client_id_partner` | Partner Access | read |

## Pagination Parameters

| Parameter | Type | Default | Max | Description |
|-----------|------|---------|-----|-------------|
| `page_size` | int | 1000 | 10000 | Records per page |
| `page` | int | — | — | Page number (1-based) |
| `page_token` | string | — | — | Base64 offset token |
| `include_count` | bool | true | — | Include total count in response |

## Deployment

```bash
# Deploy from source
gcloud run deploy bq-nsp-api \
  --project=mos-gcp-host-dev-prj \
  --region=me-central2 \
  --source=. \
  --service-account=mos-gcp-dataroom-cloudrun@mos-gcp-host-dev-prj.iam.gserviceaccount.com \
  --allow-unauthenticated \
  --memory=512Mi \
  --cpu=1 \
  --max-instances=10 \
  --timeout=300 \
  --concurrency=80
```

## Testing

```bash
BASE_URL="https://bq-nsp-api-487990962362.me-central2.run.app"

# Get token
TOKEN=$(curl -s -X POST "$BASE_URL/oauth/token" \
  -d "grant_type=client_credentials&client_id=client_id_1&client_secret=secret_key_abc123xyz" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# List tables
curl -s "$BASE_URL/api/tables" -H "Authorization: Bearer $TOKEN"

# Get data
curl -s "$BASE_URL/api/nsp_base/data?page_size=5&page=1" -H "Authorization: Bearer $TOKEN"

# Get count
curl -s "$BASE_URL/api/nsp_base/count" -H "Authorization: Bearer $TOKEN"
```

## Change Log

| Date | Revision | Changes |
|------|----------|---------|
| 2026-01-21 | 00001 | Initial deployment by c.skvasudevan@mos.gov.sa |
| 2026-01-29 | 00034 | Last revision before fixes |
| 2026-08-13 | 00035 | Service account changed to `mos-gcp-dataroom-cloudrun` (was using default compute SA without BQ permissions) |
| 2026-08-13 | 00036 | Fixed webook dataset references: `dw_webook_views` → `dw_webook_01_views` with correct view names |
| 2026-08-15 | — | Fixed broken BQ views: `Events_orders_Attendees_Baseview_01` and `Events_EventDetails_Baseview_01` (missing ROW_NUMBER() dedup + nested field handling) |

## Known Issues / Notes

1. **Secrets hardcoded** — OAuth client_id/secret pairs are in plain text in main.py. Should be moved to Secret Manager.
2. **In-memory token store** — Tokens are lost on cold start. Each new instance has empty token store.
3. **No rate limiting** — No protection against brute force on the token endpoint.
