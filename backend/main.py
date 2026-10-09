import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from engine.pricing import compute_price
from engine.validator import validate_config


app = FastAPI(
    title="Monetize360 Pricing Engine",
    version="0.3.0",
    description="Configuration-driven universal pricing API",
)

DB_PATH = Path(__file__).resolve().parent.parent / "monetize360.db"


# -------------------- Database --------------------

def get_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database():
    with get_connection() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS configurations (
                config_id TEXT PRIMARY KEY,
                active_version INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS config_versions (
                config_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                config_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (config_id, version),
                FOREIGN KEY (config_id)
                    REFERENCES configurations(config_id)
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_id TEXT NOT NULL,
                action TEXT NOT NULL,
                version INTEGER,
                details TEXT,
                created_at TEXT NOT NULL
            );
        """)


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def record_audit(connection, config_id, action, version=None, details=None):
    connection.execute(
        """
        INSERT INTO audit_log
            (config_id, action, version, details, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            config_id,
            action,
            version,
            json.dumps(details or {}),
            now_utc(),
        ),
    )


def get_config_row(connection, config_id, version=None):
    if version is None:
        row = connection.execute(
            """
            SELECT v.version, v.config_json, v.created_at
            FROM configurations c
            JOIN config_versions v ON v.config_id = c.config_id
            WHERE c.config_id = ? AND v.version = c.active_version
            """,
            (config_id,),
        ).fetchone()
    else:
        row = connection.execute(
            """
            SELECT version, config_json, created_at
            FROM config_versions
            WHERE config_id = ? AND version = ?
            """,
            (config_id, version),
        ).fetchone()

    return row


initialize_database()


# -------------------- Request models --------------------

class PriceRequest(BaseModel):
    config: dict
    product_id: str
    context: dict = Field(default_factory=dict)


class SimulationRequest(BaseModel):
    config: dict
    product_id: str
    context: dict = Field(default_factory=dict)
    scenarios: list[dict] = Field(default_factory=list)


class ConfigSaveRequest(BaseModel):
    config_id: str = Field(min_length=1, max_length=100)
    config: dict


class ConfigVersionRequest(BaseModel):
    config: dict


class PublishRequest(BaseModel):
    version: int = Field(ge=1)


class RollbackRequest(BaseModel):
    version: int = Field(ge=1)


# -------------------- Existing endpoints --------------------

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
        return compute_price(
            request.config,
            request.product_id,
            request.context,
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/api/v1/simulate")
def simulate(request: SimulationRequest):
    scenarios = request.scenarios or [
        {"name": "default", "context": request.context}
    ]

    results = []

    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise HTTPException(
                status_code=422,
                detail=f"Scenario {index} must be an object",
            )

        name = scenario.get("name", f"scenario_{index + 1}")
        context = scenario.get("context", {})

        if not isinstance(context, dict):
            raise HTTPException(
                status_code=422,
                detail=f"Context for scenario '{name}' must be an object",
            )

        try:
            result = compute_price(
                request.config,
                request.product_id,
                context,
            )
            results.append({
                "name": name,
                "context": context,
                **result,
            })
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Scenario '{name}' failed: {exc}",
            )

    prices = [item["price"] for item in results]

    return {
        "product_id": request.product_id,
        "scenario_count": len(results),
        "results": results,
        "comparison": {
            "minimum_price": min(prices),
            "maximum_price": max(prices),
            "price_difference": round(max(prices) - min(prices), 2),
        },
    }


@app.post("/api/v1/configs/validate")
def validate_configuration(config: dict):
    return validate_config(config)


# -------------------- Configuration management --------------------

@app.post("/api/v1/configs")
def create_configuration(request: ConfigSaveRequest):
    config_id = request.config_id.strip()

    if not config_id:
        raise HTTPException(status_code=422, detail="config_id is required")

    validation = validate_config(request.config)

    if not validation["valid"]:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Configuration is invalid",
                "errors": validation["errors"],
            },
        )

    timestamp = now_utc()

    try:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO configurations
                    (config_id, active_version, created_at)
                VALUES (?, NULL, ?)
                """,
                (config_id, timestamp),
            )

            connection.execute(
                """
                INSERT INTO config_versions
                    (config_id, version, config_json, created_at)
                VALUES (?, 1, ?, ?)
                """,
                (
                    config_id,
                    json.dumps(request.config),
                    timestamp,
                ),
            )

            record_audit(
                connection,
                config_id,
                "created",
                1,
                {"message": "Initial configuration version created"},
            )
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"Configuration '{config_id}' already exists",
        )

    return {
        "config_id": config_id,
        "version": 1,
        "published": False,
        "created_at": timestamp,
        "warnings": validation["warnings"],
    }


@app.get("/api/v1/configs")
def list_configurations():
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT
                c.config_id,
                c.active_version,
                c.created_at,
                COUNT(v.version) AS version_count
            FROM configurations c
            LEFT JOIN config_versions v ON v.config_id = c.config_id
            GROUP BY c.config_id, c.active_version, c.created_at
            ORDER BY c.created_at DESC
            """
        ).fetchall()

    return {"configurations": [dict(row) for row in rows]}


