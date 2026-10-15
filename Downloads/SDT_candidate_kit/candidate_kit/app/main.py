"""Employee Attendance & Analytics API (FastAPI + MongoDB)."""
import os, re, math
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator, ConfigDict, StrictInt
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # OS environment variables override values from .env
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
STATUSES = PRESENCE + ["ABSENT", "LEAVE"]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def half_up(value, places=2):
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def epoch_ms(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def from_ms(value):
    if value is None: return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 100000000000 or value > 4102444800000:
        raise HTTPException(422, "Timestamp must be an integer epoch-millisecond value")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def as_utc(dt):
    if dt is None: return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def ist_date(dt): return as_utc(dt).astimezone(IST).date()


def shift_start_dt(day: date, shift_start: str):
    h, m = map(int, shift_start.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def shift_end_dt(day: date, emp):
    h, m = map(int, emp["shift_end"].split(":"))
    end_day = day + timedelta(days=1) if emp["shift_end"] <= emp["shift_start"] else day
    return datetime(end_day.year, end_day.month, end_day.day, h, m, tzinfo=IST)


def attendance_day(ts, emp):
    local = as_utc(ts).astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.strftime("%H:%M") < emp["shift_end"]:
        day -= timedelta(days=1)
    return day


def late_minutes(ts, emp, day):
    start = shift_start_dt(day, emp["shift_start"]).astimezone(UTC)
    seconds = (as_utc(ts) - start).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def derived_fields(emp, day_string, pin, pout, status):
    if status in ("ABSENT", "LEAVE"):
        return {"punch_in": None, "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    day = date.fromisoformat(day_string)
    pin = as_utc(pin) if pin else None
    pout = as_utc(pout) if pout else None
    work = None; overtime = 0; half = False
    late = late_minutes(pin, emp, day) if pin else 0
    if pin and pout:
        work = half_up((pout - pin).total_seconds() / 3600, 2)
        end = shift_end_dt(day, emp).astimezone(UTC)
        mins = int((pout - end).total_seconds() // 60)
        overtime = mins if mins >= 30 else 0
        half = work < 4.50
    return {"punch_in": pin, "punch_out": pout, "work_hours": work, "late_minutes": late, "overtime_minutes": overtime, "half_day": half}


def serialize_attendance(doc):
    if not doc: return doc
    out = dict(doc); out.pop("_id", None)
    for k in ("punch_in", "punch_out"):
        out[k] = epoch_ms(out.get(k))
    out["history"] = out.get("history") or []
    out.setdefault("half_day", False)
    out.setdefault("late_minutes", 0)
    out.setdefault("overtime_minutes", 0)
    for entry in out["history"]:
        entry["at"] = epoch_ms(entry.get("at"))
        for field, change in (entry.get("changes") or {}).items():
            if field in ("punch_in", "punch_out"):
                change["from"] = epoch_ms(change.get("from")); change["to"] = epoch_ms(change.get("to"))
    return out


def serialize_employee(doc):
    out = dict(doc); out.pop("_id", None)
    if "created_at" in out: out["created_at"] = epoch_ms(out["created_at"])
    return out


def valid_date(s, field="date"):
    try:
        if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s): raise ValueError()
        return date.fromisoformat(s)
    except Exception: raise HTTPException(422, f"{field} must be a valid YYYY-MM-DD date")


def valid_month(s):
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", s):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, s.split("-")); start = date(year, month, 1)
    end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start, end


def month_working_days(joined_on, month_start, next_month):
    try:
        joined_date = date.fromisoformat(joined_on)
    except (ValueError, TypeError):
        joined_date = month_start

    start = max(joined_date, month_start)
    end = next_month - timedelta(days=1)
    if start > end: return 0
    return sum(1 for i in range((end-start).days+1) if (start+timedelta(days=i)).weekday() < 5)


def ensure_indexes():
    # Unique constraints make check-then-insert races safe.
    db.employees.create_index(
    [("joined_on", ASCENDING)],
    name="ix_employee_joined"
    )
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_emp_date_status")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_status_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_emp_punchin")


@app.on_event("startup")
def startup():
    try: ensure_indexes()
    except PyMongoError:
        # Startup should still let /health report a clear 503 if Mongo is unavailable.
        pass


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def check_joined(cls, v): valid_date(v, "joined_on"); return v
    @field_validator("shift_end")
    @classmethod
    def check_shift(cls, v, info):
        if info.data.get("shift_start") == v: raise ValueError("shift_start and shift_end must differ")
        return v

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def check_ts(cls, v):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 100000000000 or v > 4102444800000):
            raise ValueError("Timestamp must be an integer epoch-millisecond value in the supported range")
        return v

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    @field_validator("punched_at")
    @classmethod
    def check_ts(cls, v):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 100000000000 or v > 4102444800000):
            raise ValueError("Timestamp must be an integer epoch-millisecond value in the supported range")
        return v

