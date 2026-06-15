# Preflight checklist — sem-classifier (prod)

## Before apply

- [ ] Run `make verify-prod SERVICE=sem-classifier` (prod only)
- [ ] Secrets filled (no CHANGE_ME values)
- [ ] Gateway settings have no REPLACE_WITH placeholders (prod)
- [ ] Image available: `ghcr.io/luisfpal/sem-classifier:latest`

## After apply

- [ ] `kubectl get pods -n sem-classifier` — all Ready
- [ ] `curl -fsS http://localhost:8080/__health` (dev port-forward)
- [ ] `./k8s/app.sh --service sem-classifier token` returns JWT (dev)
- [ ] `make test-service SERVICE=sem-classifier` passes (dev)
- [ ] Usage reports: `cd usage-report && ./run.sh --namespace sem-classifier`
