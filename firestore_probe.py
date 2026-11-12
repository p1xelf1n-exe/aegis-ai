import asyncio
import logging
import os
import re
import time
from datetime import datetime

import httpx

PROJECT = "database-3-9462a"
CHANNELS = ("global", "aegis-ai")
ENDPOINT = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents:runQuery"
LIMIT = 50
log = logging.getLogger("aegis.firestore")


def describe_error(response):
    status = "UNKNOWN"
    message = "No structured Firestore error available."
    shape = "non-json"
    try:
        payload = response.json()
        shape = type(payload).__name__
        envelopes = payload if isinstance(payload, list) else [payload]
        error = next((item["error"] for item in envelopes
                      if isinstance(item, dict) and isinstance(item.get("error"), dict)), {})
        candidate = str(error.get("status", "UNKNOWN"))
        if re.fullmatch(r"[A-Z_]{1,64}", candidate):
            status = candidate
        if isinstance(error.get("message"), str):
            message = error["message"][:8000]
    except (ValueError, TypeError):
        pass
    for name in ("GROQ_API_KEY", "SITE_USERNAME", "SITE_PASSWORD", "FIREBASE_ID_TOKEN", "FIREBASE_REFRESH_TOKEN"):
        value = os.getenv(name, "")
        if value:
            message = message.replace(value, "[REDACTED]")
    message = re.sub(r"https?://\S+", "[URL OMITTED]", message)
    message = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", message, flags=re.I)
    message = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[TOKEN REDACTED]", message)
    message = " ".join(message.split())[:1200]
    content_type = re.sub(r"[^a-zA-Z0-9/+.;= -]", "", response.headers.get("content-type", "unknown"))[:100]
    return f"HTTP {response.status_code}; status={status}; shape={shape}; content-type={content_type}; message={message}"


def query_body(channel):
    if channel not in CHANNELS:
        raise ValueError("Channel is outside the allowlist")
    return {"structuredQuery": {
        "select": {"fields": [{"fieldPath": "channelId"}, {"fieldPath": "timestamp"}]},
        "from": [{"collectionId": "messages"}],
        "where": {"fieldFilter": {"field": {"fieldPath": "channelId"},
                                  "op": "EQUAL", "value": {"stringValue": channel}}},
        "orderBy": [{"field": {"fieldPath": "timestamp"}, "direction": "DESCENDING"}],
        "limit": LIMIT,
    }}


async def read_channel(client, channel):
    response = await client.post(ENDPOINT, json=query_body(channel))
    if response.status_code != 200:
        raise RuntimeError(
            f"Firestore read failed for channel={channel}: {describe_error(response)}. "
            "No writes attempted. Do not weaken database rules."
        )
    rows = response.json()
    if not isinstance(rows, list):
        raise RuntimeError("Unexpected Firestore query response")
    documents = []
    read_times = []
    prefix = f"projects/{PROJECT}/databases/(default)/documents/messages/"
    for row in rows:
        if "error" in row:
            raise RuntimeError("Firestore returned a query error; details not logged")
        if row.get("readTime"):
            read_times.append(datetime.fromisoformat(row["readTime"].replace("Z", "+00:00")))
        doc = row.get("document")
        if not doc:
            continue
        fields = doc.get("fields", {})
        if not doc.get("name", "").startswith(prefix):
            raise RuntimeError("Document outside approved database and collection")
        if fields.get("channelId", {}).get("stringValue") != channel:
            raise RuntimeError("Document outside approved channel")
        timestamp = fields.get("timestamp", {}).get("timestampValue")
        if not timestamp:
            raise RuntimeError("Message timestamp is missing or not a Firestore timestamp")
        documents.append((doc["name"], datetime.fromisoformat(timestamp.replace("Z", "+00:00"))))
    if not read_times:
        raise RuntimeError("No server read time returned; cannot safely establish baseline")
    return documents, max(read_times)


async def main():
    if os.getenv("SEND_ENABLED", "false").lower() != "false":
        raise RuntimeError("This diagnostic is read-only; SEND_ENABLED must be false")
    duration = int(os.getenv("SESSION_SECONDS", "300"))
    interval = int(os.getenv("POLL_SECONDS", "60"))
    if not 1 <= duration <= 600 or interval < 30:
        raise RuntimeError("Diagnostic duration must be 1-600s and polling at least 30s")
    log.info("Read-only Firestore diagnostic: Server 3; global and aegis-ai only. No Groq or message writes.")
    seen = {channel: set() for channel in CHANNELS}
    cutoffs = {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        for channel in CHANNELS:
            docs, cutoff = await read_channel(client, channel)
            seen[channel].update(name for name, _ in docs)
            cutoffs[channel] = cutoff
            log.info("Baseline established: channel=%s; recent documents=%s", channel, len(docs))
        log.info("Firestore baseline complete. Monitoring counts only; message contents are not fetched.")
        deadline = time.monotonic() + duration
        while time.monotonic() + interval < deadline:
            await asyncio.sleep(interval)
            for channel in CHANNELS:
                docs, _ = await read_channel(client, channel)
                fresh = [name for name, stamp in docs if name not in seen[channel] and stamp > cutoffs[channel]]
                seen[channel].update(name for name, _ in docs)
                log.info("Read-only poll: channel=%s; new documents=%s", channel, len(fresh))
                if len(docs) == LIMIT:
                    log.info("Recent-message window full: counts may miss bursts; this is not a production cursor.")
    log.info("Read-only diagnostic complete. No messages sent.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except Exception as exc:
        log.error("Diagnostic stopped (%s). No messages sent.", type(exc).__name__)
        if isinstance(exc, (RuntimeError, ValueError)):
            log.error("%s", exc)
        raise SystemExit(1)
