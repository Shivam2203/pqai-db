"""Server

Attributes:
    app (fastapi.applications.FastAPI): FastAPI instance
    PORT (int): Port number
"""

import os
import re
import json
import uvicorn
import botocore
from botocore.exceptions import ClientError
import boto3
import dotenv
from fastapi import FastAPI, Response
from utils import image
from pymongo import MongoClient

dotenv.load_dotenv()

from core.storage import S3Bucket, MongoDB

# Determine storage type
STORAGE_TYPE = os.environ.get("STORAGE", "s3").lower()
print(f"Using storage type: {STORAGE_TYPE}")

# Initialize storage based on type
if STORAGE_TYPE == "mongodb":
    # Initialize MongoDB
    MONGO_HOST = os.environ.get("MONGO_HOST", "localhost")
    MONGO_PORT = int(os.environ.get("MONGO_PORT", 27017))
    MONGO_DB = os.environ.get("MONGO_DB", "pqai")
    MONGO_COLL = os.environ.get("MONGO_COLL", "bibliography")
    
    mongo_client = MongoClient(MONGO_HOST, MONGO_PORT)
    storage = MongoDB(mongo_client, MONGO_DB, MONGO_COLL, "publicationNumber")
    print(f"MongoDB connected: {MONGO_HOST}:{MONGO_PORT}/{MONGO_DB}/{MONGO_COLL}")
    
    # S3 is still needed for drawings (optional)
    AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
    config = botocore.config.Config(
        read_timeout=400, connect_timeout=400, retries={"max_attempts": 0}
    )
    credentials = {
        "aws_access_key_id": AWS_ACCESS_KEY_ID,
        "aws_secret_access_key": AWS_SECRET_ACCESS_KEY,
    }
    botoclient = boto3.client("s3", **credentials, config=config)
    bucket_name = os.environ.get("AWS_S3_BUCKET_NAME")
    s3_storage = S3Bucket(botoclient, bucket_name) if bucket_name else None
else:
    # Initialize S3 only
    AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
    
    config = botocore.config.Config(
        read_timeout=400, connect_timeout=400, retries={"max_attempts": 0}
    )
    credentials = {
        "aws_access_key_id": AWS_ACCESS_KEY_ID,
        "aws_secret_access_key": AWS_SECRET_ACCESS_KEY,
    }
    botoclient = boto3.client("s3", **credentials, config=config)
    bucket_name = os.environ.get("AWS_S3_BUCKET_NAME")
    storage = S3Bucket(botoclient, bucket_name)
    s3_storage = storage
    print(f"S3 bucket configured: {bucket_name}")

app = FastAPI()


def get_drawing_prefix(doc_id):
    """Maps document ids to their drawing path prefixes
    For US Patent No. 7,654,321 the drawings are stored as follows:
        07654321-1.tif
        07654321-2.tif
        ...
        ...
    For US patent applications, say, for US20080156487A1, they're stored as:
        US20080156487A1-1.tif
        US20080156487A1-2.tif
        ...
        ...
    This function creates the appropriate key prefix on the basis of whether
    the supplied number is a patent or an application.
    Args:
        doc_id (str): Document identifier (e.g. patent number)
    Returns:
        str: Drawing prefix
    """
    if len(doc_id) > 12:
        return f"images/{doc_id}-"

    num = re.search(r"\d+", doc_id).group(0)
    while len(num) < 8:
        num = "0" + num
    return f"images/{num}-"


@app.get("/documents/{doc_id}")
@app.get("/patents/{doc_id}")
async def get_doc(doc_id: str):
    """Return a document's data in JSON format
    """
    try:
        if STORAGE_TYPE == "mongodb":
            # For MongoDB, try different patent number formats
            from pymongo import MongoClient
            client = MongoClient('localhost', 27017)
            db = client['pqai']
            coll = db['bibliography']
            
            # Try different query formats
            query = {
                "$or": [
                    {"publicationNumber": doc_id},
                    {"publicationNumber": f"US{doc_id}"},
                    {"publicationNumber": f"US{doc_id}A"},
                    {"number": doc_id},
                    {"publicationNumber": f"US{doc_id.replace('US', '')}"}
                ]
            }
            
            result = coll.find_one(query)
            if result:
                result.pop('_id', None)
                return result
            else:
                return Response(status_code=404)
        else:
            # For S3
            doc = storage.get(f"patents/{doc_id}.json")
            return json.loads(doc)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return Response(status_code=404)
        return Response(status_code=500)
    except Exception as e:
        print(f"Error: {e}")
        return Response(status_code=500)


@app.get("/patents/{doc_id}/drawings")
async def list_drawings(doc_id: str):
    """Return a list of drawings associated with a document, e.g., [1, 2, 3]
    """
    if not s3_storage:
        return Response(status_code=501, content="Drawings not configured")
    prefix = get_drawing_prefix(doc_id)
    keys = s3_storage.ls(prefix)
    if not keys:
        return Response(status_code=404)
    drawings = [re.search(r"-(\d+)", key).group(1) for key in keys]
    return {"drawings": drawings}


@app.get("/patents/{doc_id}/drawings/{drawing_num}")
async def get_drawing(doc_id: str, drawing_num: int):
    """Return image data of a particular drawing
    """
    if not s3_storage:
        return Response(status_code=501, content="Drawings not configured")
    if drawing_num < 1:
        return Response(status_code=404)
    prefix = get_drawing_prefix(doc_id)
    key = f"{prefix}{drawing_num}.tif"
    try:
        tif_data = s3_storage.get(key)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return Response(status_code=404)
        return Response(status_code=500)
    return Response(content=tif_data, media_type="image/tiff")


@app.get('/patents/{doc_id}/thumbnails/{thumbnail_num}')
def get_patent_thumbnail(doc_id: str, thumbnail_num: str, w: int = 100, h: int = 100):
    """Returns image data of a particular thumbnail.
    """
    if not s3_storage:
        return Response(status_code=501, content="Drawings not configured")
    if thumbnail_num < 1:
        return Response(status_code=404)
    prefix = get_drawing_prefix(doc_id)
    key = f"{prefix}{thumbnail_num}.tif"
    try:
        tif_data = s3_storage.get(key)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return Response(status_code=404)
        return Response(status_code=500)
    tif_data_thumbnail = image.get_resized_image(tif_data, w, h)
    return Response(content=tif_data_thumbnail, media_type="image/tiff")


@app.get("/")
async def root():
    """Root endpoint with API information"""
    return {
        "message": "PQAI Database API",
        "storage_type": STORAGE_TYPE,
        "endpoints": [
            "/patents/{doc_id}",
            "/patents/{doc_id}/drawings",
            "/patents/{doc_id}/drawings/{drawing_num}",
            "/patents/{doc_id}/thumbnails/{thumbnail_num}"
        ]
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
