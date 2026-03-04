from typing import Optional
import os
import psycopg
from fastapi import FastAPI, HTTPException
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field

app = FastAPI(title="EcoApp API (Demo)")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql:"
)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def points_per_garment(container_type: str) -> int:
    c = container_type.strip().lower()
    if c == "cotton":
        return 20
    if c == "synthetic":
        return 10
    if c == "mixed":
        return 15
    if c == "unknown":
        return 5
    raise HTTPException(
        status_code=400,
        detail="Invalid container_type. Use cotton/synthetic/mixed/unknown."
    )


class SignupRequest(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=6, max_length=72)
    profile_image_url: Optional[str] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    user_id: str
    email: EmailStr
    display_name: str
    points_total: int
    profile_image_url: Optional[str] = None


class DropoffRequest(BaseModel):
    user_id: str
    center_id: str
    container_type: str
    garments_count: int = Field(gt=0)


@app.get("/")
def root():
    return {"message": "EcoApp API running. Go to /docs"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/signup", response_model=UserResponse)
def signup(payload: SignupRequest):
    if len(payload.password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Password must be <= 72 bytes for bcrypt.")

    email = normalize_email(payload.email)
    password_hash = pwd_context.hash(payload.password)

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (email, display_name, password_hash, profile_image_url)
                    VALUES (%s, %s, %s, %s)
                    RETURNING user_id::text, email, display_name, points_total, profile_image_url
                    """,
                    (email, payload.display_name, password_hash, payload.profile_image_url),
                )
                row = cur.fetchone()
            conn.commit()

        return UserResponse(
            user_id=row[0],
            email=row[1],
            display_name=row[2],
            points_total=row[3],
            profile_image_url=row[4],
        )

    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="Email already registered.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Signup failed: {e}")


@app.post("/login", response_model=UserResponse)
def login(payload: LoginRequest):
    if len(payload.password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Password must be <= 72 bytes for bcrypt.")

    email = normalize_email(payload.email)

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id::text, email, display_name, points_total,
                           profile_image_url, password_hash
                    FROM users
                    WHERE email = %s
                    """,
                    (email,),
                )
                row = cur.fetchone()

        if not row:
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        stored_hash = row[5]
        if not stored_hash or not pwd_context.verify(payload.password, stored_hash):
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        return UserResponse(
            user_id=row[0],
            email=row[1],
            display_name=row[2],
            points_total=row[3],
            profile_image_url=row[4],
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Login failed: {e}")


@app.get("/centers")
def list_centers():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT center_id, name, address, latitude, longitude
                    FROM recycling_centers
                    ORDER BY center_id
                    """
                )
                rows = cur.fetchall()

        return [
            {
                "center_id": r[0],
                "name": r[1],
                "address": r[2],
                "latitude": float(r[3]),
                "longitude": float(r[4]),
            }
            for r in rows
        ]

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"List centers failed: {e}")


@app.post("/dropoffs")
def create_dropoff(payload: DropoffRequest):
    ctype = payload.container_type.strip().lower()
    ppg = points_per_garment(ctype)
    points_earned = payload.garments_count * ppg

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM users WHERE user_id::text = %s", (payload.user_id,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="User not found.")

                cur.execute("SELECT 1 FROM recycling_centers WHERE center_id = %s", (payload.center_id,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Center not found.")

                cur.execute(
                    """
                    INSERT INTO dropoffs (user_id, center_id, container_type,
                                          garments_count, points_earned)
                    VALUES (%s::uuid, %s, %s, %s, %s)
                    RETURNING dropoff_id::text, scanned_at
                    """,
                    (payload.user_id, payload.center_id,
                     ctype, payload.garments_count, points_earned),
                )
                dropoff_row = cur.fetchone()

                cur.execute(
                    """
                    UPDATE users
                    SET points_total = points_total + %s
                    WHERE user_id::text = %s
                    RETURNING points_total
                    """,
                    (points_earned, payload.user_id),
                )
                new_total = cur.fetchone()[0]

            conn.commit()

        return {
            "dropoff_id": dropoff_row[0],
            "scanned_at": str(dropoff_row[1]),
            "points_earned": points_earned,
            "new_points_total": new_total,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Create dropoff failed: {e}")


@app.get("/users/{user_id}/history")
def user_history(user_id: str):
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT d.dropoff_id::text,
                           d.center_id,
                           d.container_type,
                           d.garments_count,
                           d.points_earned,
                           d.scanned_at
                    FROM dropoffs d
                    WHERE d.user_id::text = %s
                    ORDER BY d.scanned_at DESC
                    """,
                    (user_id,),
                )
                rows = cur.fetchall()

        return [
            {
                "dropoff_id": r[0],
                "center_id": r[1],
                "container_type": r[2],
                "garments_count": r[3],
                "points_earned": r[4],
                "scanned_at": str(r[5]),
            }
            for r in rows
        ]

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"History fetch failed: {e}")
