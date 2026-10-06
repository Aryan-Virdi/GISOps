import json
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

load_dotenv()
# prepare_threshold=None keeps this working through Supabase's transaction pooler too.
pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=4, open=False,
                      kwargs={"row_factory": dict_row, "prepare_threshold": None})


@asynccontextmanager
async def lifespan(app):
    pool.open()
    yield
    pool.close()


app = FastAPI(lifespan=lifespan)
SRIDS = {4326, 3857, 3310}  # WGS84 degrees, Web Mercator, California (Teale) Albers
ACRE_M2 = 4046.8564224


def feature(geom_json, props):
    return {"type": "Feature", "geometry": json.loads(geom_json), "properties": props}


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/api/features")
def features(srid: int = Query(3310)):
    """Stored WGS84 geometry, transformed to the requested projection inside PostGIS."""
    if srid not in SRIDS:
        raise HTTPException(400, "srid must be 4326, 3857 or 3310")
    with pool.connection() as c:
        fields = c.execute(
            """SELECT id, name, crop,
                      ST_Area(geom::geography) / %s AS acres,   -- true ground area, not projected area
                      ST_AsGeoJSON(ST_Transform(geom, %s::int)) AS g
               FROM fields ORDER BY id""", (ACRE_M2, srid)).fetchall()
        sightings = c.execute(
            """SELECT s.id, s.label, s.notes, s.observed_at,
                      ST_X(s.geom) AS lon, ST_Y(s.geom) AS lat,
                      ST_X(t.p) AS x, ST_Y(t.p) AS y, ST_AsGeoJSON(t.p) AS g
               FROM sightings s, LATERAL (SELECT ST_Transform(s.geom, %s::int) AS p) t
               ORDER BY s.id""", (srid,)).fetchall()
    return {
        "srid": srid,
        "fields": {"type": "FeatureCollection", "features": [
            feature(r["g"], {"id": r["id"], "idx": i, "name": r["name"], "crop": r["crop"],
                             "acres": round(r["acres"], 1)}) for i, r in enumerate(fields)]},
        "sightings": {"type": "FeatureCollection", "features": [
            feature(r["g"], {"id": r["id"], "label": r["label"], "lon": r["lon"], "lat": r["lat"],
                             "x": r["x"], "y": r["y"]}) for r in sightings]},
    }


@app.get("/api/clip")
def clip():
    """Spatial join: which field (if any) contains each sighting."""
    with pool.connection() as c:
        rows = c.execute(
            """SELECT s.id AS sighting_id, f.id AS field_id
               FROM sightings s LEFT JOIN fields f ON ST_Intersects(f.geom, s.geom)
               ORDER BY s.id""").fetchall()
    return {"assignments": {r["sighting_id"]: r["field_id"] for r in rows}}


@app.get("/api/fields/{field_id}/sightings")
def field_sightings(field_id: int):
    with pool.connection() as c:
        field = c.execute("SELECT id, name, crop FROM fields WHERE id = %s", (field_id,)).fetchone()
        if not field:
            raise HTTPException(404, "Field not found")
        rows = c.execute(
            """SELECT s.id, s.label, s.notes, s.observed_at, ST_X(s.geom) AS lon, ST_Y(s.geom) AS lat
               FROM sightings s JOIN fields f ON ST_Intersects(f.geom, s.geom)
               WHERE f.id = %s ORDER BY s.observed_at DESC""", (field_id,)).fetchall()
    for r in rows:
        r["observed_at"] = r["observed_at"].isoformat()
    return {"field": field, "sightings": rows}