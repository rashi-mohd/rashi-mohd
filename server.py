from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
import psycopg2
import json
from utils import (
    get_utc_date_filters, get_app_type_filter, get_agent_type_filter, get_organization_filter, auth_required, check_api_user, SECRET_KEY, is_admin, get_session_user_email, can_update_subscription
)
from data_access.recruiter_dashboard import (
    fetch_clients_overview, fetch_overview_stats,
    fetch_client_summary_stats, fetch_job_summary_stats, fetch_call_reasons_summary,
    fetch_jobs, fetch_client_job_list_with_stats, fetch_client_call_reasons_summary,
    fetch_job_call_reasons_summary
)
from data_access.recruiter_analysis import (
    fetch_recruiter_call_reasons_trend,
    fetch_recruiter_flaws_trend,
    fetch_bad_recruiter_calls,
    fetch_failed_call_cut_short_calls,
    fetch_calls_with_specific_duration,
    fetch_low_candidate_rating_calls,
    fetch_distinct_call_ended_reasons_for_dropdown,
    fetch_calls_by_reason,
    fetch_distinct_recruiter_flaws_for_dropdown,
    fetch_calls_by_flaw,
    fetch_candidate_feedback_distribution,
    fetch_feedback_issue_distribution,
)
from data_access.job_analysis import (
    fetch_all_jobs_with_detailed_stats
)
from data_access.cheating import (
    fetch_cheating_analysis_trend,
    fetch_cheating_summary_stats,
    fetch_cheating_detection_breakdown,
    fetch_top_jobs_by_cheating_percentage,
    fetch_organization_cheating_stats,
    fetch_null_analysis_trend
)
from data_access.evaluation_quality import (
    fetch_clearance_rate_stats,
    fetch_clearance_rate_trend,
    fetch_lowest_clearance_jobs,
    fetch_communication_errors_trend,
    fetch_call_analysis_errors_trend,
    fetch_communication_analysis_error_calls,
    fetch_call_analysis_error_calls,
    fetch_suspicious_clearance_trend,
    fetch_suspicious_cleared_calls,
    fetch_question_type_clearance_rate_trend
)
from data_access.candidate_reported_issues import (
    fetch_reported_issues_trend,
    fetch_reported_issues_summary,
    fetch_reported_issues_table
)
from data_access.call_duration_analysis import (
    fetch_call_duration_analysis,
    upsert_call_duration_comment,
    fetch_rca_categories,
    get_call_rca_detail,
    fetch_mismatch_sids_for_date,
)
from services.call_duration_analysis import run_call_duration_analysis, get_analysis_status
from services.call_duration_rca import run_rca_for_call, is_posthog_configured
from data_access.interview_potentials import (
    get_potential,
    get_potentials_for_organization,
    get_potentials_for_period,
    get_potentials_for_date_range,
    upsert_potential,
    delete_potential,
    get_all_potentials
)
from data_access.subscription_updates import get_subscription_logs_for_organization
from services.subscription_service import process_subscription_update
from services.potential_integration import add_potential_status_to_clients, add_potential_status_to_clients_with_date_range
from data_access.business_dashboard import fetch_daily_call_breakdown, fetch_monthly_summaries, fetch_org_id_by_name, fetch_per_credit_cost_inr
from data_access.notification_config import fetch_all_organizations, fetch_organization_with_config, fetch_jobs_for_organization, fetch_job_with_config
from services.notification_config import update_org_notification_config, update_job_notification_config

app = Flask(__name__)
app.secret_key = SECRET_KEY

# --- Authentication Routes ---


@app.route("/")
def login():
    if "authenticated" in session and session["authenticated"]:
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/auth", methods=["POST"])
def auth():
    email_form = request.form.get("email")
    passkey_form = request.form.get("passkey")
    
    if not email_form or not passkey_form:
        return render_template("login.html", error="Both email and passkey are required")
    
    # Load client config
    with open('config/clients.json', 'r') as f:
        clients_config = json.load(f)
    
    if email_form in clients_config:
        client_info = clients_config[email_form]

        if client_info["passkey"] == passkey_form:
            session["authenticated"] = True
            session["user_email"] = email_form
            # Support both old (user_ids) and new (organization_ids) config format
            session["accessible_organization_ids"] = client_info.get("accessible_organization_ids", [])
            session["is_admin"] = client_info["is_admin"]
            session["can_update_subscription"] = client_info.get("can_update_subscription", False)

            next_url = request.args.get('next') or url_for('dashboard')
            return redirect(next_url)
    
    return render_template("login.html", error="Invalid email or passkey combination")



@app.route("/logout")
def logout():
    session.pop("authenticated", None)
    return redirect(url_for("login"))

# --- Dashboard Route ---


@app.route("/dashboard")
@auth_required
def dashboard():
    return render_template("dashboard.html", user=get_session_user_email(), is_admin=is_admin(), can_update_subscription=can_update_subscription())

# --- Recruiter Performance Dashboard Route ---
@app.route("/recruiter-call-analysis")
@auth_required
def recruiter_dashboard():
    return render_template("recruiter_dashboard.html", user=get_session_user_email(), is_admin=is_admin(), can_update_subscription=can_update_subscription())

# --- Job Analysis Page Route ---
@app.route("/job-analysis")
@auth_required
def job_analysis():
    return render_template("job_analysis.html", user=get_session_user_email(), is_admin=is_admin(), can_update_subscription=can_update_subscription())

# --- Cheating Analysis Page Route ---
@app.route("/cheating-analysis")
@auth_required
def cheating_analysis():
    # Only allow admin users to access this page
    if not is_admin():
        return redirect(url_for("dashboard"))
    return render_template("cheating_analysis.html", user=get_session_user_email(), is_admin=is_admin(), can_update_subscription=can_update_subscription())

# --- Evaluation Quality Page Route ---
@app.route("/evaluation-quality")
@auth_required
def evaluation_quality():
    if not is_admin():
        return redirect(url_for("dashboard"))
    return render_template("evaluation_quality.html", user=get_session_user_email(), is_admin=is_admin(), can_update_subscription=can_update_subscription())


# --- Candidate Reported Issues Page Route ---
@app.route("/candidate-reported-issues")
@auth_required
def candidate_reported_issues():
    if not is_admin():
        return redirect(url_for("dashboard"))
    return render_template("candidate_reported_issues.html", user=get_session_user_email(), is_admin=is_admin(), can_update_subscription=can_update_subscription())


# --- API Endpoints ---

# GET /api/clients - Fetches client list with PERIOD-FILTERED stats


