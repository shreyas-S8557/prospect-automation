from __future__ import annotations

import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import pipeline_bridge  # noqa: F401 -- sets up sys.path + loads .env
from app.api import accounts, analytics, campaigns, emails, health, jobs, pipeline_actions, prospects, settings
from app.config import CORS_ORIGINS
from app.db import database
from app.utils.logging import configure_logging

configure_logging()
logger = logging.getLogger("prospect_automation.backend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_app_tables()
    logger.info("Backend started. Database: %s", database.DB_PATH)
    yield


app = FastAPI(
    title="Prospect Automation API",
    version="1.0.0",
    description=(
        "Orchestration/API layer over the existing prospect-automation pipeline. "
        "See /docs for the interactive schema."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Never leak a Python stack trace to the frontend (PHASE 29). Full
    # detail goes to the server log only, redacted of anything
    # secret-shaped first.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. See server logs for details."},
    )


app.include_router(health.router)
app.include_router(campaigns.router)
app.include_router(pipeline_actions.router)
app.include_router(prospects.router)
app.include_router(emails.router)
app.include_router(jobs.router)
app.include_router(analytics.router)
app.include_router(accounts.router)
app.include_router(settings.router)


@app.get("/")
def root() -> dict:
    return {"name": "Prospect Automation API", "docs": "/docs", "health": "/api/health"}
