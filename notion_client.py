import json
import os
import urllib.error
import urllib.request


NOTION_BASE = "https://api.notion.com/v1"


class NotionError(RuntimeError):
    pass


def load_env_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


class NotionClient:
    def __init__(self, token=None, notion_version=None):
        load_env_file()
        self.token = token or os.environ["NOTION_TOKEN"]
        self.notion_version = notion_version or os.getenv("NOTION_VERSION", "2022-06-28")

    def request(self, method, path, payload=None):
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        request = urllib.request.Request(
            url=f"{NOTION_BASE}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": self.notion_version,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise NotionError(f"Notion API {exc.code} for {method} {path}: {body}") from exc

    def create_database(self, parent_page_id, title, properties):
        return self.request(
            "POST",
            "/databases",
            {
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "title": [{"type": "text", "text": {"content": title}}],
                "properties": properties,
            },
        )

    def update_database(self, database_id, properties):
        try:
            return self.request("PATCH", f"/databases/{database_id}", {"properties": properties})
        except NotionError as database_error:
            if "object_not_found" not in str(database_error) and "Invalid request URL" not in str(database_error):
                raise
            try:
                return self.request("PATCH", f"/data_sources/{database_id}", {"properties": properties})
            except NotionError as data_source_error:
                raise NotionError(
                    f"{database_error}\nFallback data source update failed: {data_source_error}"
                ) from data_source_error

    def query_database(self, database_id, payload=None):
        results = []
        request_payload = dict(payload or {})
        request_payload.setdefault("page_size", 100)
        while True:
            response = self.query_collection_page(database_id, request_payload)
            results.extend(response.get("results", []))
            if not response.get("has_more"):
                return results
            request_payload["start_cursor"] = response["next_cursor"]

    def query_collection_page(self, collection_id, payload):
        query_path = os.getenv("NOTION_QUERY_PATH", "auto")
        if query_path == "data_sources":
            return self.request("POST", f"/data_sources/{collection_id}/query", payload)
        if query_path == "databases":
            return self.request("POST", f"/databases/{collection_id}/query", payload)

        try:
            return self.request("POST", f"/databases/{collection_id}/query", payload)
        except NotionError as database_error:
            if "object_not_found" not in str(database_error) and "Invalid request URL" not in str(database_error):
                raise
            try:
                return self.request("POST", f"/data_sources/{collection_id}/query", payload)
            except NotionError:
                raise database_error

    def update_page_properties(self, page_id, properties):
        return self.request("PATCH", f"/pages/{page_id}", {"properties": properties})
