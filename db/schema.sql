-- API Usage Tracking Schema
-- Records one row per authenticated API request through KrakenD.
-- No token counting — this is request-level tracking for non-LLM services.

CREATE TABLE IF NOT EXISTS api_usage (
    id            SERIAL PRIMARY KEY,
    timestamp     TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    username      VARCHAR(250) NOT NULL,
    service_name  VARCHAR(255) DEFAULT 'sem-image-classifier',
    endpoint_type VARCHAR(50)  NOT NULL,
    status_code   INTEGER,
    url_path      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_api_usage_timestamp ON api_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_api_usage_username  ON api_usage(username);
