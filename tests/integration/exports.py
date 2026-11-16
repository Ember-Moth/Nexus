"""Export files in the runner's isolated S3 service (moto), seen as a browser and an operator."""

import json

import boto3
import httpx
from botocore.config import Config

BUCKET = "nexus-app"


def object_store(service_directory):
    data = json.loads((service_directory / "services.json").read_text())
    return boto3.session.Session().client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{data['s3_port']}",
        region_name="us-east-1",
        aws_access_key_id=data["s3_access_key"],
        aws_secret_access_key=data["s3_secret_key"],
        config=Config(s3={"addressing_style": "path"}),
    )


def keys(store, prefix):
    pages = store.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix)
    return sorted(item["Key"] for page in pages for item in page.get("Contents", ()))


async def download(client, path, headers):
    """Ask the API for the download URL, then fetch the file from object storage directly.

    Returns the API's JSON body and the storage response; nothing but the URL is sent there.
    """
    issued = await client.get(path + "/download", headers=headers)
    assert issued.status_code == 200, issued.text
    assert issued.headers["cache-control"] == "no-store"
    body = issued.json()
    async with httpx.AsyncClient(trust_env=False) as browser:
        stored = await browser.get(body["url"])
    assert stored.status_code == 200, stored.text
    return body, stored
