# Deploying to Production

## Generator-first workflow (current)

Production does **not** use `k8s/app.sh`. Each service ships a pre-rendered bundle from the same templates as dev.

### On the build machine

```bash
cp services/sem-classifier/prod.overlay.yaml.example services/sem-classifier/prod.overlay.yaml
# Fill ~6 values: jwk_url, issuer, ingress.host, optional namespace/image digest

make render-prod SERVICE=sem-classifier
make verify-prod SERVICE=sem-classifier    # MUST pass
make prod-pack SERVICE=sem-classifier        # services/sem-classifier/generated/prod-bundle.tar.gz
```

### On the prod cluster (manual, ordered)

Follow `services/sem-classifier/generated/prod/apply-order.txt` and `preflight-checklist.md`.

```bash
export NS=sem-classifier
kubectl apply -f /secure/sem-classifier/secrets.local.yaml
kubectl apply -n "$NS" -f bentoml-config.yaml
# ... remaining steps from apply-order.txt
```

Secrets and filled overlay live **outside the repository**. See [multi-service-architecture.md](multi-service-architecture.md) and [deployment-verification.md](deployment-verification.md).
