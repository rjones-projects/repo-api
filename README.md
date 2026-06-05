# Repo API

A lightweight FastAPI service for reading files from and committing files to GitHub repositories. Used by IDP

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # add your GITHUB_TOKEN
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Interactive docs: http://localhost:8080/docs

## Authentication

Pass a GitHub Personal Access Token (PAT) via:

- `Authorization: Bearer <token>` header
- `?token=<token>` query parameter
- `GITHUB_TOKEN` environment variable (fallback)

Scopes needed: `repo` for private repos, `public_repo` for public only.

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

Commits one or more files in a single Git commit. **Creates the repository automatically** if it does not exist.

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

Response:

```json
{
  "repo": "owner/my-repo",
  "branch": "main",
  "commit_sha": "abc123...",
  "files_committed": ["infra/main.tf", "infra/variables.tf"]
}
```

## Docker

```bash
docker build -t repo-api .
docker run -p 8080:8080 -e GITHUB_TOKEN=<token> repo-api
```

## Environment variables

| Variable | Description |
|----------|-------------|
| `GITHUB_TOKEN` | GitHub Personal Access Token (used as fallback when no token is passed per-request) |

#added github variables for 
CATALOG_OWNER=rjones-projects
CATALOG_REPO=repo-api



docker build -t repo-api .
docker run -p 8080:8080 repo-api 

docker tag repo-api europe-west2-docker.pkg.dev/idp-poc-495014/repo-api/repo-api:latest

docker push europe-west2-docker.pkg.dev/idp-poc-495014/repo-api/repo-api:latest

docker tag repo-api europe-west2-docker.pkg.dev/vf-gned-ngdi-alpha-ing/repo-api/repo-api:latest

docker push europe-west2-docker.pkg.dev/vf-gned-ngdi-alpha-ing/repo-api/repo-api:latest

