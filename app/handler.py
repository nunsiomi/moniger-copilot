"""AWS Lambda entry point.

Wraps the FastAPI app with Mangum so the same app code runs unchanged on
Lambda (behind API Gateway / Lambda Function URL) and on Railway/uvicorn —
see railway.json for the non-Lambda deployment path.
"""
from mangum import Mangum

from app.main import app

handler = Mangum(app)
