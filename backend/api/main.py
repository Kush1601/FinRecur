from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.db import db_is_reachable
from api.routes.clusters import router as clusters_router
from api.routes.decisions import router as decisions_router
from api.routes.exceptions import router as exceptions_router
from api.routes.fixes import router as fixes_router
from api.routes.ledger import router as ledger_router
from api.routes.policy import router as policy_router
from api.routes.recurrence import router as recurrence_router
from api.routes.runs import router as runs_router
from api.settings import settings

app = FastAPI(title="FinRecur API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.CORS_ORIGINS.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(runs_router)
app.include_router(exceptions_router)
app.include_router(decisions_router)
app.include_router(clusters_router)
app.include_router(fixes_router)
app.include_router(ledger_router)
app.include_router(policy_router)
app.include_router(recurrence_router)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "db": db_is_reachable()}
