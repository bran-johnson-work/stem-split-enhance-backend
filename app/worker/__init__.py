"""Stem-separation worker: the compute process that drains the Postgres job queue.

Runs as a separate process (``python -m app.worker``) on a CPU or GPU host, using
Postgres purely as coordination: it claims ``processing_jobs`` rows, downloads inputs
from S3, runs ``app.engine``, uploads stems back to S3, and records results.

This package (and only this package) imports ``app.engine`` — hence the heavy
torch/demucs dependencies live on the worker image, not the API image.
"""
