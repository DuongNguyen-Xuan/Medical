"""Local PostgreSQL table browser with JSON and CSV endpoints for Power BI."""

import csv
import io
import json
import os
import re
from datetime import date, datetime, time
from decimal import Decimal

import psycopg
from psycopg import sql
from psycopg.types.json import Json, Jsonb
from flask import Flask, Response, jsonify, redirect, render_template_string, request


app = Flask(__name__)

RAW_DATABASE_ENDPOINTS = {
    "list_tables",
    "list_columns",
    "get_table_info",
    "get_row",
    "insert_row",
    "update_row",
    "delete_row",
    "table_data",
    "powerbi_data",
    "export_csv",
}


@app.before_request
def block_unscoped_database_browser():
    if request.endpoint in RAW_DATABASE_ENDPOINTS:
        return jsonify({"error": "Trình duyệt dữ liệu thô đã bị khóa; hãy dùng dashboard y tế."}), 403
    return None


@app.after_request
def prevent_medical_data_caching(response):
    if request.path.startswith("/api/medical/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


DB_CONFIG = {
    "host": os.getenv("PGHOST", "localhost"),
    "port": int(os.getenv("PGPORT", "5432")),
    "dbname": os.getenv("PGDATABASE", "K12_Staging_Local_Restore"),
    "user": os.getenv("PGUSER", "postgres"),
    "password": os.getenv("PGPASSWORD", ""),
}


def connect_db():
    return psycopg.connect(**DB_CONFIG, connect_timeout=5)


def json_value(value):
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return value


def mask_contact_text(value):
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "***", text, flags=re.IGNORECASE)
    return re.sub(r"(?<!\d)(?:\+?84|0)(?:[\s().-]*\d){8,10}(?!\d)", "***", text)


def requested_table():
    schema = request.args.get("schema", "").strip()
    table = request.args.get("table", "").strip()
    if not schema or not table:
        return None, None, (jsonify({"error": "Cần truyền schema và table."}), 400)
    return schema, table, None


def table_exists(conn, schema, table):
    row = conn.execute(
        """SELECT EXISTS (
               SELECT 1 FROM information_schema.tables
               WHERE table_schema = %s AND table_name = %s
                 AND table_type IN ('BASE TABLE', 'VIEW', 'FOREIGN')
           )""",
        (schema, table),
    ).fetchone()
    return row[0]


@app.get("/api/tables")
def list_tables():
    search = request.args.get("q", "").strip()
    pattern = f"%{search}%"
    try:
        with connect_db() as conn:
            rows = conn.execute(
                """SELECT n.nspname AS schema, c.relname AS name,
                          CASE c.relkind WHEN 'r' THEN 'table'
                                         WHEN 'p' THEN 'partitioned table'
                                         WHEN 'v' THEN 'view' ELSE c.relkind::text END AS kind,
                          GREATEST(c.reltuples, 0)::bigint AS estimated_rows
                   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE c.relkind IN ('r', 'p', 'v')
                     AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                     AND (%s = '%%' OR n.nspname ILIKE %s OR c.relname ILIKE %s)
                   ORDER BY n.nspname, c.relname""",
                (pattern, pattern, pattern),
            ).fetchall()
        return jsonify([
            {"schema": r[0], "table": r[1], "kind": r[2], "estimated_rows": r[3]}
            for r in rows
        ])
    except psycopg.Error as exc:
        return jsonify({"error": f"Không kết nối được PostgreSQL: {exc}"}), 503


@app.get("/api/columns")
def list_columns():
    schema, table, error = requested_table()
    if error:
        return error
    try:
        with connect_db() as conn:
            rows = conn.execute(
                """SELECT column_name, data_type, is_nullable
                   FROM information_schema.columns
                   WHERE table_schema = %s AND table_name = %s
                   ORDER BY ordinal_position""",
                (schema, table),
            ).fetchall()
        if not rows:
            return jsonify({"error": "Không tìm thấy bảng."}), 404
        return jsonify([{"name": r[0], "type": r[1], "nullable": r[2] == "YES"} for r in rows])
    except psycopg.Error as exc:
        return jsonify({"error": str(exc)}), 503


def table_info(conn, schema, table):
    if schema in ("pg_catalog", "information_schema") or schema.startswith("pg_"):
        return None
    relation = conn.execute(
        """SELECT c.oid, c.relkind FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = %s AND c.relname = %s""",
        (schema, table),
    ).fetchone()
    if not relation or relation[1] not in ("r", "p"):
        return None
    pk = conn.execute(
        """SELECT a.attname
           FROM pg_index i
           CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, position)
           JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
           WHERE i.indrelid = %s AND i.indisprimary
             AND k.position <= i.indnkeyatts
           ORDER BY k.position""",
        (relation[0],),
    ).fetchall()
    columns = conn.execute(
        """SELECT column_name, data_type, is_nullable, column_default,
                  is_identity, is_generated
           FROM information_schema.columns
           WHERE table_schema = %s AND table_name = %s
           ORDER BY ordinal_position""",
        (schema, table),
    ).fetchall()
    if not columns:
        return None
    primary_key = [r[0] for r in pk]
    details = []
    for name, data_type, nullable, default, identity, generated in columns:
        can_write = generated == "NEVER" and identity == "NO"
        can_insert = can_write and not (name in primary_key and default is not None)
        details.append({
            "name": name, "type": data_type, "nullable": nullable == "YES",
            "has_default": default is not None, "primary_key": name in primary_key,
            "insertable": can_insert,
            "updatable": can_write and name not in primary_key,
        })
    return {"columns": details, "primary_key": primary_key}


def adapt_input(value, data_type):
    if value == "":
        return None
    if data_type == "jsonb":
        return Jsonb(json.loads(value) if isinstance(value, str) else value)
    if data_type == "json":
        return Json(json.loads(value) if isinstance(value, str) else value)
    if (data_type.endswith("[]") or data_type == "ARRAY") and isinstance(value, str):
        return json.loads(value)
    if data_type == "bytea" and isinstance(value, str):
        return bytes.fromhex(value)
    return value


def body_table_info(body):
    schema = str(body.get("schema", "")).strip()
    table = str(body.get("table", "")).strip()
    values = body.get("values", {})
    if not schema or not table or not isinstance(values, dict):
        return None, None, None, (jsonify({"error": "Thiếu schema, table hoặc values không hợp lệ."}), 400)
    return schema, table, values, None


@app.get("/api/table-info")
def get_table_info():
    schema, table, error = requested_table()
    if error:
        return error
    try:
        with connect_db() as conn:
            info = table_info(conn, schema, table)
        if not info:
            return jsonify({"error": "Không tìm thấy bảng có thể chỉnh sửa."}), 404
        return jsonify(info)
    except psycopg.Error as exc:
        return jsonify({"error": str(exc)}), 503


