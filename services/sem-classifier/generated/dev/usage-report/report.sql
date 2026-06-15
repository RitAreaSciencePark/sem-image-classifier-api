WITH bounds AS (
    SELECT __START_EXPR__ AS start_at, __END_EXPR__ AS end_at
),
base AS (
    SELECT
        id,
        "timestamp",
        COALESCE(NULLIF(username, ''), '<unknown>') AS username,
        COALESCE(NULLIF(service_name, ''), '<unknown>') AS service_name,
        COALESCE(NULLIF(endpoint_type, ''), '<unknown>') AS endpoint_type,
        status_code,
        url_path
    FROM api_usage, bounds
    WHERE (bounds.start_at IS NULL OR "timestamp" >= bounds.start_at)
      AND "timestamp" <= bounds.end_at
),
endpoint_counts AS (
    SELECT endpoint_type AS name, COUNT(*)::int AS n
    FROM base GROUP BY endpoint_type ORDER BY n DESC, name ASC
),
status_counts AS (
    SELECT COALESCE(status_code::text, '<null>') AS name, COUNT(*)::int AS n
    FROM base GROUP BY status_code ORDER BY name ASC
),
user_counts AS (
    SELECT username AS name, COUNT(*)::int AS n
    FROM base GROUP BY username ORDER BY n DESC, name ASC LIMIT 25
),
path_counts AS (
    SELECT url_path AS name, COUNT(*)::int AS n
    FROM base GROUP BY url_path ORDER BY n DESC, name ASC LIMIT 25
),
time_buckets AS (
    SELECT
        date_trunc(__BUCKET__, "timestamp" AT TIME ZONE __TZ__) AS bucket_at,
        COUNT(*)::int AS n,
        COUNT(*) FILTER (WHERE status_code >= 400)::int AS errors
    FROM base GROUP BY bucket_at ORDER BY bucket_at ASC
),
hour_heatmap AS (
    SELECT
        EXTRACT(ISODOW FROM "timestamp" AT TIME ZONE __TZ__)::int AS dow,
        EXTRACT(HOUR FROM "timestamp" AT TIME ZONE __TZ__)::int AS hour,
        COUNT(*)::int AS n
    FROM base GROUP BY dow, hour ORDER BY dow, hour
),
recent AS (
    SELECT id, "timestamp", username, service_name, endpoint_type, status_code, url_path
    FROM base ORDER BY "timestamp" DESC LIMIT 25
)
SELECT json_build_object(
    'report_meta', json_build_object(
        'report_version', '2',
        'generator', 'usage-report/run.sh',
        'namespace', __META_NAMESPACE__,
        'since', __META_SINCE__,
        'until', __META_UNTIL__,
        'bucket', __BUCKET__,
        'timezone', __TZ__,
        'postgres_pod', __META_POSTGRES_POD__,
        'kube_context', __META_KUBE_CONTEXT__,
        'generated_at_utc', to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
    ),
    'generated_at_utc', to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    'timezone', __TZ__,
    'bucket', __BUCKET__,
    'requested_start_utc', (SELECT CASE WHEN start_at IS NULL THEN NULL ELSE to_char(start_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') END FROM bounds),
    'requested_end_utc', (SELECT to_char(end_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') FROM bounds),
    'coverage_start', (SELECT to_char(MIN("timestamp") AT TIME ZONE __TZ__, 'YYYY-MM-DD HH24:MI:SS') FROM base),
    'coverage_end', (SELECT to_char(MAX("timestamp") AT TIME ZONE __TZ__, 'YYYY-MM-DD HH24:MI:SS') FROM base),
    'total', (SELECT COUNT(*)::int FROM base),
    'unique_users', (SELECT COUNT(DISTINCT username)::int FROM base),
    'error_count', (SELECT COUNT(*) FILTER (WHERE status_code >= 400)::int FROM base),
    'endpoint_counts', COALESCE((SELECT json_agg(json_build_object('name', name, 'n', n) ORDER BY n DESC, name ASC) FROM endpoint_counts), '[]'::json),
    'status_counts', COALESCE((SELECT json_agg(json_build_object('name', name, 'n', n) ORDER BY name ASC) FROM status_counts), '[]'::json),
    'user_counts', COALESCE((SELECT json_agg(json_build_object('name', name, 'n', n) ORDER BY n DESC, name ASC) FROM user_counts), '[]'::json),
    'path_counts', COALESCE((SELECT json_agg(json_build_object('name', name, 'n', n) ORDER BY n DESC, name ASC) FROM path_counts), '[]'::json),
    'time_buckets', COALESCE((SELECT json_agg(json_build_object(
        'bucket', to_char(bucket_at, 'YYYY-MM-DD HH24:MI:SS'), 'n', n, 'errors', errors
    ) ORDER BY bucket_at ASC) FROM time_buckets), '[]'::json),
    'hour_heatmap', COALESCE((SELECT json_agg(json_build_object('dow', dow, 'hour', hour, 'n', n) ORDER BY dow, hour) FROM hour_heatmap), '[]'::json),
    'recent', COALESCE((SELECT json_agg(json_build_object(
        'id', id,
        'timestamp', to_char("timestamp" AT TIME ZONE __TZ__, 'YYYY-MM-DD HH24:MI:SS'),
        'username', username,
        'service_name', service_name,
        'endpoint_type', endpoint_type,
        'status_code', status_code,
        'url_path', url_path
    ) ORDER BY "timestamp" DESC) FROM recent), '[]'::json)
) AS report;