@app.route("/api/clients", methods=["GET"])
def get_clients():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)

    try:
        # Compute previous period for credit change comparison
        prev_period_date_filter_sql = ""
        prev_period_query_params = []
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")

        if start_date and end_date:
            from datetime import datetime as dt_cls, timedelta
            start_dt = dt_cls.strptime(start_date, '%Y-%m-%d')
            end_dt = dt_cls.strptime(end_date, '%Y-%m-%d')
            window_days = (end_dt - start_dt).days + 1  # inclusive
            prev_end_dt = start_dt - timedelta(days=1)
            prev_start_dt = prev_end_dt - timedelta(days=window_days - 1)
            prev_args = {
                'start_date': prev_start_dt.strftime('%Y-%m-%d'),
                'end_date': prev_end_dt.strftime('%Y-%m-%d')
            }
            prev_period_date_filter_sql, prev_period_query_params = get_utc_date_filters(prev_args)

        client_data = fetch_clients_overview(
            date_filter_sql, query_params_date,
            prev_period_date_filter_sql=prev_period_date_filter_sql,
            prev_period_query_params=prev_period_query_params)

        # Add potential status to client data based on date range

        if start_date and end_date:
            # Use date-aware calculation with pro-rating
            client_data = add_potential_status_to_clients_with_date_range(
                client_data, start_date, end_date
            )
        else:
            # No date range specified, use current month
            from datetime import datetime
            now = datetime.now()
            client_data = add_potential_status_to_clients(client_data, now.year, now.month)

        return jsonify(client_data)
    except psycopg2.Error as e:
        print(f"API Error in /api/clients: {e}")
        return jsonify({"error": "Database error fetching client list"}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Unexpected API Error in /api/clients: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


# GET /api/overview_stats - Fetches overall stats for the dashboard homepage (Period Filtered)
@app.route("/api/overview_stats", methods=["GET"])
def get_overview_stats_api():  # Renamed function slightly to avoid clash
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    app_type_filter_sql = get_app_type_filter(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)
    # Admin-only toggle: exclude internal Fabric orgs from overall metrics.
    # Default (false) → show all orgs including internal ones.
    exclude_internal = request.args.get("exclude_internal", "false").lower() == "true"

    try:
        stats = fetch_overview_stats(date_filter_sql, query_params_date, app_type_filter_sql, agent_type_filter_sql, exclude_internal=exclude_internal)
        return jsonify(stats)
    except psycopg2.Error as e:
        print(f"API Error in /api/overview_stats: {e}")
        return jsonify({"error": f"Database error fetching overview stats: {e}"}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Unexpected API Error in /api/overview_stats: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


# GET /api/job_stats - Fetches list of jobs for a specific organization (NOT period filtered)
@app.route("/api/job_stats", methods=["GET"])
def get_job_stats():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    organization_id = request.args.get("organization_id") or request.args.get("user_id")  # Support both for backward compatibility
    if not organization_id:
        return jsonify({"error": "organization_id parameter is required"}), 400

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)

    try:
        job_list_with_stats = fetch_client_job_list_with_stats(
            organization_id, date_filter_sql, query_params_date)
        return jsonify(job_list_with_stats)
    except psycopg2.Error as e:
        print(f"API Error in /api/job_stats: {e}")
        return jsonify({"error": "Database error fetching job list"}), 500
    except Exception as e:
        print(f"Unexpected API Error in /api/job_stats: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


# GET /api/client_summary_stats - Fetches detailed stats for a specific organization (Period Filtered)
@app.route("/api/client_summary_stats", methods=["GET"])
def get_client_summary_stats_api():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    organization_id = request.args.get("organization_id") or request.args.get("user_id")  # Support both for backward compatibility
    if not organization_id:
        return jsonify({"error": "organization_id parameter is required"}), 400

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    app_type_filter_sql = get_app_type_filter(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        summary_data = fetch_client_summary_stats(
            organization_id, date_filter_sql, query_params_date, app_type_filter_sql, agent_type_filter_sql)
        return jsonify(summary_data)
    except psycopg2.Error as e:
        print(f"API Error in /api/client_summary_stats: {e}")
        return jsonify({"error": f"Database error fetching client summary stats: {e}"}), 500
    except Exception as e:
        print(f"Unexpected API Error in /api/client_summary_stats: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


# GET /api/job_summary_stats - Fetches detailed stats and trends for a specific job (Period Filtered)
@app.route("/api/job_summary_stats", methods=["GET"])
def get_job_summary_stats_api():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify({"error": "job_id parameter is required"}), 400

    group_by = request.args.get("group_by", "week")
    if group_by not in ['day', 'week']:
        group_by = 'week'

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    app_type_filter_sql = get_app_type_filter(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        job_data = fetch_job_summary_stats(
            job_id, date_filter_sql, query_params_date, group_by, app_type_filter_sql, agent_type_filter_sql)
        return jsonify(job_data)
    except psycopg2.Error as e:
        print(f"API Error in /api/job_summary_stats: {e}")
        return jsonify({"error": f"Database error fetching job summary stats: {e}"}), 500
    except Exception as e:
        print(f"Unexpected API Error in /api/job_summary_stats: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

# GET /api/call_reasons_summary - NEW: Fetches call ended reason counts (Period Filtered)


@app.route("/api/call_reasons_summary", methods=["GET"])
def get_call_reasons_summary():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        reasons_data = fetch_call_reasons_summary(
            date_filter_sql, query_params_date, agent_type_filter_sql)
        return jsonify(reasons_data)
    except psycopg2.Error as e:
        print(f"API Error in /api/call_reasons_summary: {e}")
        return jsonify({"error": f"Database error fetching call reason summary: {e}"}), 500
    except Exception as e:
        print(f"Unexpected API Error in /api/call_reasons_summary: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/client_call_reasons", methods=["GET"])
def get_client_call_reasons():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    organization_id = request.args.get("organization_id") or request.args.get("user_id")  # Support both for backward compatibility
    if not organization_id:
        return jsonify({"error": "organization_id parameter is required"}), 400

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)
    try:
        reasons_data = fetch_client_call_reasons_summary(
            organization_id, date_filter_sql, query_params_date, agent_type_filter_sql)
        return jsonify(reasons_data)
    except psycopg2.Error as e:
        print(f"API Error in /api/client_call_reasons: {e}")
        return jsonify({"error": f"Database error fetching client call reason summary: {e}"}), 500
    except Exception as e:
        print(f"Unexpected API Error in /api/client_call_reasons: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/job_call_reasons", methods=["GET"])
def get_job_call_reasons():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify({"error": "job_id parameter is required"}), 400

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)
    try:
        reasons_data = fetch_job_call_reasons_summary(
            job_id, date_filter_sql, query_params_date, agent_type_filter_sql)
        return jsonify(reasons_data)
    except psycopg2.Error as e:
        print(f"API Error in /api/job_call_reasons: {e}")
        return jsonify({"error": f"Database error fetching job call reason summary: {e}"}), 500
    except Exception as e:
        print(f"Unexpected API Error in /api/job_call_reasons: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/jobs", methods=["GET"])
def get_jobs_api():
    auth_error = check_api_user()
    if auth_error:
        return auth_error
    user_id = request.args.get("user_id")
    try:
        jobs_list = fetch_jobs(user_id)
        return jsonify(jobs_list)
    except psycopg2.Error as e:
        print(f"API Error in /api/jobs: {e}")
        return jsonify({"error": "Database error fetching job list"}), 500
    except Exception as e:
        print(f"Unexpected API Error in /api/jobs: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/job_analysis_data", methods=["GET"])
def get_job_analysis_data():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    try:
        all_jobs_data = fetch_all_jobs_with_detailed_stats(date_filter_sql, query_params_date)
        return jsonify(all_jobs_data)
    except psycopg2.Error as e:
        print(f"API Error in /api/job_analysis_data: {e}")
        return jsonify({"error": f"Database error fetching job analysis data: {e}"}), 500
    except Exception as e:
        print(f"Unexpected API Error in /api/job_analysis_data: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


# --- Recruiter Performance API Endpoints ---

@app.route("/api/recruiter/feedback_distribution", methods=["GET"])
def get_candidate_feedback_distribution():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_candidate_feedback_distribution(
            date_filter_sql, query_params_date, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/recruiter/feedback_distribution: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/recruiter/feedback_issue_distribution", methods=["GET"])
def get_feedback_issue_distribution():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)
    group_by = request.args.get("group_by", "week")
    if group_by not in ['day', 'week', 'month']:
        group_by = 'week'

    try:
        data = fetch_feedback_issue_distribution(
            date_filter_sql, query_params_date, group_by, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/recruiter/feedback_issue_distribution: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/recruiter/reasons_trend", methods=["GET"])
def get_recruiter_reasons_trend():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)
    group_by = request.args.get("group_by", "week")
    if group_by not in ['day', 'week']:
        group_by = 'week'

    try:
        trend_data = fetch_recruiter_call_reasons_trend(
            date_filter_sql, query_params_date, group_by, agent_type_filter_sql)
        return jsonify(trend_data)
    except psycopg2.Error as e:
        print(f"API Error in /api/recruiter/reasons_trend: {e}")
        return jsonify({"error": f"Database error fetching reasons trend: {e}"}), 500
    except Exception as e:
        print(f"Unexpected API Error in /api/recruiter/reasons_trend: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/recruiter/flaws_trend", methods=["GET"])
def get_recruiter_flaws_trend():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)
    group_by = request.args.get("group_by", "week")
    if group_by not in ['day', 'week']:
        group_by = 'week'

    try:
        trend_data = fetch_recruiter_flaws_trend(
            date_filter_sql, query_params_date, group_by, agent_type_filter_sql)
        return jsonify(trend_data)
    except psycopg2.Error as e:
        print(f"API Error in /api/recruiter/flaws_trend: {e}")
        return jsonify({"error": f"Database error fetching flaws trend: {e}"}), 500
    except ValueError as e:
        print(f"API Error (Value Error) in /api/recruiter/flaws_trend: {e}")
        return jsonify({"error": f"Error processing call analysis data: {e}"}), 500
    except Exception as e:
        print(f"Unexpected API Error in /api/recruiter/flaws_trend: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/recruiter/bad_calls", methods=["GET"])
def get_bad_recruiter_calls():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        bad_calls = fetch_bad_recruiter_calls(
            date_filter_sql, query_params_date, agent_type_filter_sql=agent_type_filter_sql)
        return jsonify(bad_calls)
    except psycopg2.Error as e:
        print(f"API Error in /api/recruiter/bad_calls: {e}")
        return jsonify({"error": f"Database error fetching bad calls: {e}"}), 500
    except ValueError as e:
        print(f"API Error (Value Error) in /api/recruiter/bad_calls: {e}")
        return jsonify({"error": f"Error processing call analysis data: {e}"}), 500
    except Exception as e:
        print(f"Unexpected API Error in /api/recruiter/bad_calls: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/recruiter/failed_call_cut_short", methods=["GET"])
def get_failed_call_cut_short():
    auth_error = check_api_user()
    if auth_error:
        return auth_error
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)
    try:
        data = fetch_failed_call_cut_short_calls(
            date_filter_sql, query_params_date, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/recruiter/failed_call_cut_short: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/recruiter/calls_specific_duration", methods=["GET"])
def get_calls_specific_duration():
    auth_error = check_api_user()
    if auth_error:
        return auth_error
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)
    try:
        # Durations can be passed as params if needed, otherwise use defaults
        min_dur = request.args.get('min_duration', 180, type=int)
        max_dur = request.args.get('max_duration', 360, type=int)
        data = fetch_calls_with_specific_duration(
            date_filter_sql, query_params_date, min_dur, max_dur, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/recruiter/calls_specific_duration: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/recruiter/low_candidate_rating_calls", methods=["GET"])
def get_low_candidate_rating_calls():
    auth_error = check_api_user()
    if auth_error:
        return auth_error
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)
    try:
        data = fetch_low_candidate_rating_calls(
            date_filter_sql, query_params_date, agent_type_filter_sql=agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/recruiter/low_candidate_rating_calls: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/recruiter/distinct_call_reasons", methods=["GET"])
def get_distinct_call_reasons():
    auth_error = check_api_user()
    if auth_error:
        return auth_error
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)
    try:
        data = fetch_distinct_call_ended_reasons_for_dropdown(
            date_filter_sql, query_params_date, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/recruiter/distinct_call_reasons: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/recruiter/calls_by_reason", methods=["GET"])
def get_calls_by_reason_api():
    auth_error = check_api_user()
    if auth_error:
        return auth_error
    reason = request.args.get("reason")
    if not reason:
        return jsonify({"error": "reason parameter is required"}), 400
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)
    try:
        data = fetch_calls_by_reason(
            date_filter_sql, query_params_date, reason, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/recruiter/calls_by_reason: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/recruiter/distinct_recruiter_flaws", methods=["GET"])
def get_distinct_recruiter_flaws():
    auth_error = check_api_user()
    if auth_error:
        return auth_error
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)
    try:
        data = fetch_distinct_recruiter_flaws_for_dropdown(
            date_filter_sql, query_params_date, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/recruiter/distinct_recruiter_flaws: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/recruiter/calls_by_flaw", methods=["GET"])
def get_calls_by_flaw_api():
    auth_error = check_api_user()
    if auth_error:
        return auth_error
    flaw = request.args.get("flaw")
    if not flaw:
        return jsonify({"error": "flaw parameter is required"}), 400
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)
    try:
        data = fetch_calls_by_flaw(date_filter_sql, query_params_date, flaw, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/recruiter/calls_by_flaw: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


# --- Cheating Analysis API Endpoints ---

@app.route("/api/cheating_analysis_trend", methods=["GET"])
def get_cheating_analysis_trend():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    group_by = request.args.get("group_by", "month")
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_cheating_analysis_trend(date_filter_sql, query_params_date, group_by, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/cheating_analysis_trend: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/cheating_summary_stats", methods=["GET"])
def get_cheating_summary_stats():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_cheating_summary_stats(date_filter_sql, query_params_date, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/cheating_summary_stats: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/cheating_detection_breakdown", methods=["GET"])
def get_cheating_detection_breakdown():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    group_by = request.args.get("group_by", "month")
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_cheating_detection_breakdown(date_filter_sql, query_params_date, group_by, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/cheating_detection_breakdown: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/null_analysis_trend", methods=["GET"])
def get_null_analysis_trend():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    group_by = request.args.get("group_by", "month")
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_null_analysis_trend(date_filter_sql, query_params_date, group_by, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/null_analysis_trend: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/top_jobs_cheating", methods=["GET"])
def get_top_jobs_cheating():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)
    organization_filter_sql, organization_filter_params = get_organization_filter(request.args)

    try:
        data = fetch_top_jobs_by_cheating_percentage(
            date_filter_sql,
            query_params_date,
            agent_type_filter_sql,
            organization_filter_sql,
            organization_filter_params
        )
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/top_jobs_cheating: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/organization_cheating_stats", methods=["GET"])
def get_organization_cheating_stats():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_organization_cheating_stats(
            date_filter_sql,
            query_params_date,
            agent_type_filter_sql
        )
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/organization_cheating_stats: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


# --- Evaluation Quality API Endpoints ---

@app.route("/api/evaluation/clearance_rate_stats", methods=["GET"])
def get_clearance_rate_stats():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_clearance_rate_stats(date_filter_sql, query_params_date, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/evaluation/clearance_rate_stats: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/evaluation/clearance_rate_trend", methods=["GET"])
def get_clearance_rate_trend():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    group_by = request.args.get("group_by", "month")
    if group_by not in ['day', 'week', 'month']:
        group_by = 'month'
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_clearance_rate_trend(date_filter_sql, query_params_date, group_by, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/evaluation/clearance_rate_trend: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/evaluation/question_type_clearance_rate_trend", methods=["GET"])
def get_question_type_clearance_rate_trend():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    group_by = request.args.get("group_by", "month")
    if group_by not in ['day', 'week', 'month']:
        group_by = 'month'
    question_type = request.args.get("question_type", "general")
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)

    try:
        data = fetch_question_type_clearance_rate_trend(
            date_filter_sql, query_params_date, group_by, question_type
        )
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/evaluation/question_type_clearance_rate_trend: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/evaluation/lowest_clearance_jobs", methods=["GET"])
def get_lowest_clearance_jobs():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_lowest_clearance_jobs(date_filter_sql, query_params_date, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/evaluation/lowest_clearance_jobs: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/evaluation/communication_errors_trend", methods=["GET"])
def get_communication_errors_trend():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    group_by = request.args.get("group_by", "month")
    if group_by not in ['day', 'week', 'month']:
        group_by = 'month'
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_communication_errors_trend(date_filter_sql, query_params_date, group_by, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/evaluation/communication_errors_trend: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/evaluation/call_analysis_errors_trend", methods=["GET"])
def get_call_analysis_errors_trend():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    group_by = request.args.get("group_by", "month")
    if group_by not in ['day', 'week', 'month']:
        group_by = 'month'
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_call_analysis_errors_trend(date_filter_sql, query_params_date, group_by, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/evaluation/call_analysis_errors_trend: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/evaluation/communication_error_calls", methods=["GET"])
def get_communication_error_calls():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_communication_analysis_error_calls(date_filter_sql, query_params_date, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/evaluation/communication_error_calls: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/evaluation/call_analysis_error_calls", methods=["GET"])
def get_call_analysis_error_calls():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_call_analysis_error_calls(date_filter_sql, query_params_date, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/evaluation/call_analysis_error_calls: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/evaluation/suspicious_clearance_trend", methods=["GET"])
def get_suspicious_clearance_trend():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    group_by = request.args.get("group_by", "month")
    if group_by not in ['day', 'week', 'month']:
        group_by = 'month'
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_suspicious_clearance_trend(date_filter_sql, query_params_date, group_by, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/evaluation/suspicious_clearance_trend: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/evaluation/suspicious_cleared_calls", methods=["GET"])
def get_suspicious_cleared_calls():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_suspicious_cleared_calls(date_filter_sql, query_params_date, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/evaluation/suspicious_cleared_calls: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


# --- Candidate Reported Issues API Endpoints ---

@app.route("/api/reported_issues/trend", methods=["GET"])
def get_reported_issues_trend():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    group_by = request.args.get("group_by", "month")
    if group_by not in ['day', 'week', 'month']:
        group_by = 'month'
    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_reported_issues_trend(date_filter_sql, query_params_date, group_by, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/reported_issues/trend: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/reported_issues/summary", methods=["GET"])
def get_reported_issues_summary():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_reported_issues_summary(date_filter_sql, query_params_date, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/reported_issues/summary: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/reported_issues/table", methods=["GET"])
def get_reported_issues_table():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    date_filter_sql, query_params_date = get_utc_date_filters(request.args)
    agent_type_filter_sql = get_agent_type_filter(request.args)

    try:
        data = fetch_reported_issues_table(date_filter_sql, query_params_date, agent_type_filter_sql)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/reported_issues/table: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


# --- Interview Potentials API Endpoints ---

# GET /api/interview_potential - Get potential for specific organization, year, month
@app.route("/api/interview_potential", methods=["GET"])
def get_interview_potential():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    organization_id = request.args.get("organization_id")
    year = request.args.get("year")
    month = request.args.get("month")

    if not organization_id:
        return jsonify({"error": "organization_id parameter is required"}), 400

    if not year or not month:
        return jsonify({"error": "year and month parameters are required"}), 400

    try:
        year = int(year)
        month = int(month)
    except ValueError:
        return jsonify({"error": "year and month must be integers"}), 400

    try:
        potential_data = get_potential(organization_id, year, month)
        if potential_data:
            return jsonify(potential_data)
        else:
            return jsonify({"message": "No potential set for this period"}), 404
    except Exception as e:
        print(f"API Error in /api/interview_potential: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "An unexpected error occurred"}), 500


# GET /api/interview_potentials/organization - Get all potentials for an organization
@app.route("/api/interview_potentials/organization", methods=["GET"])
def get_organization_potentials():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    organization_id = request.args.get("organization_id")
    if not organization_id:
        return jsonify({"error": "organization_id parameter is required"}), 400

    try:
        potentials = get_potentials_for_organization(organization_id)
        return jsonify(potentials)
    except Exception as e:
        print(f"API Error in /api/interview_potentials/organization: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "An unexpected error occurred"}), 500


# GET /api/interview_potentials/period - Get all potentials for a specific period
@app.route("/api/interview_potentials/period", methods=["GET"])
def get_period_potentials():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    year = request.args.get("year")
    month = request.args.get("month")

    if not year or not month:
        return jsonify({"error": "year and month parameters are required"}), 400

    try:
        year = int(year)
        month = int(month)
    except ValueError:
        return jsonify({"error": "year and month must be integers"}), 400

    try:
        potentials = get_potentials_for_period(year, month)
        return jsonify(potentials)
    except Exception as e:
        print(f"API Error in /api/interview_potentials/period: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "An unexpected error occurred"}), 500


# POST /api/interview_potential - Create or update a potential
@app.route("/api/interview_potential", methods=["POST"])
def save_interview_potential():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    # Get JSON data from request body
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    organization_id = data.get("organization_id")
    company_name = data.get("company_name")
    year = data.get("year")
    month = data.get("month")
    potential = data.get("potential")

    # Validation
    if not organization_id:
        return jsonify({"error": "organization_id is required"}), 400

    if not company_name:
        return jsonify({"error": "company_name is required"}), 400

    if year is None or month is None:
        return jsonify({"error": "year and month are required"}), 400

    if potential is None:
        return jsonify({"error": "potential is required"}), 400

    try:
        year = int(year)
        month = int(month)
        potential = int(potential)
    except (ValueError, TypeError):
        return jsonify({"error": "year, month, and potential must be integers"}), 400

    try:
        result = upsert_potential(organization_id, company_name, year, month, potential)
        return jsonify(result), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        print(f"API Error in /api/interview_potential POST: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "An unexpected error occurred"}), 500


# DELETE /api/interview_potential - Delete a potential
@app.route("/api/interview_potential", methods=["DELETE"])
def delete_interview_potential():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    organization_id = request.args.get("organization_id")
    year = request.args.get("year")
    month = request.args.get("month")

    if not organization_id or not year or not month:
        return jsonify({"error": "organization_id, year, and month are required"}), 400

    try:
        year = int(year)
        month = int(month)
    except ValueError:
        return jsonify({"error": "year and month must be integers"}), 400

    try:
        deleted = delete_potential(organization_id, year, month)
        if deleted:
            return jsonify({"message": "Potential deleted successfully"}), 200
        else:
            return jsonify({"error": "Potential not found"}), 404
    except Exception as e:
        print(f"API Error in /api/interview_potential DELETE: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "An unexpected error occurred"}), 500


# --- Subscription Update API Endpoints ---

@app.route("/api/subscription/<org_id>", methods=["PATCH"])
def update_subscription(org_id):
    """
    Update subscription for an organization via external API with logging

    Args:
        org_id: Organization UUID

    Returns:
        JSON response with success/error message
    """
    # Check authentication
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    # Check permission
    if not can_update_subscription():
        return jsonify({"error": "You do not have permission to update subscriptions"}), 403

    # Get request data
    request_data = request.get_json()
    if not request_data:
        return jsonify({"error": "Request body must be JSON"}), 400

    # Get current user email
    updated_by = get_session_user_email()
    # Get organization name for logging
    organization_name = request_data.get('organization_name', '')

    try:
        # Process subscription update
        success, response_data, status_code = process_subscription_update(
            organization_id=org_id,
            organization_name=organization_name,
            update_data=request_data,
            updated_by=updated_by
        )

        return jsonify(response_data), status_code

    except Exception as e:
        print(f"Unexpected error in /api/subscription PATCH: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/subscription/<org_id>", methods=["GET"])
def get_subscription(org_id):
    """
    Get current subscription data for an organization

    Args:
        org_id: Organization UUID

    Returns:
        JSON response with subscription data
    """
    # Check authentication
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    # Check permission
    if not can_update_subscription():
        return jsonify({"error": "You do not have permission to view subscription details"}), 403

    try:
        from data_access.subscription_updates import get_current_subscription_data_from_main_app
        subscription_data = get_current_subscription_data_from_main_app(org_id)

        if not subscription_data:
            return jsonify({"error": "Subscription not found"}), 404

        return jsonify(subscription_data), 200

    except Exception as e:
        print(f"Unexpected error in /api/subscription GET: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/subscription_logs/<org_id>", methods=["GET"])
def get_subscription_logs_endpoint(org_id):
    """
    Get subscription update logs for an organization

    Args:
        org_id: Organization UUID

    Returns:
        JSON response with list of subscription update logs
    """
    # Check authentication
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    # Check permission - only subscription managers or admins can view logs
    if not can_update_subscription() and not is_admin():
        return jsonify({"error": "You do not have permission to view subscription logs"}), 403

    try:
        logs = get_subscription_logs_for_organization(org_id)
        return jsonify(logs), 200

    except Exception as e:
        print(f"Unexpected error in /api/subscription_logs GET: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "An unexpected error occurred"}), 500


# --- Call Duration Analysis API Endpoints ---

@app.route("/api/call_duration_analysis", methods=["GET"])
def get_call_duration_analysis():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    if not is_admin():
        return jsonify({"error": "Admin access required"}), 403

    analysis_date = request.args.get("analysis_date")

    try:
        data = fetch_call_duration_analysis(analysis_date)
        return jsonify(data)
    except Exception as e:
        print(f"API Error in /api/call_duration_analysis: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "An unexpected error occurred"}), 500



@app.route("/api/call_duration_analysis/status", methods=["GET"])
@auth_required
def get_call_duration_analysis_status():
    """Check if analysis is currently running."""
    if not is_admin():
        return jsonify({"error": "Admin access required"}), 403

    analysis_date_str = request.args.get("analysis_date")
    try:
        status = get_analysis_status(analysis_date_str)
        return jsonify(status)
    except Exception as e:
        print(f"API Error in /api/call_duration_analysis/status: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/call_duration_analysis/run", methods=["POST"])
def trigger_call_duration_analysis():
    """Manual trigger for running analysis for a specific date."""
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    if not is_admin():
        return jsonify({"error": "Admin access required"}), 403

    from datetime import datetime as dt
    analysis_date_str = request.args.get("analysis_date")
    try:
        if analysis_date_str:
            target_date = dt.strptime(analysis_date_str, '%Y-%m-%d').date()
        else:
            target_date = None  # Defaults to yesterday IST

        result = run_call_duration_analysis(target_date)
        if result.get('already_running'):
            return jsonify(result), 409
        return jsonify(result)
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400
    except Exception as e:
        print(f"API Error in /api/call_duration_analysis/run: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Analysis failed: {str(e)}"}), 500


@app.route("/api/call_duration_analysis/comment", methods=["POST"])
def save_call_duration_comment():
    """Save or update a comment for a call duration analysis row."""
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    if not is_admin():
        return jsonify({"error": "Admin access required"}), 403

    body = request.get_json(silent=True) or {}
    call_sid = body.get("call_sid", "").strip()
    analysis_date = body.get("analysis_date", "").strip()
    comment = body.get("comment", "")

    if not call_sid or not analysis_date:
        return jsonify({"error": "call_sid and analysis_date are required"}), 400

    try:
        from datetime import datetime as dt
        dt.strptime(analysis_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({"error": "Invalid analysis_date format. Use YYYY-MM-DD."}), 400

    try:
        updated = upsert_call_duration_comment(call_sid, analysis_date, comment)
        if not updated:
            return jsonify({"error": "No matching record found for the given call_sid and analysis_date"}), 404
        return jsonify({"success": True})
    except Exception as e:
        print(f"API Error in /api/call_duration_analysis/comment: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/call_duration_analysis/rca_categories", methods=["GET"])
def get_rca_categories():
    """Return {call_sid: rca_category} for all rows of a date. Used for frontend polling."""
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    analysis_date = request.args.get("date", "").strip()
    if not analysis_date:
        return jsonify({"error": "date parameter required"}), 400

    try:
        categories = fetch_rca_categories(analysis_date)
        return jsonify({"categories": categories})
    except Exception as e:
        print(f"API Error in /api/call_duration_analysis/rca_categories: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/call_duration_analysis/rca_detail", methods=["GET"])
def get_rca_detail():
    """Return full RCA detail strings for a single call (for the RCA modal)."""
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    call_sid = request.args.get("call_sid", "").strip()
    analysis_date = request.args.get("date", "").strip()
    if not call_sid or not analysis_date:
        return jsonify({"error": "call_sid and date are required"}), 400

    try:
        detail = get_call_rca_detail(call_sid, analysis_date)
        if not detail:
            return jsonify({"error": "No RCA record found"}), 404
        return jsonify(detail)
    except Exception as e:
        print(f"API Error in /api/call_duration_analysis/rca_detail: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route("/api/call_duration_analysis/rca", methods=["POST"])
def trigger_rca_for_call():
    """Manually re-run RCA for a single call. Sets rca_category to Pending then runs in background."""
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    if not is_admin():
        return jsonify({"error": "Admin access required"}), 403

    if not is_posthog_configured():
        return jsonify({"error": "PostHog not configured on this server"}), 503

    body = request.get_json(silent=True) or {}
    call_sid = body.get("call_sid", "").strip()
    analysis_date = body.get("analysis_date", "").strip()

    if not call_sid or not analysis_date:
        return jsonify({"error": "call_sid and analysis_date are required"}), 400

    try:
        from datetime import datetime as dt
        dt.strptime(analysis_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({"error": "Invalid analysis_date format. Use YYYY-MM-DD."}), 400

    # Mark as Pending immediately
    from database.sqlite_db import get_sqlite_connection
    conn = get_sqlite_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE call_duration_analysis SET rca_category = 'Pending' WHERE call_sid = ? AND analysis_date = ?",
            (call_sid, analysis_date)
        )
        conn.commit()
        if cursor.rowcount == 0:
            return jsonify({"error": "No matching record found"}), 404
    finally:
        conn.close()

    import threading
    t = threading.Thread(target=run_rca_for_call, args=(call_sid, analysis_date), daemon=True)
    t.start()

    return jsonify({"success": True, "message": "RCA queued — check rca_categories for updates"})


@app.route("/api/call_duration_analysis/rca_date", methods=["POST"])
def trigger_rca_for_date():
    """Run RCA for all mismatch rows on a given date. Marks each as Pending, then runs sequentially in background."""
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    if not is_admin():
        return jsonify({"error": "Admin access required"}), 403

    if not is_posthog_configured():
        return jsonify({"error": "PostHog not configured on this server"}), 503

    body = request.get_json(silent=True) or {}
    analysis_date = body.get("analysis_date", "").strip()

    if not analysis_date:
        return jsonify({"error": "analysis_date is required"}), 400

    try:
        from datetime import datetime as dt
        dt.strptime(analysis_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({"error": "Invalid analysis_date format. Use YYYY-MM-DD."}), 400

    mismatch_sids = fetch_mismatch_sids_for_date(analysis_date)
    if not mismatch_sids:
        return jsonify({"success": True, "queued": 0, "message": "No mismatch rows found for this date"})

    # Mark all as Pending
    from database.sqlite_db import get_sqlite_connection
    conn = get_sqlite_connection()
    try:
        cursor = conn.cursor()
        cursor.executemany(
            "UPDATE call_duration_analysis SET rca_category = 'Pending' WHERE call_sid = ? AND analysis_date = ?",
            [(sid, analysis_date) for sid in mismatch_sids]
        )
        conn.commit()
    finally:
        conn.close()

    from services.call_duration_analysis import _run_background_rca
    import threading
    t = threading.Thread(target=_run_background_rca, args=(mismatch_sids, analysis_date), daemon=True)
    t.start()

    return jsonify({
        "success": True,
        "queued": len(mismatch_sids),
        "message": f"RCA queued for {len(mismatch_sids)} mismatch rows — check rca_categories for updates"
    })


# ───────────────────────────────────────────────────────────
# Business Dashboard
# ───────────────────────────────────────────────────────────

@app.route("/business-dashboard")
@auth_required
def business_dashboard():
    if not is_admin():
        return redirect(url_for("dashboard"))
    return render_template(
        "business_dashboard.html",
        user=get_session_user_email(),
        is_admin=is_admin()
    )


# GET /api/business/summary?months=N&view=month|quarter|ytd
@app.route("/api/business/summary", methods=["GET"])
def api_business_summary():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    if not is_admin():
        return jsonify({"error": "Admin access required"}), 403

    from datetime import datetime as dt_cls, timedelta
    import calendar as cal_mod
    import pytz

    IST = pytz.timezone('Asia/Kolkata')
    UTC = pytz.utc
    MONTH_ABBR = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    MONTH_FULL = ['January', 'February', 'March', 'April', 'May', 'June',
                  'July', 'August', 'September', 'October', 'November', 'December']

    try:
        months = int(request.args.get('months', 3))
        months = max(1, min(months, 24))
        curView = request.args.get('view', 'month')

        now_ist = dt_cls.now(IST)
        cur_year = now_ist.year
        cur_month = now_ist.month
        last_day_cur = cal_mod.monthrange(cur_year, cur_month)[1]

        # ── VIEW-based period for KPIs + All Clients table ─────────────
        # Monthly  → current calendar month
        # Quarterly → start of current quarter → end of current month
        # YTD       → Jan 1 of current year → end of current month
        if curView == 'quarter':
            q_start_month = ((cur_month - 1) // 3) * 3 + 1
            view_start_ist = IST.localize(dt_cls(cur_year, q_start_month, 1, 0, 0, 0))
            q_num = (cur_month - 1) // 3 + 1
            view_label = f"Q{q_num}"
            view_display = f"Q{q_num} {cur_year}"
            view_pot_start = (cur_year, q_start_month)
        elif curView == 'ytd':
            view_start_ist = IST.localize(dt_cls(cur_year, 1, 1, 0, 0, 0))
            view_label = "YTD"
            view_display = f"Year to Date {cur_year}"
            view_pot_start = (cur_year, 1)
        else:  # month
            view_start_ist = IST.localize(dt_cls(cur_year, cur_month, 1, 0, 0, 0))
            view_label = MONTH_ABBR[cur_month - 1]
            view_display = f"{MONTH_FULL[cur_month - 1]} {cur_year}"
            view_pot_start = None  # use get_potentials_for_period instead

        view_end_ist = IST.localize(dt_cls(cur_year, cur_month, last_day_cur, 23, 59, 59))
        view_start_utc = view_start_ist.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')
        view_end_utc = view_end_ist.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')

        # ── Per-org call/credit data for VIEW period ───────────────────
        client_rows = fetch_clients_overview("BETWEEN %s AND %s", [view_start_utc, view_end_utc])

        # ── Potentials for the VIEW period ────────────────────────────
        if view_pot_start is None:
            # Monthly: single-month lookup
            pot_by_org = {}
            for org_id, p in get_potentials_for_period(cur_year, cur_month).items():
                pot_by_org[org_id] = int(p.get('potential') or 0)
        else:
            # Quarterly / YTD: sum potentials across all months in the view period
            range_pots = get_potentials_for_date_range(
                view_pot_start[0], view_pot_start[1], cur_year, cur_month
            )
            pot_by_org = {}
            for org_id, pot_list in range_pots.items():
                pot_by_org[org_id] = sum(int(p.get('potential') or 0) for p in pot_list)

        clients_out = []
        for c in client_rows:
            org_id = str(c.get('id', ''))
            pot = pot_by_org.get(org_id, 0)
            clients_out.append({
                'org_id': org_id,
                'name': c.get('organization_name', ''),
                'admin_email': c.get('admin_email', ''),
                'calls': int(c.get('total_completed_calls') or 0),
                'credits': round(float(c.get('total_credits') or 0), 1),
                'potential': int(pot),
            })

        cur_calls = sum(c['calls'] for c in clients_out)
        cur_credits = round(sum(c['credits'] for c in clients_out), 1)
        cur_potential = sum(c['potential'] for c in clients_out)

        # ── Per-credit cost (INR) — for revenue KPI ───────────────────
        cost_by_org = fetch_per_credit_cost_inr([c['org_id'] for c in clients_out])
        for c in clients_out:
            c['per_credit_cost_inr'] = cost_by_org.get(c['org_id'], 0.0)

        # ── Daily breakdown: ALWAYS current calendar month (sparkline) ─
        cur_month_start_ist = IST.localize(dt_cls(cur_year, cur_month, 1, 0, 0, 0))
        cur_month_end_ist   = IST.localize(dt_cls(cur_year, cur_month, last_day_cur, 23, 59, 59))
        cur_month_start_utc = cur_month_start_ist.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')
        cur_month_end_utc   = cur_month_end_ist.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')
        daily_data    = fetch_daily_call_breakdown(cur_month_start_utc, cur_month_end_utc, cur_year, cur_month)
        daily         = daily_data['calls']
        daily_credits = daily_data['credits']

        # ── History: N full months before current calendar month (trend chart) ──
        hist_start_month = cur_month - months
        hist_start_year = cur_year
        while hist_start_month <= 0:
            hist_start_month += 12
            hist_start_year -= 1

        hist_start_ist = IST.localize(dt_cls(hist_start_year, hist_start_month, 1, 0, 0, 0))
        hist_end_ist = cur_month_start_ist - timedelta(seconds=1)

        hist_start_utc = hist_start_ist.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')
        hist_end_utc = hist_end_ist.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')

        monthly_rows = fetch_monthly_summaries(hist_start_utc, hist_end_utc)

        if cur_month > 1:
            hist_pot_end_year, hist_pot_end_month = cur_year, cur_month - 1
        else:
            hist_pot_end_year, hist_pot_end_month = cur_year - 1, 12

        hist_potentials = get_potentials_for_date_range(
            hist_start_year, hist_start_month,
            hist_pot_end_year, hist_pot_end_month
        )

        history_out = []
        for row in monthly_rows:
            yr_str, mo_str = row['year_month'].split('-')
            yr, mo = int(yr_str), int(mo_str)
            month_pot = 0
            for org_id, pot_list in hist_potentials.items():
                for p in pot_list:
                    if p['year'] == yr and p['month'] == mo:
                        month_pot += int(p.get('potential') or 0)
            history_out.append({
                'month': MONTH_ABBR[mo - 1],
                'calls': row['calls'],
                'credits': round(row['credits'], 1),
                'potential': month_pot,
            })

        # ── Previous period for KPI delta arrows ──────────────────────
        # Monthly  → previous calendar month
        # Quarterly → previous quarter
        # YTD       → same YTD window one year ago
        if curView == 'quarter':
            # Previous quarter
            if q_start_month == 1:   # Q1 → Q4 of previous year
                pq_start, pq_end, pq_year = 10, 12, cur_year - 1
            elif q_start_month == 4: # Q2 → Q1
                pq_start, pq_end, pq_year = 1, 3, cur_year
            elif q_start_month == 7: # Q3 → Q2
                pq_start, pq_end, pq_year = 4, 6, cur_year
            else:                    # Q4 → Q3
                pq_start, pq_end, pq_year = 7, 9, cur_year
            pq_end_day = cal_mod.monthrange(pq_year, pq_end)[1]
            prev_start_ist = IST.localize(dt_cls(pq_year, pq_start, 1, 0, 0, 0))
            prev_end_ist   = IST.localize(dt_cls(pq_year, pq_end, pq_end_day, 23, 59, 59))
        elif curView == 'ytd':
            # Jan 1 → end of current month, one year ago
            prev_end_day = cal_mod.monthrange(cur_year - 1, cur_month)[1]
            prev_start_ist = IST.localize(dt_cls(cur_year - 1, 1, 1, 0, 0, 0))
            prev_end_ist   = IST.localize(dt_cls(cur_year - 1, cur_month, prev_end_day, 23, 59, 59))
        else:  # month — previous calendar month, same day-of-month as today
            pm = cur_month - 1 if cur_month > 1 else 12
            py = cur_year if cur_month > 1 else cur_year - 1
            pm_last = cal_mod.monthrange(py, pm)[1]
            # Compare up to the same day of month as today (e.g. Jun 6 vs May 6)
            pm_day = min(now_ist.day, pm_last)
            prev_start_ist = IST.localize(dt_cls(py, pm, 1, 0, 0, 0))
            prev_end_ist   = IST.localize(dt_cls(py, pm, pm_day, 23, 59, 59))

        prev_start_utc = prev_start_ist.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')
        prev_end_utc   = prev_end_ist.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')
        prev_rows      = fetch_monthly_summaries(prev_start_utc, prev_end_utc)
        prev_calls     = sum(r['calls'] for r in prev_rows)
        prev_credits   = round(sum(r['credits'] for r in prev_rows), 1)

        return jsonify({
            'current': {
                'month': view_label,
                'displayMonth': view_display,
                'calls': cur_calls,
                'credits': cur_credits,
                'potential': cur_potential,
                'daily': daily,
                'daily_credits': daily_credits,
                'clients': clients_out,  # VIEW period — used for all sections
                'prev': {                # Previous period for KPI delta arrows
                    'calls': prev_calls,
                    'credits': prev_credits,
                },
            },
            'history': history_out,
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"API Error in /api/business/summary: {e}")
        return jsonify({"error": str(e)}), 500


# POST /api/business/forecast  { client_name, forecast }
@app.route("/api/business/forecast", methods=["POST"])
def api_business_forecast():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    if not is_admin():
        return jsonify({"error": "Admin access required"}), 403

    data = request.get_json(force=True) or {}
    org_id = (data.get('org_id') or '').strip()
    client_name = (data.get('client_name') or '').strip()
    forecast_raw = data.get('forecast', 0)

    if not org_id:
        return jsonify({"error": "org_id is required"}), 400

    try:
        forecast = int(forecast_raw)
    except (TypeError, ValueError):
        forecast = 0

    try:
        from datetime import datetime
        import pytz
        now = datetime.now(pytz.timezone('Asia/Kolkata'))
        year, month = now.year, now.month

        if forecast <= 0:
            delete_potential(org_id, year, month)
            return jsonify({"ok": True, "deleted": True})

        upsert_potential(org_id, client_name, year, month, forecast)
        return jsonify({"ok": True})

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# POST /api/business/research  { clients: [{name, domain, industry}] }
# Market Research via Claude API — to be implemented in a later iteration
@app.route("/api/business/research", methods=["POST"])
def api_business_research():
    auth_error = check_api_user()
    if auth_error:
        return auth_error

    if not is_admin():
        return jsonify({"error": "Admin access required"}), 403

    return jsonify({
        "error": "Market Research is not yet configured on this server.",
        "status": "coming_soon"
    }), 503


# ───────────────────────────────────────────────────────────
# Notification Config
# ───────────────────────────────────────────────────────────

@app.route("/notification-config")
@auth_required
def notification_config_list():
    if not can_update_subscription():
        return redirect(url_for('dashboard'))
    orgs = fetch_all_organizations()
    return render_template(
        "notification_config_list.html",
        organizations=orgs,
        user=get_session_user_email(),
        is_admin=is_admin(),
        can_update_subscription=can_update_subscription()
    )


@app.route("/notification-config/<org_id>")
@auth_required
def notification_config_detail(org_id):
    if not can_update_subscription():
        return redirect(url_for('dashboard'))
    org = fetch_organization_with_config(org_id)
    if not org:
        flash("Organization not found.", "error")
        return redirect(url_for('notification_config_list'))
    config = org.get('notification_config') or {}
    return render_template(
        "notification_config_detail.html",
        org=org,
        config=config,
        config_json=json.dumps(config, indent=2),
        user=get_session_user_email(),
        is_admin=is_admin(),
        can_update_subscription=can_update_subscription()
    )


@app.route("/notification-config/<org_id>", methods=["POST"])
@auth_required
def notification_config_save(org_id):
    if not can_update_subscription():
        return redirect(url_for('dashboard'))

    raw_json = request.form.get('notification_config_json', '{}')
    try:
        config = json.loads(raw_json)
    except json.JSONDecodeError as e:
        flash(f"Invalid JSON: {e}", "error")
        return redirect(url_for('notification_config_detail', org_id=org_id))

    propagate = request.form.get('update_all_jobs', 'false') == 'true'
    success, status_code, response_text = update_org_notification_config(org_id, config, propagate_to_jobs=propagate)

    if success:
        msg = "Config saved and propagated to all jobs." if propagate else "Org config saved (jobs not updated)."
        flash(msg, "success")
    else:
        flash(f"API error ({status_code}): {response_text}", "error")

    return redirect(url_for('notification_config_detail', org_id=org_id))


@app.route("/notification-config/<org_id>/jobs")
@auth_required
def notification_config_jobs(org_id):
    if not can_update_subscription():
        return redirect(url_for('dashboard'))
    org = fetch_organization_with_config(org_id)
    if not org:
        flash("Organization not found.", "error")
        return redirect(url_for('notification_config_list'))
    jobs = fetch_jobs_for_organization(org_id)
    return render_template(
        "notification_config_jobs.html",
        org=org,
        jobs=jobs,
        user=get_session_user_email(),
        is_admin=is_admin(),
        can_update_subscription=can_update_subscription()
    )


@app.route("/notification-config/<org_id>/job/<job_id>")
@auth_required
def notification_config_job_detail(org_id, job_id):
    if not can_update_subscription():
        return redirect(url_for('dashboard'))
    job = fetch_job_with_config(job_id)
    if not job or str(job.get('organization_id')) != str(org_id):
        flash("Job not found.", "error")
        return redirect(url_for('notification_config_jobs', org_id=org_id))
    config = job.get('notification_config') or {}
    return render_template(
        "notification_config_detail.html",
        org={'id': job.get('organization_id'), 'company_name': job.get('org_name')},
        job=job,
        config=config,
        config_json=json.dumps(config, indent=2),
        is_job_level=True,
        user=get_session_user_email(),
        is_admin=is_admin(),
        can_update_subscription=can_update_subscription()
    )


@app.route("/notification-config/<org_id>/job/<job_id>", methods=["POST"])
@auth_required
def notification_config_job_save(org_id, job_id):
    if not can_update_subscription():
        return redirect(url_for('dashboard'))

    raw_json = request.form.get('notification_config_json', '{}')
    try:
        config = json.loads(raw_json)
    except json.JSONDecodeError as e:
        flash(f"Invalid JSON: {e}", "error")
        return redirect(url_for('notification_config_job_detail', org_id=org_id, job_id=job_id))

    success, status_code, response_text = update_job_notification_config(job_id, config)

    if success:
        flash("Job-level config saved.", "success")
    else:
        flash(f"API error ({status_code}): {response_text}", "error")

    return redirect(url_for('notification_config_job_detail', org_id=org_id, job_id=job_id))


if __name__ == "__main__":
    import os
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
        from services.scheduler import init_scheduler
        init_scheduler()
    # Set debug=False in production!
    # Use host='0.0.0.0' to listen on all available network interfaces
    app.run(debug=True, host='0.0.0.0', port=5123)  # Port added for clarity
