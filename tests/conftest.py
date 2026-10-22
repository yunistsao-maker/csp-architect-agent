import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(1, str(Path(__file__).resolve().parent))  # tests can import helpers from each other (ScriptedModel)

import os  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture()
def aws(monkeypatch):
    """moto-backed S3 + DynamoDB with the tables / buckets the portal agents use."""
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": "us-east-1",
                 "AWS_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    from moto import mock_aws
    import boto3
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        for b in ("uploads-bucket", "jobs-bucket"):
            s3.create_bucket(Bucket=b)
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="sessions", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}])
        yield {"s3": s3, "table": table, "ddb": ddb}


os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
