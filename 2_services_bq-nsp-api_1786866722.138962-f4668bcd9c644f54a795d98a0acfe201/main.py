from flask import Flask, jsonify, request
from google.cloud import bigquery
import os
import base64
import math
from functools import wraps
import secrets
import hashlib
import time

app = Flask(__name__)
_client = None

# OAuth 2.0 Client Credentials Store
# In production, store these in Secret Manager
OAUTH_CLIENTS = {
    'client_id_1': {
        'client_secret': 'secret_key_abc123xyz',
        'name': 'MOS Data Room',
        'scopes': ['read', 'count']
    },
    'client_id_analytics': {
        'client_secret': 'analytics_secret_456def',
        'name': 'Analytics Team',
        'scopes': ['read', 'count']
    },
    'client_id_partner': {
        'client_secret': 'partner_secret_789ghi',
        'name': 'Partner Access',
        'scopes': ['read']
    }
}

# Token store (in-memory, use Redis in production)
ACCESS_TOKENS = {}

def generate_access_token(client_id, expires_in=3600):
    """Generate a new access token"""
    token = secrets.token_urlsafe(512)
    expiry = time.time() + expires_in
    
    ACCESS_TOKENS[token] = {
        'client_id': client_id,
        'expires_at': expiry,
        'scopes': OAUTH_CLIENTS[client_id]['scopes']
    }
    
    return token, expiry

