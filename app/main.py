"""
GitHub File API — fetch files and commit changes to GitHub repos.
"""

import base64
import json
import logging
import os
import yaml
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.responses import Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from ghapi.all import GhApi
from fastcore.net import HTTP4xxClientError
from pydantic import BaseModel, Field

from app.catalog_resolver import CatalogResolver

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── App setup ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="GitHub File API",
    description="Fetch files from GitHub repositories and return them as YAML",
    version="1.0.0",
)

security = HTTPBearer(auto_error=False)

# ── Catalog config ───────────────────────────────────────────────────────────

CATALOG_OWNER = os.getenv("CATALOG_OWNER", "rjones-projects")
CATALOG_REPO  = os.getenv("CATALOG_REPO",  "catalog")
CATALOG_FILE  = os.getenv("CATALOG_FILE",  "catalog.yaml")
CATALOG_REF   = os.getenv("CATALOG_REF",   "HEAD")


# ── Models ───────────────────────────────────────────────────────────────────

class CommitRequest(BaseModel):
    message: str = Field(..., description="Commit message")
    files: dict[str, str] = Field(..., description="Mapping of file path to content")
    folder: str = Field("", description="Source prefix stripped from file paths before committing")
    destination: str = Field("", description="Target folder path in the repo")
    branch: str = Field("main", description="Branch to commit to (created from default branch if absent)")
    private: bool = Field(False, description="Make the repo private when auto-creating it")


class CommitResponse(BaseModel):
    repo: str
    branch: str
    commit_sha: str
    files_committed: list[str]


class CatalogResolveRequest(BaseModel):
    building_blocks: list[str] = Field(
        ...,
        min_length=1,
        description="List of catalog building block names, e.g. ['bucket', 'sql', 'network']",
    )
    terraform_version: Optional[str] = Field("~> 1.9", description="Required Terraform version constraint")
    backend: Optional[str] = Field(None, description="Backend type, e.g. 'gcs', 's3', 'azurerm'")
    modules_ref: Optional[str] = Field("main", description="Git ref used to pin module sources")


class CatalogResolveResponse(BaseModel):
    main_tf: str
    variables_tf: str
    summary: dict


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_github_client(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    token: Optional[str] = Query(None, description="GitHub Personal Access Token"),
) -> GhApi:
    """Resolve GitHub token from Bearer header, ?token= query param, or GITHUB_TOKEN env var."""
    resolved_token = None
    if credentials:
        resolved_token = credentials.credentials
    elif token:
        resolved_token = token
    elif os.getenv("GITHUB_TOKEN"):
        resolved_token = os.getenv("GITHUB_TOKEN")
    return GhApi(token=resolved_token)


