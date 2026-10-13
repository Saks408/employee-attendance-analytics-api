"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000 --reload
Environment: MONGO_URI and MONGO_DB
"""
from bson import json_util
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.collection import ReturnDocument

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENT_STATUSES | {"ABSENT", "LEAVE"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        client.admin.command("ping")
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True, name="uq_employee_attendance_date",
        )
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)],
            name="ix_attendance_date_employee",
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", DESCENDING)],
            name="ix_attendance_employee_date",
        )
        db.attendance_logs.create_index(
            [("status", ASCENDING), ("date", DESCENDING)],
            name="ix_attendance_status_date",
        )
        db.employees.create_index(
            [("department", ASCENDING), ("joined_on", ASCENDING)],
            name="ix_employee_department_joined",
        )
        logger.info("MongoDB connected and indexes are ready.")
    except PyMongoError:
        logger.exception("MongoDB connection or index setup failed.")
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# ------------------------------ Common helpers ------------------------------ #
def fail(status_code: int, detail: str):
    raise HTTPException(status_code=status_code, detail=detail)


def as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def whole_seconds(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def epoch_ms(value: Optional[datetime]):
    if value is None:
        return None
    return int(value.timestamp() * 1000)


def timestamp_to_datetime(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value < 100_000_000_000:
        fail(422, "Timestamp must be epoch milliseconds as an integer")
    try:
        return whole_seconds(datetime.fromtimestamp(value / 1000, tz=timezone.utc)).astimezone(IST)
    except (ValueError, OverflowError, OSError):
        fail(422, "Invalid timestamp")


def parse_date(value: str, field_name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        fail(422, f"{field_name} must use YYYY-MM-DD format")
    if parsed.isoformat() != value:
        fail(422, f"{field_name} must use YYYY-MM-DD format")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        fail(422, "month must use YYYY-MM format")
    try:
        first = date.fromisoformat(value + "-01")
    except ValueError:
        fail(422, "month must use YYYY-MM format")
    if first.month == 12:
        next_month = date(first.year + 1, 1, 1)
    else:
        next_month = date(first.year, first.month + 1, 1)
    return first, next_month - timedelta(days=1)


def time_tuple(value: str) -> tuple[int, int]:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        fail(422, "Time must use HH:MM format")
    h, m = value.split(":")
    return int(h), int(m)


def round_half_up(value, places=2) -> float:
    quantizer = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantizer, rounding=ROUND_HALF_UP))


def date_is_working_day(value: date) -> bool:
    return value.weekday() < 5


def shift_is_overnight(start: str, end: str) -> bool:
    return time_tuple(end) <= time_tuple(start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = as_ist(whole_seconds(punch_in))
    if shift_is_overnight(shift_start, shift_end) and (local.hour, local.minute) < time_tuple(shift_end):
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def shift_end_datetime(date_str: str, shift_start: str, shift_end: str) -> datetime:
    day = parse_date(date_str)
    h, m = time_tuple(shift_end)
    end = datetime.combine(day, time(h, m), tzinfo=IST)
    if shift_is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return end



def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_day: Optional[str] = None,
) -> int:
    local = as_ist(whole_seconds(punch_in))
    h, m = time_tuple(shift_start)

    # For overnight shifts, use the attendance date as shift-start date.
    shift_day = (
        parse_date(attendance_day)
        if attendance_day is not None
        else local.date()
    )

    start = datetime.combine(
        shift_day,
        time(h, m),
        tzinfo=IST,
    )

    delta_seconds = (local - start).total_seconds()

    # Up to and including 10 minutes late is not considered late.
    if delta_seconds <= 600:
        return 0

    # Count complete late minutes from shift start.
    return int(delta_seconds // 60)



def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    end = shift_end_datetime(date_str, shift_start, shift_end)
    seconds = (as_ist(whole_seconds(punch_out)) - end).total_seconds()
    if seconds < 1800:
        return 0
    return int(seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (whole_seconds(punch_out) - whole_seconds(punch_in)).total_seconds()
    return round_half_up(Decimal(str(seconds)) / Decimal("3600"), 2)


def serialize_employee(doc: dict) -> dict:
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = epoch_ms(doc["created_at"])
    return doc


def serialize_attendance(doc: dict) -> dict:
    doc.pop("_id", None)
    for key in ("punch_in", "punch_out"):
        if isinstance(doc.get(key), datetime):
            doc[key] = epoch_ms(doc[key])
    doc.setdefault("history", [])
    doc.setdefault("half_day", False)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    return doc


def employee_or_404(emp_code: str) -> dict:
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        fail(404, "Employee not found")
    return employee


# ---------------------------------- Models ---------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        value = value.strip()
        if not re.fullmatch(r"[^@\\s]+@[^@\\s]+\\.[^@\\s]+", value):
            raise ValueError("Invalid email address")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_time(cls, value):
        time_tuple(value)
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, value):
        parse_date(value, "joined_on")
        return value

    @model_validator(mode="after")
    def valid_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end cannot be the same")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value not in PRESENT_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    punched_at: Optional[StrictInt] = None


class RegularizeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None

    @field_validator("status")
    @classmethod
    def valid_regularization_status(cls, value):
        if value is not None and value not in ALL_STATUSES:
            raise ValueError("Invalid attendance status")
        return value


# --------------------------------- Health ----------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        fail(503, "MongoDB unavailable")


# ------------------------------- Employees ---------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        fail(409, "emp_code already exists")
    except PyMongoError:
        logger.exception("Could not create employee")
        fail(503, "Database operation failed")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department:
        query["department"] = department
    total = db.employees.count_documents(query)
    docs = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
                .skip((page - 1) * page_size).limit(page_size))
    for doc in docs:
        if isinstance(doc.get("created_at"), datetime):
            doc["created_at"] = epoch_ms(doc["created_at"])
    return {"items": docs, "total": total, "page": page, "page_size": page_size}


# ------------------------------- Attendance -------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employee_or_404(body.emp_code)
    ts = (whole_seconds(datetime.now(timezone.utc)).astimezone(IST)
          if body.punched_at is None else timestamp_to_datetime(body.punched_at))
    date_str = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
             ts,
         emp["shift_start"],
           date_str,
         ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        fail(409, "Attendance already exists for this employee and date")
    except PyMongoError:
        logger.exception("Could not record punch-in")
        fail(503, "Database operation failed")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee_or_404(body.emp_code)
    punch_out_time = (whole_seconds(datetime.now(timezone.utc)).astimezone(IST)
                      if body.punched_at is None else timestamp_to_datetime(body.punched_at))
    # A punch-out may close a same-day or overnight attendance record.
    candidates = list(db.attendance_logs.find({
        "emp_code": body.emp_code,
        "punch_in": {"$lte": punch_out_time},
        "punch_out": None,
        "status": {"$in": list(PRESENT_STATUSES)},
    }).sort("punch_in", DESCENDING).limit(1))
    if not candidates:
        existing = db.attendance_logs.find_one({
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_time},
            "punch_out": {"$ne": None},
        })
        if existing:
            fail(409, "Attendance already punched out")
        fail(404, "No open punch-in found")
    record = candidates[0]
    punch_in_time = as_ist(record["punch_in"])
    if punch_out_time <= punch_in_time:
        fail(422, "punch_out must be after punch_in")
    if (punch_out_time - punch_in_time).total_seconds() > 24 * 3600:
        fail(422, "punch_out cannot be more than 24 hours after punch_in")
    emp = employee_or_404(body.emp_code)
    work_hours = compute_work_hours(punch_in_time, punch_out_time)
    overtime = compute_overtime(punch_out_time, emp["shift_start"], emp["shift_end"], record["date"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punch_out_time,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": work_hours < 4.50,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        fail(409, "Attendance was already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if emp_code:
        if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
            fail(404, "Employee not found")
        query["emp_code"] = emp_code
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        fail(422, "date_from must be on or before date_to")
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        if status not in ALL_STATUSES:
            fail(422, "Invalid status")
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query, {"_id": 0})
                .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip((page - 1) * page_size).limit(page_size))
    for doc in docs:
        doc.setdefault("history", [])
        doc.setdefault("half_day", False)
        doc.setdefault("late_minutes", 0)
        doc.setdefault("overtime_minutes", 0)
        for field in ("punch_in", "punch_out"):
            if isinstance(doc.get(field), datetime):
                doc[field] = epoch_ms(doc[field])
    return {"items": docs, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(emp_code: str, date_str: str, body: RegularizeIn):
    parse_date(date_str)
    employee_or_404(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        fail(404, "Attendance record not found")

    changes = {}
    if body.status is not None and body.status != record.get("status"):
        changes["status"] = body.status
    new_in = timestamp_to_datetime(body.punch_in) if body.punch_in is not None else record.get("punch_in")
    new_out = timestamp_to_datetime(body.punch_out) if body.punch_out is not None else record.get("punch_out")

    if body.punch_in is not None and new_in != record.get("punch_in"):
        changes["punch_in"] = new_in
    if body.punch_out is not None and new_out != record.get("punch_out"):
        changes["punch_out"] = new_out

    new_status = changes.get("status", record.get("status"))
    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            fail(422, "ABSENT and LEAVE records cannot include punch times")
        changes["punch_in"] = None
        changes["punch_out"] = None
        new_in = new_out = None
    else:
        if new_in is None:
            fail(422, "Present attendance requires punch_in")
        if attendance_date(new_in, employee_or_404(emp_code)["shift_start"],
                           employee_or_404(emp_code)["shift_end"]) != date_str:
            fail(422, "punch_in does not belong to this attendance date")
        if new_out is not None:
            if new_out <= new_in:
                fail(422, "punch_out must be after punch_in")
            if (new_out - new_in).total_seconds() > 24 * 3600:
                fail(422, "punch_out cannot be more than 24 hours after punch_in")

    if not changes:
        fail(422, "No changes supplied")

    emp = employee_or_404(emp_code)
    if new_status in PRESENT_STATUSES and new_in is not None:
        changes["late_minutes"] = compute_late_minutes(new_in, emp["shift_start"],date_str,)
        if new_out is not None:
            hours = compute_work_hours(new_in, new_out)
            changes["work_hours"] = hours
            changes["overtime_minutes"] = compute_overtime(
                new_out, emp["shift_start"], emp["shift_end"], date_str
            )
            changes["half_day"] = hours < 4.50
        else:
            changes["work_hours"] = None
            changes["overtime_minutes"] = 0
            changes["half_day"] = False
    else:
        changes.update({"work_hours": None, "late_minutes": 0,
                        "overtime_minutes": 0, "half_day": False})

    
    history_change = {}
    for key, new in changes.items():
       old = record.get(key)

       if old == new:
          continue

       if isinstance(old, datetime):
        old = epoch_ms(old)
       if isinstance(new, datetime):
        new = epoch_ms(new)

        history_change[key] = {"from": old, "to": new}


    history_item = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by.strip(),
        "reason": body.reason.strip(),
        "changes": history_change,
    }
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": record.get("history", [])},
        {"$set": changes, "$push": {"history": history_item}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        fail(409, "Attendance was changed by another request; retry")
    return serialize_attendance(result)


# ------------------------------- Aggregations ------------------------------- #

def employee_monthly_pipeline(emp_code: str, month_start: date, month_end: date):
    start, end = month_start.isoformat(), month_end.isoformat()

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start, "$lte": end},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                    {
                                        "$lte": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date",
                                                        "format": "%Y-%m-%d",
                                                    }
                                                }
                                            },
                                            6,
                                        ]
                                    },
                                    {
                                        "$gte": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date",
                                                        "format": "%Y-%m-%d",
                                                    }
                                                }
                                            },
                                            2,
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    return pipeline



@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = employee_or_404(emp_code)
    first, last = parse_month(month)
    working_days = sum(1 for n in range((last - first).days + 1)
                       if date_is_working_day(first + timedelta(days=n))
                       and (first + timedelta(days=n)).isoformat() >= emp["joined_on"])
    result = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last)))
    stats = result[0] if result else {}
    present_days = int(stats.get("present_days", 0))
    attendance_pct = None if working_days == 0 else round_half_up(present_days / working_days * 100, 2)
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": present_days, "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


def department_summary_pipeline(first: date, last: date, department: Optional[str] = None):
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
        {"$match": {"employee.joined_on": {"$lte": last.isoformat()}}},
    ]

    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$group": {
                "_id": "$employee.department",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                    {"$lte": [
                                        {"$dayOfWeek": {"$dateFromString": {
                                            "dateString": "$date",
                                            "format": "%Y-%m-%d",
                                        }}},
                                        6,
                                    ]},
                                    {"$gte": [
                                        {"$dayOfWeek": {"$dateFromString": {
                                            "dateString": "$date",
                                            "format": "%Y-%m-%d",
                                        }}},
                                        2,
                                    ]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "hours_sum": {"$sum": {"$cond": [
                    {"$and": [
                        {"$in": ["$status", list(PRESENT_STATUSES)]},
                        {"$ne": ["$work_hours", None]},
                    ]},
                    "$work_hours",
                    0,
                ]}},
                "hours_count": {"$sum": {"$cond": [
                    {"$and": [
                        {"$in": ["$status", list(PRESENT_STATUSES)]},
                        {"$ne": ["$work_hours", None]},
                    ]},
                    1,
                    0,
                ]}},
                "late_count": {"$sum": {"$cond": [
                    {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                    1,
                    0,
                ]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [
                    {"$eq": ["$status", "LEAVE"]}, 1, 0,
                ]}},
                "on_duty_count": {"$sum": {"$cond": [
                    {"$eq": ["$status", "ON_DUTY"]}, 1, 0,
                ]}},
            }
        }
    ])

    return pipeline


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first, last = parse_month(month)

    match = {"joined_on": {"$lte": last.isoformat()}}
    if department:
        match["department"] = department

    employees = list(
        db.employees.find(
            match,
            {"_id": 0, "emp_code": 1, "department": 1},
        )
    )

    if department and not employees:
        if db.employees.count_documents({"department": department}) == 0:
            fail(404, "Department not found")

    grouped = {}

    for emp in employees:
        grouped.setdefault(
            emp["department"],
            {
                "department": emp["department"],
                "headcount": 0,
                "present_days": 0,
                "hours_sum": 0.0,
                "hours_count": 0,
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            },
        )
        grouped[emp["department"]]["headcount"] += 1

    pipeline = department_summary_pipeline(first, last, department)

    for row in db.attendance_logs.aggregate(pipeline):
        key = row["_id"]

        if key not in grouped:
            continue

        grouped[key].update({
            k: row.get(k, 0)
            for k in (
                "present_days",
                "hours_sum",
                "hours_count",
                "late_count",
                "total_late_minutes",
                "leave_count",
                "on_duty_count",
            )
        })

    output = []

    for key in sorted(grouped):
        row = grouped[key]

        output.append({
            "department": key,
            "headcount": row["headcount"],
            "present_days": row["present_days"],
            "avg_work_hours": (
                None
                if row["hours_count"] == 0
                else round_half_up(
                    row["hours_sum"] / row["hours_count"], 2
                )
            ),
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })

    return {"month": month, "items": output}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = parse_month(month)

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": first.isoformat(),
                    "$lte": last.isoformat(),
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$facet": {
                "selected": [
                    {"$limit": limit},
                    {"$group": {"_id": None, "cutoff_rank": {"$max": "$rank"}}},
                ],
                "all_rows": [
                    {"$sort": {"total_late_minutes": -1, "_id": 1}},
                ],
            }
        },
        {
            "$project": {
                "rows": {
                    "$let": {
                        "vars": {
                            "cutoff": {
                                "$ifNull": [
                                    {"$arrayElemAt": ["$selected.cutoff_rank", 0]},
                                    0,
                                ]
                            }
                        },
                        "in": {
                            "$filter": {
                                "input": "$all_rows",
                                "as": "row",
                                "cond": {"$lte": ["$$row.rank", "$$cutoff"]},
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "rank": 1,
            }
        },
        {"$sort": {"rank": 1, "emp_code": 1}},
    ])

    rows = list(db.attendance_logs.aggregate(pipeline))

    return {"month": month, "items": rows}




def department_trend_pipeline(department, start, end):
    start_datetime = datetime.combine(
        start, time.min, tzinfo=timezone.utc
    )
    day_count = (end - start).days + 1

    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "employees": 1,
                "dates": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "date": {
                                    "$dateAdd": {
                                        "startDate": start_datetime,
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                                "format": "%Y-%m-%d",
                                "timezone": "UTC",
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$dates"},
        {
            "$set": {
                "active_employees": {
                    "$filter": {
                        "input": "$employees",
                        "as": "emp",
                        "cond": {"$lte": ["$$emp.joined_on", "$dates"]},
                    }
                }
            }
        },
        {
            "$set": {
                "headcount": {"$size": "$active_employees"},
                "active_codes": {
                    "$map": {
                        "input": "$active_employees",
                        "as": "emp",
                        "in": "$$emp.emp_code",
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$dates", "codes": "$active_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "status": 1}},
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$in": [
                                    "$$log.status",
                                    list(PRESENT_STATUSES),
                                ]
                            },
                        }
                    }
                },
                "day_of_week": {
                    "$isoDayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$dates",
                            "format": "%Y-%m-%d",
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$lte": ["$day_of_week", 5]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {"$sort": {"dates": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"dates": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$dates",
                "headcount": 1,
                "present_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str, to_date: str):
    start = parse_date(from_date, "from_date")
    end = parse_date(to_date, "to_date")

    if start > end:
        fail(422, "from_date must be on or before to_date")

    if (end - start).days + 1 > 92:
        fail(422, "Date range cannot exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        fail(404, "Department not found")

    pipeline = department_trend_pipeline(department, start, end)

    try:
        rows = list(db.employees.aggregate(pipeline))
    except PyMongoError:
        logger.exception("Department trend aggregation failed")
        fail(503, "Could not calculate department trend")

    return {
        "department": department,
        "from_date": from_date,
        "to_date": to_date,
        "items": rows,
    }

# --------------------------------- Explain ---------------------------------- #

EXPLAIN_CONFIG = {
    "attendance_list": "attendance_logs",
    "employee_monthly": "attendance_logs",
    "department_summary": "attendance_logs",
    "late_leaderboard": "attendance_logs",
    "department_trend": "employees",
}



@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint not in EXPLAIN_CONFIG:
        fail(422, "Unknown endpoint name")

    collection_name = EXPLAIN_CONFIG[endpoint]

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            fail(422, "emp_code and month are required")

        employee_or_404(emp_code)
        first, last = parse_month(month)
        pipeline = employee_monthly_pipeline(emp_code, first, last)

    elif endpoint == "department_summary":
        if not month:
            fail(422, "month is required")

        first, last = parse_month(month)
        pipeline = department_summary_pipeline(first, last, department)
        collection_name = "attendance_logs"

    elif endpoint == "late_leaderboard":
        if not month:
            fail(422, "month is required")

        first, last = parse_month(month)

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": first.isoformat(),
                        "$lte": last.isoformat(),
                    }
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]

        if department:
            pipeline.append({"$match": {"employee.department": department}})

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$facet": {
                    "selected": [
                        {"$limit": limit},
                        {
                            "$group": {
                                "_id": None,
                                "cutoff_rank": {"$max": "$rank"},
                            }
                        },
                    ],
                    "all_rows": [
                        {"$sort": {"total_late_minutes": -1, "_id": 1}},
                    ],
                }
            },
            {
                "$project": {
                    "rows": {
                        "$let": {
                            "vars": {
                                "cutoff": {
                                    "$ifNull": [
                                        {
                                            "$arrayElemAt": [
                                                "$selected.cutoff_rank", 0
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "in": {
                                "$filter": {
                                    "input": "$all_rows",
                                    "as": "row",
                                    "cond": {
                                        "$lte": ["$$row.rank", "$$cutoff"]
                                    },
                                }
                            },
                        }
                    }
                }
            },
            {"$unwind": "$rows"},
            {"$replaceRoot": {"newRoot": "$rows"}},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "rank": 1,
                }
            },
            {"$sort": {"rank": 1, "emp_code": 1}},
        ])
        collection_name = "attendance_logs"

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            fail(422, "department, from_date and to_date are required")

        start = parse_date(from_date, "from_date")
        end = parse_date(to_date, "to_date")

        if start > end:
            fail(422, "from_date must be on or before to_date")

        if (end - start).days + 1 > 92:
            fail(422, "Date range cannot exceed 92 days")

        if db.employees.count_documents({"department": department}) == 0:
            fail(404, "Department not found")

        pipeline = department_trend_pipeline(department, start, end)
        collection_name = "employees"

    elif endpoint == "attendance_list":
        query = {}

        if emp_code:
            if db.employees.find_one(
                {"emp_code": emp_code}, {"_id": 1}
            ) is None:
                fail(404, "Employee not found")
            query["emp_code"] = emp_code

        if date_from:
            parse_date(date_from, "date_from")

        if date_to:
            parse_date(date_to, "date_to")

        if date_from and date_to and date_from > date_to:
            fail(422, "date_from must be on or before date_to")

        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to

        if status:
            if status not in ALL_STATUSES:
                fail(422, "Invalid status")
            query["status"] = status

        pipeline = [
            {"$match": query},
            {"$sort": {"date": -1, "emp_code": 1}},
            {"$skip": (page - 1) * page_size},
            {"$limit": page_size},
        ]
        collection_name = "attendance_logs"

    else:
        fail(422, "This explain endpoint is not implemented yet")

    try:
        result = db.command({
            "explain": {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return json.loads(json_util.dumps(result))
    except PyMongoError:
        logger.exception("Explain failed")
        fail(503, "Could not explain query")
