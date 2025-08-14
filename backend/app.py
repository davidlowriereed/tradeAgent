# backend/app.py (sketch)
from contextlib import asynccontextmanager
from fastapi import FastAPI
from .scheduler import agents_loop
from .services.market import market_loop
from .db import connect_async, ensure_schema

@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_async(); ensure_schema()
    t1 = asyncio.create_task(market_loop(), name="market_loop")
    t2 = asyncio.create_task(agents_loop(), name="agents_loop")
    try:
        yield
    finally:
        for t in (t1, t2):
            t.cancel(); 
            with contextlib.suppress(asyncio.CancelledError): await t

def create_app():
    app = FastAPI(lifespan=lifespan)
    # include routers, routes...
    return app

app = create_app()