class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_ts(cls, v):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 100000000000 or v > 4102444800000):
            raise ValueError("Timestamp must be an integer epoch-millisecond value in the supported range")
        return v


def pagination(page, page_size):
    if page < 1 or page_size < 1 or page_size > 100: raise HTTPException(422, "Invalid pagination parameters")


@app.get("/health")
def health():
    try: db.command("ping"); return {"status": "ok"}
    except Exception: raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end: raise HTTPException(422, "shift_start and shift_end must differ")
    doc = body.model_dump(); doc["created_at"] = datetime.now(UTC)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    docs = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size))
    return {"items": [serialize_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "Employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    day = attendance_day(ts, emp); d = day.isoformat()
    fields = derived_fields(emp, d, ts, None, body.status)
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, **fields, "history": []}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if status is not None and status not in STATUSES: raise HTTPException(422, "Invalid status")
    if date_from: valid_date(date_from, "date_from")
    if date_to: valid_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
    q = {}
    if emp_code: q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status: q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size))
    return {"items": [serialize_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "Employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    q = {"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "status": {"$in": PRESENCE}}
    record = db.attendance_logs.find_one({**q, "punch_out": None}, sort=[("punch_in", DESCENDING)])
    if not record:
        if db.attendance_logs.find_one(q, sort=[("punch_in", DESCENDING)]): raise HTTPException(409, "Attendance record already punched out")
        raise HTTPException(404, "No matching punch-in found")
    pin = as_utc(record.get("punch_in")); seconds = (ts-pin).total_seconds()
    if seconds <= 0 or seconds > 86400: raise HTTPException(422, "Punch-out must be after punch-in and within 24 hours")
    computed = derived_fields(emp, record["date"], pin, ts, record["status"])
    update = {k: computed[k] for k in ("punch_out", "work_hours", "overtime_minutes", "half_day")}
    result = db.attendance_logs.update_one({"_id": record["_id"], "punch_out": None}, {"$set": update})
    if result.modified_count != 1: raise HTTPException(409, "Attendance record was updated concurrently")
    return serialize_attendance(db.attendance_logs.find_one({"_id": record["_id"]}))


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = valid_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "Employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec: raise HTTPException(404, "Attendance record not found")
    changes_in = body.model_dump(exclude_unset=True)
    new_status = changes_in.get("status", rec["status"])
    if new_status in ("ABSENT", "LEAVE") and (changes_in.get("punch_in") is not None or changes_in.get("punch_out") is not None):
        raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
    pin = from_ms(changes_in["punch_in"]) if changes_in.get("punch_in") is not None else (None if new_status in ("ABSENT", "LEAVE") else rec.get("punch_in"))
    pout = from_ms(changes_in["punch_out"]) if changes_in.get("punch_out") is not None else (None if new_status in ("ABSENT", "LEAVE") else rec.get("punch_out"))
    if new_status in PRESENCE and pin is None: raise HTTPException(422, "Presence status requires punch_in")
    if pin is not None and attendance_day(pin, emp).isoformat() != date: raise HTTPException(422, "punch_in must belong to the attendance date")
    if pout is not None:
        if pin is None or (pout-pin).total_seconds() <= 0 or (pout-pin).total_seconds() > 86400: raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    computed = derived_fields(emp, date, pin, pout, new_status)
    desired = {"status": new_status, **computed}
    old_values = {k: rec.get(k) for k in desired}
    changes = {}
    for k, val in desired.items():
        old = old_values.get(k)
        if k in ("punch_in", "punch_out"):
            old_cmp = epoch_ms(old); new_cmp = epoch_ms(val)
            if old_cmp != new_cmp: changes[k] = {"from": old, "to": val}
        elif old != val: changes[k] = {"from": old, "to": val}
    if not changes: raise HTTPException(422, "Request does not change the record")
    history_entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-swap on the previous history length and key fields prevents lost concurrent audit entries.
    cas = {"_id": rec["_id"]}

    if "history" in rec:
        cas["history"] = rec["history"]
    else:
        cas["history"] = {"$exists": False}
    for k in ("status", "punch_in", "punch_out", "late_minutes", "work_hours", "overtime_minutes", "half_day"):
        if k in rec: cas[k] = rec[k]
        else: cas[k] = {"$exists": False}
    result = db.attendance_logs.update_one(cas, {"$set": desired, "$push": {"history": history_entry}})
    if result.modified_count != 1: raise HTTPException(409, "Attendance record was updated concurrently")
    return serialize_attendance(db.attendance_logs.find_one({"_id": rec["_id"]}))


def monthly_match(month_start, next_month, emp_code=None):
    q = {"date": {"$gte": month_start.isoformat(), "$lt": next_month.isoformat()}}
    if emp_code: q["emp_code"] = emp_code
    return q


def employee_monthly_pipeline(emp_code, month_start, next_month):
    return [ {"$match": monthly_match(month_start, next_month, emp_code)},
      {"$set": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
      {"$group": {"_id": None,
       "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", PRESENCE]}, {"$gte": ["$_weekday", 2]}, {"$lte": ["$_weekday", 6]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
       "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
       "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
       "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
       "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}} ]

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    ms, nm = valid_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "Employee not found")
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, ms, nm)))
    vals = rows[0] if rows else {}
    wd = month_working_days(emp["joined_on"], ms, nm)
    pdays = half_up(vals.get("present_days", 0), 2)
    pct = half_up(pdays / wd * 100, 2) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": pdays,
      "leave_days": vals.get("leave_days", 0), "late_count": vals.get("late_count", 0),
      "total_late_minutes": vals.get("total_late_minutes", 0), "total_overtime_minutes": vals.get("total_overtime_minutes", 0), "attendance_pct": pct}


