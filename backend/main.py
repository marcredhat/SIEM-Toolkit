from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from db import engine, Base
from routers import coverage, ingest, settings, quality

Base.metadata.create_all(bind=engine)

# Runtime migration: add columns that didn't exist in earlier schema versions
from sqlalchemy import text
with engine.connect() as _conn:
    _conn.execute(text(
        "ALTER TABLE active_sources ADD COLUMN IF NOT EXISTS parser_detected INTEGER DEFAULT 0"
    ))
    _conn.commit()

app = FastAPI(title="SIEM Toolkit", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(coverage.router, prefix="/api/coverage", tags=["Coverage"])
app.include_router(ingest.router,   prefix="/api/ingest",   tags=["Ingest"])
app.include_router(settings.router, prefix="/api/settings", tags=["Settings"])
app.include_router(quality.router,  prefix="/api/quality",  tags=["Quality"])


@app.get("/health")
def health():
    return {"status": "ok"}
