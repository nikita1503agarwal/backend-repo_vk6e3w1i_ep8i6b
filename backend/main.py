from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import List, Optional
import os
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware

SUPABASE_URL = os.getenv("SUPABASE_URL") or ""
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY") or ""
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""

app = FastAPI(title="Shadow Enchanters API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase: Optional[Client] = None

if SUPABASE_URL and (SUPABASE_ANON_KEY or SUPABASE_SERVICE_ROLE_KEY):
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY)

class SignupPayload(BaseModel):
    email: str
    password: str
    name: str
    phone: Optional[str] = None
    instagram: Optional[str] = None
    linkedin: Optional[str] = None

class QuizAnswer(BaseModel):
    question_id: int
    answer_value: int

class QuizSubmission(BaseModel):
    answers: List[QuizAnswer]

class PointsChange(BaseModel):
    student_id: str
    delta: int
    reason: str

HOUSES = ["Gryffindor", "Slytherin", "Hufflepuff", "Ravenclaw"]


def ensure_client():
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    return supabase

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/auth/signup")
def signup(payload: SignupPayload):
    client = ensure_client()
    try:
        auth_res = client.auth.sign_up({"email": payload.email, "password": payload.password})
        user = auth_res.user
        if not user:
            raise HTTPException(status_code=400, detail="Signup failed")
        # Insert student profile with placeholder house (assigned after quiz)
        profile = {
            "id": user.id,
            "name": payload.name,
            "email": payload.email,
            "phone": payload.phone,
            "instagram": payload.instagram,
            "linkedin": payload.linkedin,
            "assigned_house": None,
            "total_points": 0,
        }
        client.table("students").insert(profile).execute()
        return {"user_id": user.id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


def assign_house_from_quiz(answers: List[QuizAnswer]) -> str:
    # Simple scoring: each answer_value is 0..3 mapping to houses order
    scores = {h: 0 for h in HOUSES}
    for a in answers:
        idx = max(0, min(3, a.answer_value))
        scores[HOUSES[idx]] += 1
    # tie-breaker by order
    best = max(scores.items(), key=lambda x: x[1])[0]
    return best

@app.post("/quiz/submit/{user_id}")
def quiz_submit(user_id: str, submission: QuizSubmission):
    client = ensure_client()
    try:
        house = assign_house_from_quiz(submission.answers)
        # upsert student assigned house and store answers
        client.table("students").update({"assigned_house": house}).eq("id", user_id).execute()
        client.table("quiz_answers").insert({"student_id": user_id, "answers": [a.model_dump() for a in submission.answers]}).execute()
        # ensure house exists and give zero totals entries
        # increment house_points if not exist
        client.rpc("ensure_house_exists", {"house_name": house}).execute()
        return {"assigned_house": house}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/student/dashboard/{user_id}")
def student_dashboard(user_id: str):
    client = ensure_client()
    try:
        student_res = client.table("students").select("id,name,email,assigned_house,total_points").eq("id", user_id).single().execute()
        student = student_res.data
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")
        houses_res = client.table("houses").select("name,total_points").execute()
        transactions = client.table("point_transactions").select("delta,reason,created_at").eq("student_id", user_id).order("created_at", desc=True).limit(50).execute().data
        return {
            "student": student,
            "houses": houses_res.data or [],
            "transactions": transactions or [],
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/admin/overview")
def admin_overview():
    client = ensure_client()
    try:
        houses = client.table("houses").select("name,total_points").execute().data
        top = client.table("students").select("id,name,assigned_house,total_points").order("total_points", desc=True).limit(10).execute().data
        return {"houses": houses or [], "top": top or []}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/admin/students")
def admin_students(house: Optional[str] = None):
    client = ensure_client()
    try:
        query = client.table("students").select("id,name,email,assigned_house,total_points")
        if house:
            query = query.eq("assigned_house", house)
        res = query.order("name").execute().data
        return res or []
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/admin/points")
def admin_points(change: PointsChange):
    client = ensure_client()
    try:
        # get student
        stu = client.table("students").select("id,assigned_house,total_points").eq("id", change.student_id).single().execute().data
        if not stu:
            raise HTTPException(status_code=404, detail="Student not found")
        new_total = (stu["total_points"] or 0) + change.delta
        client.table("students").update({"total_points": new_total}).eq("id", change.student_id).execute()
        # insert transaction
        client.table("point_transactions").insert({
            "student_id": change.student_id,
            "delta": change.delta,
            "reason": change.reason,
        }).execute()
        # update house total
        house = stu["assigned_house"]
        if house:
            # upsert house
            client.rpc("adjust_house_points", {"house_name": house, "delta": change.delta}).execute()
        return {"ok": True, "new_total": new_total}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/admin/bootstrap")
def admin_bootstrap():
    """Create default houses, functions, and default admin user if not present."""
    client = ensure_client()
    try:
        # ensure houses
        for h in HOUSES:
            client.table("houses").upsert({"name": h, "total_points": 0}).execute()
        # create default admin
        if SUPABASE_SERVICE_ROLE_KEY:
            auth = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
            existing = auth.auth.admin.list_users({"email": "harshvardhanpurohit2020@gmail.com"})
            if not existing or (hasattr(existing, "data") and not existing.data):
                auth.auth.admin.create_user({
                    "email": "harshvardhanpurohit2020@gmail.com",
                    "password": "admin123@@@",
                    "email_confirm": True
                })
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
