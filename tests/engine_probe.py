"""
Fresh-process probe for restart and recovery tests.

Each invocation starts a brand new Python process and therefore a brand
new engine instance. That is what makes it a real restart test: nothing
is inherited from a previous action except the contents of the data
directory.

Usage:
    python tests/engine_probe.py <data_dir> <command> [args...]

Commands:
    upload   <path> [<path>...]   upload files from explicit paths
    corpus   <dir>                upload every file in a directory
    status                        print /api/status
    search   <query> [page] [limit]
    delete   <name>
    bulk-delete <name> [<name>...]
    rebuild                       trigger a rebuild
    list                          print indexed document names

The command result is printed as a single JSON object on the last line
of stdout so the caller can parse it without being confused by engine
log output.
"""

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _settle(client, expected_documents=None, timeout=60.0):
    """Wait for a settled READY index."""

    deadline = time.time() + timeout
    last = None

    while time.time() < deadline:
        last = client.get("/api/status").get_json()

        if last["indexing"]["state"] in {"READY", "ERROR"}:
            if expected_documents is None:
                return last

            if last["documents"] == expected_documents:
                return last

        time.sleep(0.05)

    return last


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2

    data_dir = argv[1]
    command = argv[2]
    arguments = argv[3:]

    os.environ["SEARCH_ENGINE_DATA_DIR"] = data_dir

    sys.path.insert(0, str(REPO_ROOT))

    import app as engine

    engine.app.config["TESTING"] = True
    client = engine.app.test_client()

    if command in {"upload", "corpus"}:
        paths = []

        if command == "corpus":
            for name in sorted(os.listdir(arguments[0])):
                paths.append(os.path.join(arguments[0], name))
        else:
            paths = arguments

        uploaded = []
        failed = []

        for path in paths:
            with open(path, "rb") as handle:
                response = client.post(
                    "/api/upload",
                    data={
                        "file": (
                            handle,
                            os.path.basename(path),
                            "application/octet-stream",
                        )
                    },
                )

            payload = response.get_json()

            if payload.get("failed_count"):
                failed.extend(payload["failed"])
            else:
                uploaded.extend(payload.get("uploaded", []))

        result = _settle(client)
        result = dict(result)
        result["uploaded"] = uploaded
        result["failed"] = failed

    elif command == "status":
        result = client.get("/api/status").get_json()

    elif command == "search":
        query = arguments[0]
        params = {"q": query}

        if len(arguments) > 1:
            params["page"] = arguments[1]

        if len(arguments) > 2:
            params["limit"] = arguments[2]

        response = client.get("/api/search", query_string=params)
        payload = response.get_json()

        if isinstance(payload, list):
            result = {
                "shape": "list",
                "titles": [item["title"] for item in payload],
                "scores": [item["score"] for item in payload],
            }
        else:
            result = {
                "shape": "object",
                "titles": [item["title"] for item in payload["results"]],
                "scores": [item["score"] for item in payload["results"]],
                "pagination": payload["pagination"],
            }

    elif command == "delete":
        response = client.delete(f"/api/documents/{arguments[0]}")
        result = dict(response.get_json() or {})
        result["status_code"] = response.status_code
        _settle(client)

    elif command == "bulk-delete":
        response = client.post(
            "/api/documents/bulk-delete",
            json={"filenames": list(arguments)},
        )
        result = dict(response.get_json() or {})
        result["status_code"] = response.status_code
        _settle(client)

    elif command == "rebuild":
        response = client.post("/api/rebuild")
        result = response.get_json()
        result["settled"] = _settle(client)

    elif command == "list":
        result = {
            "documents": sorted(
                engine.DOCUMENT_METADATA.keys()
            ),
        }

    else:
        print(f"unknown command: {command}")
        return 2

    print(json.dumps(result, sort_keys=True, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
