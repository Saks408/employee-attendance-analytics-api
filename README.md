# Employee Attendance & Analytics API

A FastAPI application for employee management, attendance tracking, attendance regularization, and analytics using MongoDB.

## Requirements

- Python 3.10+
- MongoDB 6.0 or newer
- pip

## Setup

Create and activate a virtual environment.

**Windows PowerShell:**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create a `.env` file locally using `.env.example` as a reference:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Do not commit `.env` or credentials to Git.

## Load Sample Data

If sample data is provided, run:

```bash
python sample_seed.py
```

## Run the Application

From the project root:

```bash
uvicorn app.main:app --port 8000
```

For development with automatic reload:

```bash
uvicorn app.main:app --port 8000 --reload
```

## API Documentation

Once the server is running, open:

- Swagger UI: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

## Submission

The submission includes `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this README.

Do not commit environment files, secrets, virtual environments, or database credentials.