@app.get("/api/row")
def get_row():
    schema, table, error = requested_table()
    if error:
        return error
    try:
        key = json.loads(request.args.get("key", "{}"))
        if not isinstance(key, dict) or not key:
            return jsonify({"error": "Cần truyền khóa chính của bản ghi."}), 400
        with connect_db() as conn:
            info = table_info(conn, schema, table)
            if not info or not info["primary_key"] or set(key) != set(info["primary_key"]):
                return jsonify({"error": "Bảng không có khóa chính phù hợp."}), 400
            where = sql.SQL(" AND ").join(
                sql.SQL("{} = %s").format(sql.Identifier(name)) for name in info["primary_key"]
            )
            query = sql.SQL("SELECT * FROM {}.{} WHERE {} LIMIT 1").format(
                sql.Identifier(schema), sql.Identifier(table), where
            )
            row = conn.execute(query, [key[name] for name in info["primary_key"]]).fetchone()
            cols = conn.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position""",
                (schema, table),
            ).fetchall()
        if row is None:
            return jsonify({"error": "Không tìm thấy bản ghi."}), 404
        return jsonify({name[0]: json_value(value) for name, value in zip(cols, row)})
    except (ValueError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/rows")
def insert_row():
    body = request.get_json(silent=True) or {}
    schema, table, values, error = body_table_info(body)
    if error:
        return error
    try:
        with connect_db() as conn:
            info = table_info(conn, schema, table)
            if not info:
                return jsonify({"error": "Chỉ có thể ghi vào bảng PostgreSQL thông thường."}), 400
            allowed = {c["name"]: c for c in info["columns"] if c["insertable"] or (c["primary_key"] and c["updatable"])}
            unknown = set(values) - set(allowed)
            if unknown:
                return jsonify({"error": "Cột không thể ghi: " + ", ".join(sorted(unknown))}), 400
            if values:
                names = list(values)
                query = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({}) RETURNING 1").format(
                    sql.Identifier(schema), sql.Identifier(table),
                    sql.SQL(", ").join(map(sql.Identifier, names)),
                    sql.SQL(", ").join(sql.Placeholder() for _ in names),
                )
                params = [adapt_input(values[n], allowed[n]["type"]) for n in names]
            else:
                query = sql.SQL("INSERT INTO {}.{} DEFAULT VALUES RETURNING 1").format(
                    sql.Identifier(schema), sql.Identifier(table)
                )
                params = []
            conn.execute(query, params).fetchone()
            conn.commit()
        return jsonify({"message": "Đã thêm bản ghi."}), 201
    except (ValueError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 400


@app.put("/api/rows")
def update_row():
    body = request.get_json(silent=True) or {}
    schema, table, values, error = body_table_info(body)
    if error:
        return error
    key = body.get("key")
    if not isinstance(key, dict) or not key:
        return jsonify({"error": "Thiếu khóa chính của bản ghi."}), 400
    try:
        with connect_db() as conn:
            info = table_info(conn, schema, table)
            if not info or not info["primary_key"] or set(key) != set(info["primary_key"]):
                return jsonify({"error": "Bảng cần có khóa chính hợp lệ để cập nhật."}), 400
            allowed = {c["name"]: c for c in info["columns"] if c["updatable"]}
            unknown = set(values) - set(allowed)
            if unknown:
                return jsonify({"error": "Cột không thể cập nhật: " + ", ".join(sorted(unknown))}), 400
            if not values:
                return jsonify({"error": "Chưa có cột nào để cập nhật."}), 400
            set_clause = sql.SQL(", ").join(
                sql.SQL("{} = %s").format(sql.Identifier(name)) for name in values
            )
            where = sql.SQL(" AND ").join(
                sql.SQL("{} = %s").format(sql.Identifier(name)) for name in info["primary_key"]
            )
            query = sql.SQL("UPDATE {}.{} SET {} WHERE {} RETURNING 1").format(
                sql.Identifier(schema), sql.Identifier(table), set_clause, where
            )
            params = [adapt_input(values[n], allowed[n]["type"]) for n in values]
            params.extend(key[n] for n in info["primary_key"])
            changed = conn.execute(query, params).fetchone()
            conn.commit()
        if not changed:
            return jsonify({"error": "Không tìm thấy bản ghi để cập nhật."}), 404
        return jsonify({"message": "Đã cập nhật bản ghi."})
    except (ValueError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 400


@app.delete("/api/rows")
def delete_row():
    body = request.get_json(silent=True) or {}
    schema = str(body.get("schema", "")).strip()
    table = str(body.get("table", "")).strip()
    key = body.get("key")
    if not schema or not table or not isinstance(key, dict) or not key:
        return jsonify({"error": "Thiếu schema, table hoặc khóa chính."}), 400
    try:
        with connect_db() as conn:
            info = table_info(conn, schema, table)
            if not info or not info["primary_key"] or set(key) != set(info["primary_key"]):
                return jsonify({"error": "Bảng cần có khóa chính hợp lệ để xóa."}), 400
            where = sql.SQL(" AND ").join(
                sql.SQL("{} = %s").format(sql.Identifier(name)) for name in info["primary_key"]
            )
            query = sql.SQL("DELETE FROM {}.{} WHERE {} RETURNING 1").format(
                sql.Identifier(schema), sql.Identifier(table), where
            )
            deleted = conn.execute(query, [key[n] for n in info["primary_key"]]).fetchone()
            conn.commit()
        if not deleted:
            return jsonify({"error": "Không tìm thấy bản ghi để xóa."}), 404
        return jsonify({"message": "Đã xóa bản ghi."})
    except psycopg.Error as exc:
        return jsonify({"error": str(exc)}), 400


def fetch_rows(schema, table, limit, offset):
    with connect_db() as conn:
        if not table_exists(conn, schema, table):
            return None, None
        query = sql.SQL("SELECT * FROM {}.{} LIMIT %s OFFSET %s").format(
            sql.Identifier(schema), sql.Identifier(table)
        )
        with conn.cursor() as cur:
            cur.execute(query, (limit, offset))
            headers = [desc.name for desc in cur.description]
            rows = cur.fetchall()
        return headers, rows


@app.get("/api/data")
def table_data():
    schema, table, error = requested_table()
    if error:
        return error
    try:
        limit = min(max(int(request.args.get("limit", "100")), 1), 1000)
        offset = max(int(request.args.get("offset", "0")), 0)
        headers, rows = fetch_rows(schema, table, limit, offset)
        if headers is None:
            return jsonify({"error": "Không tìm thấy bảng."}), 404
        return jsonify({
            "schema": schema,
            "table": table,
            "limit": limit,
            "offset": offset,
            "columns": headers,
            "rows": [[json_value(v) for v in row] for row in rows],
        })
    except (ValueError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/powerbi")
def powerbi_data():
    """Return records as a JSON array that Power BI's Web connector can import."""
    schema, table, error = requested_table()
    if error:
        return error
    try:
        limit = min(max(int(request.args.get("limit", "1000")), 1), 100000)
        offset = max(int(request.args.get("offset", "0")), 0)
        headers, rows = fetch_rows(schema, table, limit, offset)
        if headers is None:
            return jsonify({"error": "Không tìm thấy bảng."}), 404
        return jsonify([
            {name: json_value(value) for name, value in zip(headers, row)}
            for row in rows
        ])
    except (ValueError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/dashboard")
def dashboard_home():
    return render_template_string(DASHBOARD_HOME, roles=MEDICAL_ROLES)


@app.get("/api/medical/dashboard")
def medical_dashboard_data():
    start_value = request.args.get("from", "").strip()
    end_value = request.args.get("to", "").strip()
    school_value = request.args.get("school_id", "").strip()
    try:
        start_date = date.fromisoformat(start_value) if start_value else None
        end_date = date.fromisoformat(end_value) if end_value else None
        school_id = int(school_value) if school_value else None
        if school_id is not None and school_id < 1:
            raise ValueError("school_id phải là số nguyên dương.")
        if start_date and end_date and start_date > end_date:
            raise ValueError("Ngày bắt đầu không được sau ngày kết thúc.")
    except ValueError as exc:
        return jsonify({"error": f"Tham số không hợp lệ: {exc}"}), 400

    school_filter = school_id
    start_filter = start_date
    end_filter = end_date
    try:
        with connect_db() as conn:
            schools = conn.execute(
                """SELECT id, name FROM public.school
                   WHERE deleted_at IS NULL
                   ORDER BY name, id"""
            ).fetchall()

            student_profiles = conn.execute(
                """SELECT COUNT(*)
                   FROM public.profile_student p
                   WHERE p.deleted_at IS NULL
                     AND (%s::integer IS NULL OR p.school_id = %s)""",
                (school_filter, school_filter),
            ).fetchone()[0]

            health = conn.execute(
                """SELECT COUNT(*), COUNT(DISTINCT user_id),
                          COUNT(*) FILTER (WHERE NULLIF(BTRIM(di_ung), '') IS NOT NULL),
                          COUNT(*) FILTER (WHERE NULLIF(BTRIM(current_disease), '') IS NOT NULL)
                   FROM public.medical_student_health_records h
                   WHERE h.deleted_at IS NULL
                     AND (%s::integer IS NULL OR h.school_id = %s)
                     AND (%s::date IS NULL OR h.created_at::date >= %s)
                     AND (%s::date IS NULL OR h.created_at::date <= %s)""",
                (school_filter, school_filter, start_filter, start_filter, end_filter, end_filter),
            ).fetchone()

            quick = conn.execute(
                """SELECT COUNT(*), COUNT(DISTINCT user_id),
                          COUNT(*) FILTER (WHERE status_send_to_parent::text = 'DA_GUI_CHO_PH'),
                          COUNT(*) FILTER (WHERE status_send_to_parent::text = 'CHO_GVCN_XAC_NHAN')
                   FROM public.medical_quick_examinations q
                   WHERE q.deleted_at IS NULL
                     AND (%s::integer IS NULL OR q.school_id = %s)
                     AND (%s::date IS NULL OR q.day::date >= %s)
                     AND (%s::date IS NULL OR q.day::date <= %s)""",
                (school_filter, school_filter, start_filter, start_filter, end_filter, end_filter),
            ).fetchone()

            exams = conn.execute(
                """SELECT COUNT(*), COUNT(DISTINCT user_id),
                          AVG(bmi) FILTER (WHERE bmi > 0),
                          COUNT(*) FILTER (WHERE NULLIF(BTRIM(z_score_bmi_conclude), '') IS NOT NULL)
                   FROM public.medical_examination_generals e
                   WHERE e.deleted_at IS NULL
                     AND (%s::integer IS NULL OR e.school_id = %s)
                     AND (%s::date IS NULL OR e.examination_date::date >= %s)
                     AND (%s::date IS NULL OR e.examination_date::date <= %s)""",
                (school_filter, school_filter, start_filter, start_filter, end_filter, end_filter),
            ).fetchone()

            bhyt = conn.execute(
                """SELECT COUNT(*), COUNT(DISTINCT user_id),
                          COUNT(DISTINCT user_id) FILTER (WHERE ngay_het_han::date >= CURRENT_DATE),
                          COUNT(*) FILTER (WHERE ngay_het_han::date >= CURRENT_DATE
                                             AND ngay_het_han::date < CURRENT_DATE + 30),
                          COUNT(*) FILTER (WHERE ngay_het_han::date < CURRENT_DATE)
                   FROM public.medical_manager_bhyts b
                   WHERE b.deleted_at IS NULL
                     AND (%s::integer IS NULL OR b.school_id = %s)
                     AND (%s::date IS NULL OR COALESCE(b.ngay_hieu_luc, b.created_at)::date >= %s)
                     AND (%s::date IS NULL OR COALESCE(b.ngay_hieu_luc, b.created_at)::date <= %s)""",
                (school_filter, school_filter, start_filter, start_filter, end_filter, end_filter),
            ).fetchone()

            vaccination = conn.execute(
                """SELECT COUNT(*),
                          (SELECT COUNT(*)
                           FROM public.medical_vaccination_records r
                           JOIN public.medical_vaccination_events re
                             ON re.id = r.medical_vaccination_event_id
                            AND re.deleted_at IS NULL
                           WHERE r.deleted_at IS NULL
                             AND (%s::integer IS NULL OR r.school_id = %s)
                             AND (%s::date IS NULL OR re.date::date >= %s)
                             AND (%s::date IS NULL OR re.date::date <= %s)),
                          (SELECT COUNT(DISTINCT r.user_id)
                           FROM public.medical_vaccination_records r
                           JOIN public.medical_vaccination_events re
                             ON re.id = r.medical_vaccination_event_id
                            AND re.deleted_at IS NULL
                           WHERE r.deleted_at IS NULL
                             AND (%s::integer IS NULL OR r.school_id = %s)
                             AND (%s::date IS NULL OR re.date::date >= %s)
                             AND (%s::date IS NULL OR re.date::date <= %s))
                   FROM public.medical_vaccination_events v
                   WHERE v.deleted_at IS NULL
                     AND (%s::integer IS NULL OR v.school_id = %s)
                     AND (%s::date IS NULL OR v.date::date >= %s)
                     AND (%s::date IS NULL OR v.date::date <= %s)""",
                (
                    school_filter, school_filter, start_filter, start_filter, end_filter, end_filter,
                    school_filter, school_filter, start_filter, start_filter, end_filter, end_filter,
                    school_filter, school_filter, start_filter, start_filter, end_filter, end_filter,
                ),
            ).fetchone()

            medicine = conn.execute(
                """SELECT COUNT(DISTINCT m.id), COUNT(d.id),
                          COUNT(DISTINCT m.id) FILTER (
                              WHERE d.start_date::date <= CURRENT_DATE
                                AND (d.end_date IS NULL OR d.end_date::date >= CURRENT_DATE))
                   FROM public.medical_coach_medicines m
                   LEFT JOIN public.medical_coach_medicine_details d
                     ON d.medical_coach_medicine_id = m.id AND d.deleted_at IS NULL
                   WHERE m.deleted_at IS NULL
                     AND (%s::integer IS NULL OR m.school_id = %s)
                     AND (%s::date IS NULL OR m.created_at::date >= %s)
                     AND (%s::date IS NULL OR m.created_at::date <= %s)""",
                (school_filter, school_filter, start_filter, start_filter, end_filter, end_filter),
            ).fetchone()

            series = conn.execute(
                """SELECT 'Khám nhanh' AS category, date_trunc('month', q.day)::date AS period,
                          COUNT(*) AS total
                   FROM public.medical_quick_examinations q
                   WHERE q.deleted_at IS NULL
                     AND q.day IS NOT NULL
                     AND (%s::integer IS NULL OR q.school_id = %s)
                     AND (%s::date IS NULL OR q.day::date >= %s)
                     AND (%s::date IS NULL OR q.day::date <= %s)
                   GROUP BY 1, 2
                   UNION ALL
                   SELECT 'Khám định kỳ', date_trunc('month', e.examination_date)::date, COUNT(*)
                   FROM public.medical_examination_generals e
                   WHERE e.deleted_at IS NULL
                     AND e.examination_date IS NOT NULL
                     AND (%s::integer IS NULL OR e.school_id = %s)
                     AND (%s::date IS NULL OR e.examination_date::date >= %s)
                     AND (%s::date IS NULL OR e.examination_date::date <= %s)
                   GROUP BY 1, 2
                   UNION ALL
                   SELECT 'Sự kiện tiêm chủng', date_trunc('month', v.date)::date, COUNT(*)
                   FROM public.medical_vaccination_events v
                   WHERE v.deleted_at IS NULL
                     AND v.date IS NOT NULL
                     AND (%s::integer IS NULL OR v.school_id = %s)
                     AND (%s::date IS NULL OR v.date::date >= %s)
                     AND (%s::date IS NULL OR v.date::date <= %s)
                   GROUP BY 1, 2
                   UNION ALL
                   SELECT 'BHYT', date_trunc('month', COALESCE(b.ngay_hieu_luc, b.created_at))::date, COUNT(*)
                   FROM public.medical_manager_bhyts b
                   WHERE b.deleted_at IS NULL
                     AND COALESCE(b.ngay_hieu_luc, b.created_at) IS NOT NULL
                     AND (%s::integer IS NULL OR b.school_id = %s)
                     AND (%s::date IS NULL OR COALESCE(b.ngay_hieu_luc, b.created_at)::date >= %s)
                     AND (%s::date IS NULL OR COALESCE(b.ngay_hieu_luc, b.created_at)::date <= %s)
                   GROUP BY 1, 2
                   UNION ALL
                   SELECT 'Hồ sơ thuốc', date_trunc('month', m.created_at)::date, COUNT(*)
                   FROM public.medical_coach_medicines m
                   WHERE m.deleted_at IS NULL
                     AND m.created_at IS NOT NULL
                     AND (%s::integer IS NULL OR m.school_id = %s)
                     AND (%s::date IS NULL OR m.created_at::date >= %s)
                     AND (%s::date IS NULL OR m.created_at::date <= %s)
                   GROUP BY 1, 2
                   ORDER BY period, category""",
                (
                    school_filter, school_filter, start_filter, start_filter, end_filter, end_filter,
                    school_filter, school_filter, start_filter, start_filter, end_filter, end_filter,
                    school_filter, school_filter, start_filter, start_filter, end_filter, end_filter,
                    school_filter, school_filter, start_filter, start_filter, end_filter, end_filter,
                    school_filter, school_filter, start_filter, start_filter, end_filter, end_filter,
                ),
            ).fetchall()

        def records(fields, rows):
            return [
                {name: json_value(value) for name, value in zip(fields, row)}
                for row in rows
            ]

        return jsonify({
            "filters": {
                "from": start_date.isoformat() if start_date else None,
                "to": end_date.isoformat() if end_date else None,
                "school_id": school_id,
            },
            "schools": [{"id": row[0], "name": row[1]} for row in schools],
            "metrics": {
                "student_profiles": student_profiles,
                "health_profiles": health[0],
                "students_with_health_profiles": health[1],
                "allergy_profiles": health[2],
                "current_condition_profiles": health[3],
                "quick_examinations": quick[0],
                "students_with_quick_examinations": quick[1],
                "parent_notifications_sent": quick[2],
                "teacher_confirmations_pending": quick[3],
                "periodic_examinations": exams[0],
                "students_with_periodic_examinations": exams[1],
                "average_bmi": float(exams[2]) if exams[2] is not None else None,
                "examinations_with_bmi_conclusion": exams[3],
                "bhyt_records": bhyt[0],
                "students_with_bhyt": bhyt[1],
                "bhyt_valid": bhyt[2],
                "bhyt_expiring_soon": bhyt[3],
                "bhyt_expired": bhyt[4],
                "vaccination_events": vaccination[0],
                "vaccination_records": vaccination[1],
                "students_vaccinated": vaccination[2],
                "medicine_records": medicine[0],
                "medicine_details": medicine[1],
                "active_medicine_records": medicine[2],
            },
            "series": records(["category", "period", "total"], series),
        })
    except psycopg.Error as exc:
        return jsonify({"error": f"Không truy vấn được dữ liệu y tế: {exc}"}), 503


@app.get("/api/medical/records")
def medical_student_records():
    role_key = request.args.get("role", "").strip()
    categories = MEDICAL_RECORD_CATEGORIES_BY_ROLE.get(role_key)
    if categories is None:
        return jsonify({"error": "Không tìm thấy dashboard cho đối tượng này."}), 404

    try:
        start_value = request.args.get("from", "").strip()
        end_value = request.args.get("to", "").strip()
        school_value = request.args.get("school_id", "").strip()
        start_date = date.fromisoformat(start_value) if start_value else None
        end_date = date.fromisoformat(end_value) if end_value else None
        school_id = int(school_value) if school_value else None
        limit = min(max(int(request.args.get("limit", "100")), 1), 200)
        offset = min(max(int(request.args.get("offset", "0")), 0), 10000)
        search = request.args.get("q", "").strip().casefold()[:100]
        category_filter = request.args.get("category", "").strip()
        if school_id is not None and school_id < 1:
            raise ValueError("school_id phải là số nguyên dương.")
        if start_date and end_date and start_date > end_date:
            raise ValueError("Ngày bắt đầu không được sau ngày kết thúc.")
        if category_filter and category_filter not in categories:
            raise ValueError("Loại hồ sơ không phù hợp với dashboard.")
    except ValueError as exc:
        return jsonify({"error": f"Tham số không hợp lệ: {exc}"}), 400

    class_join = {
        alias: f"""LEFT JOIN LATERAL (
                       SELECT string_agg(DISTINCT c.name, ', ' ORDER BY c.name) AS classroom
                       FROM public.classroom_student cs
                       JOIN public.classroom c ON c.id = cs.classroom_id
                                                   AND c.deleted_at IS NULL
                       WHERE cs.student_id = u.id AND cs.deleted_at IS NULL
                         AND c.school_id = {alias}.school_id
                   ) cl ON TRUE"""
        for alias in ("q", "e", "b", "r", "m")
    }
    queries = {
        "health": [
            ("""SELECT h.created_at AS record_date, u.full_name AS student_name,
                       COALESCE(c.name, cl.classroom, '') AS classroom, 'Hồ sơ sức khỏe' AS record_type,
                       COALESCE(h.di_ung, '') AS symptoms,
                       COALESCE(h.current_disease, h.medical_history, '') AS diagnosis,
                       concat_ws(' · ',
                           NULLIF(h.medical_history, ''),
                           CASE WHEN h.chieu_cao IS NOT NULL THEN 'Chiều cao: ' || h.chieu_cao::text END,
                           CASE WHEN h.can_nang IS NOT NULL THEN 'Cân nặng: ' || h.can_nang::text END,
                           NULLIF(h.nhom_mau, ''), NULLIF(h.vaccination, ''), NULLIF(h.note, '')) AS details,
                       '***' AS contact
                FROM public.medical_student_health_records h
                JOIN public.users u ON u.id = h.user_id AND u.deleted_at IS NULL
                LEFT JOIN public.classroom c ON c.id = h.classroom_id AND c.deleted_at IS NULL
                LEFT JOIN LATERAL (
                    SELECT string_agg(DISTINCT cr.name, ', ' ORDER BY cr.name) AS classroom
                    FROM public.classroom_student cs
                    JOIN public.classroom cr ON cr.id = cs.classroom_id AND cr.deleted_at IS NULL
                    WHERE cs.student_id = u.id AND cs.deleted_at IS NULL
                      AND cr.school_id = h.school_id
                ) cl ON TRUE
                WHERE h.deleted_at IS NULL
                  AND (%s::integer IS NULL OR h.school_id = %s)
                  AND (%s::date IS NULL OR h.created_at::date >= %s)
                  AND (%s::date IS NULL OR h.created_at::date <= %s)
                ORDER BY h.created_at DESC NULLS LAST LIMIT 500""",),
            ("""SELECT q.day AS record_date, u.full_name AS student_name,
                       COALESCE(cl.classroom, '') AS classroom, 'Khám nhanh' AS record_type,
                       COALESCE(q.symptom, '') AS symptoms, COALESCE(q.diagnostic, '') AS diagnosis,
                       concat_ws(' · ', NULLIF(q.description, ''), NULLIF(q.consultation, ''),
                                 NULLIF(q.treatments, ''), NULLIF(q.disease_group, '')) AS details,
                       '***' AS contact
                FROM public.medical_quick_examinations q
                JOIN public.users u ON u.id = q.user_id AND u.deleted_at IS NULL
                {class_join}
                WHERE q.deleted_at IS NULL
                  AND (%s::integer IS NULL OR q.school_id = %s)
                  AND (%s::date IS NULL OR q.day::date >= %s)
                  AND (%s::date IS NULL OR q.day::date <= %s)
                ORDER BY q.day DESC NULLS LAST LIMIT 500""".format(class_join=class_join["q"]),),
            ("""SELECT e.examination_date AS record_date, u.full_name AS student_name,
                       COALESCE(cl.classroom, '') AS classroom, 'Khám định kỳ' AS record_type,
                       COALESCE(e.kham_lam_sang, '') AS symptoms,
                       concat_ws(' · ', NULLIF(e.tong_hop_benh_ly, ''), NULLIF(e.cac_benh_ve_mat, ''),
                                 NULLIF(e.cac_benh_tai_mui_hong, ''), NULLIF(e.co_xuong_khop, ''),
                                 NULLIF(e.da_lieu, ''), NULLIF(e.rang_ham_mat, ''),
                                 NULLIF(e.ho_hap, ''), NULLIF(e.tieu_hoa, ''),
                                 NULLIF(e.than_tiet_nieu, ''), NULLIF(e.than_kinh_tam_than, '')) AS diagnosis,
                       concat_ws(' · ',
                           CASE WHEN e.height IS NOT NULL THEN 'Chiều cao: ' || e.height::text END,
                           CASE WHEN e.weight IS NOT NULL THEN 'Cân nặng: ' || e.weight::text END,
                           CASE WHEN e.bmi IS NOT NULL THEN 'BMI: ' || e.bmi::text END,
                           CASE WHEN e.huyet_ap IS NOT NULL THEN 'Huyết áp: ' || e.huyet_ap END,
                           CASE WHEN e.nhip_tim IS NOT NULL THEN 'Nhịp tim: ' || e.nhip_tim::text END,
                           NULLIF(e.tu_van_suc_khoe, '')) AS details,
                       '***' AS contact
                FROM public.medical_examination_generals e
                JOIN public.users u ON u.id = e.user_id AND u.deleted_at IS NULL
                {class_join}
                WHERE e.deleted_at IS NULL
                  AND (%s::integer IS NULL OR e.school_id = %s)
                  AND (%s::date IS NULL OR e.examination_date::date >= %s)
                  AND (%s::date IS NULL OR e.examination_date::date <= %s)
                ORDER BY e.examination_date DESC NULLS LAST LIMIT 500""".format(class_join=class_join["e"]),),
        ],
        "insurance": [
            ("""SELECT COALESCE(b.ngay_hieu_luc, b.created_at) AS record_date,
                       u.full_name AS student_name, COALESCE(cl.classroom, '') AS classroom,
                       'BHYT' AS record_type, '' AS symptoms, '' AS diagnosis,
                       concat_ws(' · ', 'Mã BHYT: ' || NULLIF(b.ma_bao_hiem, ''),
                                 'Ngày hết hạn: ' || b.ngay_het_han::date::text,
                                 'Nơi đăng ký: ' || NULLIF(b.noi_dang_ky_kcbbd, '')) AS details,
                       '***' AS contact
                FROM public.medical_manager_bhyts b
                JOIN public.users u ON u.id = b.user_id AND u.deleted_at IS NULL
                {class_join}
                WHERE b.deleted_at IS NULL
                  AND (%s::integer IS NULL OR b.school_id = %s)
                  AND (%s::date IS NULL OR COALESCE(b.ngay_hieu_luc, b.created_at)::date >= %s)
                  AND (%s::date IS NULL OR COALESCE(b.ngay_hieu_luc, b.created_at)::date <= %s)
                ORDER BY COALESCE(b.ngay_hieu_luc, b.created_at) DESC NULLS LAST LIMIT 500""".format(class_join=class_join["b"]),),
        ],
        "vaccination": [
            ("""SELECT v.date AS record_date, u.full_name AS student_name,
                       COALESCE(cl.classroom, '') AS classroom, 'Tiêm chủng' AS record_type,
                       '' AS symptoms, '' AS diagnosis,
                       concat_ws(' · ', NULLIF(v.name, ''), NULLIF(v.vaccine_name, ''),
                                 'Mũi: ' || NULLIF(r.shot::text, ''), NULLIF(r.note, '')) AS details,
                       '***' AS contact
                FROM public.medical_vaccination_records r
                JOIN public.medical_vaccination_events v
                  ON v.id = r.medical_vaccination_event_id AND v.deleted_at IS NULL
                JOIN public.users u ON u.id = r.user_id AND u.deleted_at IS NULL
                {class_join}
                WHERE r.deleted_at IS NULL
                  AND (%s::integer IS NULL OR r.school_id = %s)
                  AND (%s::date IS NULL OR v.date::date >= %s)
                  AND (%s::date IS NULL OR v.date::date <= %s)
                ORDER BY v.date DESC NULLS LAST LIMIT 500""".format(class_join=class_join["r"]),),
        ],
        "medicine": [
            ("""SELECT COALESCE(d.start_date, m.created_at) AS record_date,
                       u.full_name AS student_name, COALESCE(cl.classroom, '') AS classroom,
                       'Quản lý thuốc' AS record_type, '' AS symptoms, '' AS diagnosis,
                       concat_ws(' · ', NULLIF(d.medicine_name, ''), NULLIF(d.dosage, ''),
                                 NULLIF(d.usage, ''), NULLIF(d.use_at_time, ''),
                                 CASE WHEN d.end_date IS NOT NULL THEN 'Đến: ' || d.end_date::date::text END) AS details,
                       '***' AS contact
                FROM public.medical_coach_medicines m
                JOIN public.users u ON u.id = m.user_id AND u.deleted_at IS NULL
                LEFT JOIN public.medical_coach_medicine_details d
                  ON d.medical_coach_medicine_id = m.id AND d.deleted_at IS NULL
                {class_join}
                WHERE m.deleted_at IS NULL
                  AND (%s::integer IS NULL OR m.school_id = %s)
                  AND (%s::date IS NULL OR COALESCE(d.start_date, m.created_at)::date >= %s)
                  AND (%s::date IS NULL OR COALESCE(d.start_date, m.created_at)::date <= %s)
                ORDER BY COALESCE(d.start_date, m.created_at) DESC NULLS LAST LIMIT 500""".format(class_join=class_join["m"]),),
        ],
    }

    selected_categories = (category_filter,) if category_filter else categories
    params = (school_id, school_id, start_date, start_date, end_date, end_date)
    try:
        with connect_db() as conn:
            records = []
            for category in selected_categories:
                for (query,) in queries[category]:
                    rows = conn.execute(query, params).fetchall()
                    records.extend({
                        "record_date": json_value(row[0]),
                        "student_name": row[1],
                        "classroom": row[2],
                        "record_type": row[3],
                        "symptoms": mask_contact_text(row[4]),
                        "diagnosis": mask_contact_text(row[5]),
                        "details": mask_contact_text(row[6]),
                        "contact": "***",
                    } for row in rows)

        if search:
            records = [
                row for row in records
                if search in " ".join(str(value or "") for key, value in row.items() if key != "contact").casefold()
            ]
        records.sort(key=lambda row: row["record_date"] or "", reverse=True)
        return jsonify({
            "role": role_key,
            "categories": list(categories),
            "limit": limit,
            "offset": offset,
            "has_more": len(records) > offset + limit,
            "records": records[offset:offset + limit],
        })
    except psycopg.Error as exc:
        return jsonify({"error": f"Không truy vấn được hồ sơ y tế: {exc}"}), 503


@app.get("/dashboard/<role_key>")
def medical_role_dashboard(role_key):
    role = MEDICAL_ROLES_BY_KEY.get(role_key)
    if role is None:
        return jsonify({"error": "Không tìm thấy dashboard cho đối tượng này."}), 404
    return render_template_string(MEDICAL_DASHBOARD, role=role, palette=MEDICAL_PALETTE)


@app.get("/export.csv")
def export_csv():
    schema, table, error = requested_table()
    if error:
        return error
    try:
        limit = min(max(int(request.args.get("limit", "100000")), 1), 100000)
        offset = max(int(request.args.get("offset", "0")), 0)
        headers, rows = fetch_rows(schema, table, limit, offset)
        if headers is None:
            return jsonify({"error": "Không tìm thấy bảng."}), 404
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([json_value(v) for v in row])
        filename = f"{schema}_{table}.csv"
        return Response(
            "\ufeff" + output.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except (ValueError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 400


PAGE = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>K12 Database Browser</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f6fb;color:#172033;font:14px Segoe UI,Arial,sans-serif}
header{background:#172554;color:white;padding:20px 28px}header h1{margin:0 0 5px;font-size:22px}header p{margin:0;color:#cbd5e1}
main{max-width:1500px;margin:22px auto;padding:0 18px;display:grid;grid-template-columns:320px 1fr;gap:18px}
section{background:white;border:1px solid #e1e7f0;border-radius:10px;box-shadow:0 3px 10px #12223b0a}
.sidebar{padding:16px;max-height:calc(100vh - 145px);display:flex;flex-direction:column}.sidebar h2{font-size:16px;margin:0 0 12px}
input{padding:10px;border:1px solid #cbd5e1;border-radius:6px;font:inherit;width:100%;margin-bottom:10px}
#tables{overflow:auto}button.table{display:block;text-align:left;width:100%;border:0;background:white;padding:9px 8px;border-bottom:1px solid #f0f2f6;cursor:pointer;color:#172033}
button.table:hover,button.table.active{background:#eff6ff;color:#1d4ed8}small{color:#64748b;display:block;margin-top:3px}
.content{padding:18px;min-width:0}.bar{display:flex;justify-content:space-between;gap:12px;align-items:start;flex-wrap:wrap}.bar h2{margin:0 0 5px;font-size:19px}.muted{color:#64748b}.actions a,.actions button,.row-action{display:inline-block;text-decoration:none;background:#2563eb;color:white;padding:8px 11px;border:0;border-radius:6px;margin:0 0 10px 6px;cursor:pointer}.row-action{font-size:12px;padding:5px 7px;margin:0 3px 0 0}.row-action.delete{background:#dc2626}
.columns{margin:15px 0;padding:12px;background:#f8fafc;border-radius:6px;color:#475569}.scroll{overflow:auto;max-height:68vh;border:1px solid #e2e8f0;border-radius:6px}table{border-collapse:collapse;width:100%;white-space:nowrap}th,td{padding:8px 10px;border-bottom:1px solid #e8edf4;text-align:left;max-width:360px;overflow:hidden;text-overflow:ellipsis}th{background:#eaf0f8;position:sticky;top:0;z-index:1}#status{padding:18px;color:#64748b}
dialog{width:min(760px,94vw);max-height:90vh;border:0;border-radius:10px;padding:0;box-shadow:0 16px 60px #0f172a55}dialog::backdrop{background:#0f172a88}.dialog-head,.dialog-foot{padding:16px 20px;background:#f8fafc;display:flex;justify-content:space-between;align-items:center}.dialog-head h3{margin:0}.dialog-body{padding:18px 20px;max-height:65vh;overflow:auto}.field{display:grid;grid-template-columns:minmax(150px,1fr) 2fr;gap:12px;align-items:center;margin-bottom:10px}.field label{font-weight:600}.field input,.field textarea{margin:0}.field textarea{min-height:70px;padding:9px;border:1px solid #cbd5e1;border-radius:6px;font:inherit}.dialog-foot button{padding:9px 14px;border:0;border-radius:6px;cursor:pointer}.dialog-foot .save{background:#2563eb;color:white}.notice{margin:12px 0;color:#b91c1c}
@media(max-width:800px){main{grid-template-columns:1fr}.sidebar{max-height:40vh}}
</style></head><body>
<header><h1>K12 Staging · Table Browser</h1><p>Danh mục và xem trước dữ liệu PostgreSQL · Power BI có thể lấy CSV qua liên kết xuất dữ liệu</p></header>
<main><section class="sidebar"><h2>Danh sách bảng</h2><input id="search" placeholder="Tìm schema hoặc tên bảng..."><div id="tables">Đang tải...</div></section>
<section class="content"><div class="bar"><div><h2 id="heading">Chọn một bảng</h2><div class="muted" id="subheading">Bản xem trước tối đa 100 dòng</div></div><div class="actions"><button id="addRow" hidden>+ Thêm dòng</button><a id="csv" href="#" hidden>Xuất CSV / Power BI</a></div></div><div class="columns" id="columns" hidden></div><div class="scroll" id="preview"><div id="status">Chọn bảng ở danh sách bên trái.</div></div></section></main>
<dialog id="editor"><form id="editorForm"><div class="dialog-head"><h3 id="dialogTitle">Bản ghi</h3><button type="button" id="closeDialog">Đóng</button></div><div class="dialog-body"><div id="formFields"></div><div class="notice" id="formError"></div></div><div class="dialog-foot"><span class="muted">Để trống trường tùy chọn để lưu NULL.</span><button class="save" type="submit">Lưu</button></div></form></dialog>
<script>
const list=document.querySelector('#tables'), search=document.querySelector('#search'), dialog=document.querySelector('#editor'); let selected=null, timer, currentData=null, tableInfo=null, editingKey=null;
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function loadTables(){list.textContent='Đang tải...';try{let r=await fetch('/api/tables?q='+encodeURIComponent(search.value));if(!r.ok)throw new Error((await r.json()).error);let a=await r.json();list.innerHTML=a.map(x=>`<button class="table ${selected&&selected.schema===x.schema&&selected.table===x.table?'active':''}" data-s="${esc(x.schema)}" data-t="${esc(x.table)}">${esc(x.table)}<small>${esc(x.schema)} · ~${Number(x.estimated_rows).toLocaleString()} dòng · ${esc(x.kind)}</small></button>`).join('')||'<div id="status">Không tìm thấy bảng.</div>';list.querySelectorAll('button').forEach(b=>b.onclick=()=>openTable(b.dataset.s,b.dataset.t))}catch(e){list.textContent=e.message}}
async function openTable(schema,table){selected={schema,table};loadTables();document.querySelector('#heading').textContent=schema+'.'+table;document.querySelector('#subheading').textContent='Đang tải bản xem trước...';document.querySelector('#preview').innerHTML='<div id="status">Đang tải dữ liệu...</div>';let params=new URLSearchParams({schema,table});document.querySelector('#csv').href='/export.csv?'+params;document.querySelector('#csv').hidden=false;try{let [dr,ir]=await Promise.all([fetch('/api/data?'+new URLSearchParams({schema,table,limit:100})),fetch('/api/table-info?'+params)]);let d=await dr.json();tableInfo=ir.ok?await ir.json():null;if(!dr.ok)throw new Error(d.error);currentData=d;document.querySelector('#subheading').textContent=`Hiển thị ${d.rows.length} dòng đầu tiên`;document.querySelector('#addRow').hidden=!tableInfo;let ci=document.querySelector('#columns');ci.hidden=false;ci.innerHTML='<b>Cột:</b> '+d.columns.map(x=>{let c=tableInfo?.columns.find(y=>y.name===x);return `${esc(x)} <span class="muted">(${esc(c?.type||'')})</span>`}).join(' · ');let actions=tableInfo?.primary_key.length?'<th>Thao tác</th>':'';document.querySelector('#preview').innerHTML='<table><thead><tr>'+d.columns.map(x=>'<th>'+esc(x)+'</th>').join('')+actions+'</tr></thead><tbody>'+d.rows.map((row,i)=>'<tr>'+row.map(v=>'<td title="'+esc(v)+'">'+esc(v)+'</td>').join('')+(actions?`<td><button class="row-action" data-edit="${i}">Sửa</button><button class="row-action delete" data-delete="${i}">Xóa</button></td>`:'')+'</tr>').join('')+'</tbody></table>';document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>editRow(Number(b.dataset.edit)));document.querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>deleteRow(Number(b.dataset.delete)))}catch(e){document.querySelector('#preview').innerHTML='<div id="status">'+esc(e.message)+'</div>'}}
function openEditor(row=null,key=null){if(!tableInfo)return;editingKey=key;document.querySelector('#dialogTitle').textContent=row?'Sửa bản ghi':'Thêm bản ghi';document.querySelector('#formError').textContent='';let fields=tableInfo.columns.filter(c=>row?c.updatable:c.insertable);document.querySelector('#formFields').innerHTML=fields.map(c=>{let val=row?.[c.name];let text=val==null?'':(typeof val==='object'?JSON.stringify(val):String(val));let area=/json|\[\]/i.test(c.type)||c.type==='ARRAY';let required=!row&&!c.nullable&&!c.has_default;return `<div class="field"><label for="f_${esc(c.name)}">${esc(c.name)}${required?' *':''}<small>${esc(c.type)}${c.primary_key?' · khóa chính':''}</small></label>${area?`<textarea id="f_${esc(c.name)}" name="${esc(c.name)}" ${required?'required':''}>${esc(text)}</textarea>`:`<input id="f_${esc(c.name)}" name="${esc(c.name)}" value="${esc(text)}" ${required?'required':''} placeholder="${c.nullable?'Để trống = NULL':''}">`}</div>`}).join('')||'<p>Bảng không có cột nào có thể nhập hoặc sửa.</p>';dialog.showModal()}
async function editRow(i){let row=currentData.rows[i],key={};tableInfo.primary_key.forEach(k=>key[k]=row[currentData.columns.indexOf(k)]);let res=await fetch('/api/row?'+new URLSearchParams({schema:selected.schema,table:selected.table,key:JSON.stringify(key)}));let full=await res.json();if(!res.ok){alert(full.error);return}openEditor(full,key)}
async function deleteRow(i){if(!confirm('Xóa bản ghi này? Thao tác sẽ ghi trực tiếp vào database.'))return;let row=currentData.rows[i],key={};tableInfo.primary_key.forEach(k=>key[k]=row[currentData.columns.indexOf(k)]);let r=await fetch('/api/rows',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({...selected,key})}),d=await r.json();if(!r.ok){alert(d.error);return}await openTable(selected.schema,selected.table)}
document.querySelector('#addRow').onclick=()=>openEditor();document.querySelector('#closeDialog').onclick=()=>dialog.close();document.querySelector('#editorForm').onsubmit=async e=>{e.preventDefault();let values={};new FormData(e.currentTarget).forEach((v,k)=>{let c=tableInfo.columns.find(x=>x.name===k);if(!(v===''&&!editingKey&&c?.has_default))values[k]=v});let r=await fetch('/api/rows',{method:editingKey?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...selected,key:editingKey,values})}),d=await r.json();if(!r.ok){document.querySelector('#formError').textContent=d.error;return}dialog.close();await openTable(selected.schema,selected.table)};
search.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(loadTables,200)});loadTables();
</script></body></html>"""


@app.get("/")
def home():
    return redirect("/dashboard")


MEDICAL_ROLES = [
    {
        "key": "ban-giam-hieu",
        "title": "Ban giám hiệu",
        "subtitle": "Điều hành và phân bổ nguồn lực chăm sóc sức khỏe học sinh",
        "story": "Là Ban giám hiệu, tôi muốn theo dõi quy mô hồ sơ sức khỏe, hoạt động khám, BHYT và tiêm chủng theo cơ sở và thời gian để ưu tiên nguồn lực, xây dựng kế hoạch chăm sóc sức khỏe và phối hợp với phụ huynh.",
        "metrics": [
            ("healthProfiles", "Hồ sơ sức khỏe", "health_profiles"),
            ("quickExams", "Lượt khám nhanh", "quick_examinations"),
            ("periodicExams", "Lượt khám định kỳ", "periodic_examinations"),
            ("bhytValid", "Học sinh có BHYT chưa qua ngày hết hạn", "bhyt_valid"),
            ("bhytExpiring", "Hồ sơ BHYT hết hạn trong 30 ngày", "bhyt_expiring_soon"),
            ("vaccinationEvents", "Sự kiện tiêm chủng", "vaccination_events"),
            ("vaccinationRecords", "Lượt tiêm đã ghi nhận", "vaccination_records"),
            ("medicineRecords", "Hồ sơ thuốc", "medicine_records"),
        ],
        "charts": [
            {"category": "Khám nhanh", "type": "bar"},
            {"category": "Khám định kỳ", "type": "line"},
            {"category": "Sự kiện tiêm chủng", "type": "area"},
        ],
        "enabled": True,
    },
    {
        "key": "truong-bo-phan-y-te",
        "title": "Trưởng bộ phận y tế học đường",
        "subtitle": "Điều phối nghiệp vụ y tế và theo dõi khối lượng xử lý",
        "story": "Là Trưởng bộ phận y tế học đường, tôi muốn theo dõi khối lượng khám, tình trạng thông báo, kết quả khám định kỳ và hồ sơ cần lưu ý để phân công nhân sự, lập kế hoạch theo dõi và báo cáo nhà trường.",
        "metrics": [
            ("quickExams", "Lượt khám nhanh", "quick_examinations"),
            ("periodicExams", "Lượt khám định kỳ", "periodic_examinations"),
            ("parentNotifications", "Thông báo đã gửi phụ huynh", "parent_notifications_sent"),
            ("teacherConfirmations", "Chờ giáo viên xác nhận", "teacher_confirmations_pending"),
            ("bmiConclusions", "Hồ sơ khám có kết luận BMI", "examinations_with_bmi_conclusion"),
            ("bhytExpiring", "Hồ sơ BHYT hết hạn trong 30 ngày", "bhyt_expiring_soon"),
            ("activeMedicineRecords", "Hồ sơ thuốc đang theo dõi", "active_medicine_records"),
        ],
        "charts": [
            {"category": "Khám nhanh", "type": "area"},
            {"category": "Khám định kỳ", "type": "line"},
        ],
        "enabled": True,
    },
    {
        "key": "nhan-vien-y-te",
        "title": "Nhân viên y tế / y tá trường học",
        "subtitle": "Theo dõi lượt khám nhanh và trạng thái phối hợp",
        "story": "Là Nhân viên y tế, tôi muốn theo dõi lượt khám nhanh, số học sinh đến khám và tình trạng gửi thông báo hoặc chờ xác nhận để sắp xếp công việc và tiếp tục xử lý các trường hợp cần phối hợp.",
        "metrics": [
            ("quickExams", "Lượt khám nhanh", "quick_examinations"),
            ("quickStudents", "Học sinh có lượt khám", "students_with_quick_examinations"),
            ("parentNotifications", "Thông báo đã gửi phụ huynh", "parent_notifications_sent"),
            ("teacherConfirmations", "Chờ giáo viên xác nhận", "teacher_confirmations_pending"),
        ],
        "charts": [{"category": "Khám nhanh", "type": "bar"}],
        "enabled": True,
    },
    {
        "key": "bac-si-don-vi-kham",
        "title": "Bác sĩ / đơn vị khám sức khỏe",
        "subtitle": "Theo dõi tổng hợp hoạt động khám định kỳ",
        "story": "Là Bác sĩ hoặc đơn vị khám, tôi muốn theo dõi số lượt khám định kỳ và mức độ hoàn thành dữ liệu khám ở dạng tổng hợp để phối hợp tổ chức khám và lập báo cáo mà không truy cập thông tin cá nhân ngoài phạm vi được phép.",
        "metrics": [
            ("periodicExams", "Lượt khám định kỳ", "periodic_examinations"),
            ("examStudents", "Học sinh có lượt khám định kỳ", "students_with_periodic_examinations"),
            ("bmiConclusions", "Hồ sơ có kết luận BMI", "examinations_with_bmi_conclusion"),
        ],
        "charts": [{"category": "Khám định kỳ", "type": "line"}],
        "enabled": True,
    },
    {
        "key": "phu-trach-bhyt",
        "title": "Phụ trách BHYT học sinh",
        "subtitle": "Rà soát hồ sơ bảo hiểm theo cơ sở và thời gian",
        "story": "Là Người phụ trách BHYT, tôi muốn theo dõi số hồ sơ có ngày hết hạn đã qua hoặc sắp đến theo cơ sở để rà soát và chủ động phối hợp gia hạn.",
        "metrics": [
            ("bhytRecords", "Hồ sơ BHYT", "bhyt_records"),
            ("bhytStudents", "Học sinh có hồ sơ BHYT", "students_with_bhyt"),
            ("bhytValid", "Học sinh có ngày hết hạn chưa qua", "bhyt_valid"),
            ("bhytExpiring", "Hết hạn trong 30 ngày", "bhyt_expiring_soon"),
            ("bhytExpired", "Đã qua ngày hết hạn", "bhyt_expired"),
        ],
        "charts": [{"category": "BHYT", "type": "line"}],
        "enabled": True,
    },
    {
        "key": "dieu-phoi-tiem-chung",
        "title": "Điều phối tiêm chủng",
        "subtitle": "Theo dõi sự kiện tiêm và tình trạng cập nhật bản ghi",
        "story": "Là Người điều phối tiêm chủng, tôi muốn theo dõi sự kiện và số lượt tiêm đã được ghi nhận theo thời gian để lập kế hoạch, rà soát dữ liệu và phối hợp với gia đình.",
        "metrics": [
            ("vaccinationEvents", "Sự kiện tiêm chủng", "vaccination_events"),
            ("vaccinationRecords", "Bản ghi tiêm", "vaccination_records"),
            ("vaccinatedStudents", "Học sinh có bản ghi tiêm", "students_vaccinated"),
        ],
        "charts": [{"category": "Sự kiện tiêm chủng", "type": "area"}],
        "enabled": True,
    },
    {
        "key": "quan-ly-thuoc",
        "title": "Phụ trách quản lý thuốc tại trường",
        "subtitle": "Theo dõi hồ sơ quản lý thuốc ở mức tổng hợp",
        "story": "Là Người phụ trách quản lý thuốc, tôi muốn theo dõi số hồ sơ thuốc, chi tiết thuốc và hồ sơ có thời gian sử dụng hiện hành để rà soát thông tin phục vụ quy trình bảo quản và sử dụng thuốc tại trường.",
        "metrics": [
            ("medicineRecords", "Hồ sơ thuốc", "medicine_records"),
            ("medicineDetails", "Chi tiết thuốc ghi nhận", "medicine_details"),
            ("activeMedicineRecords", "Hồ sơ thuốc có thời gian hiện hành", "active_medicine_records"),
        ],
        "charts": [{"category": "Hồ sơ thuốc", "type": "bar"}],
        "enabled": True,
    },
    {
        "key": "giao-vien-chu-nhiem",
        "title": "Giáo viên chủ nhiệm",
        "subtitle": "Phối hợp xác nhận thông báo y tế của lớp được phân công",
        "story": "Là Giáo viên chủ nhiệm, tôi muốn theo dõi tổng hợp hoạt động khám và thông báo y tế toàn trường để phối hợp công việc; dữ liệu theo lớp chỉ được mở sau khi có đăng nhập và phân quyền.",
        "metrics": [
            ("teacherConfirmations", "Thông báo chờ xác nhận", "teacher_confirmations_pending"),
            ("parentNotifications", "Thông báo đã gửi phụ huynh", "parent_notifications_sent"),
        ],
        "charts": [{"category": "Khám nhanh", "type": "area"}],
        "enabled": True,
    },
    {
        "key": "phu-huynh",
        "title": "Phụ huynh / người giám hộ",
        "subtitle": "Cổng theo dõi thông tin của học sinh được giám hộ",
        "story": "Là Phụ huynh, tôi muốn theo dõi số liệu hoạt động y tế tổng hợp của nhà trường; hồ sơ, thông báo và lịch khám của con chỉ được mở sau khi xác minh quan hệ giám hộ.",
        "metrics": [
            ("quickExams", "Lượt khám nhanh toàn trường", "quick_examinations"),
            ("periodicExams", "Lượt khám định kỳ toàn trường", "periodic_examinations"),
            ("vaccinationEvents", "Sự kiện tiêm chủng toàn trường", "vaccination_events"),
        ],
        "charts": [{"category": "Khám nhanh", "type": "line"}],
        "enabled": True,
    },
    {
        "key": "hoc-sinh",
        "title": "Học sinh",
        "subtitle": "Thông tin chăm sóc sức khỏe phù hợp với học sinh",
        "story": "Là Học sinh, tôi muốn theo dõi số liệu hoạt động y tế tổng hợp của nhà trường; lịch khám cá nhân và thông báo riêng chỉ được mở qua tài khoản phù hợp độ tuổi và quy tắc đồng ý của người giám hộ.",
        "metrics": [
            ("quickExams", "Lượt khám nhanh toàn trường", "quick_examinations"),
            ("periodicExams", "Lượt khám định kỳ toàn trường", "periodic_examinations"),
            ("vaccinationEvents", "Sự kiện tiêm chủng toàn trường", "vaccination_events"),
        ],
        "charts": [{"category": "Khám định kỳ", "type": "area"}],
        "enabled": True,
    },
    {
        "key": "cong-tac-hoc-sinh",
        "title": "Phòng công tác học sinh",
        "subtitle": "Phối hợp hỗ trợ sức khỏe học sinh ở mức tổng hợp",
        "story": "Là Người phụ trách công tác học sinh, tôi muốn theo dõi khối lượng hoạt động y tế ở mức tổng hợp để phối hợp giữa nhà trường, bộ phận y tế và gia đình mà không truy cập dữ liệu sức khỏe vượt quá nhiệm vụ.",
        "metrics": [
            ("quickExams", "Lượt khám nhanh", "quick_examinations"),
            ("periodicExams", "Lượt khám định kỳ", "periodic_examinations"),
            ("parentNotifications", "Thông báo đã gửi phụ huynh", "parent_notifications_sent"),
        ],
        "charts": [
            {"category": "Khám nhanh", "type": "area"},
            {"category": "Khám định kỳ", "type": "bar"},
        ],
        "enabled": True,
    },
]
MEDICAL_ROLES_BY_KEY = {role["key"]: role for role in MEDICAL_ROLES}
MEDICAL_RECORD_CATEGORIES_BY_ROLE = {
    "ban-giam-hieu": ("health", "insurance", "vaccination", "medicine"),
    "truong-bo-phan-y-te": ("health", "insurance", "vaccination", "medicine"),
    "nhan-vien-y-te": ("health",),
    "bac-si-don-vi-kham": ("health",),
    "phu-trach-bhyt": ("insurance",),
    "dieu-phoi-tiem-chung": ("vaccination",),
    "quan-ly-thuoc": ("medicine",),
    "giao-vien-chu-nhiem": ("health",),
    "phu-huynh": ("health",),
    "hoc-sinh": ("health",),
    "cong-tac-hoc-sinh": ("health",),
}
MEDICAL_RECORD_CATEGORY_LABELS = {
    "health": "Khám và hồ sơ sức khỏe",
    "insurance": "Bảo hiểm y tế",
    "vaccination": "Tiêm chủng",
    "medicine": "Quản lý thuốc",
}
for role in MEDICAL_ROLES:
    role["record_categories"] = [
        {"key": key, "label": MEDICAL_RECORD_CATEGORY_LABELS[key]}
        for key in MEDICAL_RECORD_CATEGORIES_BY_ROLE[role["key"]]
    ]
MEDICAL_PALETTE = {
    "Khám nhanh": "#2563eb",
    "Khám định kỳ": "#0f766e",
    "Sự kiện tiêm chủng": "#d97706",
    "BHYT": "#7c3aed",
    "Hồ sơ thuốc": "#0891b2",
}


DASHBOARD_HOME = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard y tế học đường | Chọn đối tượng</title>
<style>
:root{font-family:"Segoe UI",Arial,sans-serif;color:#172033;background:#f3f6fb}*{box-sizing:border-box}
body{margin:0}header{background:#14532d;color:#fff;padding:26px max(24px,calc((100vw - 1240px)/2))}
h1{margin:0 0 6px;font-size:27px}header p{margin:0;color:#dcfce7}
main{max-width:1240px;margin:34px auto;padding:0 20px}.intro{color:#64748b;margin-bottom:20px;line-height:1.55}
.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
a.card{display:flex;flex-direction:column;min-height:190px;padding:20px;background:white;border:1px solid #dce8df;border-radius:12px;text-decoration:none;color:inherit;box-shadow:0 4px 18px #12223b0b;transition:transform .15s,border-color .15s}
a.card:hover,a.card:focus{transform:translateY(-2px);border-color:#15803d;outline:none}.tag{color:#15803d;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
h2{margin:9px 0;font-size:18px}p{line-height:1.55}.desc{color:#475569;font-size:13px;margin:0 0 12px}.link{margin-top:auto;color:#166534;font-weight:600;font-size:13px}
.pending{display:block;margin-top:8px;color:#b45309;font-size:12px;font-weight:600}
footer{max-width:1240px;margin:auto;padding:20px;color:#64748b;font-size:12px;line-height:1.5}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){.cards{grid-template-columns:1fr}main{margin:24px auto;padding:0 12px}a.card{min-height:0}}
</style></head><body>
<header><h1>Dashboard Y tế học đường</h1><p>Chọn đối tượng để mở dashboard nghiệp vụ riêng.</p></header>
<main><p class="intro">Có 11 dashboard theo vai trò, hiện bật ở chế độ số liệu tổng hợp. Dữ liệu cá nhân, hồ sơ theo lớp và thông tin riêng của học sinh chưa được mở.</p>
<section class="cards" aria-label="Chọn đối tượng dashboard y tế">
{% for role in roles %}<a class="card" href="/dashboard/{{ role.key }}"><span class="tag">Đối tượng {{ loop.index }}</span><h2>{{ role.title }}</h2><p class="desc">{{ role.subtitle }}</p><span class="link">Mở dashboard →</span></a>{% endfor %}
</section></main><footer>Dữ liệu sức khỏe là dữ liệu nhạy cảm. Tất cả dashboard chỉ hiển thị tổng hợp toàn trường, chưa giới hạn theo tài khoản/lớp và không có hồ sơ cá nhân. Chỉ chạy cục bộ cho đến khi triển khai xác thực và phân quyền.</footer></body></html>"""


MEDICAL_DASHBOARD = r"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ role.title }} | Dashboard y tế</title>
<style>
:root{font-family:"Segoe UI",Arial,sans-serif;color:#172033;background:#f3f6fb}*{box-sizing:border-box}
body{margin:0}header{background:#14532d;color:#fff;padding:24px max(24px,calc((100vw - 1320px)/2))}
header h1{margin:0 0 5px;font-size:25px}header p{margin:0;color:#dcfce7}.home-link{display:inline-block;color:#dcfce7;font-size:13px;margin-bottom:10px;text-decoration:none}
main{max-width:1320px;margin:22px auto;padding:0 20px}.toolbar{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin-bottom:14px;padding:14px;background:white;border:1px solid #dce8df;border-radius:10px}
.toolbar label{font-size:13px;font-weight:600;color:#475569}.toolbar input,.toolbar select{display:block;margin-top:5px;padding:9px 10px;min-width:165px;border:1px solid #cbd5e1;border-radius:7px;font:inherit;background:white}
button{font:inherit}.toolbar button{padding:10px 15px;border:0;border-radius:6px;background:#15803d;color:white;cursor:pointer}.toolbar button:disabled{opacity:.55;cursor:wait}
.status{display:none;padding:13px;background:#fff7ed;color:#9a3412;border-radius:8px;margin-bottom:14px}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}.card,.panel,.story,.locked{background:white;border:1px solid #dce8df;border-radius:10px;box-shadow:0 3px 10px #12223b0a}
.card{padding:15px}.card span{display:block;color:#64748b;font-size:13px}.card strong{display:block;margin-top:7px;font-size:21px;overflow-wrap:anywhere}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.panel,.story,.locked{padding:16px;margin-bottom:14px;min-width:0}.panel h2,.story h2{font-size:17px;margin:0 0 7px}.panel>p,.story p{margin:0;color:#475569;line-height:1.6}
.chart svg{display:block;width:100%;height:auto;min-height:190px}.chart svg [data-clickable="true"]{cursor:pointer}.chart svg [data-clickable="true"]:hover{filter:brightness(.88)}
.data-table{overflow:hidden}.table-scroll{overflow-x:auto;margin-top:10px}table{width:100%;border-collapse:collapse;font-size:13px;text-align:left}th,td{padding:9px 11px;border-bottom:1px solid #e2e8f0;white-space:nowrap}th{background:#f8fafc;color:#334155}td:last-child,th:last-child{text-align:right}
.records-toolbar{display:flex;gap:10px;align-items:end;flex-wrap:wrap;margin:12px 0}.records-toolbar label{font-size:13px;font-weight:600;color:#475569}.records-toolbar input,.records-toolbar select{display:block;margin-top:5px;padding:9px 10px;min-width:180px;border:1px solid #cbd5e1;border-radius:7px;font:inherit;background:white}.records-toolbar button{padding:10px 15px;border:0;border-radius:6px;background:#15803d;color:white;cursor:pointer}.record-table{min-width:1000px}.record-table td:nth-child(1){font-weight:600}
.notice{padding:13px 15px;margin:0 0 16px;border-left:4px solid #d97706;background:#fffbeb;color:#713f12;border-radius:5px;font-size:13px;line-height:1.55}
.locked{border-color:#f4d5a6;background:#fffbeb}.locked h2{margin:0 0 8px;color:#92400e;font-size:17px}.locked p{margin:0;line-height:1.55}
.muted{color:#64748b;font-size:12px}footer{padding:8px 0 28px;color:#64748b;font-size:12px;line-height:1.5}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:620px){header{padding:20px}main{padding:0 12px}.grid{grid-template-columns:1fr}.cards{gap:8px}.card{padding:11px}.card strong{font-size:18px}.toolbar label,.toolbar input,.toolbar select{width:100%}}
</style></head><body>
<header><a class="home-link" href="/dashboard">← Chọn đối tượng</a><h1>{{ role.title }}</h1><p>{{ role.subtitle }}</p></header>
<main>
<article class="story"><h2>Mục tiêu nghiệp vụ</h2><p>{{ role.story }}</p></article>
{% if role.enabled %}
 <form class="toolbar" id="filters">
  <label>Từ ngày<input id="fromDate" type="date"></label><label>Đến ngày<input id="toDate" type="date"></label>
  <label>Cơ sở<select id="school"><option value="">Tất cả cơ sở</option></select></label><button id="loadButton" type="submit">Lọc dữ liệu</button>
 </form>
 <div id="status" class="status" role="alert"></div>
 <p class="notice"><b>Lưu ý bảo mật:</b> Chọn vai trò chỉ đổi màn hình và nhóm hồ sơ, không xác thực người xem hoặc giới hạn theo lớp/quan hệ giám hộ. Trang sẽ hiển thị tên học sinh và thông tin sức khỏe; cột liên hệ luôn là *** và không đọc từ cơ sở dữ liệu. Chỉ chạy cục bộ trên máy tin cậy; không mở ra mạng hoặc Internet.</p>
 <section class="cards" aria-label="Chỉ số theo vai trò"><div class="card"><span>Hồ sơ học sinh hiện có (tổng hợp)</span><strong id="studentProfiles">—</strong></div>{% for metric in role.metrics %}<div class="card"><span>{{ metric[1] }}</span><strong id="{{ metric[0] }}">—</strong></div>{% endfor %}</section>
 <section class="grid" aria-label="Biểu đồ theo vai trò">{% for chart in role.charts %}<div class="panel chart"><h2>{{ chart.category }} theo tháng</h2><p>Bấm điểm dữ liệu để lọc về tháng tương ứng. Dạng biểu đồ: {{ chart.type }}.</p><div id="chart-{{ loop.index0 }}"></div></div>{% endfor %}
 {% if role.key in ["ban-giam-hieu","truong-bo-phan-y-te","phu-trach-bhyt"] %}<div class="panel"><h2>Tổng hợp BHYT</h2><p id="bhytSummary">Đang tải...</p></div>{% endif %}
 {% if role.key in ["ban-giam-hieu","truong-bo-phan-y-te","cong-tac-hoc-sinh"] %}<div class="panel"><h2>Hồ sơ sức khỏe (tổng hợp)</h2><p id="healthSummary">Đang tải...</p></div>{% endif %}</section>
 <section class="panel data-table"><h2>Bảng dữ liệu tổng hợp theo tháng</h2><p class="muted">Chỉ gồm số lượt tổng hợp theo tháng và nhóm nghiệp vụ; không hiển thị hồ sơ cá nhân.</p><div id="seriesTable" class="table-scroll" aria-live="polite">Đang tải...</div></section>
 <section class="panel data-table"><h2>Hồ sơ học sinh theo dashboard</h2><p class="muted">Hồ sơ được lọc theo loại, cơ sở, ngày và từ khóa. Thông tin liên hệ được che.</p>
  <form class="records-toolbar" id="recordFilters"><label>Tìm học sinh / nội dung<input id="recordSearch" type="search" maxlength="100" placeholder="Tên, lớp, triệu chứng, chẩn đoán..."></label><label>Loại hồ sơ<select id="recordCategory"><option value="">Tất cả loại hồ sơ của vai trò</option>{% for category in role.record_categories %}<option value="{{ category.key }}">{{ category.label }}</option>{% endfor %}</select></label><button id="recordsLoadButton" type="submit">Tìm hồ sơ</button></form>
  <div id="recordsStatus" class="status" role="alert"></div><div id="studentRecords" class="table-scroll" aria-live="polite">Đang tải hồ sơ...</div><button id="recordsMoreButton" type="button" hidden>Tải thêm hồ sơ</button>
 </section>
 <footer>Bộ lọc thời gian áp dụng theo ngày khám/sự kiện hoặc ngày tạo hồ sơ tương ứng. Phân loại BHYT chỉ dựa trên ngày hết hạn, không thay thế trạng thái nghiệp vụ. Số lượt tiêm được tính từ bản ghi tiêm thực tế, không suy ra từ số sự kiện.</footer>
{% else %}
 <section class="locked" role="status"><h2>Dashboard chưa mở quyền truy cập</h2><p>{{ role.access_note }}</p></section>
 <p class="muted">Không gọi API dữ liệu và không hiển thị số liệu học sinh trên trang này.</p>
{% endif %}
</main>
{% if role.enabled %}
<script>
const API="/api/medical/dashboard",RECORDS_API="/api/medical/records",ROLE_KEY={{ role.key|tojson }},numberFormat=new Intl.NumberFormat("vi-VN"),svgNS="http://www.w3.org/2000/svg",metrics={{ role.metrics|tojson }},charts={{ role.charts|tojson }},palette={{ palette|tojson }};
let currentData=null,recordsOffset=0;
function setMetric(id,value){const element=document.getElementById(id);if(element)element.textContent=value===null||value===undefined?"—":numberFormat.format(value)}
function drawChart(targetId,chart,color){
 const {category,type}=chart,target=document.getElementById(targetId);target.replaceChildren();const rows=(currentData.series||[]).filter(row=>row.category===category);
 if(!rows.length){const empty=document.createElement("p");empty.className="muted";empty.textContent="Không có dữ liệu trong khoảng lọc.";target.appendChild(empty);return}
 const W=680,H=225,L=48,R=12,T=18,B=44,pw=W-L-R,ph=H-T-B,slot=pw/rows.length,max=Math.max(1,...rows.map(row=>Number(row.total)||0)),svg=document.createElementNS(svgNS,"svg");
 svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.setAttribute("role","img");svg.setAttribute("aria-label",`${category} theo tháng, dạng ${type}`);target.appendChild(svg);
 for(let n=0;n<=3;n++){const y=T+ph*n/3,line=document.createElementNS(svgNS,"line");line.setAttribute("x1",L);line.setAttribute("x2",W-R);line.setAttribute("y1",y);line.setAttribute("y2",y);line.setAttribute("stroke","#e8edf4");svg.appendChild(line);const label=document.createElementNS(svgNS,"text");label.setAttribute("x",L-6);label.setAttribute("y",y+4);label.setAttribute("text-anchor","end");label.setAttribute("fill","#64748b");label.setAttribute("font-size","10");label.textContent=numberFormat.format(Math.round(max*(3-n)/3));svg.appendChild(label)}
 const points=rows.map((row,index)=>({row,value:Number(row.total)||0,x:L+index*slot+slot/2,y:T+ph-(Number(row.total)||0)/max*ph,month:String(row.period).slice(0,7)}));
 const selectMonth=month=>{document.getElementById("fromDate").value=`${month}-01`;const lastDay=new Date(Number(month.slice(0,4)),Number(month.slice(5,7)),0).getDate();document.getElementById("toDate").value=`${month}-${String(lastDay).padStart(2,"0")}`;document.getElementById("filters").requestSubmit()};
 if(type==="bar"){points.forEach(point=>{const width=Math.max(3,Math.min(28,slot*.64)),rect=document.createElementNS(svgNS,"rect");rect.setAttribute("x",point.x-width/2);rect.setAttribute("y",point.y);rect.setAttribute("width",width);rect.setAttribute("height",Math.max(1,T+ph-point.y));rect.setAttribute("rx",3);rect.setAttribute("fill",color);rect.setAttribute("data-clickable","true");rect.setAttribute("tabindex","0");rect.setAttribute("role","button");const title=document.createElementNS(svgNS,"title");title.textContent=`${point.month}: ${numberFormat.format(point.value)} · Bấm để lọc tháng`;rect.appendChild(title);rect.addEventListener("click",()=>selectMonth(point.month));rect.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();selectMonth(point.month)}});svg.appendChild(rect)})}
 else{const linePath=points.map((point,index)=>`${index?"L":"M"}${point.x},${point.y}`).join(" "),baseline=T+ph;if(type==="area"){const area=document.createElementNS(svgNS,"path");area.setAttribute("d",`${linePath} L${points[points.length-1].x},${baseline} L${points[0].x},${baseline} Z`);area.setAttribute("fill",color);area.setAttribute("fill-opacity",".16");svg.appendChild(area)}const line=document.createElementNS(svgNS,"path");line.setAttribute("d",linePath);line.setAttribute("fill","none");line.setAttribute("stroke",color);line.setAttribute("stroke-width","2.5");svg.appendChild(line);points.forEach(point=>{const dot=document.createElementNS(svgNS,"circle");dot.setAttribute("cx",point.x);dot.setAttribute("cy",point.y);dot.setAttribute("r",4);dot.setAttribute("fill",color);dot.setAttribute("data-clickable","true");dot.setAttribute("tabindex","0");dot.setAttribute("role","button");const title=document.createElementNS(svgNS,"title");title.textContent=`${point.month}: ${numberFormat.format(point.value)} · Bấm để lọc tháng`;dot.appendChild(title);dot.addEventListener("click",()=>selectMonth(point.month));dot.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();selectMonth(point.month)}});svg.appendChild(dot)})}
 points.forEach((point,index)=>{if(index%Math.max(1,Math.ceil(points.length/8))===0||index===points.length-1){const text=document.createElementNS(svgNS,"text");text.setAttribute("x",point.x);text.setAttribute("y",H-15);text.setAttribute("text-anchor","middle");text.setAttribute("fill","#475569");text.setAttribute("font-size","10");text.textContent=point.month;svg.appendChild(text)}});
}
function renderSeriesTable(series){
 const target=document.getElementById("seriesTable");target.replaceChildren();if(!series.length){const empty=document.createElement("p");empty.className="muted";empty.textContent="Không có dữ liệu trong khoảng lọc.";target.appendChild(empty);return}
 const table=document.createElement("table"),head=document.createElement("thead"),headRow=document.createElement("tr");["Tháng","Nhóm nghiệp vụ","Số lượt"].forEach(label=>{const cell=document.createElement("th");cell.textContent=label;headRow.appendChild(cell)});head.appendChild(headRow);table.appendChild(head);const body=document.createElement("tbody");
 [...series].reverse().forEach(row=>{const tr=document.createElement("tr");[String(row.period).slice(0,7),row.category,numberFormat.format(Number(row.total)||0)].forEach(value=>{const cell=document.createElement("td");cell.textContent=value;tr.appendChild(cell)});body.appendChild(tr)});table.appendChild(body);target.appendChild(table)
}
function renderStudentRecords(records,append){
 const target=document.getElementById("studentRecords");if(!append)target.replaceChildren();
 if(!records.length&&!append){const empty=document.createElement("p");empty.className="muted";empty.textContent="Không tìm thấy hồ sơ phù hợp với bộ lọc.";target.appendChild(empty);return}
 let table=append?target.querySelector("table"):null;
 if(!table){table=document.createElement("table");table.className="record-table";const head=document.createElement("thead"),headRow=document.createElement("tr");
  ["Học sinh","Lớp","Ngày","Loại hồ sơ","Triệu chứng","Chẩn đoán / ghi nhận","Chi tiết","Liên hệ"].forEach(label=>{const cell=document.createElement("th");cell.textContent=label;headRow.appendChild(cell)});head.appendChild(headRow);table.appendChild(head);table.appendChild(document.createElement("tbody"));target.appendChild(table)}
 const body=table.querySelector("tbody");records.forEach(record=>{const row=document.createElement("tr"),dateValue=record.record_date?new Date(record.record_date).toLocaleDateString("vi-VN"):"";[record.student_name,record.classroom,dateValue,record.record_type,record.symptoms,record.diagnosis,record.details,"***"].forEach(value=>{const cell=document.createElement("td");cell.textContent=value||"—";row.appendChild(cell)});body.appendChild(row)})
}
async function loadStudentRecords(event){
 if(event)event.preventDefault();const append=event?.currentTarget?.id==="recordsMoreButton",button=append?document.getElementById("recordsMoreButton"):document.getElementById("recordsLoadButton"),status=document.getElementById("recordsStatus");if(!append)recordsOffset=0;button.disabled=true;if(!append)button.textContent="Đang tải...";status.style.display="none";
 try{const params=new URLSearchParams({role:ROLE_KEY,from:document.getElementById("fromDate").value,to:document.getElementById("toDate").value,school_id:document.getElementById("school").value,q:document.getElementById("recordSearch").value,category:document.getElementById("recordCategory").value,limit:"200",offset:String(recordsOffset)});const response=await fetch(`${RECORDS_API}?${params}`,{cache:"no-store"}),data=await response.json();if(!response.ok)throw new Error(data.error||"Không tải được hồ sơ học sinh.");renderStudentRecords(data.records||[],append);recordsOffset+=data.records?.length||0;document.getElementById("recordsMoreButton").hidden=!data.has_more}
 catch(error){status.textContent=`Không tải được hồ sơ học sinh: ${error.message}`;status.style.display="block"}
 finally{button.disabled=false;if(!append)button.textContent="Tìm hồ sơ"}
}
function renderData(data){
 currentData=data;const values=data.metrics;
 setMetric("studentProfiles",values.student_profiles);
 metrics.forEach(([id,,key])=>setMetric(id,values[key]));
 const bhyt=document.getElementById("bhytSummary");if(bhyt)bhyt.textContent=`${numberFormat.format(values.bhyt_records)} hồ sơ · ${numberFormat.format(values.bhyt_valid)} học sinh có ngày hết hạn chưa qua · ${numberFormat.format(values.bhyt_expiring_soon)} hồ sơ hết hạn trong 30 ngày tới · ${numberFormat.format(values.bhyt_expired)} hồ sơ đã qua ngày hết hạn.`;
 const health=document.getElementById("healthSummary");if(health)health.textContent=`${numberFormat.format(values.students_with_health_profiles)} học sinh có hồ sơ sức khỏe · ${numberFormat.format(values.allergy_profiles)} hồ sơ có ghi nhận dị ứng · ${numberFormat.format(values.current_condition_profiles)} hồ sơ có ghi nhận bệnh hiện tại.`;
 charts.forEach((chart,index)=>drawChart(`chart-${index}`,chart,palette[chart.category]));
 renderSeriesTable(data.series||[]);
}
async function loadData(event){
 if(event)event.preventDefault();const button=document.getElementById("loadButton"),status=document.getElementById("status");button.disabled=true;button.textContent="Đang tải...";status.style.display="none";
 try{const params=new URLSearchParams({from:document.getElementById("fromDate").value,to:document.getElementById("toDate").value,school_id:document.getElementById("school").value});if(params.get("from")&&params.get("to")&&params.get("from")>params.get("to"))throw new Error("Ngày bắt đầu không được sau ngày kết thúc.");const response=await fetch(`${API}?${params}`),data=await response.json();if(!response.ok)throw new Error(data.error||"Không tải được số liệu y tế.");renderData(data);await loadStudentRecords()}
 catch(error){status.textContent=`Không tải được dữ liệu y tế: ${error.message}`;status.style.display="block"}
 finally{button.disabled=false;button.textContent="Lọc dữ liệu"}
}
async function start(){
 const status=document.getElementById("status");try{
  const formatDate=value=>`${value.getFullYear()}-${String(value.getMonth()+1).padStart(2,"0")}-${String(value.getDate()).padStart(2,"0")}`,today=new Date(),end=formatDate(today),startDate=new Date(today);startDate.setFullYear(startDate.getFullYear()-3);const start=formatDate(startDate);
  document.getElementById("fromDate").value=start;document.getElementById("toDate").value=end;
  const response=await fetch(`${API}?${new URLSearchParams({from:start,to:end})}`),data=await response.json();if(!response.ok)throw new Error(data.error||"Không tải được danh mục cơ sở.");
  const select=document.getElementById("school");data.schools.forEach(item=>{const option=document.createElement("option");option.value=item.id;option.textContent=item.name||`Cơ sở ${item.id}`;select.appendChild(option)});
  document.getElementById("filters").addEventListener("submit",loadData);document.getElementById("recordFilters").addEventListener("submit",loadStudentRecords);document.getElementById("recordsMoreButton").addEventListener("click",loadStudentRecords);renderData(data);await loadStudentRecords();
 }catch(error){status.textContent=`Không tải được dashboard: ${error.message}`;status.style.display="block"}
}
start();
</script>
{% endif %}
</body></html>"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("APP_PORT", "5000")), debug=False)
