# GSlide API Kubernetes manifests

FastAPI + Chromium Deployment + ClusterIP Service, plus the shared
`/app_data` PVC and secret template. **No Ingress, no HPA, no SearXNG.**

Apply this repo **before** gslide-web: web mounts PVC `gslide-app-data`
read-only and expects Service `api:8000`.

## Files

| File | Purpose |
| --- | --- |
| `00-namespace.yaml` | `gslide` namespace (optional — drop if platform-managed). |
| `01-configmap.yaml` | Non-sensitive API URLs and feature switches (`gslide-api-config`). |
| `02-secret.example.yaml` | **Template only.** Bind real credentials via external secrets. |
| `03-pvc.yaml` | `10Gi` `/app_data` PVC (ReadWriteOnce). |
| `20-api.yaml` | FastAPI + Chromium Deployment + ClusterIP Service. |

## Apply

```bash
kubectl apply -f 00-namespace.yaml
kubectl apply -f 01-configmap.yaml
# create gslide-secrets via your external-secrets solution, NOT from the template
kubectl apply -f 03-pvc.yaml
kubectl apply -f 20-api.yaml
```

Replace `ghcr.io/<org>/gslide-api:latest` in `20-api.yaml` with the image
this GitLab repo builds.

Chromium renders exports at `EXPORT_PAGE_BASE_URL=http://web:3000`. The web
Service must exist before export works; generation without export can start
as soon as this API is ready.

## Resources (4 concurrent users, starting point)

| Workload | Replicas | CPU request | CPU limit | Memory request | Memory limit |
| --- | ---: | ---: | ---: | ---: | ---: |
| FastAPI + export runtime | 1 | `1000m` | `2000m` | `2Gi` | `4Gi` |

Keep **one** API replica while SQLite + RWO `/app_data` is in use (`Recreate`
strategy). SearXNG / automatic web image search is out of scope for this
deploy (`WEB_IMAGE_SEARCH_ENABLED=false`, no `SEARXNG_BASE_URL`).

Probes use `/api/v1/auth/status`. Startup up to 120 s. Liveness is
conservative so a healthy pod is not restarted mid slow LLM call.

The current image runs as root (Chromium + `/root/.cache`).
`allowPrivilegeEscalation: false` and `capabilities.drop: [ALL]` are set.

## What is intentionally NOT here

- No Ingress resource (DevOps owns the shared Ingress).
- No SearXNG.
- No HPA, no PodDisruptionBudget.
- No populated Secret.
