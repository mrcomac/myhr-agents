import os

AWS_ACCESS_KEY = os.environ["AWS_ACCESS_KEY_ID"]
AWS_SECRET_KEY = os.environ["AWS_SECRET_ACCESS_KEY"]
AWS_REGION     = os.environ.get("AWS_REGION", "us-east-1")
