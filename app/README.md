# Terraform Module repo

A lightweight REST API (FastAPI on Alpine Linux) that accepts a list of Terraform modules and returns ready-to-use `main.tf` and `variables.tf` files.

## How it works

1. Each module source is checked against the **Terraform Public Registry API** — if it matches the `<namespace>/<module>/<provider>` pattern, variable metadata is fetched automatically.
2. For non-registry sources (GitHub, git URLs, local paths) the provider is **inferred from the source string**.
3. Variables from all modules are **merged and deduplicated**; required variables (no upstream default) receive safe placeholder defaults and are annotated with a comment.
4. The `terraform {}` block, provider stubs, module blocks, and `variables.tf` are generated in valid HCL.

---

## Running

```bash
# Docker
docker build -t repo-api .
docker run -p 8080:8085 repo-api

# Compose
docker compose up

# Local dev
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

---

## API

### `GET /health`
Returns `{"status": "ok"}`.



