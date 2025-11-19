import os
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

# Supabase client
from supabase import create_client, Client

SUPABASE_URL = os.getenv("SUPABASE_URL") or "https://bgvfintzfwbaxgkrabci.supabase.co"
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY") or ""
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""

# Prefer service role for backend operations
SUPABASE_KEY = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY
if not SUPABASE_URL or not SUPABASE_KEY:
    # We will still start, but many endpoints will raise until env is set
    pass

supabase: Optional[Client] = None
try:
    if SUPABASE_URL and SUPABASE_KEY:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception:
    supabase = None

HOUSES = ["Gryffindor", "Slytherin", "Hufflepuff", "Ravenclaw"]

app = FastAPI(title="Shadow Enchanters API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Models
# -----------------------------
class SignupRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    phone: Optional[str] = None
    instagram: Optional[str] = None
    linkedin: Optional[str] = None

class QuizSubmitRequest(BaseModel):
    answers: List[str]

class PointsRequest(BaseModel):
    student_id: str
    delta: int
    reason: str

# -----------------------------
# Utilities
# -----------------------------

def require_supabase():
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.")


def ensure_houses_seeded() -> None:
    require_supabase()
    # Upsert the known houses
    rows = [{"name": h, "total_points": 0} for h in HOUSES]
    supabase.table("houses").upsert(rows, on_conflict="name").execute()


def ensure_house_exists(name: str) -> None:
    require_supabase()
    try:
        # Try RPC first (if created in SQL)
        supabase.rpc("ensure_house_exists", {"house_name": name}).execute()
    except Exception:
        # Fallback to upsert
        supabase.table("houses").upsert({"name": name, "total_points": 0}, on_conflict="name").execute()


def adjust_house_points(house: Optional[str], delta: int) -> None:
    if not house:
        return
    require_supabase()
    try:
        supabase.rpc("adjust_house_points", {"house_name": house, "delta": delta}).execute()
    except Exception:
        # Fallback: manual update
        # Ensure house exists, then update
        ensure_house_exists(house)
        # get current
        res = supabase.table("houses").select("total_points").eq("name", house).limit(1).execute()
        current = 0
        if res.data:
            current = int(res.data[0].get("total_points") or 0)
        supabase.table("houses").update({"total_points": current + delta}).eq("name", house).execute()


def map_quiz_to_house(answers: List[str]) -> str:
    # Simple scoring based on keywords; customize as needed
    scores = {h: 0 for h in HOUSES}
    for a in answers:
        al = a.lower()
        if any(k in al for k in ["brave", "courage", "daring", "bold"]):
            scores["Gryffindor"] += 1
        if any(k in al for k in ["ambition", "cunning", "power", "lead"]):
            scores["Slytherin"] += 1
        if any(k in al for k in ["loyal", "patience", "kind", "fair"]):
            scores["Hufflepuff"] += 1
        if any(k in al for k in ["wisdom", "learn", "wit", "clever"]):
            scores["Ravenclaw"] += 1
    # Choose max, fallback Gryffindor
    house = max(scores.items(), key=lambda x: x[1])[0]
    return house


def admin_guard(x_admin_key: Optional[str]) -> None:
    # Simple guard: require ADMIN_API_KEY env for admin routes
    required = os.getenv("ADMIN_API_KEY") or None
    if not required:
        # If not set, allow but warn via exception if needed
        return
    if x_admin_key != required:
        raise HTTPException(status_code=401, detail="Unauthorized admin access")

# -----------------------------
# Health
# -----------------------------
@app.get("/")
def read_root():
    return {
        "app": "Shadow Enchanters API",
        "supabase": "ready" if supabase else "not_configured",
    }

# -----------------------------
# Auth / Signup
# -----------------------------
@app.post("/auth/signup")
def signup(payload: SignupRequest):
    require_supabase()
    # Create auth user
    try:
        auth_res = supabase.auth.sign_up({"email": payload.email, "password": payload.password})
        user = auth_res.user
        if user is None:
            # If user already exists, fetch it via admin
            user = supabase.auth.admin.get_user_by_email(payload.email).user
        user_id = user.id
    except Exception as e:
        # Try admin create
        try:
            admin_created = supabase.auth.admin.create_user({
                "email": payload.email,
                "password": payload.password,
                "email_confirm": True,
            })
            user_id = admin_created.user.id
        except Exception as e2:
            raise HTTPException(status_code=400, detail=f"Signup failed: {str(e2)}")

    # Create/update profile in students
    ensure_houses_seeded()
    profile = {
        "id": user_id,
        "name": payload.name,
        "email": payload.email,
        "phone": payload.phone,
        "instagram": payload.instagram,
        "linkedin": payload.linkedin,
        "assigned_house": None,
        "total_points": 0,
    }
    supabase.table("students").upsert(profile, on_conflict="id").execute()
    return {"user_id": user_id, "message": "Signup successful"}

# -----------------------------
# Quiz
# -----------------------------
@app.post("/quiz/submit/{user_id}")
def submit_quiz(user_id: str, payload: QuizSubmitRequest):
    require_supabase()
    house = map_quiz_to_house(payload.answers)

    # Save quiz answers
    supabase.table("quiz_answers").insert({
        "student_id": user_id,
        "answers": payload.answers,
    }).execute()

    # Assign house on student profile
    ensure_house_exists(house)
    supabase.table("students").update({"assigned_house": house}).eq("id", user_id).execute()

    return {"assigned_house": house}

# -----------------------------
# Student Dashboard
# -----------------------------
@app.get("/student/dashboard/{user_id}")
def student_dashboard(user_id: str):
    require_supabase()
    # Student profile
    prof_res = supabase.table("students").select("id,name,email,assigned_house,total_points").eq("id", user_id).limit(1).execute()
    if not prof_res.data:
        raise HTTPException(status_code=404, detail="Student not found")
    profile = prof_res.data[0]

    # House standings
    houses_res = supabase.table("houses").select("name,total_points").execute()
    houses = houses_res.data or []

    # Transactions
    tx_res = supabase.table("point_transactions").select("id,delta,reason,created_at").eq("student_id", user_id).order("created_at", desc=True).limit(20).execute()
    transactions = tx_res.data or []

    return {"profile": profile, "houses": houses, "transactions": transactions}

# -----------------------------
# Admin
# -----------------------------
@app.get("/admin/overview")
def admin_overview(x_admin_key: Optional[str] = Header(default=None)):
    admin_guard(x_admin_key)
    require_supabase()
    houses_res = supabase.table("houses").select("name,total_points").order("total_points", desc=True).execute()
    top_students = supabase.table("students").select("id,name,assigned_house,total_points").order("total_points", desc=True).limit(10).execute()
    return {"houses": houses_res.data or [], "top_students": top_students.data or []}

@app.get("/admin/students")
def admin_students(house: Optional[str] = Query(default=None), x_admin_key: Optional[str] = Header(default=None)):
    admin_guard(x_admin_key)
    require_supabase()
    q = supabase.table("students").select("id,name,email,assigned_house,total_points")
    if house:
        q = q.eq("assigned_house", house)
    res = q.order("name").execute()
    return res.data or []

@app.post("/admin/points")
def admin_points(payload: PointsRequest, x_admin_key: Optional[str] = Header(default=None)):
    admin_guard(x_admin_key)
    require_supabase()
    # Fetch student to know current house
    sres = supabase.table("students").select("assigned_house,total_points").eq("id", payload.student_id).limit(1).execute()
    if not sres.data:
        raise HTTPException(status_code=404, detail="Student not found")
    student = sres.data[0]
    house = student.get("assigned_house")

    # Record transaction
    supabase.table("point_transactions").insert({
        "student_id": payload.student_id,
        "delta": payload.delta,
        "reason": payload.reason,
    }).execute()

    # Update student total
    new_total = int(student.get("total_points", 0)) + payload.delta
    supabase.table("students").update({"total_points": new_total}).eq("id", payload.student_id).execute()

    # Update house total via RPC/fallback
    adjust_house_points(house, payload.delta)

    return {"ok": True, "new_total": new_total}

@app.post("/admin/bootstrap")
def admin_bootstrap(x_admin_key: Optional[str] = Header(default=None)):
    # Allow call if ADMIN_API_KEY configured and matches, else allow if service role key is present (within trusted backend)
    admin_guard(x_admin_key)
    require_supabase()

    # Seed houses
    ensure_houses_seeded()

    # Create default admin user
    admin_email = "harshvardhanpurohit2020@gmail.com"
    admin_password = "admin123@@@"
    try:
        get_res = supabase.auth.admin.get_user_by_email(admin_email)
        admin_user = get_res.user
        if admin_user is None:
            raise Exception("not found")
    except Exception:
        created = supabase.auth.admin.create_user({
            "email": admin_email,
            "password": admin_password,
            "email_confirm": True,
        })
        admin_user = created.user

    # Ensure an admin profile exists in students with zero points
    supabase.table("students").upsert({
        "id": admin_user.id,
        "name": "Admin",
        "email": admin_email,
        "total_points": 0,
        "assigned_house": None,
    }, on_conflict="id").execute()

    return {"ok": True, "message": "Bootstrap complete", "admin_email": admin_email}

# -----------------------------
# Diagnostics
# -----------------------------
@app.get("/test")
def test_supabase():
    status: Dict[str, Any] = {
        "backend": "running",
        "supabase_url": bool(SUPABASE_URL),
        "supabase_key": bool(SUPABASE_KEY),
        "connected": False,
        "tables": [],
    }
    if supabase is None:
        return status
    try:
        # Try a lightweight query
        res = supabase.table("houses").select("name").limit(1).execute()
        status["connected"] = True
        status["tables"] = ["houses"]
    except Exception as e:
        status["error"] = str(e)
    return status

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
