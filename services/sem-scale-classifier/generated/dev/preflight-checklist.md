# Preflight checklist — sem-scale-classifier (dev)

## Before apply

- [ ] Run `make verify-prod SERVICE=sem-scale-classifier` (prod only)
- [ ] Secrets filled (no CHANGE_ME values)
- [ ] Gateway settings have no REPLACE_WITH placeholders (prod)
- [ ] Image available: `ghcr.io/luisfpal/sem-scale-classifier:latest`

## After apply

- [ ] `kubectl get pods -n sem-scale-classifier` — all Ready
- [ ] `curl -fsS http://localhost:8082/__health` (dev port-forward)
- [ ] `./k8s/app.sh --service sem-scale-classifier token` returns JWT (dev)
- [ ] `make test-service SERVICE=sem-scale-classifier` passes (dev)
- [ ] Usage reports: `cd usage-report && ./run.sh --namespace sem-scale-classifier`
