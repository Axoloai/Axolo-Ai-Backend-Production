"""FastAPI app. Run locally:  uvicorn app.main:app --reload
Serverless (AWS Lambda):    handler = app.main.handler"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mangum import Mangum

from app.api.queue import close_pool
from app.api.router import router
from app.db import dispose_engine
from app.errors import AppError, fail, ok
from app.redis import close_redis

log = logging.getLogger("axolo.api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await close_pool()
    await close_redis()
    await dispose_engine()


app = FastAPI(title="Axolo Git Agent", version="1.0.0", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(AppError)
async def _app_error(_req: Request, e: AppError):
    return JSONResponse(fail(e.code, e.message), status_code=e.status)


@app.exception_handler(RequestValidationError)
async def _validation_error(_req: Request, e: RequestValidationError):
    first = e.errors()[0] if e.errors() else {}
    where = ".".join(str(x) for x in first.get("loc", []) if x != "body")
    return JSONResponse(fail("bad_request", f"{where}: {first.get('msg', 'invalid')}"), status_code=422)


@app.exception_handler(Exception)
async def _unexpected(_req: Request, e: Exception):
    log.exception("unexpected error")
    return JSONResponse(fail("internal_error", "Something went wrong."), status_code=500)


@app.get("/health")
async def health():
    return ok({"status": "up"})


handler = Mangum(app, lifespan="off")
