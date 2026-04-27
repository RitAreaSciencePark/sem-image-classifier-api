// KrakenD HTTP server plugin for request-level API usage tracking.

package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	_ "github.com/lib/pq"
)

var pluginName = "krakend-server-example"

// HandlerRegisterer is the symbol loaded by KrakenD.
var HandlerRegisterer = registerer(pluginName)

// Shared DB connection initialized at plugin startup.
var db *sql.DB
var usageServiceName = "unknown-service"

// responseRecorder captures downstream status/body while preserving passthrough behavior.
type responseRecorder struct {
	header      http.Header
	body        bytes.Buffer
	status      int
	wroteHeader bool
}

func (r *responseRecorder) Header() http.Header {
	if r.header == nil {
		r.header = make(http.Header)
	}
	return r.header
}

func (r *responseRecorder) WriteHeader(statusCode int) {
	if !r.wroteHeader {
		r.status = statusCode
		r.wroteHeader = true
	}
}

func (r *responseRecorder) Write(b []byte) (int, error) {
	return r.body.Write(b)
}

type registerer string

func (r registerer) RegisterHandlers(f func(
	name string,
	handler func(context.Context, map[string]interface{}, http.Handler) (http.Handler, error),
)) {
	f(string(r), r.registerHandlers)
}

func (r registerer) registerHandlers(_ context.Context, extra map[string]interface{}, h http.Handler) (http.Handler, error) {
	config, ok := extra[pluginName].(map[string]interface{})
	if !ok {
		return h, errors.New("configuration not found")
	}

	// Extract tracked paths.
	var paths []string
	if pArr, ok := config["paths"].([]interface{}); ok {
		for _, v := range pArr {
			if s, ok := v.(string); ok {
				paths = append(paths, s)
			}
		}
	}
	logger.Debug(fmt.Sprintf("[PLUGIN] Intercepting paths: %v", paths))

	if serviceName, ok := config["service_name"].(string); ok && serviceName != "" {
		usageServiceName = serviceName
	}

	// Initialize database connection.
	if dbConfig, ok := config["database"].(map[string]interface{}); ok {
		host, _ := dbConfig["host"].(string)
		port, _ := dbConfig["port"].(string)
		user, _ := dbConfig["user"].(string)
		password, _ := dbConfig["password"].(string)
		dbname, _ := dbConfig["dbname"].(string)
		if password == "" {
			password = os.Getenv("KRAKEND_DB_PASSWORD")
		}

		if host != "" && user != "" && dbname != "" {
			if err := initDatabase(host, port, user, password, dbname); err != nil {
				logger.Error("[PLUGIN] DB init failed:", err)
			} else {
				logger.Info("[PLUGIN] Database connected")
			}
		}
	}

	return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		// Intercept configured API paths only.
		if !matchPath(paths, req.URL.Path) {
			h.ServeHTTP(w, req)
			return
		}

		// Extract username from JWT claims.
		token := extractBearerToken(req)
		username := decodeJWTUsername(token)

		// Proxy to backend via recorder.
		rec := &responseRecorder{}
		h.ServeHTTP(rec, req)

		// Default status when backend doesn't explicitly write one.
		if !rec.wroteHeader {
			rec.status = http.StatusOK
		}

		// Forward response headers/body to client.
		for k, vv := range rec.header {
			for _, v := range vv {
				w.Header().Add(k, v)
			}
		}
		w.WriteHeader(rec.status)
		w.Write(rec.body.Bytes())

		// Record usage asynchronously to avoid blocking request path.
		if username != "" {
			statusCode := rec.status
			urlPath := req.URL.Path
			go recordUsage(username, urlPath, statusCode)
		}
	}), nil
}

// matchPath supports exact and suffix matches.
func matchPath(paths []string, reqPath string) bool {
	for _, p := range paths {
		if p == reqPath || strings.HasSuffix(reqPath, p) {
			return true
		}
	}
	return false
}

