# DECISIONS.md

### 1. Indexes
I created a unique index on `employees.emp_code` to prevent duplicate employee codes. A unique compound index on `attendance_logs.emp_code` and `date` prevents duplicate daily attendance records. Other indexes support attendance queries by date and employee, employee history, status filtering, and department-based employee lookups. I considered adding more indexes for every filter combination, but rejected that because extra indexes increase storage and write costs.

### 2. Punch-in race
If two identical punch-in requests arrive simultaneously, both may initially find no existing record. MongoDB's unique compound index on `emp_code` and `date` allows only one insert to succeed. The other request receives a duplicate-key error, which the API converts to HTTP 409 Conflict.

### 3. Ties
The leaderboard ranks employees by total late minutes. Employees tied at the cutoff share the same rank, and all employees with that rank are returned, even if the result exceeds the requested limit. This avoids arbitrarily excluding employees with equal results.

### 4. Headcount
The department summary first selects employees who joined by the end of the month and counts them by department. It then aggregates attendance logs and merges the results, so employees with no logs are still included.

### 5. Scaling to 100x data
I would review query plans using realistic production-sized data, optimize slow aggregations, and add appropriate indexes based on measured query performance.
