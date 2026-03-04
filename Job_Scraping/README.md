# 🎯 Job Monitor for International Students

A Python tool that automatically tracks new job postings daily, specifically designed for international students seeking new grad positions and internships in the US.

## Features

- **Multi-source scraping**:
  - GitHub job lists (SimplifyJobs - the best curated lists)
  - LinkedIn public job search
  - Indeed job listings
  - Direct company career pages (Google, Meta, Amazon, Microsoft, Apple)
- **Visa sponsorship tracking**: Highlights jobs that sponsor visas ✅❌❓
- **Resume-based filtering**: Parses your resume PDF and ranks jobs by role/skill match
- **Multiple notification methods**:
  - 📧 Email digests with HTML formatting
  - 💬 Slack notifications
  - 🖥️ Colorful terminal output
  - 📊 CSV/Excel exports
- **Smart deduplication**: SQLite database tracks jobs and only shows new ones
- **Scheduled runs**: Automatically runs daily at your preferred time

## Quick Start

### 1. Install Dependencies

```bash
cd job_monitor
pip install -r requirements.txt
```

### 2. Configure Your Preferences

Edit `config.json` to customize:

```json
{
  "keywords": [
    "software engineer new grad",
    "data scientist intern"
  ],
  "scrapers": ["github", "linkedin", "indeed", "companies"],
  "target_companies": ["google", "meta", "amazon", "microsoft", "apple"],
  
  "email": {
    "enabled": true,
    "sender_email": "your-email@gmail.com",
    "sender_password": "your-app-password",
    "recipient_email": "your-email@gmail.com"
  },
  
  "slack": {
    "enabled": true,
    "webhook_url": "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
  }
}
```

### 3. Run the Monitor

```bash
# Run once and exit (good for testing)
python job_monitor.py --once

# Run with daily scheduling (keeps running)
python job_monitor.py

# Export all jobs from last 30 days to CSV
python job_monitor.py --export
```

### 4. (Optional) Run as a Website with a Button

If you prefer manually triggering updates in a browser:

```bash
python web_app.py
```

Then open:

```text
http://127.0.0.1:5000
```

Click **Run Update Now** to trigger scraping, filtering, CSV export, and email notifications immediately.

### 5. One-Click Run (No Terminal Window)

For Windows, you can double-click these files:

- `start_web_app.vbs`: Starts the web app in background and opens `http://127.0.0.1:5000`
- `run_update_once_hidden.vbs`: Runs one full update (`--once`) silently in background

You can pin these to Start menu or create Desktop shortcuts for faster access.

## Resume-Based Filtering (Recommended)

Set your resume path and preferences in `config.json`:

```json
"resume_profile": {
  "resume_path": "C:\\Job\\Xu_Nicole_Columbia_QMSS.pdf",
  "needs_sponsorship": true,
  "target_roles": ["data scientist", "data analyst", "machine learning engineer"],
  "skill_keywords": ["python", "sql", "pandas", "machine learning"],
  "preferred_locations": ["United States", "Remote", "New York"],
  "preferred_job_types": ["new_grad", "fulltime", "intern"]
},
"filters": {
  "min_match_score": 35,
  "include_unknown_visa": true
}
```

How it works:
- Jobs are filtered by visa compatibility when `needs_sponsorship` is `true`
- Jobs are scored by title match, skills, location, job type, and sponsorship signal
- Output is sorted by highest `match_score` first
- CSV includes `match_score` and `match_reasons`

## Setting Up Notifications

### Email (Gmail)

1. Go to [Google Account Settings](https://myaccount.google.com/)
2. Security → 2-Step Verification (enable if not already)
3. Search for "App passwords" → Generate a new app password
4. Use this 16-character password in `config.json`

### Slack

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Create New App → From scratch
3. Choose a name and workspace
4. Go to "Incoming Webhooks" → Activate
5. Click "Add New Webhook to Workspace"
6. Select a channel and authorize
7. Copy the webhook URL to `config.json`

## Running as a Background Service

### On macOS/Linux (using cron):

```bash
# Edit crontab
crontab -e

# Add this line to run daily at 9 AM
0 9 * * * cd /path/to/job_monitor && python job_monitor.py --once >> job_monitor.log 2>&1
```

### On Windows (Task Scheduler):

1. Open Task Scheduler
2. Create Basic Task → Daily trigger at 9:00 AM
3. Action: Start a program
4. Program: `python`
5. Arguments: `C:\path\to\job_monitor.py --once`

## Data Sources

### GitHub Job Lists (Best Source!)
- [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions)
- [SimplifyJobs/Summer2025-Internships](https://github.com/SimplifyJobs/Summer2025-Internships)

These are community-maintained lists specifically curated for new grads and interns, with visa sponsorship information clearly marked.

### Company Career Pages
The tool scrapes directly from major tech companies known to sponsor H-1B visas:
- Google Careers
- Meta Careers
- Amazon Jobs
- Microsoft Careers
- Apple Jobs

## Output Examples

### Terminal Output (with colors):
```
============================================================
📋 NEW JOB POSITIONS FOUND
============================================================

Summary: Found 23 new positions
  📋 Full-time/New Grad: 15
  🎓 Internships: 8

1. Software Engineer, New Grad 2025
   🏢 Google | 📍 Mountain View, CA
   ✅ Visa | NEW_GRAD | 🔗 https://careers.google.com/jobs/...

2. Data Science Intern - Summer 2025  
   🏢 Meta | 📍 Menlo Park, CA
   ✅ Visa | INTERN | 🔗 https://metacareers.com/jobs/...
```

### CSV Export:
| title | company | location | job_type | visa_sponsor | url | source |
|-------|---------|----------|----------|--------------|-----|--------|
| Software Engineer | Google | Mountain View | new_grad | Yes | ... | Google Careers |

### Slack Notification:
Beautiful formatted cards with job details, links, and visa status.

### Email Digest:
HTML-formatted email with job cards grouped by type, with attached CSV.

## Tips for International Students

1. **OPT/CPT Timeline**: Make sure you understand your work authorization dates
2. **H-1B Sponsors**: This tool prioritizes companies known to sponsor - look for the ✅
3. **Apply Early**: Many companies fill new grad positions by fall for the following year
4. **Track Applications**: Use the exported CSV to track where you've applied
5. **GitHub Lists**: These are updated frequently by the community - very reliable!

## Customization

### Add More Keywords
Edit `config.json` to add roles you're targeting:
```json
"keywords": [
  "your target role here",
  "another role intern"
]
```

### Filter Companies
Exclude companies you're not interested in:
```json
"filters": {
  "exclude_companies": ["Company A", "Company B"]
}
```

## Troubleshooting

**"No jobs found from LinkedIn"**: LinkedIn has anti-scraping measures. The tool uses their public API which has limitations. GitHub and Indeed usually work better.

**Email not sending**: 
- Make sure you're using an App Password, not your regular password
- Check that 2FA is enabled
- Verify SMTP settings match your provider

**Rate limiting**: The tool adds delays between requests. If you're blocked, wait 24 hours.

## File Structure

```
job_monitor/
├── job_monitor.py    # Main script
├── config.json       # Your configuration
├── requirements.txt  # Python dependencies
├── jobs.db          # SQLite database (created on first run)
├── daily_jobs.csv   # Daily export (created each run)
└── job_monitor.log  # Log file
```

---

Good luck with your job search! 🍀

*Note: This tool is for personal use. Respect websites' terms of service and rate limits.*
