"""
Data Access Layer for Business Dashboard.
Provides daily and monthly call/credit aggregations.
"""

import psycopg2
import traceback
from utils import get_db_connection, build_organization_filter_clause
import calendar


def fetch_daily_call_breakdown(start_utc, end_utc, year, month):
    """
    Returns daily call counts and credits – one entry per calendar day in (year, month) –
    for DONE calls that occurred on each day (IST).

    Args:
        start_utc (str): UTC datetime string for the start of the period, e.g. '2026-02-01 18:30:00'
        end_utc (str):   UTC datetime string for the end of the period,   e.g. '2026-02-28 18:29:59'
        year (int):  Calendar year  (used to size the output arrays)
        month (int): Calendar month (used to size the output arrays)

    Returns:
        dict: {
            'calls':   list[int],   len == days_in_month, index 0 == day 1
            'credits': list[float], len == days_in_month, index 0 == day 1
        }
    """
    conn = get_db_connection()
    org_filter_clause, accessible_ids = build_organization_filter_clause(column_name='jp.organization_id')
    days_in_month = calendar.monthrange(year, month)[1]
    daily_calls   = [0]   * days_in_month
    daily_credits = [0.0] * days_in_month

    try:
        with conn.cursor() as cur:
            org_params = accessible_ids if accessible_ids else []
            org_where = org_filter_clause if org_filter_clause else ""

            query = f"""
                SELECT
                    EXTRACT(DAY FROM cl.created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata')::int AS day_num,
                    COUNT(*) AS calls,
                    SUM(COALESCE(cl.call_credits, 1.0)) AS credits
                FROM call_management_calllog cl
                JOIN jobs_management_jobprofile jp ON cl.assistant_id = jp.profile_assistant_id
                WHERE cl.call_status = 'DONE'
                  AND cl.created_at BETWEEN %s AND %s
                  AND EXTRACT(MONTH FROM cl.created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata') = %s
                  AND EXTRACT(YEAR  FROM cl.created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata') = %s
                  {org_where}
                GROUP BY day_num
                ORDER BY day_num
            """
            params = [start_utc, end_utc, month, year] + org_params
            cur.execute(query, params)
            rows = cur.fetchall()
            for row in rows:
                day_idx = int(row['day_num']) - 1  # 0-indexed
                if 0 <= day_idx < days_in_month:
                    daily_calls[day_idx]   = int(row['calls'])
                    daily_credits[day_idx] = round(float(row['credits']), 1)
    except psycopg2.Error as e:
        print(f"Database error in fetch_daily_call_breakdown: {e}")
        traceback.print_exc()
    finally:
        conn.close()

    return {'calls': daily_calls, 'credits': daily_credits}


