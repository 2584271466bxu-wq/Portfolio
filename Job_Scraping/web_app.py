#!/usr/bin/env python3
"""
Local web UI for manually triggering job updates.
"""

from datetime import datetime
import json
from typing import Dict, List
from flask import Flask, redirect, render_template_string, request, url_for

from job_monitor import JobMonitor, SCRIPT_DIR


CONFIG_PATH = f"{SCRIPT_DIR}/config.json"
app = Flask(__name__)

ALLOWED_JOB_TYPES = ["fulltime", "intern", "new_grad"]


LAST_RESULT: Dict = {
    "ran_at": None,
    "new_found": 0,
    "new_matched": 0,
    "backlog_matched": 0,
    "total_sent": 0,
    "csv_path": "",
    "jobs": [],
    "error": "",
}

SETTINGS_RESULT: Dict = {
    "message": "",
    "error": "",
}


def _load_config() -> Dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as config_file:
        return json.load(config_file)


def _save_config(config: Dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as config_file:
        json.dump(config, config_file, indent=2)


def get_configured_job_types() -> List[str]:
    config = _load_config()
    configured = config.get("job_types", [])
    safe_job_types = [job_type for job_type in configured if job_type in ALLOWED_JOB_TYPES]
    return safe_job_types if safe_job_types else ALLOWED_JOB_TYPES.copy()


def manually_edit_scraped_job_types(selected_job_types: List[str]) -> List[str]:
    cleaned_job_types = [job_type for job_type in selected_job_types if job_type in ALLOWED_JOB_TYPES]
    if not cleaned_job_types:
        raise ValueError("Select at least one job type.")

    config = _load_config()
    config["job_types"] = cleaned_job_types
    _save_config(config)
    return cleaned_job_types


def _build_notification_jobs(monitor: JobMonitor, new_jobs: List[dict]) -> Dict:
    filtered_new_jobs = monitor.filter_and_rank_jobs(new_jobs)

    backlog_cfg = monitor.config.get("backlog", {})
    backlog_enabled = bool(backlog_cfg.get("enabled", True))
    backlog_since_days = int(backlog_cfg.get("since_days", 7))
    backlog_max_jobs = int(backlog_cfg.get("max_jobs", 10))

    filtered_backlog_jobs: List[dict] = []
    if backlog_enabled:
        new_job_ids = {j.get("job_id") for j in new_jobs if j.get("job_id")}
        recent_unnotified = monitor.db.get_recent_unnotified_jobs(since_days=backlog_since_days)
        backlog_candidates = [j for j in recent_unnotified if j.get("job_id") not in new_job_ids]
        filtered_backlog_jobs = monitor.filter_and_rank_jobs(backlog_candidates)[:backlog_max_jobs]

    jobs_to_notify: List[dict] = []
    seen_ids = set()

    for job in filtered_new_jobs:
        jid = job.get("job_id")
        if jid and jid not in seen_ids:
            seen_ids.add(jid)
        enriched = dict(job)
        enriched["notification_bucket"] = "new"
        jobs_to_notify.append(enriched)

    for job in filtered_backlog_jobs:
        jid = job.get("job_id")
        if jid and jid in seen_ids:
            continue
        if jid:
            seen_ids.add(jid)
        enriched = dict(job)
        enriched["notification_bucket"] = "backlog"
        jobs_to_notify.append(enriched)

    return {
        "jobs_to_notify": jobs_to_notify,
        "filtered_new_jobs": filtered_new_jobs,
        "filtered_backlog_jobs": filtered_backlog_jobs,
    }


def run_manual_update() -> Dict:
    monitor = JobMonitor(config_path=CONFIG_PATH)
    new_jobs = monitor.run_daily_scan()
    new_job_dicts = [job.__dict__ for job in new_jobs]

    package = _build_notification_jobs(monitor, new_job_dicts)
    jobs_to_notify = package["jobs_to_notify"]
    filtered_new_jobs = package["filtered_new_jobs"]
    filtered_backlog_jobs = package["filtered_backlog_jobs"]

    csv_path = ""
    if jobs_to_notify:
        csv_path = monitor.export_to_csv(jobs_to_notify)
        monitor.send_notifications(jobs_to_notify, csv_path=csv_path)

        notified_ids = [j.get("job_id") for j in jobs_to_notify if j.get("job_id")]
        if notified_ids:
            monitor.db.mark_notified(notified_ids)

    return {
        "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "new_found": len(new_jobs),
        "new_matched": len(filtered_new_jobs),
        "backlog_matched": len(filtered_backlog_jobs),
        "total_sent": len(jobs_to_notify),
        "csv_path": csv_path,
        "jobs": jobs_to_notify[:50],
        "error": "",
    }


PAGE_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Job Scraper Manual Update</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; background: #f7f7f9; color: #1f2937; }
    .card { background: white; border: 1px solid #e5e7eb; border-radius: 10px; padding: 16px; margin-bottom: 16px; }
    button { background: #2563eb; color: white; border: none; padding: 10px 14px; border-radius: 8px; cursor: pointer; font-weight: 600; }
    button:hover { background: #1d4ed8; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #e5e7eb; text-align: left; padding: 8px; vertical-align: top; }
    th { background: #f3f4f6; }
    .muted { color: #6b7280; font-size: 14px; }
    .bucket { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #e5e7eb; display: inline-block; }
    .error { color: #b91c1c; font-weight: 600; }
  </style>
</head>
<body>
  <div class="card">
    <h2>Job Scraper Manual Update</h2>
    <p class="muted">Click the button to run scraping, filtering, CSV export, and email notifications now.</p>
    <form method="post" action="{{ url_for('run_update') }}">
      <button type="submit">Run Update Now</button>
    </form>
  </div>

  <div class="card">
    <h3>Scraped Job Types</h3>
    <p class="muted">Manually choose which job types to scrape.</p>
    <form method="post" action="{{ url_for('update_job_types') }}">
      <label><input type="checkbox" name="job_types" value="fulltime" {% if 'fulltime' in current_job_types %}checked{% endif %}> Full-time</label><br>
      <label><input type="checkbox" name="job_types" value="intern" {% if 'intern' in current_job_types %}checked{% endif %}> Intern</label><br>
      <label><input type="checkbox" name="job_types" value="new_grad" {% if 'new_grad' in current_job_types %}checked{% endif %}> New grad</label><br><br>
      <button type="submit">Save Job Types</button>
    </form>
    {% if settings_result.message %}
      <p><strong>{{ settings_result.message }}</strong></p>
    {% endif %}
    {% if settings_result.error %}
      <p class="error">{{ settings_result.error }}</p>
    {% endif %}
  </div>

  <div class="card">
    <h3>Last Run</h3>
    {% if result.ran_at %}
      <p><strong>Time:</strong> {{ result.ran_at }}</p>
      <p><strong>Found New:</strong> {{ result.new_found }} | <strong>Matched New:</strong> {{ result.new_matched }} | <strong>Matched Backlog:</strong> {{ result.backlog_matched }} | <strong>Sent:</strong> {{ result.total_sent }}</p>
      <p><strong>CSV:</strong> {{ result.csv_path if result.csv_path else 'No CSV generated (no matched jobs)' }}</p>
    {% else %}
      <p class="muted">No run yet in this web session.</p>
    {% endif %}
    {% if result.error %}
      <p class="error">Error: {{ result.error }}</p>
    {% endif %}
  </div>

  <div class="card">
    <h3>Latest Matched Jobs</h3>
    {% if result.jobs %}
      <table>
        <thead>
          <tr>
            <th>Title</th>
            <th>Company</th>
            <th>Location</th>
            <th>Score</th>
            <th>Bucket</th>
            <th>Link</th>
          </tr>
        </thead>
        <tbody>
          {% for job in result.jobs %}
          <tr>
            <td>{{ job.title }}</td>
            <td>{{ job.company }}</td>
            <td>{{ job.location }}</td>
            <td>{{ job.match_score }}</td>
            <td><span class="bucket">{{ job.notification_bucket }}</span></td>
            <td><a href="{{ job.url }}" target="_blank" rel="noopener noreferrer">Open</a></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="muted">No matched jobs shown yet.</p>
    {% endif %}
  </div>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(
        PAGE_TEMPLATE,
        result=LAST_RESULT,
        settings_result=SETTINGS_RESULT,
        current_job_types=get_configured_job_types(),
    )


@app.post("/run")
def run_update():
    global LAST_RESULT
    try:
        LAST_RESULT = run_manual_update()
    except Exception as exc:
        LAST_RESULT = {
            "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "new_found": 0,
            "new_matched": 0,
            "backlog_matched": 0,
            "total_sent": 0,
            "csv_path": "",
            "jobs": [],
            "error": str(exc),
        }
    return redirect(url_for("index"))


@app.post("/job-types")
def update_job_types():
    global SETTINGS_RESULT
    try:
        selected = request.form.getlist("job_types")
        updated_job_types = manually_edit_scraped_job_types(selected)
        SETTINGS_RESULT = {
            "message": f"Updated job types: {', '.join(updated_job_types)}",
            "error": "",
        }
    except Exception as exc:
        SETTINGS_RESULT = {
            "message": "",
            "error": str(exc),
        }
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
