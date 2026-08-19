"""FastAPI application layer over the existing prospect-automation pipeline.

This package is deliberately a thin orchestration/API layer: all discovery,
qualification, email-finding, validation, generation, and sending logic
lives in ../scripts/pipeline (untouched). See app/pipeline_bridge.py for how
that package is imported.
"""
