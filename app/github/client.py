import base64
from typing import Any

import httpx

from app.config import get_settings


class GitHubError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class GitHubClient:
    def __init__(self, token: str | None = None) -> None:
        settings = get_settings()
        self._token = token or settings.github_token
        self._base = "https://api.github.com"

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self._token:
            raise GitHubError("AUTH_MISSING", "GITHUB_TOKEN is not configured")
        url = f"{self._base}{path}"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.request(method, url, headers=self._headers(), **kwargs)
        if r.status_code == 401:
            raise GitHubError("AUTH_FAILED", "Invalid GitHub token")
        if r.status_code >= 400:
            raise GitHubError("API_ERROR", f"GitHub {r.status_code}: {r.text[:300]}")
        if r.status_code == 204:
            return None
        return r.json()

    async def list_repos(self, per_page: int = 30) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/user/repos?per_page={per_page}&sort=updated")
        return data if isinstance(data, list) else []

    async def get_file(self, owner: str, repo: str, path: str, ref: str = "main") -> str:
        try:
            data = await self._request("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={ref}")
        except GitHubError:
            data = await self._request("GET", f"/repos/{owner}/{repo}/contents/{path}?ref=master")
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            raise GitHubError("INVALID_FILE", f"Cannot read {path}")
        raw = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return raw

    async def get_default_branch(self, owner: str, repo: str) -> str:
        data = await self._request("GET", f"/repos/{owner}/{repo}")
        return data.get("default_branch", "main")

    async def list_directory(
        self,
        owner: str,
        repo: str,
        path: str = "",
        ref: str | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """One directory level via Contents API (for file explorer)."""
        branch = ref or await self.get_default_branch(owner, repo)
        path_clean = path.strip().strip("/")
        if path_clean:
            endpoint = f"/repos/{owner}/{repo}/contents/{path_clean}?ref={branch}"
        else:
            endpoint = f"/repos/{owner}/{repo}/contents?ref={branch}"
        data = await self._request("GET", endpoint)
        if isinstance(data, dict):
            entry_type = "dir" if data.get("type") == "dir" else "file"
            return branch, [
                {
                    "name": data.get("name", ""),
                    "path": data.get("path", path_clean),
                    "type": entry_type,
                    "size": data.get("size", 0),
                }
            ]
        entries: list[dict[str, Any]] = []
        for item in data if isinstance(data, list) else []:
            entry_type = "dir" if item.get("type") == "dir" else "file"
            entries.append(
                {
                    "name": item.get("name", ""),
                    "path": item.get("path", ""),
                    "type": entry_type,
                    "size": item.get("size", 0),
                }
            )
        entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, (e.get("name") or "").lower()))
        return branch, entries

    async def create_branch(self, owner: str, repo: str, branch: str, from_sha: str) -> None:
        await self._request(
            "POST",
            f"/repos/{owner}/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": from_sha},
        )

    async def get_branch_sha(self, owner: str, repo: str, branch: str) -> str:
        data = await self._request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
        return data["object"]["sha"]

    async def upsert_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        await self._request("PUT", f"/repos/{owner}/{repo}/contents/{path}", json=body)

    async def file_sha(self, owner: str, repo: str, path: str, branch: str) -> str | None:
        try:
            data = await self._request("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={branch}")
            return data.get("sha") if isinstance(data, dict) else None
        except GitHubError:
            return None

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
