# Documentation index

Public documentation for the **Reusable Async ML API Platform**. These files describe tracked code and `make` targets only.

## Get started

- [Dev environment overview](dev-environment-overview.md) — why Stencil, architecture, ports
- [Dev environment setup](dev-environment-setup.md) — provision cluster, deploy services
- [Dev deployment operations](dev-deployment-operations.md) — redeploy classes, requirements

## Maintain

- [Multi-service architecture](multi-service-architecture.md) — codegen model, repo layout
- [Adding a service](adding-a-service.md) — `make onboard`, namespaces, ports
- [Inference workers](inference-workers.md) — batching, worker tuning
- [Autoscaling](autoscaling.md) — CPU HPA, validation profiles
- [Usage reports](usage-reports.md) — HTML API usage reports for operators
- [Testing and CI](testing-and-ci.md) — `make verify`, E2E, prod bundle gate

## Deploy

- [Production deployment](production-deployment.md) — render, verify, apply order
- [Deployment verification](deployment-verification.md) — pass/fail checklists

## Related

- [tests/README.md](../tests/README.md) — unit vs E2E layout
- [scripts/README.md](../scripts/README.md) — benchmarks, load tests, usage reports
- [scripts/reports/README.md](../scripts/reports/README.md) — usage report operator guide
- [k8s/env/README.md](../k8s/env/README.md) — cluster SSH/tunnel config only
- [Stencil docs](https://gitlab.com/area7/datacenter/codes/stencil/docs/-/tree/main/docs) — virtual datacenter
- [Buckets Explorer](https://github.com/luisfpal/s3bucket-manager-app) — sibling NFFA-DI service

Run `make help` from the repo root for the operator command list.