def verify_oauth_token(required_scope=None):
    """Decorator to verify OAuth 2.0 access token"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            auth_header = request.headers.get('Authorization', '')
            
            if not auth_header.startswith('Bearer '):
                return jsonify({
                    'error': 'invalid_request',
                    'error_description': 'Missing or invalid Authorization header'
                }), 401
            
            token = auth_header.split('Bearer ')[1]
            
            # Check if token exists and is valid
            if token not in ACCESS_TOKENS:
                return jsonify({
                    'error': 'invalid_token',
                    'error_description': 'Token not found or invalid'
                }), 401
            
            token_data = ACCESS_TOKENS[token]
            
            # Check if token is expired
            if time.time() > token_data['expires_at']:
                del ACCESS_TOKENS[token]
                return jsonify({
                    'error': 'invalid_token',
                    'error_description': 'Token has expired'
                }), 401
            
            # Check scope if required
            if required_scope and required_scope not in token_data['scopes']:
                return jsonify({
                    'error': 'insufficient_scope',
                    'error_description': f'Token does not have required scope: {required_scope}'
                }), 403
            
            # Log access
            client_id = token_data['client_id']
            print(f"API accessed by client: {OAUTH_CLIENTS[client_id]['name']}")
            
            request.client_id = client_id
            request.client_name = OAUTH_CLIENTS[client_id]['name']
            
            return f(*args, **kwargs)
        
        return decorated_function
    return decorator

def get_bq_client():
    global _client
    if _client is None:
        _client = bigquery.Client()
    return _client

# OAuth 2.0 Token Endpoint
@app.route('/oauth/token', methods=['POST'])
def oauth_token():
    """OAuth 2.0 Token Endpoint - Client Credentials Grant"""
    
    # Get credentials from request
    grant_type = request.form.get('grant_type')
    client_id = request.form.get('client_id')
    client_secret = request.form.get('client_secret')
    
    # Also check Basic Auth header
    if not client_id or not client_secret:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Basic '):
            import base64
            try:
                decoded = base64.b64decode(auth_header.split('Basic ')[1]).decode('utf-8')
                client_id, client_secret = decoded.split(':', 1)
            except:
                pass
    
    # Validate grant_type
    if grant_type != 'client_credentials':
        return jsonify({
            'error': 'unsupported_grant_type',
            'error_description': 'Only client_credentials grant type is supported'
        }), 400
    
    # Validate client credentials
    if not client_id or not client_secret:
        return jsonify({
            'error': 'invalid_request',
            'error_description': 'client_id and client_secret are required'
        }), 400
    
    if client_id not in OAUTH_CLIENTS:
        return jsonify({
            'error': 'invalid_client',
            'error_description': 'Invalid client_id'
        }), 401
    
    if OAUTH_CLIENTS[client_id]['client_secret'] != client_secret:
        return jsonify({
            'error': 'invalid_client',
            'error_description': 'Invalid client_secret'
        }), 401
    
    # Generate access token
    expires_in = 3600  # 1 hour
    access_token, expiry = generate_access_token(client_id, expires_in)
    
    return jsonify({
        'access_token': access_token,
        'token_type': 'Bearer',
        'expires_in': expires_in,
        'scope': ' '.join(OAUTH_CLIENTS[client_id]['scopes'])
    }), 200

# Define all your tables
TABLES = {
    'nsp_base': {
        'name': '`mos-gcp-host-dev-prj.dw_nsp_api_views.nsp_base_view_01`',
        'filters': ['contactid', 'region', 'status_ar', 'individual_type_ar'],
        'order_by': 'contactid'
    },
    'fr_base': {
        'name': '`mos-gcp-host-dev-prj.dw_ms_crm_onprem_views.fr_base_view_01`',
        'filters': ['booking_id', 'contact_name'],
        'order_by': 'booking_id'
    },
    'appdynamics_base': {
        'name': '`mos-gcp-host-dev-prj.dw_appdynamics_views.appdynamics_base_view_01`',
        'filters': ['Severity', 'Application', 'Service_Name', 'EventType'],
        'order_by': 'Event_Time'
    },
    'webook_orders': {
        'name': '`mos-gcp-host-dev-prj.dw_webook_01_views.Events_orders_Attendees_Baseview_01`',
        'filters': ['event_id', 'order_number'],
        'order_by': 'event_id'
    },
    'webook_event_details': {
        'name': '`mos-gcp-host-dev-prj.dw_webook_01_views.Events_EventDetails_Baseview_01`',
        'filters': ['event_id', 'venue_id'],
        'order_by': 'event_id'
    },
    'webook_Attendees_details': {
        'name': '`mos-gcp-host-dev-prj.dw_webook_01_views.webook_Event_Attendees_BaseView_01`',
        'filters': ['event_id', 'venue_id'],
        'order_by': 'event_id'
    },
    'nafes_licenses': {
        'name': '`mos-gcp-host-dev-prj.dw_ms_crm_onprem_views.nafes_licenses_view_01`',
        'filters': ['License_ID', 'Facility_ID'],
        'order_by': 'License_ID'
    },
    'nafes_requests': {
        'name': '`mos-gcp-host-dev-prj.dw_ms_crm_onprem_views.nafes_requests_view_01`',
        'filters': ['Request_ID', 'FacilityId'],
        'order_by': 'Request_ID'
    }
}

@app.route('/')
@app.route('/health')
def health():
    return jsonify({'status': 'healthy', 'service': 'bq-nsp-api'}), 200

@app.route('/api/tables', methods=['GET'])
@verify_oauth_token(required_scope='read')
def list_tables():
    return jsonify({
        'status': 'success',
        'tables': list(TABLES.keys()),
        'client': request.client_name
    }), 200

@app.route('/api/<table_key>/data', methods=['GET'])
@verify_oauth_token(required_scope='read')
def get_table_data(table_key):
    """Generic endpoint to fetch data from any table"""
    
    if table_key not in TABLES:
        return jsonify({
            'status': 'error',
            'message': f'Table "{table_key}" not found',
            'available_tables': list(TABLES.keys())
        }), 404
    
    try:
        client = get_bq_client()
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
    table_config = TABLES[table_key]
    table_name = table_config['name']
    allowed_filters = table_config['filters']
    order_by = table_config.get('order_by', 'RAND()')
    
    page_size = request.args.get('page_size', 1000, type=int)
    page_token = request.args.get('page_token', None)
    page_number = request.args.get('page', None, type=int)
    include_count = request.args.get('include_count', 'true').lower() == 'true'
    
    if page_size > 10000:
        page_size = 10000
    
    where_conditions = []
    query_params = []
    
    for filter_field in allowed_filters:
        param_value = request.args.get(filter_field) or request.args.get(filter_field.replace('_', ''))
        
        if param_value:
            where_conditions.append(f"{filter_field} = @{filter_field}")
            query_params.append(bigquery.ScalarQueryParameter(filter_field, "STRING", param_value))
    
    where_clause = " AND ".join(where_conditions) if where_conditions else "1=1"
    
    offset = 0
    if page_number is not None:
        offset = (page_number - 1) * page_size
        if offset < 0:
            offset = 0
    elif page_token:
        try:
            offset = int(base64.b64decode(page_token).decode('utf-8'))
        except:
            offset = 0
    
    total_records = None
    total_pages = None
    
    if include_count:
        count_query = f"SELECT COUNT(*) as total FROM {table_name} WHERE {where_clause}"
        count_job_config = bigquery.QueryJobConfig(query_parameters=query_params)
        
        try:
            count_job = client.query(count_query, job_config=count_job_config)
            count_result = list(count_job.result())[0]
            total_records = count_result['total']
            total_pages = math.ceil(total_records / page_size)
        except Exception as e:
            print(f"Count error: {e}")
    
    data_query = f"SELECT * FROM {table_name} WHERE {where_clause} ORDER BY {order_by} LIMIT @page_size OFFSET @offset"
    
    data_query_params = query_params + [
        bigquery.ScalarQueryParameter("page_size", "INT64", page_size),
        bigquery.ScalarQueryParameter("offset", "INT64", offset)
    ]
    
    data_job_config = bigquery.QueryJobConfig(query_parameters=data_query_params)
    
    try:
        query_job = client.query(data_query, job_config=data_job_config)
        results = query_job.result()
        rows = [dict(row) for row in results]
        
        current_page = (offset // page_size) + 1
        records_in_page = len(rows)
        
        next_page_token = None
        prev_page_token = None
        
        if len(rows) == page_size:
            next_offset = offset + page_size
            next_page_token = base64.b64encode(str(next_offset).encode('utf-8')).decode('utf-8')
        
        if offset > 0:
            prev_offset = max(0, offset - page_size)
            prev_page_token = base64.b64encode(str(prev_offset).encode('utf-8')).decode('utf-8')
        
        is_last_page = len(rows) < page_size
        
        response = {
            'status': 'success',
            'table': table_key,
            'data': rows,
            'pagination': {
                'page_size': page_size,
                'records_in_current_page': records_in_page,
                'current_page': current_page,
                'is_first_page': offset == 0,
                'is_last_page': is_last_page
            }
        }
        
        if total_records is not None:
            response['pagination']['total_records'] = total_records
            response['pagination']['total_pages'] = total_pages
            response['pagination']['remaining_records'] = max(0, total_records - offset - records_in_page)
        
        return jsonify(response), 200
        
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/<table_key>/count', methods=['GET'])
@verify_oauth_token(required_scope='count')
def get_table_count(table_key):
    """Get total count for any table"""
    
    if table_key not in TABLES:
        return jsonify({
            'status': 'error',
            'message': f'Table "{table_key}" not found',
            'available_tables': list(TABLES.keys())
        }), 404
    
    try:
        client = get_bq_client()
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
    table_config = TABLES[table_key]
    table_name = table_config['name']
    allowed_filters = table_config['filters']
    
    where_conditions = []
    query_params = []
    filters_applied = {}
    
    for filter_field in allowed_filters:
        param_value = request.args.get(filter_field) or request.args.get(filter_field.replace('_', ''))
        
        if param_value:
            where_conditions.append(f"{filter_field} = @{filter_field}")
            query_params.append(bigquery.ScalarQueryParameter(filter_field, "STRING", param_value))
            filters_applied[filter_field] = param_value
    
    where_clause = " AND ".join(where_conditions) if where_conditions else "1=1"
    
    query = f"SELECT COUNT(*) as total FROM {table_name} WHERE {where_clause}"
    job_config = bigquery.QueryJobConfig(query_parameters=query_params)
    
    try:
        query_job = client.query(query, job_config=job_config)
        result = list(query_job.result())[0]
        
        return jsonify({
            'status': 'success',
            'table': table_key,
            'total_records': result['total'],
            'filters_applied': filters_applied
        }), 200
        
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/data', methods=['GET'])
@verify_oauth_token(required_scope='read')
def api_data():
    return get_table_data('nsp_base')

@app.route('/api/count', methods=['GET'])
@verify_oauth_token(required_scope='count')
def api_count():
    return get_table_count('nsp_base')

def query_bigquery(req):
    with app.request_context(req.environ):
        return app.full_dispatch_request()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)