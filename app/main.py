from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db import DbSession
from app.errors import ApiError
from app.routers import queries, runs

app = FastAPI(
    title="Drone Inspection Data Ingestion",
    version="0.1.0",
    description="Ingestion pipeline for drone infrastructure inspection runs.",
)

app.include_router(runs.router)
app.include_router(queries.router)


@app.exception_handler(ApiError)
def handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
    """Single place producing the documented error envelope."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": exc.error_code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.get("/health", tags=["ops"])
def health(db: DbSession) -> dict[str, str]:
    """Liveness plus database reachability.

    One endpoint on purpose: an API that cannot reach Postgres can
    serve nothing useful, so there is no meaningful state where it
    should report itself healthy.
    """
    db.execute(text("SELECT 1"))
    return {"status": "ok"}