def department_summary_pipeline(month_start, next_month, department=None):
    match = {"joined_on": {"$lt": next_month.isoformat()}}
    if department is not None: match["department"] = department
    return [ {"$match": match},
      {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
        {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", month_start.isoformat()]}, {"$lt": ["$date", next_month.isoformat()]}]}}}], "as": "logs"}},
      {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
      {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
       "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", PRESENCE]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
       "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$logs.status", PRESENCE]}, {"$ne": ["$logs.work_hours", None]}, {"$ne": [{"$type": "$logs.work_hours"}, "missing"]}]}, "$logs.work_hours", None]}},
       "avg_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", PRESENCE]}, {"$ne": ["$logs.work_hours", None]}, {"$ne": [{"$type": "$logs.work_hours"}, "missing"]}]}, 1, 0]}},
       "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
       "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
       "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
       "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
      {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
       "avg_work_hours": {"$cond": [{"$gt": ["$avg_hours_count", 0]}, "$avg_work_hours", None]},
       "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}, {"$sort": {"department": 1}} ]

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    ms, nm = valid_month(month)
    rows = list(db.employees.aggregate(department_summary_pipeline(ms, nm, department)))
    for row in rows:
        if row.get("avg_work_hours") is not None: row["avg_work_hours"] = half_up(row["avg_work_hours"], 2)
        row["present_days"] = half_up(row.get("present_days", 0), 2)
    return {"month": month, "items": rows}


def leaderboard_pipeline(month_start, next_month, department=None, limit=10):
    pipeline = [{"$match": {"date": {"$gte": month_start.isoformat(), "$lt": next_month.isoformat()}, "late_minutes": {"$gt": 0}}},
      {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
      {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
      {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
      {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
      {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}}]
    return pipeline

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    ms, nm = valid_month(month)
    rows = list(db.attendance_logs.aggregate(leaderboard_pipeline(ms, nm, department, limit)))
    return {"month": month, "items": rows}


def trend_pipeline(department, from_day, to_day):
    # Generate the requested calendar range in MongoDB, then lookup that day's logs.
    # Starting from one department employee ensures even an all-zero-log range is emitted.
    count_days = (to_day - from_day).days + 1
    start_dt = datetime(from_day.year, from_day.month, from_day.day, tzinfo=UTC)
    return [
      {"$match": {"department": department}},
      {"$limit": 1},
      {"$set": {"_days": {"$map": {"input": {"$range": [0, count_days]}, "as": "i",
          "in": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": from_day.isoformat()}}, "unit": "day", "amount": "$$i"}}}}}},
      {"$unwind": "$_days"},
      {"$set": {"date": {"$dateToString": {"date": "$_days", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
      {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [
          {"$match": {"department": department}},
          {"$match": {"$expr": {"$lte": ["$joined_on", "$$d"]}}},
          {"$count": "n"}], "as": "_hc"}},
      {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$_hc.n", 0]}, 0]},
                 "_weekday": {"$dayOfWeek": "$_days"}}},
      {"$lookup": {"from": "attendance_logs", "let": {"d": "$date"}, "pipeline": [
           {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
          {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "_employee"}},
          {"$unwind": "$_employee"}, {"$match": {"_employee.department": department}},
          {"$group": {"_id": None,
             "present_count": {"$sum": {"$cond": [{"$in": ["$status", PRESENCE]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
             "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}], "as": "_daily"}},
      {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$_daily.present_count", 0]}, 0]},
                 "late_count": {"$ifNull": [{"$arrayElemAt": ["$_daily.late_count", 0]}, 0]},
                 "is_working_day": {"$and": [{"$gte": ["$_weekday", 2]}, {"$lte": ["$_weekday", 6]}]}}},
      {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
          {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None]}}},
      {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
      {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
          "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}}},
      {"$sort": {"date": 1}}
    ]

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    fd, td = valid_date(from_, "from"), valid_date(to, "to")
    if td < fd: raise HTTPException(422, "to must be on or after from")
    if (td-fd).days + 1 > 92: raise HTTPException(422, "Date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}): raise HTTPException(404, "Unknown department")
    # MongoDB $densify supports dates; convert calendar strings to dates and back in the pipeline.
    pipeline = trend_pipeline(department, fd, td)
    try: rows = list(db.employees.aggregate(pipeline))
    except PyMongoError as exc: raise HTTPException(500, f"Trend aggregation failed: {str(exc)}")
    for r in rows:
        if isinstance(r.get("date"), datetime): r["date"] = r["date"].date().isoformat()
        if r.get("attendance_rate") is not None: r["attendance_rate"] = half_up(r["attendance_rate"], 4)
        if r.get("moving_avg_7d") is not None: r["moving_avg_7d"] = half_up(r["moving_avg_7d"], 4)
    return {"department": department, "items": rows}


def explain_query(endpoint, params):
    if endpoint == "attendance_list":
        q = {}
        for key in ("emp_code", "status"):
            if params.get(key) is not None: q[key] = params[key]
        dateq = {}
        if params.get("date_from"): dateq["$gte"] = params["date_from"]
        if params.get("date_to"): dateq["$lte"] = params["date_to"]
        if dateq: q["date"] = dateq
        page, size = params.get("page", 1), params.get("page_size", 20)
        op = {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*size, "limit": size}
        return "attendance_logs", db.command({"explain": op, "verbosity": "executionStats"})
    if endpoint == "employee_monthly":
        if not params.get("emp_code") or not params.get("month"): raise HTTPException(422, "emp_code and month are required")
        ms, nm = valid_month(params["month"]); coll="attendance_logs"; pipe=employee_monthly_pipeline(params["emp_code"], ms, nm)
    elif endpoint == "department_summary":
        if not params.get("month"): raise HTTPException(422, "month is required")
        ms, nm=valid_month(params["month"]); coll="employees"; pipe=department_summary_pipeline(ms,nm,params.get("department"))
    elif endpoint == "late_leaderboard":
        if not params.get("month"): raise HTTPException(422, "month is required")
        ms,nm=valid_month(params["month"]); coll="attendance_logs"; pipe=leaderboard_pipeline(ms,nm,params.get("department"),params.get("limit",10))
    else:
        if not params.get("department") or not params.get("from") or not params.get("to"): raise HTTPException(422, "department, from and to are required")
        fd,td=valid_date(params["from"],"from"),valid_date(params["to"],"to"); coll="employees"; pipe=trend_pipeline(params["department"],fd,td)
    return coll, db.command({"explain": {"aggregate": coll, "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"})

@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                  limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
                  status: Optional[str] = None, from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
                  page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed: raise HTTPException(422, "Unknown endpoint")
    params = {"emp_code": emp_code, "month": month, "department": department, "limit": limit, "date_from": date_from,
              "date_to": date_to, "status": status, "from": from_, "to": to, "page": page, "page_size": page_size}
    coll, result = explain_query(endpoint, params)
    return {"endpoint": endpoint, "collection": coll, "explain": result}