@app.get("/api/v1/configs/{config_id}")
def get_configuration(config_id: str, version: int | None = None):
    with get_connection() as connection:
        exists = connection.execute(
            "SELECT config_id, active_version FROM configurations WHERE config_id = ?",
            (config_id,),
        ).fetchone()

        if exists is None:
            raise HTTPException(status_code=404, detail="Configuration not found")

        row = get_config_row(connection, config_id, version)

    if row is None:
        raise HTTPException(status_code=404, detail="Configuration version not found")

    return {
        "config_id": config_id,
        "version": row["version"],
        "active_version": exists["active_version"],
        "published": exists["active_version"] == row["version"],
        "created_at": row["created_at"],
        "config": json.loads(row["config_json"]),
    }


@app.post("/api/v1/configs/{config_id}/versions")
def create_configuration_version(
    config_id: str,
    request: ConfigVersionRequest,
):
    validation = validate_config(request.config)

    if not validation["valid"]:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Configuration is invalid",
                "errors": validation["errors"],
            },
        )

    timestamp = now_utc()

    with get_connection() as connection:
        exists = connection.execute(
            "SELECT config_id FROM configurations WHERE config_id = ?",
            (config_id,),
        ).fetchone()

        if exists is None:
            raise HTTPException(status_code=404, detail="Configuration not found")

        version = connection.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1 AS next_version
            FROM config_versions WHERE config_id = ?
            """,
            (config_id,),
        ).fetchone()["next_version"]

        connection.execute(
            """
            INSERT INTO config_versions
                (config_id, version, config_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                config_id,
                version,
                json.dumps(request.config),
                timestamp,
            ),
        )

        record_audit(
            connection,
            config_id,
            "version_created",
            version,
        )

    return {
        "config_id": config_id,
        "version": version,
        "published": False,
        "created_at": timestamp,
        "warnings": validation["warnings"],
    }


@app.get("/api/v1/configs/{config_id}/versions")
def list_configuration_versions(config_id: str):
    with get_connection() as connection:
        exists = connection.execute(
            "SELECT config_id, active_version FROM configurations WHERE config_id = ?",
            (config_id,),
        ).fetchone()

        if exists is None:
            raise HTTPException(status_code=404, detail="Configuration not found")

        rows = connection.execute(
            """
            SELECT version, created_at
            FROM config_versions
            WHERE config_id = ?
            ORDER BY version DESC
            """,
            (config_id,),
        ).fetchall()

    return {
        "config_id": config_id,
        "active_version": exists["active_version"],
        "versions": [
            {
                **dict(row),
                "published": row["version"] == exists["active_version"],
            }
            for row in rows
        ],
    }


@app.post("/api/v1/configs/{config_id}/publish")
def publish_configuration(config_id: str, request: PublishRequest):
    with get_connection() as connection:
        row = get_config_row(connection, config_id, request.version)

        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Configuration or version not found",
            )

        config = json.loads(row["config_json"])
        validation = validate_config(config)

        if not validation["valid"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Cannot publish an invalid configuration",
                    "errors": validation["errors"],
                },
            )

        connection.execute(
            "UPDATE configurations SET active_version = ? WHERE config_id = ?",
            (request.version, config_id),
        )

        record_audit(
            connection,
            config_id,
            "published",
            request.version,
        )

    return {
        "config_id": config_id,
        "active_version": request.version,
        "published": True,
        "warnings": validation["warnings"],
    }


@app.post("/api/v1/configs/{config_id}/rollback")
def rollback_configuration(config_id: str, request: RollbackRequest):
    with get_connection() as connection:
        exists = connection.execute(
            "SELECT config_id FROM configurations WHERE config_id = ?",
            (config_id,),
        ).fetchone()

        if exists is None:
            raise HTTPException(status_code=404, detail="Configuration not found")

        row = get_config_row(connection, config_id, request.version)

        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Target configuration version not found",
            )

        validation = validate_config(json.loads(row["config_json"]))

        if not validation["valid"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Cannot roll back to an invalid configuration",
                    "errors": validation["errors"],
                },
            )

        connection.execute(
            "UPDATE configurations SET active_version = ? WHERE config_id = ?",
            (request.version, config_id),
        )

        record_audit(
            connection,
            config_id,
            "rollback",
            request.version,
        )

    return {
        "config_id": config_id,
        "active_version": request.version,
        "rolled_back": True,
        "warnings": validation["warnings"],
    }


@app.get("/api/v1/configs/{config_id}/audit")
def configuration_audit(config_id: str):
    with get_connection() as connection:
        exists = connection.execute(
            "SELECT config_id FROM configurations WHERE config_id = ?",
            (config_id,),
        ).fetchone()

        if exists is None:
            raise HTTPException(status_code=404, detail="Configuration not found")

        rows = connection.execute(
            """
            SELECT id, action, version, details, created_at
            FROM audit_log
            WHERE config_id = ?
            ORDER BY id DESC
            """,
            (config_id,),
        ).fetchall()

    return {
        "config_id": config_id,
        "events": [
            {
                "id": row["id"],
                "action": row["action"],
                "version": row["version"],
                "details": json.loads(row["details"] or "{}"),
                "created_at": row["created_at"],
            }
            for row in rows
        ],
    }
