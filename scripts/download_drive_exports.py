#!/usr/bin/env python3
"""Stream completed AEF GeoTIFF exports from Google Drive with resume support."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import ee.oauth
import requests
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path.home() / ".config" / "earthengine" / "credentials",
    )
    parser.add_argument("--chunk-mib", type=int, default=32)
    parser.add_argument("--read-timeout", type=int, default=600)
    parser.add_argument("--max-retries", type=int, default=12)
    return parser.parse_args()


def drive_session(credentials_path: Path) -> AuthorizedSession:
    payload = json.loads(credentials_path.read_text(encoding="utf-8"))
    scopes = payload["scopes"]
    if isinstance(scopes, str):
        scopes = scopes.split()
    credentials = Credentials(
        None,
        refresh_token=payload["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=ee.oauth.CLIENT_ID,
        client_secret=ee.oauth.CLIENT_SECRET,
        scopes=scopes,
    )
    credentials.refresh(Request())
    return AuthorizedSession(credentials)


def matching_files(session: AuthorizedSession, prefix: str) -> list[dict]:
    params = {
        "q": f"name contains '{prefix}' and trashed=false",
        "fields": "nextPageToken,files(id,name,size,mimeType,modifiedTime)",
        "pageSize": 1000,
    }
    files: list[dict] = []
    while True:
        response = session.get(
            "https://www.googleapis.com/drive/v3/files", params=params, timeout=60
        )
        response.raise_for_status()
        payload = response.json()
        files.extend(
            item for item in payload.get("files", []) if item["name"].startswith(prefix)
        )
        token = payload.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return sorted(files, key=lambda item: item["name"])


def download_one(
    session: AuthorizedSession,
    metadata: dict,
    output_dir: Path,
    chunk_bytes: int,
    read_timeout: int,
    max_retries: int,
) -> dict:
    target = output_dir / metadata["name"]
    partial = target.with_name(target.name + ".part")
    expected = int(metadata["size"])
    if target.exists() and target.stat().st_size == expected:
        return {"name": target.name, "bytes": expected, "status": "already_complete"}
    if target.exists():
        raise ValueError(f"existing file has unexpected size: {target}")

    retries = 0
    while True:
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > expected:
            raise ValueError(f"partial file exceeds Drive size: {partial}")
        if offset == expected:
            written = offset
            break
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            response = session.get(
                f"https://www.googleapis.com/drive/v3/files/{metadata['id']}",
                params={"alt": "media"},
                headers=headers,
                stream=True,
                timeout=(30, read_timeout),
            )
            response.raise_for_status()
            # A server may ignore Range and return 200. Restart in that case
            # rather than appending a second full copy to the partial file.
            append = offset > 0 and response.status_code == 206
            mode = "ab" if append else "wb"
            if not append:
                offset = 0
            written = offset
            with partial.open(mode) as stream:
                for chunk in response.iter_content(chunk_size=chunk_bytes):
                    if not chunk:
                        continue
                    stream.write(chunk)
                    stream.flush()
                    written += len(chunk)
                    print(
                        f"[DOWNLOAD] {target.name} {written / 2**30:.2f}/"
                        f"{expected / 2**30:.2f} GiB",
                        flush=True,
                    )
            if written == expected:
                break
            raise IOError(f"short response for {target}: {written} != {expected}")
        except (requests.RequestException, IOError) as exc:
            retries += 1
            if retries > max_retries:
                raise
            delay = min(30, 2**min(retries, 5))
            print(
                f"[RETRY {retries}/{max_retries}] {target.name}: "
                f"{type(exc).__name__}; resume in {delay}s",
                flush=True,
            )
            time.sleep(delay)
    if written != expected:
        raise IOError(f"download size mismatch for {target}: {written} != {expected}")
    partial.replace(target)
    return {"name": target.name, "bytes": expected, "status": "downloaded"}


def main() -> None:
    args = parse_args()
    if args.chunk_mib <= 0:
        raise ValueError("chunk-mib must be positive")
    if args.read_timeout <= 0 or args.max_retries < 0:
        raise ValueError("read-timeout must be positive and max-retries non-negative")
    args.out.mkdir(parents=True, exist_ok=True)
    session = drive_session(args.credentials)
    files = matching_files(session, args.prefix)
    if not files:
        raise FileNotFoundError(f"no Drive files start with {args.prefix!r}")
    results = [
        download_one(
            session,
            item,
            args.out,
            args.chunk_mib * 2**20,
            args.read_timeout,
            args.max_retries,
        )
        for item in files
    ]
    report = {"prefix": args.prefix, "output": str(args.out.resolve()), "files": results}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