def decode_content(content_bytes: bytes, path: str) -> object:
    text = content_bytes.decode("utf-8", errors="replace")
    if path.endswith(".json"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    if path.endswith((".yaml", ".yml")):
        try:
            docs = [d for d in yaml.safe_load_all(text) if d is not None]
            return docs[0] if len(docs) == 1 else docs
        except yaml.YAMLError:
            pass
    return text


def to_yaml_response(data: dict) -> Response:
    yaml_str = yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return Response(content=yaml_str, media_type="text/yaml; charset=utf-8")


def _http_status(exc: HTTP4xxClientError) -> int:
    if hasattr(exc, "response") and exc.response is not None:
        return exc.response.status_code
    return 0


def _github_error(exc: HTTP4xxClientError, default_status: int = 404) -> HTTPException:
    status = _http_status(exc) or default_status
    msg = str(exc)
    try:
        msg = json.loads(msg).get("message", msg)
    except Exception:
        pass
    return HTTPException(status_code=status, detail=msg)


# ── Catalog helper ────────────────────────────────────────────────────────────

def fetch_catalog(gh: GhApi) -> list:
    try:
        fc = gh.repos.get_content(
            owner=CATALOG_OWNER, repo=CATALOG_REPO, path=CATALOG_FILE, ref=CATALOG_REF
        )
    except HTTP4xxClientError as exc:
        raise HTTPException(
            status_code=_http_status(exc) or 404,
            detail=f"Catalog '{CATALOG_OWNER}/{CATALOG_REPO}/{CATALOG_FILE}' not found: {exc}",
        )

    raw_bytes = base64.b64decode(fc.content)
    try:
        documents = [d for d in yaml.safe_load_all(raw_bytes.decode("utf-8", errors="replace")) if d is not None]
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to parse catalog YAML: {exc}")

    if not documents:
        raise HTTPException(status_code=500, detail="Catalog file is empty or contains no valid documents")

    return documents


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    return {"message": "GitHub File API — visit /docs for usage"}


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Catalog endpoints ────────────────────────────────────────────────────────

@app.get(
    "/catalog",
    summary="Return the full catalog as YAML",
    response_class=Response,
    responses={200: {"content": {"text/yaml": {}}}, 404: {"description": "Catalog repo or file not found"}},
)
def get_catalog(gh: GhApi = Depends(get_github_client)):
    documents = fetch_catalog(gh)
    yaml_str = yaml.dump_all(documents, allow_unicode=True, sort_keys=False, default_flow_style=False, explicit_start=True)
    return Response(content=yaml_str, media_type="text/yaml; charset=utf-8")


@app.get(
    "/catalog/{index}",
    summary="Return a single indexed item from the catalog",
    response_class=Response,
    responses={200: {"content": {"text/yaml": {}}}, 400: {"description": "Invalid index"}, 404: {"description": "Index out of range"}},
)
def get_catalog_item(index: str, gh: GhApi = Depends(get_github_client)):
    documents = fetch_catalog(gh)
    try:
        i = int(index)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Index must be an integer, got '{index}'")

    if i < 0 or i >= len(documents):
        raise HTTPException(
            status_code=404,
            detail=f"Index {i} out of range — catalog has {len(documents)} documents (0–{len(documents)-1})",
        )

    item = documents[i]
    return to_yaml_response(item if isinstance(item, (dict, list)) else {"value": item})


# ── Repo read endpoints ──────────────────────────────────────────────────────

@app.get(
    "/repos/{owner}/{repo}/file",
    summary="Fetch a single file as YAML",
    response_class=Response,
    responses={200: {"content": {"text/yaml": {}}}, 404: {"description": "File or repo not found"}},
)
def get_file(
    owner: str,
    repo: str,
    path: str = Query(..., description="Path to the file within the repo"),
    ref: str = Query("HEAD", description="Branch, tag, or commit SHA"),
    raw: bool = Query(False, description="Return raw content without metadata wrapper"),
    gh: GhApi = Depends(get_github_client),
):
    try:
        fc = gh.repos.get_content(owner=owner, repo=repo, path=path, ref=ref)
    except HTTP4xxClientError as exc:
        raise _github_error(exc)

    if isinstance(fc, list):
        raise HTTPException(status_code=400, detail="Path points to a directory — use /tree endpoint instead")

    raw_bytes = base64.b64decode(fc.content)
    parsed = decode_content(raw_bytes, path)

    if raw:
        return to_yaml_response(parsed if isinstance(parsed, dict) else {"content": parsed})

    return to_yaml_response({
        "repo": f"{owner}/{repo}",
        "path": fc.path,
        "branch": ref,
        "sha": fc.sha,
        "size": fc.size,
        "html_url": fc.html_url,
        "content": parsed,
    })


@app.get(
    "/repos/{owner}/{repo}/tree",
    summary="List directory contents as YAML",
    response_class=Response,
    responses={200: {"content": {"text/yaml": {}}}},
)
def get_tree(
    owner: str,
    repo: str,
    path: str = Query("", description="Directory path (empty = repo root)"),
    ref: str = Query("HEAD", description="Branch, tag, or commit SHA"),
    gh: GhApi = Depends(get_github_client),
):
    try:
        contents = gh.repos.get_content(owner=owner, repo=repo, path=path, ref=ref)
    except HTTP4xxClientError as exc:
        raise _github_error(exc)

    if not isinstance(contents, list):
        contents = [contents]

    items = sorted(
        [{"name": c.name, "path": c.path, "type": c.type, "size": c.size, "sha": c.sha, "html_url": c.html_url} for c in contents],
        key=lambda x: (x["type"] != "dir", x["name"]),
    )

    return to_yaml_response({"repo": f"{owner}/{repo}", "path": path or "/", "ref": ref, "count": len(items), "entries": items})


@app.get(
    "/repos/{owner}/{repo}/files",
    summary="Fetch multiple files at once as YAML",
    response_class=Response,
    responses={200: {"content": {"text/yaml": {}}}},
)
def get_multiple_files(
    owner: str,
    repo: str,
    paths: list[str] = Query(..., description="One or more file paths (repeat ?paths= for each)"),
    ref: str = Query("HEAD", description="Branch, tag, or commit SHA"),
    gh: GhApi = Depends(get_github_client),
):
    results = {}
    for file_path in paths:
        try:
            fc = gh.repos.get_content(owner=owner, repo=repo, path=file_path, ref=ref)
            if isinstance(fc, list):
                results[file_path] = {"error": "path is a directory"}
                continue
            raw_bytes = base64.b64decode(fc.content)
            results[file_path] = {"sha": fc.sha, "size": fc.size, "content": decode_content(raw_bytes, file_path)}
        except HTTP4xxClientError as exc:
            results[file_path] = {"error": str(exc)}

    return to_yaml_response({"repo": f"{owner}/{repo}", "ref": ref, "files": results})


@app.get(
    "/repos/{owner}/{repo}/info",
    summary="Repository metadata as YAML",
    response_class=Response,
    responses={200: {"content": {"text/yaml": {}}}},
)
def get_repo_info(owner: str, repo: str, gh: GhApi = Depends(get_github_client)):
    try:
        r = gh.repos.get(owner=owner, repo=repo)
    except HTTP4xxClientError as exc:
        raise _github_error(exc)

    try:
        topics = gh.repos.get_all_topics(owner=owner, repo=repo).names
    except Exception:
        topics = []

    return to_yaml_response({
        "name": r.name,
        "full_name": r.full_name,
        "description": r.description,
        "default_branch": r.default_branch,
        "private": r.private,
        "language": r.language,
        "stars": r.stargazers_count,
        "forks": r.forks_count,
        "open_issues": r.open_issues_count,
        "topics": topics,
        "created_at": str(r.created_at),
        "updated_at": str(r.updated_at),
        "clone_url": r.clone_url,
        "html_url": r.html_url,
    })


# ── Commit endpoint ──────────────────────────────────────────────────────────

def _ensure_repo(gh: GhApi, owner: str, repo: str, private: bool) -> str:
    """Return the default branch of the repo, creating it if it doesn't exist."""
    try:
        return gh.repos.get(owner=owner, repo=repo).default_branch
    except HTTP4xxClientError as exc:
        if _http_status(exc) != 404:
            raise _github_error(exc)

    # Repo doesn't exist — create it under the owner (org or user)
    try:
        me = gh.users.get_authenticated()
        if me.login == owner:
            r = gh.repos.create_for_authenticated_user(name=repo, private=private, auto_init=True)
        else:
            r = gh.repos.create_in_org(org=owner, name=repo, private=private, auto_init=True)
    except HTTP4xxClientError as exc:
        raise _github_error(exc, default_status=422)

    return r.default_branch


@app.post(
    "/repos/{owner}/{repo}/commit",
    response_model=CommitResponse,
    summary="Commit files to a GitHub repository",
    responses={
        200: {"description": "Files committed successfully"},
        422: {"description": "No files provided or repo creation failed"},
    },
)
def commit_files(
    owner: str,
    repo: str,
    request: CommitRequest,
    gh: GhApi = Depends(get_github_client),
):
    """
    Commit one or more files to a GitHub repo in a single commit.
    The repo is created automatically if it does not exist.

    - `folder`: source prefix stripped from each file path key
    - `destination`: target directory in the repo where files are placed
    - `branch`: created from the default branch if it doesn't exist
    """
    if not request.files:
        raise HTTPException(status_code=422, detail="No files provided")

    default_branch = _ensure_repo(gh, owner, repo, request.private)
    branch = request.branch

    # Get (or create) the branch ref
    try:
        ref_obj = gh.git.get_ref(owner=owner, repo=repo, ref=f"heads/{branch}")
        base_sha = ref_obj.object.sha
    except HTTP4xxClientError as exc:
        if _http_status(exc) != 404:
            raise _github_error(exc)
        # Branch absent — fork from default branch
        default_ref = gh.git.get_ref(owner=owner, repo=repo, ref=f"heads/{default_branch}")
        base_sha = default_ref.object.sha
        gh.git.create_ref(owner=owner, repo=repo, ref=f"refs/heads/{branch}", sha=base_sha)

    base_tree_sha = gh.git.get_commit(owner=owner, repo=repo, commit_sha=base_sha).tree.sha

    # Build one blob per file and collect tree entries
    tree_entries = []
    committed_paths = []
    folder_prefix = request.folder.rstrip("/") + "/" if request.folder else ""

    for file_path, content in request.files.items():
        # Strip the source folder prefix if present
        rel = file_path[len(folder_prefix):] if folder_prefix and file_path.startswith(folder_prefix) else file_path
        dest = f"{request.destination.rstrip('/')}/{rel}" if request.destination else rel
        dest = dest.lstrip("/")

        blob = gh.git.create_blob(owner=owner, repo=repo, content=content, encoding="utf-8")
        tree_entries.append({"path": dest, "mode": "100644", "type": "blob", "sha": blob.sha})
        committed_paths.append(dest)

    new_tree = gh.git.create_tree(owner=owner, repo=repo, tree=tree_entries, base_tree=base_tree_sha)
    new_commit = gh.git.create_commit(
        owner=owner, repo=repo,
        message=request.message,
        tree=new_tree.sha,
        parents=[base_sha],
    )
    gh.git.update_ref(owner=owner, repo=repo, ref=f"heads/{branch}", sha=new_commit.sha)

    return CommitResponse(
        repo=f"{owner}/{repo}",
        branch=branch,
        commit_sha=new_commit.sha,
        files_committed=committed_paths,
    )


# ── Catalog resolve endpoint ─────────────────────────────────────────────────

@app.post(
    "/catalog/resolve",
    response_model=CatalogResolveResponse,
    summary="Resolve building blocks into Terraform files",
    responses={502: {"description": "Failed to fetch catalog mapping or module variables from GitHub"}},
)
def resolve_catalog(request: CatalogResolveRequest):
    """
    Accepts building block names, resolves them to GCP Terraform modules,
    and returns ready-to-use main.tf and variables.tf.
    """
    try:
        resolver = CatalogResolver(
            building_blocks=request.building_blocks,
            terraform_version=request.terraform_version or "~> 1.9",
            backend=request.backend,
            modules_ref=request.modules_ref or "main",
        )
        return resolver.resolve()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.exception("Unexpected error during catalog resolution")
        raise HTTPException(status_code=500, detail=str(exc))