def fetch_org_id_by_name(company_name):
    """
    Look up organization UUID by exact company_name.

    Args:
        company_name (str): The organization's company_name in the DB.

    Returns:
        str | None: UUID string, or None if not found.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM user_management_organization WHERE company_name = %s LIMIT 1",
                [company_name]
            )
            row = cur.fetchone()
            return str(row['id']) if row else None
    except psycopg2.Error as e:
        print(f"Database error in fetch_org_id_by_name: {e}")
        return None
    finally:
        conn.close()


def fetch_all_clients_summary():
    """
    Returns all-time per-org DONE call counts and credits for the Complete Client View table.
    Respects RBAC (admin sees all orgs, restricted users see their org only).

    Returns:
        list[dict]: Each dict has keys: org_id (str), name (str), calls (int), credits (float).
                    Ordered by org name.
    """
    conn = get_db_connection()
    org_filter_clause, accessible_ids = build_organization_filter_clause(column_name='o.id')
    results = []

    try:
        with conn.cursor() as cur:
            org_params = accessible_ids if accessible_ids else []
            org_where = org_filter_clause if org_filter_clause else ""

            # Step 1: get all organizations (RBAC filtered)
            cur.execute(f"""
                SELECT id, company_name
                FROM user_management_organization o
                WHERE 1=1 {org_where}
                ORDER BY company_name
            """, org_params)
            orgs = cur.fetchall()

            if not orgs:
                return []

            org_ids = [str(org['id']) for org in orgs]
            placeholders = ', '.join(['%s'] * len(org_ids))

            # Step 2: all-time DONE call counts per org
            cur.execute(f"""
                SELECT jp.organization_id, COUNT(cl.id) AS calls
                FROM call_management_calllog cl
                JOIN jobs_management_jobprofile jp ON cl.assistant_id = jp.profile_assistant_id
                WHERE cl.call_status = 'DONE'
                  AND jp.organization_id IN ({placeholders})
                GROUP BY jp.organization_id
            """, org_ids)
            calls_by_org = {str(row['organization_id']): int(row['calls']) for row in cur.fetchall()}

            # Step 3: all-time credits per org
            cur.execute(f"""
                SELECT jp.organization_id, SUM(COALESCE(cl.call_credits, 1.0)) AS credits
                FROM call_management_calllog cl
                JOIN jobs_management_jobprofile jp ON cl.assistant_id = jp.profile_assistant_id
                WHERE cl.call_status = 'DONE'
                  AND jp.organization_id IN ({placeholders})
                GROUP BY jp.organization_id
            """, org_ids)
            credits_by_org = {str(row['organization_id']): float(row['credits']) for row in cur.fetchall()}

            for org in orgs:
                oid = str(org['id'])
                results.append({
                    'org_id': oid,
                    'name': org['company_name'],
                    'calls': calls_by_org.get(oid, 0),
                    'credits': round(credits_by_org.get(oid, 0.0), 1),
                })

    except psycopg2.Error as e:
        print(f"Database error in fetch_all_clients_summary: {e}")
        traceback.print_exc()
    finally:
        conn.close()

    return results


def fetch_per_credit_cost_inr(org_ids):
    """
    Returns {org_id: per_interview_cost_inr} for the given list of org UUIDs.
    Missing or NULL values default to 0.0.
    """
    if not org_ids:
        return {}
    conn = get_db_connection()
    result = {}
    try:
        with conn.cursor() as cur:
            placeholders = ', '.join(['%s'] * len(org_ids))
            cur.execute(f"""
                SELECT id, per_interview_cost_inr
                FROM user_management_organization
                WHERE id::text IN ({placeholders})
            """, org_ids)
            for row in cur.fetchall():
                cost = row.get('per_interview_cost_inr')
                result[str(row['id'])] = float(cost) if cost is not None else 0.0
    except psycopg2.Error as e:
        print(f"Database error in fetch_per_credit_cost_inr: {e}")
    finally:
        conn.close()
    return result


def fetch_plan_data(org_ids):
    """
    Returns {org_id: {'period_end': 'YYYY-MM-DD' | None, 'interview_quota': int | None}}
    for the given list of org UUIDs. Used for Plan End and Quota Left columns.
    """
    if not org_ids:
        return {}
    conn = get_db_connection()
    result = {}
    try:
        with conn.cursor() as cur:
            placeholders = ', '.join(['%s'] * len(org_ids))
            cur.execute(f"""
                SELECT id, period_end, interview_quota
                FROM user_management_organization
                WHERE id::text IN ({placeholders})
            """, org_ids)
            for row in cur.fetchall():
                period_end = row.get('period_end')
                quota = row.get('interview_quota')
                result[str(row['id'])] = {
                    'period_end':       str(period_end)[:10] if period_end is not None else None,
                    'interview_quota':  int(quota) if quota is not None else None,
                }
    except psycopg2.Error as e:
        print(f"Database error in fetch_plan_data: {e}")
    finally:
        conn.close()
    return result


def fetch_monthly_summaries(start_utc, end_utc):
    """
    Returns per-month aggregates of DONE calls and credits within the given UTC window.

    Args:
        start_utc (str): UTC datetime string for the start of the history window.
        end_utc (str):   UTC datetime string for the end of the history window.

    Returns:
        list[dict]: Each dict has keys: year_month (str 'YYYY-MM'), calls (int), credits (float).
                    Ordered oldest → newest.
    """
    conn = get_db_connection()
    org_filter_clause, accessible_ids = build_organization_filter_clause(column_name='jp.organization_id')
    results = []

    try:
        with conn.cursor() as cur:
            org_params = accessible_ids if accessible_ids else []
            org_where = org_filter_clause if org_filter_clause else ""

            query = f"""
                SELECT
                    TO_CHAR(cl.created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM') AS year_month,
                    COUNT(*) AS calls,
                    SUM(COALESCE(cl.call_credits, 1.0)) AS credits
                FROM call_management_calllog cl
                JOIN jobs_management_jobprofile jp ON cl.assistant_id = jp.profile_assistant_id
                WHERE cl.call_status = 'DONE'
                  AND cl.created_at BETWEEN %s AND %s
                  {org_where}
                GROUP BY year_month
                ORDER BY year_month
            """
            params = [start_utc, end_utc] + org_params
            cur.execute(query, params)
            rows = cur.fetchall()
            for row in rows:
                results.append({
                    'year_month': row['year_month'],
                    'calls': int(row['calls']),
                    'credits': float(row['credits']),
                })
    except psycopg2.Error as e:
        print(f"Database error in fetch_monthly_summaries: {e}")
        traceback.print_exc()
    finally:
        conn.close()

    return results
