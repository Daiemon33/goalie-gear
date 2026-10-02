"""Where gear photos are kept.

Two interchangeable places, picked by the S3_BUCKET environment variable:

- Not set: a folder on disk (uploads/). Simple, and right for running on
  your own computer.
- Set: a private S3 bucket. A container's disk is wiped whenever it is
  replaced, so in the cloud photos must live outside the container.

Both offer the same three actions (save, delete, send to the browser), so
app.py doesn't care which one it is talking to.
"""

import os

from flask import Response, abort, send_from_directory


class LocalPhotos:
    """Photos as files in a folder."""

    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)

    def save(self, filename, data):
        with open(os.path.join(self.folder, filename), "wb") as f:
            f.write(data)

    def delete(self, filename):
        path = os.path.join(self.folder, filename)
        if os.path.exists(path):
            os.remove(path)

    def send(self, filename):
        # send_from_directory refuses paths that try to escape the folder.
        return send_from_directory(self.folder, filename)


class S3Photos:
    """Photos as objects in a private S3 bucket.

    The bucket is never public. The browser asks the app for a photo and the
    app fetches it from S3 with its own permissions, so only the app's IAM
    role (or the local MinIO login) can read the bucket.

    boto3 finds credentials by itself: the ECS task role on AWS, or the
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY variables locally. Locally,
    AWS_ENDPOINT_URL_S3 points it at MinIO instead of real S3.
    """

    PREFIX = "photos/"

    def __init__(self, bucket):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.s3 = boto3.client(
            "s3",
            config=Config(
                retries={"mode": "standard"},  # retry brief network hiccups
                # Bucket name in the path (http://host/bucket/key), not the host name.
                # Works with both AWS and MinIO.
                s3={"addressing_style": "path"},
            ),
        )

    def save(self, filename, data):
        self.s3.put_object(
            Bucket=self.bucket,
            Key=self.PREFIX + filename,
            Body=data,
            ContentType="image/jpeg",
        )

    def delete(self, filename):
        # Deleting something that is already gone is not an error in S3.
        self.s3.delete_object(Bucket=self.bucket, Key=self.PREFIX + filename)

    def send(self, filename):
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self.PREFIX + filename)
        except self.s3.exceptions.NoSuchKey:
            abort(404)
        return Response(
            obj["Body"].iter_chunks(),  # stream it, don't load it all into memory
            mimetype="image/jpeg",
            # Photos never change (a new photo gets a new name), so browsers may keep them.
            headers={"Cache-Control": "private, max-age=86400"},
        )


def make_photo_store(upload_dir):
    bucket = os.environ.get("S3_BUCKET", "")
    if bucket:
        return S3Photos(bucket)
    return LocalPhotos(upload_dir)
