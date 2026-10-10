"""Employee Attendance & Analytics API."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
VALID_PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
VALID_STATUS = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Utility helpers
# --------------------------------------------------------------------------- #
def epoch_ms_from_datetime(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def datetime_from_epoch_ms(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def round_half_up(value: Optional[float], digits: int) -> Optional[float]:
    if value is None:
        return None
    quant = Decimal("1").scaleb(-digits)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def time_from_str(raw: str) -> datetime.time:
    h, m = map(int, raw.split(":"))
    return datetime.strptime(f"{h:02d}:{m:02d}", "%H:%M").time()


def date_from_str(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    return time_from_str(shift_end) <= time_from_str(shift_start)


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    local = to_ist(punch_in_utc)
    if is_overnight_shift(shift_start, shift_end) and local.time() < time_from_str(shift_end):
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def shift_start_datetime(record_date: str, shift_start: str) -> datetime:
    return datetime.strptime(f"{record_date} {shift_start}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)


def shift_end_datetime(record_date: str, shift_end: str, overnight: bool) -> datetime:
    end_day = date_from_str(record_date)
    if overnight:
        end_day = end_day + timedelta(days=1)
    return datetime.strptime(f"{end_day.isoformat()} {shift_end}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, shift_end: str) -> int:
    local = to_ist(punch_in_utc)
    attendance_date = compute_attendance_date(punch_in_utc, shift_start, shift_end)
    start_dt = shift_start_datetime(attendance_date, shift_start)
    delta_seconds = (local - start_dt).total_seconds()
    if delta_seconds <= 600:
        return 0
    return int(delta_seconds // 60)


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    total_hours = seconds / 3600
    return round_half_up(total_hours, 2) or 0.0


def compute_overtime(punch_out_utc: datetime, shift_end: str, shift_start: str, record_date: str) -> int:
    local_out = to_ist(punch_out_utc)
    overnight = is_overnight_shift(shift_start, shift_end)
    end_dt = shift_end_datetime(record_date, shift_end, overnight)
    seconds = (local_out - end_dt).total_seconds()
    if seconds <= 0:
        return 0
    minutes = int(seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_derived_fields(record_date: str, punch_in_utc: Optional[datetime], punch_out_utc: Optional[datetime], shift_start: str, shift_end: str) -> dict[str, Any]:
    if punch_in_utc is None:
        return {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }

    late_minutes = compute_late_minutes(punch_in_utc, shift_start, shift_end)
    work_hours = None
    overtime_minutes = 0
    half_day = False

    if punch_out_utc is not None:
        work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
        overtime_minutes = compute_overtime(punch_out_utc, shift_end, shift_start, record_date)
        half_day = work_hours < 4.5

    return {
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }


def make_error(detail: str) -> HTTPException:
    return HTTPException(status_code=404, detail=detail)


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = month.split("-")
    start = f"{year}-{mon}-01"
    first = date.fromisoformat(start)
    if mon == "12":
        next_month = date(int(year) + 1, 1, 1)
    else:
        next_month = date(int(year), int(mon) + 1, 1)
    return start, next_month.isoformat()


def working_days_in_month(joined_on: str, month: str) -> int:
    month_start = date.fromisoformat(month + "-01")
    if month_start.month == 12:
        month_end = date(month_start.year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(month_start.year, month_start.month + 1, 1) - timedelta(days=1)

    start_day = max(date.fromisoformat(joined_on), month_start)
    count = 0
    current = start_day
    while current <= month_end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    item = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in item and item["created_at"] is not None:
        item["created_at"] = epoch_ms_from_datetime(item["created_at"])
    return item


def serialize_history(doc: Any) -> list[dict[str, Any]]:
    if not doc:
        return []
    out = []
    for item in doc:
        changes = {}
        for key, value in (item.get("changes") or {}).items():
            changes[key] = {
                "from": _convert_nested_datetime(value.get("from")),
                "to": _convert_nested_datetime(value.get("to")),
            }
        out.append(
            {
                "at": epoch_ms_from_datetime(item.get("at")),
                "by": item.get("by"),
                "reason": item.get("reason"),
                "changes": changes,
            }
        )
    return out


def _convert_nested_datetime(value: Any) -> Any:
    if isinstance(value, datetime):
        return epoch_ms_from_datetime(value)
    if isinstance(value, list):
        return [_convert_nested_datetime(v) for v in value]
    if isinstance(value, dict):
        return {k: _convert_nested_datetime(v) for k, v in value.items()}
    return value


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    item = {k: v for k, v in doc.items() if k != "_id"}
    item["punch_in"] = epoch_ms_from_datetime(item.get("punch_in"))
    item["punch_out"] = epoch_ms_from_datetime(item.get("punch_out"))
    item["history"] = serialize_history(item.get("history", []))
    return item


def ensure_employee_exists(emp_code: str) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return employee


def find_attendance_record(emp_code: str, date_str: str) -> Optional[dict[str, Any]]:
    return db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})


def all_dates_between(start: str, end: str) -> list[str]:
    s = date_from_str(start)
    e = date_from_str(end)
    out = []
    d = s
    while d <= e:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


@app.on_event("startup")
def startup_indexes() -> None:
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index("emp_code")
    db.attendance_logs.create_index("status")
    db.attendance_logs.create_index("late_minutes")


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        if not value:
            raise ValueError("emp_code is required")
        return value


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict[str, str]:
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict[str, Any]:
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, detail="emp_code already exists")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict[str, Any]:
    ensure_employee_exists(body.emp_code)

    if body.status not in VALID_PRESENCE:
        raise HTTPException(422, detail="status must be PRESENT, WFH or ON_DUTY")

    punched_utc = datetime_from_epoch_ms(body.punched_at) or datetime.now(timezone.utc)
    employee = db.employees.find_one({"emp_code": body.emp_code})
    attendance_date = compute_attendance_date(punched_utc, employee["shift_start"], employee["shift_end"])

    record = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punched_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_utc, employee["shift_start"], employee["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(409, detail="already punched in for this date")

    return serialize_attendance(record)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn) -> dict[str, Any]:
    ensure_employee_exists(body.emp_code)

    punch_out_utc = datetime_from_epoch_ms(body.punched_at) or datetime.now(timezone.utc)
    employee = db.employees.find_one({"emp_code": body.emp_code})

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punch_out_utc},
            "punch_out": None,
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(404, detail="no punch-in found")

    punch_in_utc = record["punch_in"]
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(422, detail="punch_out must be after punch_in")
    if punch_out_utc - punch_in_utc > timedelta(hours=24):
        raise HTTPException(422, detail="punch_out cannot be more than 24 hours after punch_in")

    updated = {
        "punch_out": punch_out_utc,
        "work_hours": compute_work_hours(punch_in_utc, punch_out_utc),
        "overtime_minutes": compute_overtime(punch_out_utc, employee["shift_end"], employee["shift_start"], record["date"]),
        "half_day": False,
    }
    updated["half_day"] = updated["work_hours"] < 4.5

    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": updated},
    )
    if result.matched_count == 0:
        raise HTTPException(409, detail="record already punched out")

    final_doc = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(final_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from cannot be after date_to")

    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size))
    items = [serialize_attendance(doc) for doc in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, payload: RegularizeRequest) -> dict[str, Any]:
    employee = ensure_employee_exists(emp_code)
    record = find_attendance_record(emp_code, date)
    if record is None:
        raise HTTPException(404, detail="attendance record not found")

    existing_status = record.get("status")
    existing_punch_in = record.get("punch_in")
    existing_punch_out = record.get("punch_out")

    new_status = payload.status if payload.status is not None else existing_status
    new_punch_in = datetime_from_epoch_ms(payload.punch_in) if payload.punch_in is not None else existing_punch_in
    new_punch_out = datetime_from_epoch_ms(payload.punch_out) if payload.punch_out is not None else existing_punch_out

    if new_status in {"ABSENT", "LEAVE"} and (new_punch_in is not None or new_punch_out is not None):
        raise HTTPException(422, detail="ABSENT and LEAVE records must have null punch times")
    if new_status in VALID_PRESENCE and new_punch_in is None:
        raise HTTPException(422, detail="presence status requires a punch_in")
    if new_punch_in is not None:
        punch_date = compute_attendance_date(new_punch_in, employee["shift_start"], employee["shift_end"])
        if punch_date != date:
            raise HTTPException(422, detail="punch_in must stay on the attendance date")
    if new_punch_in is not None and new_punch_out is not None:
        if new_punch_out <= new_punch_in:
            raise HTTPException(422, detail="punch_out must be after punch_in")
        if new_punch_out - new_punch_in > timedelta(hours=24):
            raise HTTPException(422, detail="punch_out cannot be more than 24 hours after punch_in")

    effective_status = new_status
    effective_punch_in = new_punch_in
    effective_punch_out = new_punch_out

    if effective_status in {"ABSENT", "LEAVE"}:
        effective_punch_in = None
        effective_punch_out = None

    if effective_punch_in is None and effective_punch_out is not None:
        raise HTTPException(422, detail="punch_out requires punch_in")

    derived = compute_derived_fields(date, effective_punch_in, effective_punch_out, employee["shift_start"], employee["shift_end"])
    update_doc = {
        "status": effective_status,
        "punch_in": effective_punch_in,
        "punch_out": effective_punch_out,
        "work_hours": derived["work_hours"],
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": derived["half_day"],
    }

    if existing_status == effective_status and existing_punch_in == effective_punch_in and existing_punch_out == effective_punch_out:
        raise HTTPException(422, detail="request changes nothing")

    changes: dict[str, Any] = {}
    for key in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        old_value = record.get(key)
        new_value = update_doc.get(key)
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(422, detail="request changes nothing")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": payload.regularized_by,
        "reason": payload.reason,
        "changes": {k: {"from": _convert_nested_datetime(v["from"]), "to": _convert_nested_datetime(v["to"])} for k, v in changes.items()},
    }

    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "history": record.get("history", [])},
        {"$set": update_doc, "$push": {"history": history_entry}},
    )
    if result.matched_count == 0:
        raise HTTPException(409, detail="record updated concurrently")

    final_doc = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(final_doc)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict[str, Any]:
    employee = ensure_employee_exists(emp_code)
    month_start, month_next = month_bounds(month)

    records = list(db.attendance_logs.find({"emp_code": emp_code, "date": {"$gte": month_start, "$lt": month_next}}))
    working_days = working_days_in_month(employee["joined_on"], month.split("-")[0] + "-" + month.split("-")[1])

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0

    for record in records:
        if record.get("status") in ["PRESENT", "WFH", "ON_DUTY"] and date_from_str(record["date"]).weekday() < 5:
            if record.get("half_day"):
                present_days += 0.5
            else:
                present_days += 1.0
        if record.get("status") == "LEAVE":
            leave_days += 1
        if int(record.get("late_minutes") or 0) > 0:
            late_count += 1
        total_late_minutes += int(record.get("late_minutes") or 0)
        total_overtime_minutes += int(record.get("overtime_minutes") or 0)

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
) -> dict[str, Any]:
    month_start, month_next = month_bounds(month)
    employee_filter: dict[str, Any] = {"joined_on": {"$lte": month_next}}  # inclusive end of month
    if department is not None:
        employee_filter["department"] = department

    employees = list(db.employees.find(employee_filter, {"_id": 0, "emp_code": 1, "department": 1, "joined_on": 1}))
    dept_map: dict[str, set[str]] = {}
    for employee in employees:
        dept = employee["department"]
        dept_map.setdefault(dept, set()).add(employee["emp_code"])

    if not dept_map:
        return {"month": month, "items": []}

    records = list(db.attendance_logs.find({"date": {"$gte": month_start, "$lt": month_next}}))
    dept_stats: dict[str, dict[str, Any]] = {}
    for dept, emp_codes in dept_map.items():
        dept_stats[dept] = {
            "department": dept,
            "headcount": len(emp_codes),
            "present_days": 0.0,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
            "work_hours_total": 0.0,
            "work_hours_count": 0,
        }

    for record in records:
        emp_code = record.get("emp_code")
        dept = next((employee["department"] for employee in employees if employee["emp_code"] == emp_code), None)
        if dept is None or emp_code not in dept_map.get(dept, set()):
            continue
        stats = dept_stats[dept]
        if record.get("status") in ["PRESENT", "WFH", "ON_DUTY"] and date_from_str(record["date"]).weekday() < 5:
            stats["present_days"] += 0.5 if record.get("half_day") else 1.0
        if record.get("status") == "LEAVE":
            stats["leave_count"] += 1
        if record.get("status") == "ON_DUTY":
            stats["on_duty_count"] += 1
        if int(record.get("late_minutes") or 0) > 0:
            stats["late_count"] += 1
            stats["total_late_minutes"] += int(record.get("late_minutes") or 0)
        if record.get("status") in ["PRESENT", "WFH", "ON_DUTY"] and record.get("work_hours") is not None:
            stats["work_hours_total"] += float(record.get("work_hours") or 0)
            stats["work_hours_count"] += 1

    items = []
    for dept in sorted(dept_stats):
        stats = dept_stats[dept]
        avg_work_hours = None
        if stats["work_hours_count"]:
            avg_work_hours = round_half_up(stats["work_hours_total"] / stats["work_hours_count"], 2)
        items.append(
            {
                "department": dept,
                "headcount": stats["headcount"],
                "present_days": stats["present_days"],
                "avg_work_hours": avg_work_hours,
                "late_count": stats["late_count"],
                "total_late_minutes": stats["total_late_minutes"],
                "leave_count": stats["leave_count"],
                "on_duty_count": stats["on_duty_count"],
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict[str, Any]:
    month_start, month_next = month_bounds(month)
    employee_filter = {}
    if department is not None:
        employee_filter["department"] = department
    employees = list(db.employees.find(employee_filter, {"_id": 0, "emp_code": 1, "name": 1, "department": 1}))
    emp_map = {emp["emp_code"]: emp for emp in employees}
    if not emp_map:
        return {"month": month, "items": []}

    records = list(db.attendance_logs.find({"emp_code": {"$in": list(emp_map.keys())}, "date": {"$gte": month_start, "$lt": month_next}}))
    totals = {}
    for record in records:
        total = int(record.get("late_minutes") or 0)
        if total <= 0:
            continue
        totals[record["emp_code"]] = totals.get(record["emp_code"], 0) + total

    ranked_rows = []
    for emp_code, total_minutes in totals.items():
        if total_minutes <= 0:
            continue
        emp = emp_map.get(emp_code)
        if emp is None:
            continue
        ranked_rows.append({
            "emp_code": emp_code,
            "name": emp["name"],
            "department": emp["department"],
            "total_late_minutes": total_minutes,
            "late_count": 0,
        })

    ranked_rows.sort(key=lambda x: (-x["total_late_minutes"], x["emp_code"]))
    final_rows = []
    prev_total = None
    prev_rank = 0
    for idx, row in enumerate(ranked_rows):
        if prev_total is None or row["total_late_minutes"] != prev_total:
            rank = idx + 1
            prev_total = row["total_late_minutes"]
            prev_rank = rank
        else:
            rank = prev_rank
        row["rank"] = rank
        final_rows.append(row)

    selected = [row for row in final_rows if row["rank"] <= limit]
    for row in selected:
        row.pop("late_count", None)
        row["late_count"] = sum(1 for record in records if record.get("emp_code") == row["emp_code"] and int(record.get("late_minutes") or 0) > 0)

    return {"month": month, "items": selected}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
) -> dict[str, Any]:
    if to_date < from_date:
        raise HTTPException(422, detail="to must be >= from")
    if (date_from_str(to_date) - date_from_str(from_date)).days + 1 > 92:
        raise HTTPException(422, detail="date range cannot exceed 92 days")

    employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not employees:
        raise HTTPException(404, detail="department not found")

    emp_codes = [row["emp_code"] for row in employees]
    days = all_dates_between(from_date, to_date)
    day_index = {d: {"present_count": 0.0, "late_count": 0} for d in days}

    records = list(db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": {"$gte": from_date, "$lte": to_date}}))
    for record in records:
        day = record["date"]
        if day not in day_index:
            continue
        status = record.get("status")
        if status in ["PRESENT", "WFH", "ON_DUTY"]:
            day_index[day]["present_count"] += 0.5 if record.get("half_day") else 1.0
        if int(record.get("late_minutes") or 0) > 0:
            day_index[day]["late_count"] += 1

    headcount_by_day = {}
    for d in days:
        day_value = date_from_str(d)
        headcount_by_day[d] = sum(1 for emp in employees if emp["joined_on"] <= d)

    result_items = []
    attendance_rates = []
    for d in days:
        headcount = headcount_by_day[d]
        present_count = day_index[d]["present_count"]
        late_count = day_index[d]["late_count"]
        is_working_day = date_from_str(d).weekday() < 5
        attendance_rate = None
        if is_working_day and headcount > 0:
            attendance_rate = round_half_up((present_count / headcount), 4)
        result_items.append({
            "date": d,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
        })
        if attendance_rate is not None:
            attendance_rates.append(attendance_rate)

    moving_avg = []
    for idx, item in enumerate(result_items):
        window = []
        for j in range(max(0, idx - 6), idx + 1):
            candidate = result_items[j].get("attendance_rate")
            if candidate is not None:
                window.append(candidate)
        moving_value = None
        if window:
            moving_value = round_half_up(sum(window) / len(window), 4)
        item["moving_avg_7d"] = moving_value
        moving_avg.append(moving_value)

    return {"department": department, "items": [{
        "date": item["date"],
        "is_working_day": item["is_working_day"],
        "headcount": item["headcount"],
        "present_count": item["present_count"],
        "late_count": item["late_count"],
        "attendance_rate": item["attendance_rate"],
        "moving_avg_7d": item["moving_avg_7d"],
    } for item in result_items]}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Optional[int] = Query(None, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
) -> dict[str, Any]:
    def explain_attendance_query(
        query: dict[str, Any],
        sort: Optional[list[tuple[str, int]]] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> dict[str, Any]:
        find_command: dict[str, Any] = {"find": "attendance_logs", "filter": query}
        if sort:
            find_command["sort"] = dict(sort)
        if skip:
            find_command["skip"] = skip
        if limit is not None:
            find_command["limit"] = limit
        return db.command({"explain": find_command, "verbosity": "executionStats"})

    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status:
            query["status"] = status
        result = explain_attendance_query(
            query,
            sort=[("date", -1), ("emp_code", 1)],
            skip=(page - 1) * page_size,
            limit=page_size,
        )
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required")
        month_start, month_next = month_bounds(month)
        result = explain_attendance_query({"emp_code": emp_code, "date": {"$gte": month_start, "$lt": month_next}})
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required")
        month_start, month_next = month_bounds(month)
        q = {"date": {"$gte": month_start, "$lt": month_next}}
        if department:
            dept_emp_codes = [doc["emp_code"] for doc in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
            q["emp_code"] = {"$in": dept_emp_codes}
        result = explain_attendance_query(q)
        collection = "attendance_logs"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required")
        month_start, month_next = month_bounds(month)
        q = {"date": {"$gte": month_start, "$lt": month_next}, "late_minutes": {"$gt": 0}}
        if department:
            dept_emp_codes = [doc["emp_code"] for doc in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
            q["emp_code"] = {"$in": dept_emp_codes}
        result = explain_attendance_query(q, sort=[("late_minutes", -1)], limit=limit or 10)
        collection = "attendance_logs"
    else:
        if not department or not from_date or not to_date:
            raise HTTPException(422, detail="department, from and to are required")
        if to_date < from_date:
            raise HTTPException(422, detail="to must be >= from")
        emp_codes = [doc["emp_code"] for doc in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
        result = explain_attendance_query(
            {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_date, "$lte": to_date}},
            sort=[("date", 1)],
        )
        collection = "attendance_logs"

    return {"endpoint": endpoint, "collection": collection, "explain": result}


# --------------------------------------------------------------------------- #
# FastAPI startup is in a single file and the app is already defined above.
# --------------------------------------------------------------------------- #
