# Grist REST API Quick Guide

## Setup
- **Base URL:** `https://docs.getgrist.com/api/docs/{DOC_ID}`
- **Auth Header:** `Authorization: Bearer {YOUR_API_KEY}`

---

## Common Operations

### List Tables
```bash
curl -s -H "Authorization: Bearer $API_KEY" "$BASE_URL/tables"
```

### Read Records
```bash
curl -s -H "Authorization: Bearer $API_KEY" "$BASE_URL/tables/{TABLE_NAME}/records"
```

### Add Record
```bash
curl -s -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" "$BASE_URL/tables/{TABLE_NAME}/records" -d '{
  "records": [{"fields": {"Title": "My Project", "Description": "Details"}}]
}'
```

### Delete Record ⚠️
**Important:** The `/records/delete` endpoint doesn't work reliably. Use `/apply` instead.

```bash
curl -s -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" "$BASE_URL/apply" -d '[["BulkRemoveRecord", "TABLE_NAME", [ROW_ID]]]'
```

Example to delete record ID 11 from Project table:
```bash
curl -s -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" "$BASE_URL/apply" -d '[["BulkRemoveRecord", "Project", [11]]]'
```

---

## SQL Endpoint (Advanced)

Grist supports a SQL endpoint for complex queries with JOINs. This is more efficient when you need data from multiple related tables.

### Endpoint
```
POST /api/docs/{DOC_ID}/sql
```

### Basic Query
```bash
curl -s -X POST -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  "$BASE_URL/sql" \
  -d '{"sql": "SELECT * FROM Commitments WHERE Title = \"Lab of Thought\""}'
```

### Query with JOIN
Get log entries with their linked project names:
```bash
curl -s -X POST -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  "$BASE_URL/sql" \
  -d '{
    "sql": "SELECT l.Content, l.EffectiveDate, p.Title as LinkedProject 
            FROM LogEntries l 
            LEFT JOIN Project p ON l.Target_Project = p.id 
            WHERE l.Target_Commitment = 2 OR l.Target_Project IN (1,2)"
  }'
```

**What this JOIN does:**
- Links `LogEntries.Target_Project` to `Project.id`
- Returns the project title instead of just the ID number
- Filters to either commitment-linked or project-linked entries

### JOIN Syntax
- Use `LEFT JOIN` or `INNER JOIN`
- `ON` clause links foreign key to primary key
- Example: `ON l.Target_Project = p.id`

### Useful Queries for Commitment Summary

**Get all projects for a commitment:**
```sql
SELECT p.Title, p.Description, p.Updated_At 
FROM Project p WHERE p.Commitment = 2
```

**Get all tasks for a commitment:**
```sql
SELECT t.CONTENT, t.PRIORITY FROM Tasks t WHERE t.Commitment = 2
```

**Get log entries with project names (JOIN):**
```sql
SELECT l.Content, l.EffectiveDate, p.Title as LinkedProject 
FROM LogEntries l LEFT JOIN Project p ON l.Target_Project = p.id 
WHERE l.Target_Commitment = 2 OR l.Target_Project IN (1,2)
```

---

## Document Info
- **Tables:** Tasks, Project, Commitments, LogEntries
- **Key Relationships:**
  - Project.Commitment → Commitments.id (many-to-one)
  - Tasks.Commitment → Commitments.id (many-to-one)
  - LogEntries.Target_Project → Project.id (many-to-one)
  - LogEntries.Target_Commitment → Commitments.id (many-to-one)