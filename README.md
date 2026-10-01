# Repo API

A lightweight FastAPI service for reading files from and committing files to GitHub repositories. Used by IDP

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# set GITHUB_APP_PRIVATE_KEY in .env (or use Secret Manager)
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Interactive docs (local only, set `ENABLE_DOCS=true`): http://localhost:8080/docs

## Authentication

The service authenticates to GitHub as a **GitHub App** (App ID `5146247`,
Client ID `Iv23li6by46X2HFVrY6l`). Per request for `/repos/{owner}/...` it signs a
short-lived JWT with the App's private key, looks up the App's installation on
`{owner}`, and mints an installation access token (cached until ~1 minute before
it expires).

The private key (PEM) is read from the Secret Manager secret
`GH-APP-PRIVATE-KEY` in project `idp-poc-495014` (override with
`SECRET_PROJECT` / `GITHUB_APP_KEY_SECRET`). For local dev you can instead set
`GITHUB_APP_PRIVATE_KEY`.

If the App isn't installed on that owner (or no key is configured), GitHub calls
are made **unauthenticated** (lower rate limits, no private-repo access).

> Credentials are **never** accepted from the client.

App permissions needed: Contents (read & write), Pull requests (write),
Administration (write, to create repos), Secrets (write, to set Actions secrets),
Workflows (write, to commit `.github/workflows`), Metadata (read).

> **Limitation:** installation tokens can't create repos under a *user* account —
> only in organizations. Auto-creating a repo for a user owner returns 422.

> **Local dev:** the Secret Manager client uses Application Default Credentials —
> run `gcloud auth application-default login` first.

## Security

- **Cloud Run IAM:** the service is deployed with `--no-allow-unauthenticated`, so callers
  must send a Google identity token (`Authorization: Bearer $(gcloud auth print-identity-token)`)
  and hold `roles/run.invoker` on the service.
- **Owner allowlist:** `ALLOWED_OWNERS` (semicolon/comma separated) limits which GitHub
  owners `/repos/{owner}/...` may touch; others get 403. Unset means everything is rejected.
- **Docs off by default:** `/docs`, `/redoc` and `/openapi.json` need `ENABLE_DOCS=true`.

## Endpoints

