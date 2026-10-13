# REVIEW.md

## Starter Code Defect Review

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health()` | Returns success without checking MongoDB connectivity. | Stop or misconfigure MongoDB; `/health` still returns 200. | Ping MongoDB and return 503 when unavailable. |
| 2 | `create_employee()` | Duplicate checking before insertion is vulnerable to concurrent requests. | Send two simultaneous requests with the same `emp_code`. | Add a unique index and handle duplicate-key errors as 409. |
| 3 | `create_employee()` | `datetime.now()` creates a naive timestamp. | Inspect `created_at` timezone handling. | Use timezone-aware UTC timestamps. |
| 4 | `list_employees()` | Pagination skips too many records because it uses `page * page_size`. | Request page 1; the first page of employees is skipped. | Use `(page - 1) * page_size`. |
| 5 | `list_employees()` | `total` ignores the department filter. | Filter by a department and compare `total` with matching employees. | Count documents using the same filter query. |
| 6 | `list_employees()` | Page and page size have no validation. | Try page 0 or a negative page size. | Add minimum and maximum query constraints. |
| 7 | `punch_in()` | Employee lookup result is not checked before accessing shift fields. | Punch in with an unknown employee code. | Return 404 if the employee does not exist. |
| 8 | `punch_in()` | Timestamp conversion uses the server's local timezone and treats timestamp 0 as missing. | Submit epoch milliseconds and compare results across timezones; submit `punched_at=0`. | Validate timestamps and convert them explicitly to the required timezone. |
| 9 | `punch_in()` | Duplicate checking and insertion are separate, non-atomic operations. | Send concurrent punch-in requests for the same employee and date. | Enforce uniqueness on `(emp_code, date)` and handle duplicate-key errors. |
| 10 | `compute_late_minutes()` | Assumes the punch-in date is the shift date and does not handle overnight shifts. | Test a night shift whose punch-in occurs after midnight. | Calculate the correct shift-start date before calculating lateness. |
| 11 | `compute_late_minutes()` | Lateness is calculated from the punch-in timestamp's date without an explicit grace-boundary policy. | Test timestamps immediately around the 10-minute grace boundary. | Implement the specified grace-period and minute-rounding rules explicitly. |
| 12 | `compute_work_hours()` | Python's built-in `round()` does not guarantee the required half-up rounding policy. | Test durations near a rounding midpoint. | Use explicit decimal half-up rounding. |
| 13 | `compute_overtime()` | Does not handle overnight shifts and does not enforce a minimum overtime threshold. | Test an overnight shift or a punch-out shortly after shift end. | Calculate the correct shift end and apply the specified overtime threshold. |
| 14 | `list_attendance()` | Loads all matching attendance records into Python before sorting and paginating. | Increase the dataset and observe memory use and response time. | Apply filters, sorting, skip, and limit in MongoDB. |
| 15 | `list_attendance()` | Does not validate dates, status, page, or page size. | Submit malformed dates, an invalid status, or invalid pagination values. | Validate query parameters and return 422 for invalid input. |
| 16 | `list_attendance()` | Returns MongoDB documents without normalizing timestamps and default fields. | Inspect records with missing optional fields or BSON datetimes. | Normalize response fields and serialize timestamps consistently. |
| 17 | Starter file overall | Required punch-out, regularization, analytics, and explain endpoints are absent. | Request the required paths from the API contract. | Implement the missing endpoints and their specified behavior. |

## Reviewed but Not Considered Defects

- The default shift values in `EmployeeIn` are not inherently defective; they are valid defaults if permitted by the API contract.
- Using MongoDB's `count_documents()` is appropriate for obtaining a matching document count. The defect in `list_employees()` is that it counts without applying the active filter.
- Returning HTTP 201 for a successfully created employee or punch-in is appropriate for resource creation.
- Loading a local `.env` file is a convenience, not a defect, provided secrets are not committed to the submission.
