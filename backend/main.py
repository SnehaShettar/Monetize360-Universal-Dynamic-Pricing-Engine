from engine.validator import validate_config
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from engine.pricing import compute_price

app = FastAPI(
    title="Monetize360 Pricing Engine",
    version="0.1.0",
    description="Configuration-driven universal pricing API",
)


class PriceRequest(BaseModel):
    config: dict
    product_id: str
    context: dict = Field(default_factory=dict)


@app.get("/health")
def health():
    return {"status": "ok", "service": "Monetize360"}


@app.get("/api/v1/domains")
def domains():
    return {
        "domains": ["hotel", "ride", "ecommerce", "banking"]
    }


@app.post("/api/v1/price")
def price(request: PriceRequest):
    try:
        result = compute_price(
            request.config,
            request.product_id,
            request.context,
        )
        return result
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/api/v1/simulate")
def simulate(request: PriceRequest):
    return price(request)


@app.post("/api/v1/configs/validate")
def validate_configuration(config: dict):
    result = validate_config(config)
    return result