### Read

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/repos/{owner}/{repo}/file` | Fetch a single file as YAML |
| `GET` | `/repos/{owner}/{repo}/files` | Fetch multiple files in one request |
| `GET` | `/repos/{owner}/{repo}/tree` | List directory contents |
| `GET` | `/repos/{owner}/{repo}/info` | Repository metadata |

#### `GET /repos/{owner}/{repo}/file`

Query params:
- `path` — file path within the repo (required)
- `ref` — branch, tag, or commit SHA (default: `HEAD`)
- `raw` — return content only, no metadata wrapper (default: `false`)

```bash
curl "http://localhost:8080/repos/octocat/Hello-World/file?path=README.md"
```

#### `GET /repos/{owner}/{repo}/tree`

Query params:
- `path` — directory path, empty string for root (default: `""`)
- `ref` — branch, tag, or commit SHA (default: `HEAD`)

#### `GET /repos/{owner}/{repo}/files`

Query params:
- `paths` — repeat for each file: `?paths=a.tf&paths=b.tf`
- `ref` — branch, tag, or commit SHA (default: `HEAD`)

### Commit

#### `POST /repos/{owner}/{repo}/commit`

Commits one or more files in a single Git commit.

- **Existing repo:** files are committed straight to `branch` (created from the
  default branch if it doesn't exist).
- **New repo:** the repository is **created automatically** with a `main` branch,
  then the files — plus a `terraform-plan` GitHub Actions workflow — are pushed to a
  `first-commit` branch and a **PR is opened into `main`**. The workflow runs
  `terraform plan` on the PR and posts the result as a comment.

Request body:

```json
{
  "message": "add terraform modules",
  "files": {
    "main.tf": "terraform {\n  ...\n}",
    "variables.tf": "variable \"project_id\" {\n  ...\n}"
  },
  "folder": "src",
  "destination": "infra/modules",
  "branch": "main",
  "private": false
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `message` | string | required | Commit message |
| `files` | object | required | Map of file path → content |
| `folder` | string | `""` | Source prefix stripped from file path keys |
| `destination` | string | `""` | Target folder in the repo |
| `branch` | string | `"main"` | Branch to commit to; forked from default branch if absent |
| `private` | bool | `false` | Repo visibility when auto-creating |

**Path mapping:** a file keyed `"src/main.tf"` with `folder="src"` and `destination="infra"` is committed as `infra/main.tf`.

Response (existing repo):

```json
{
  "repo": "owner/my-repo",
  "branch": "main",
  "commit_sha": "abc123...",
  "files_committed": ["infra/main.tf", "infra/variables.tf"],
  "created_repo": false,
  "pull_request_url": null,
  "workflow_path": null
}
```

Response (newly created repo):

```json
{
  "repo": "owner/IDP-demo-xyz",
  "branch": "first-commit",
  "commit_sha": "abc123...",
  "files_committed": [".github/workflows/terraform-plan.yml", "infra/main.tf"],
  "created_repo": true,
  "pull_request_url": "https://github.com/owner/IDP-demo-xyz/pull/1",
  "workflow_path": ".github/workflows/terraform-plan.yml",
  "modules_secret_set": true,
  "modules_secret_error": null,
  "wif_secrets_set": true,
  "wif_secrets_error": null
}
```

New-repo bootstrap also injects a `providers.tf` (root `google` / `google-beta`
blocks, using `gcp_project`/`gcp_region` from the request) so the generated TF has
a provider configuration, and authenticates the plan to GCP via Workload Identity
Federation. The WIF config is read from per-owner Secret Manager secrets
`{owner}_wif_provider` / `{owner}_wif_service_account` (falling back to the
`WIF_PROVIDER` / `WIF_SERVICE_ACCOUNT` env vars) and injected as repo secrets.

> **WIF prerequisite:** the Workload Identity provider's attribute-condition must
> authorize the generated repos (e.g. allow `rjones-projects/IDP-demo-*`, not just a
> single repo), and the service account needs the roles to plan the target
> resources. If `wif_secrets_set` is `false`, see `wif_secrets_error`.

> The plan-on-PR workflow runs because the branch push and PR are made with the
> App **installation token** (a push using the built-in `GITHUB_TOKEN` would not trigger it).
> The App therefore needs the permissions above, and the repo/org must allow Actions to
> have `pull-requests: write` for the plan comment to post.

> **Terraform state:** the plan workflow uses the **local backend**
> (`terraform init -input=false`) and persists `terraform.tfstate` across runs with
> `actions/cache` — a unique key per run (always saves) plus a `restore-keys` prefix
> (restores the latest prior state). Note: `terraform plan` alone doesn't write
> state; the cached file only becomes meaningful once an `apply` step exists. Cache
> entries are also branch-scoped and evicted after 7 days / 10 GB, so for durable
> shared state prefer a remote backend (GCS) over the local-backend + cache demo setup.

> **Private Terraform modules:** `terraform init` clones module sources from
> github.com. The built-in `GITHUB_TOKEN` can't read *other* private repos, so on
> repo creation an installation token is injected as a `GH_MODULES_TOKEN` Actions secret
> (`modules_secret_set: true`). The workflow's git-auth step uses that secret to
> authenticate module clones (falling back to `github.token` if it's absent).
> Installation tokens expire after 1 hour, so this only covers the first run; later runs fall back to `github.token`.

### Merge

#### `POST /repos/{owner}/{repo}/pulls/{pull_number}/merge`

Merges a pull request. The body is optional:

```json
{ "merge_method": "squash", "commit_title": "Add compute module", "commit_message": "", "sha": "<expected PR head sha>" }
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `merge_method` | `merge` \| `squash` \| `rebase` | `merge` | How to merge |
| `commit_title` | string | – | Merge commit title (merge/squash) |
| `commit_message` | string | – | Merge commit body (merge/squash) |
| `sha` | string | – | Only merge if the PR head still matches this SHA (else 409) |

Response: `{"repo": "owner/repo", "pull_number": 1, "merged": true, "merge_commit_sha": "...", "message": "Pull Request successfully merged"}`.
GitHub's refusals pass through with their status: 405 when not mergeable (failing
required checks, conflicts, branch protection), 409 on a `sha` mismatch. Needs the
App's **Pull requests: write** permission.

## Docker

```bash
docker build -t repo-api .
# Mount ADC so the container can read Secret Manager locally
docker run -p 8080:8080 \
  -e SECRET_PROJECT=idp-poc-495014 \
  -e GOOGLE_APPLICATION_CREDENTIALS=/adc.json \
  -v $HOME/.config/gcloud/application_default_credentials.json:/adc.json:ro \
  repo-api
```

## Environment variables

| Variable | Description |
|----------|-------------|
| `ALLOWED_OWNERS` | GitHub owners the API may act on, e.g. `microservicesolutions;rjones-projects` (required; empty rejects all) |
| `ENABLE_DOCS` | `true` to serve `/docs` and `/openapi.json` (default: off) |
| `SECRET_PROJECT` | GCP project holding the App key / config secrets (default: `idp-poc-495014`) |
| `GITHUB_APP_ID` | GitHub App ID (`5146247`); set by the deploy workflow from the `GH_APP_ID` Actions secret |
| `GITHUB_APP_CLIENT_ID` | GitHub App Client ID (`Iv23li6by46X2HFVrY6l`); set from the `GH_APP_CLIENT_ID` Actions secret |
| `GITHUB_APP_KEY_SECRET` | Secret Manager secret with the App private key (default: `GH-APP-PRIVATE-KEY`) |
| `GITHUB_APP_PRIVATE_KEY` | Local-dev fallback PEM when the secret is unavailable |

#added github variables for 
CATALOG_OWNER=rjones-projects
CATALOG_REPO=repo-api

# Create a service account
gcloud iam service-accounts create github-actions  --project=vf-gned-ngdi-alpha-ing

# Grant required roles
gcloud projects add-iam-policy-binding vf-gned-ngdi-alpha-ing --member="serviceAccount:github-actions@vf-gned-ngdi-alpha-ing.iam.gserviceaccount.com" --role="roles/artifactregistry.writer"
gcloud projects add-iam-policy-binding vf-gned-ngdi-alpha-ing --member="serviceAccount:github-actions@vf-gned-ngdi-alpha-ing.iam.gserviceaccount.com" --role="roles/run.developer"
#add IAM permissions
gcloud iam service-accounts add-iam-policy-binding  479677124022-compute@developer.gserviceaccount.com --project=vf-gned-ngdi-alpha-ing  --role="roles/iam.serviceAccountUser"  --member="serviceAccount:github-actions@vf-gned-ngdi-alpha-ing.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding vf-gned-ngdi-alpha-ing --member="serviceAccount:github-actions@vf-gned-ngdi-alpha-ing.iam.gserviceaccount.com" --role="roles/run.admin"

# Create WIF pool + provider (swap in your GitHub org/repo)
gcloud iam workload-identity-pools create github-pool --project=idp-poc-495014 --location=global
gcloud iam workload-identity-pools providers update-oidc github-provider --project=idp-poc-495014 --location=global --workload-identity-pool=github-pool --issuer-uri="https://token.actions.githubusercontent.com"  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" --attribute-condition="assertion.repository=='rjones-projects/repo-api'"

# Allow the pool to impersonate the SA
gcloud iam service-accounts add-iam-policy-binding github-actions@idp-poc-495014.iam.gserviceaccount.com --project=idp-poc-495014 --role="roles/iam.workloadIdentityUser" --member="principalSet://iam.googleapis.com/projects/$(gcloud projects describe idp-poc-495014 --format='value(projectNumber)')/locations/global/workloadIdentityPools/github-pool/attribute.repository/rjones-projects/repo-api"

#check the policy binding
gcloud iam service-accounts get-iam-policy github-actions@idp-poc-495014.iam.gserviceaccount.com 

#create secrets
 Settings → Secrets and variables → Actions → New repository secret

#get the secret - WIF_PROVIDER - added to repo secrets - could be vars - todo: fix pipeline
gcloud iam workload-identity-pools providers describe github-provider --project=idp-poc-495014 --location=global --workload-identity-pool=github-pool --format="value(name)"

#secret - WIF_SERVICE_ACCOUNT
github-actions@idp-poc-495014.iam.gserviceaccount.com

#added github variables for 
CATALOG_OWNER=rjones-projects
CATALOG_REPO=catalog
CATALOG_FILE=catalog.yaml

# ── Per-owner GitHub PATs via Secret Manager ──────────────────────────────────
# At request time the service reads the secret "<owner>_token" from idp-poc-495014,
# where <owner> is the {owner} in /repos/{owner}/... . Create one secret per GitHub
# owner/org you want authenticated access to (example owner: octocat).

# Create the secret and add the PAT as the first version (reads from stdin)
gcloud secrets create octocat_token --project=idp-poc-495014 --replication-policy=automatic
printf '%s' 'ghp_yourTokenHere' | gcloud secrets versions add octocat_token --project=idp-poc-495014 --data-file=-

# Grant the Cloud Run runtime service account read access. Project-level grant lets
# it read every <owner>_token without re-binding for each new owner:
gcloud projects add-iam-policy-binding idp-poc-495014 --role="roles/secretmanager.secretAccessor" --member="serviceAccount:$(gcloud projects describe idp-poc-495014 --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

# (Tighter alternative — grant per secret instead of project-wide:)
# gcloud secrets add-iam-policy-binding octocat_token --project=idp-poc-495014 --role="roles/secretmanager.secretAccessor" --member="serviceAccount:$(gcloud projects describe idp-poc-495014 --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

# If a prior deploy left GH_TOKEN as a literal env var on the service, clear it once
gcloud run services update repo-api --project=idp-poc-495014 --region=europe-west2 --remove-env-vars=GH_TOKEN

# Rotate a token by adding a new version (picked up within TOKEN_CACHE_TTL, default 5m)
printf '%s' 'ghp_newTokenHere' | gcloud secrets versions add octocat_token --project=idp-poc-495014 --data-file=-



docker build -t repo-api .
docker run -p 8085:8080 repo-api 

docker tag repo-api europe-west2-docker.pkg.dev/idp-poc-495014/repo-api/repo-api:latest

docker push europe-west2-docker.pkg.dev/idp-poc-495014/repo-api/repo-api:latest

docker tag repo-api europe-west2-docker.pkg.dev/vf-gned-ngdi-alpha-ing/repo-api/repo-api:latest

docker push europe-west2-docker.pkg.dev/vf-gned-ngdi-alpha-ing/repo-api/repo-api:latest