// extractBearerToken pulls bearer token from Authorization header.
func extractBearerToken(req *http.Request) string {
	auth := req.Header.Get("Authorization")
	const prefix = "Bearer "
	if len(auth) > len(prefix) && auth[:len(prefix)] == prefix {
		return auth[len(prefix):]
	}
	return ""
}

// decodeJWTUsername reads preferred_username/name/sub from JWT payload.
// Signature verification is handled upstream by KrakenD auth validator.
func decodeJWTUsername(token string) string {
	if token == "" {
		return ""
	}

	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return ""
	}

	// Normalize URL-safe base64 for decoding.
	payload := strings.ReplaceAll(parts[1], "_", "/")
	payload = strings.ReplaceAll(payload, "-", "+")
	for len(payload)%4 != 0 {
		payload += "="
	}

	decoded, err := base64.StdEncoding.DecodeString(payload)
	if err != nil {
		return ""
	}

	var claims struct {
		PreferredUsername string `json:"preferred_username"`
		Name             string `json:"name"`
		Sub              string `json:"sub"`
	}
	if err := json.Unmarshal(decoded, &claims); err != nil {
		return ""
	}

	if claims.PreferredUsername != "" {
		return claims.PreferredUsername
	}
	if claims.Name != "" {
		return claims.Name
	}
	return claims.Sub
}

// inferenceEndpoint maps request path to endpoint type label.
func inferenceEndpoint(urlPath string) string {
	switch {
	case strings.HasSuffix(urlPath, "/inference"):
		return "inference"
	case strings.HasSuffix(urlPath, "/status"):
		return "job_status"
	case strings.HasSuffix(urlPath, "/results"):
		return "job_results"
	default:
		return "other"
	}
}

// recordUsage inserts one usage row.
func recordUsage(username, urlPath string, statusCode int) {
	if db == nil {
		logger.Error("[PLUGIN] DB not initialized, skipping usage record")
		return
	}

	endpointType := inferenceEndpoint(urlPath)

	_, err := db.Exec(
		`INSERT INTO api_usage (timestamp, username, service_name, endpoint_type, status_code, url_path)
		 VALUES ($1, $2, $3, $4, $5, $6)`,
		time.Now(), username, usageServiceName, endpointType, statusCode, urlPath,
	)
	if err != nil {
		logger.Error("[PLUGIN] Failed to insert usage:", err)
	}
}

// initDatabase opens the PostgreSQL connection and validates connectivity.
func initDatabase(host, port, user, password, dbname string) error {
	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		return fmt.Errorf("sql.Open: %v", err)
	}

	if err = db.Ping(); err != nil {
		return fmt.Errorf("db.Ping: %v", err)
	}

	// Small pool is sufficient for async write load.
	db.SetMaxOpenConns(5)
	db.SetMaxIdleConns(2)
	db.SetConnMaxLifetime(5 * time.Minute)

	return nil
}

func main() {}

// Logger — replaced at runtime by KrakenD's logger via RegisterLogger.
var logger Logger = noopLogger{}

func (registerer) RegisterLogger(v interface{}) {
	l, ok := v.(Logger)
	if !ok {
		return
	}
	logger = l
	logger.Debug(fmt.Sprintf("[PLUGIN: %s] Logger loaded", HandlerRegisterer))
}

type Logger interface {
	Debug(v ...interface{})
	Info(v ...interface{})
	Warning(v ...interface{})
	Error(v ...interface{})
	Critical(v ...interface{})
	Fatal(v ...interface{})
}

type noopLogger struct{}

func (n noopLogger) Debug(_ ...interface{})    {}
func (n noopLogger) Info(_ ...interface{})     {}
func (n noopLogger) Warning(_ ...interface{})  {}
func (n noopLogger) Error(_ ...interface{})    {}
func (n noopLogger) Critical(_ ...interface{}) {}
func (n noopLogger) Fatal(_ ...interface{})    {}
