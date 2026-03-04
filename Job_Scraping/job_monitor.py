#!/usr/bin/env python3
"""
Job Monitor - Daily Job Position Tracker for International Students
Tracks new job postings and sends daily updates via email and CSV exports.
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import json
import hashlib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta
import os
import time
import schedule
import sqlite3
import re
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Tuple
import logging

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(SCRIPT_DIR, 'job_monitor.log')),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class Job:
    """Represents a job posting."""
    title: str
    company: str
    location: str
    url: str
    source: str
    date_posted: str
    job_type: str  # 'fulltime', 'intern', 'new_grad'
    visa_sponsor: Optional[str] = "Unknown"
    description_preview: Optional[str] = ""
    job_id: Optional[str] = ""
    
    def __post_init__(self):
        if not self.job_id:
            self.job_id = hashlib.md5(f"{self.title}{self.company}{self.url}".encode()).hexdigest()[:12]


class JobDatabase:
    """SQLite database for tracking jobs and detecting new postings."""
    
    def __init__(self, db_path: str = "jobs.db"):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                location TEXT,
                url TEXT,
                source TEXT,
                date_posted TEXT,
                job_type TEXT,
                visa_sponsor TEXT,
                description_preview TEXT,
                first_seen DATE,
                last_seen DATE,
                notified BOOLEAN DEFAULT FALSE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_date DATETIME,
                jobs_found INTEGER,
                new_jobs INTEGER
            )
        ''')
        conn.commit()
        conn.close()
    
    def add_job(self, job: Job) -> bool:
        """Add a job to the database. Returns True if it's a new job."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        
        cursor.execute("SELECT job_id FROM jobs WHERE job_id = ?", (job.job_id,))
        exists = cursor.fetchone()
        
        if exists:
            cursor.execute("UPDATE jobs SET last_seen = ? WHERE job_id = ?", (today, job.job_id))
            conn.commit()
            conn.close()
            return False
        else:
            cursor.execute('''
                INSERT INTO jobs (job_id, title, company, location, url, source, 
                                  date_posted, job_type, visa_sponsor, description_preview,
                                  first_seen, last_seen, notified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (job.job_id, job.title, job.company, job.location, job.url, 
                  job.source, job.date_posted, job.job_type, job.visa_sponsor,
                  job.description_preview, today, today, False))
            conn.commit()
            conn.close()
            return True
    
    def get_new_jobs(self, since_days: int = 1) -> List[dict]:
        """Get jobs first seen within the last N days."""
        conn = sqlite3.connect(self.db_path)
        cutoff = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
        df = pd.read_sql_query(
            "SELECT * FROM jobs WHERE first_seen >= ? ORDER BY first_seen DESC",
            conn, params=(cutoff,)
        )
        conn.close()
        return df.to_dict('records')

    def get_recent_unnotified_jobs(self, since_days: int = 7) -> List[dict]:
        """Get recent jobs that have not been notified yet."""
        conn = sqlite3.connect(self.db_path)
        cutoff = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
        df = pd.read_sql_query(
            "SELECT * FROM jobs WHERE first_seen >= ? AND notified = FALSE ORDER BY first_seen DESC",
            conn,
            params=(cutoff,)
        )
        conn.close()
        return df.to_dict('records')
    
    def mark_notified(self, job_ids: List[str]):
        """Mark jobs as notified."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.executemany(
            "UPDATE jobs SET notified = TRUE WHERE job_id = ?",
            [(jid,) for jid in job_ids]
        )
        conn.commit()
        conn.close()


class JobScraper:
    """Base class for job scrapers."""
    
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
    
    def fetch_page(self, url: str) -> Optional[BeautifulSoup]:
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return BeautifulSoup(response.text, 'html.parser')
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            return None


class GitHubNewGradScraper(JobScraper):
    """Scrapes the popular GitHub new grad/intern job lists."""
    
    # These are community-maintained lists specifically for new grads/interns
    REPOS = {
        'new_grad_2025': 'https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md',
        'intern_2025': 'https://raw.githubusercontent.com/SimplifyJobs/Summer2025-Internships/dev/README.md',
    }
    
    def scrape(self, job_type: str = 'all') -> List[Job]:
        jobs = []
        repos_to_check = self.REPOS if job_type == 'all' else {job_type: self.REPOS.get(job_type)}
        
        for repo_type, url in repos_to_check.items():
            if not url:
                continue
            try:
                response = self.session.get(url, timeout=30)
                if response.status_code == 200:
                    jobs.extend(self._parse_github_readme(response.text, repo_type))
            except Exception as e:
                logger.error(f"Error scraping {repo_type}: {e}")
        
        return jobs
    
    def _parse_github_readme(self, content: str, job_type: str) -> List[Job]:
        """Parse the markdown table from GitHub readme."""
        jobs = []
        lines = content.split('\n')
        in_table = False
        
        for line in lines:
            if '|' in line and ('Company' in line or 'company' in line.lower()):
                in_table = True
                continue
            if in_table and '|--' in line:
                continue
            if in_table and '|' in line:
                parts = [p.strip() for p in line.split('|')]
                if len(parts) >= 4:
                    # Typical format: | Company | Role | Location | Link |
                    company = self._clean_markdown(parts[1]) if len(parts) > 1 else ""
                    title = self._clean_markdown(parts[2]) if len(parts) > 2 else ""
                    location = self._clean_markdown(parts[3]) if len(parts) > 3 else ""
                    url = self._extract_url(parts[4]) if len(parts) > 4 else ""
                    
                    if company and title:
                        jobs.append(Job(
                            title=title,
                            company=company,
                            location=location,
                            url=url,
                            source="GitHub Jobs List",
                            date_posted=datetime.now().strftime("%Y-%m-%d"),
                            job_type='intern' if 'intern' in job_type else 'new_grad',
                            visa_sponsor=self._check_visa_sponsor(line)
                        ))
        
        return jobs
    
    def _clean_markdown(self, text: str) -> str:
        """Remove markdown formatting."""
        import re
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)  # [text](url) -> text
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # **text** -> text
        text = re.sub(r'<[^>]+>', '', text)  # Remove HTML tags
        return text.strip()
    
    def _extract_url(self, text: str) -> str:
        """Extract URL from markdown link."""
        import re
        match = re.search(r'\((https?://[^)]+)\)', text)
        if match:
            return match.group(1)
        match = re.search(r'https?://[^\s<>"]+', text)
        return match.group(0) if match else ""
    
    def _check_visa_sponsor(self, line: str) -> str:
        """Check if visa sponsorship is mentioned."""
        line_lower = line.lower()
        if '✅' in line or 'sponsor' in line_lower:
            return "Yes"
        elif '❌' in line or 'no sponsor' in line_lower:
            return "No"
        return "Unknown"


class IndeedScraper(JobScraper):
    """Scrapes Indeed job listings (note: may require adjustments due to anti-scraping)."""
    
    BASE_URL = "https://www.indeed.com/jobs"
    
    def scrape(self, keywords: List[str], location: str = "United States", 
               job_type: str = "all") -> List[Job]:
        jobs = []
        
        for keyword in keywords:
            params = {
                'q': keyword,
                'l': location,
                'sort': 'date',
                'fromage': '1',  # Last 24 hours
            }
            
            url = f"{self.BASE_URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
            soup = self.fetch_page(url)
            
            if soup:
                jobs.extend(self._parse_results(soup, keyword))
            
            time.sleep(2)  # Be respectful
        
        return jobs
    
    def _parse_results(self, soup: BeautifulSoup, keyword: str) -> List[Job]:
        jobs = []
        job_cards = soup.find_all('div', class_='job_seen_beacon')
        
        for card in job_cards:
            try:
                title_elem = card.find('h2', class_='jobTitle')
                company_elem = card.find('span', {'data-testid': 'company-name'})
                location_elem = card.find('div', {'data-testid': 'text-location'})
                
                if title_elem and company_elem:
                    link = title_elem.find('a')
                    url = f"https://www.indeed.com{link['href']}" if link else ""
                    
                    jobs.append(Job(
                        title=title_elem.get_text(strip=True),
                        company=company_elem.get_text(strip=True),
                        location=location_elem.get_text(strip=True) if location_elem else "",
                        url=url,
                        source="Indeed",
                        date_posted=datetime.now().strftime("%Y-%m-%d"),
                        job_type='intern' if 'intern' in keyword.lower() else 'fulltime'
                    ))
            except Exception as e:
                logger.error(f"Error parsing Indeed job card: {e}")
        
        return jobs


class LinkedInScraper(JobScraper):
    """
    Scrapes LinkedIn jobs using their public job search.
    Uses the public jobs API endpoint that doesn't require authentication.
    """
    
    BASE_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    
    def scrape(self, keywords: List[str], location: str = "United States", 
               job_type: str = "all") -> List[Job]:
        jobs = []
        
        for keyword in keywords:
            try:
                # LinkedIn public jobs API parameters
                params = {
                    'keywords': keyword,
                    'location': location,
                    'f_TPR': 'r86400',  # Last 24 hours
                    'f_E': '1,2',  # Entry level and internship
                    'start': 0,
                    'count': 25
                }
                
                url = f"{self.BASE_URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
                soup = self.fetch_page(url)
                
                if soup:
                    jobs.extend(self._parse_results(soup, keyword))
                
                time.sleep(2)  # Rate limiting
                
            except Exception as e:
                logger.error(f"Error scraping LinkedIn for '{keyword}': {e}")
        
        return jobs
    
    def _parse_results(self, soup: BeautifulSoup, keyword: str) -> List[Job]:
        jobs = []
        job_cards = soup.find_all('div', class_='base-card')
        
        for card in job_cards:
            try:
                title_elem = card.find('h3', class_='base-search-card__title')
                company_elem = card.find('h4', class_='base-search-card__subtitle')
                location_elem = card.find('span', class_='job-search-card__location')
                link_elem = card.find('a', class_='base-card__full-link')
                time_elem = card.find('time')
                
                if title_elem and company_elem:
                    title = title_elem.get_text(strip=True)
                    company = company_elem.get_text(strip=True)
                    location = location_elem.get_text(strip=True) if location_elem else ""
                    url = link_elem['href'] if link_elem else ""
                    date_posted = time_elem.get('datetime', '') if time_elem else datetime.now().strftime("%Y-%m-%d")
                    
                    # Determine job type from title
                    title_lower = title.lower()
                    if 'intern' in title_lower:
                        jtype = 'intern'
                    elif 'new grad' in title_lower or 'entry' in title_lower or 'junior' in title_lower:
                        jtype = 'new_grad'
                    else:
                        jtype = 'fulltime'
                    
                    jobs.append(Job(
                        title=title,
                        company=company,
                        location=location,
                        url=url.split('?')[0] if url else "",  # Clean URL
                        source="LinkedIn",
                        date_posted=date_posted[:10] if date_posted else datetime.now().strftime("%Y-%m-%d"),
                        job_type=jtype
                    ))
                    
            except Exception as e:
                logger.error(f"Error parsing LinkedIn job card: {e}")
        
        return jobs


class CompanyCareerScraper(JobScraper):
    """
    Scrapes jobs directly from major tech company career pages.
    These are the companies most likely to sponsor H-1B visas.
    """
    
    # Companies known to sponsor visas and their career page APIs/URLs
    COMPANY_SOURCES = {
        'google': {
            'name': 'Google',
            'url': 'https://careers.google.com/api/v3/search/',
            'type': 'api',
            'params': {
                'location': 'United States',
                'degree': 'BACHELORS',
                'employment_type': 'FULL_TIME',
                'sort_by': 'date'
            }
        },
        'meta': {
            'name': 'Meta',
            'url': 'https://www.metacareers.com/jobs',
            'type': 'page',
            'search_params': '?teams[0]=Internship%20-%20Engineering%2C%20Tech%20%26%20Design&teams[1]=University%20Grad%20-%20Engineering%2C%20Tech%20%26%20Design'
        },
        'amazon': {
            'name': 'Amazon',
            'url': 'https://www.amazon.jobs/en/search',
            'type': 'page',
            'search_params': '?base_query=&loc_query=United+States&category[]=software-development&job_type[]=Full-Time&sort=recent'
        },
        'microsoft': {
            'name': 'Microsoft',
            'url': 'https://careers.microsoft.com/v2/global/en/search',
            'type': 'page',
            'search_params': '?q=new%20grad&lc=United%20States&d=Software%20Engineering&exp=Students%20and%20graduates'
        },
        'apple': {
            'name': 'Apple',
            'url': 'https://jobs.apple.com/en-us/search',
            'type': 'page',
            'search_params': '?team=internships-STDNT-INTRN+apps-and-frameworks-SFTWR-AF'
        }
    }
    
    # Additional companies with public career pages
    EXTENDED_COMPANIES = {
        'openai': {'name': 'OpenAI', 'url': 'https://openai.com/careers'},
        'anthropic': {'name': 'Anthropic', 'url': 'https://www.anthropic.com/careers'},
        'databricks': {'name': 'Databricks', 'url': 'https://www.databricks.com/company/careers/open-positions'},
        'snowflake': {'name': 'Snowflake', 'url': 'https://careers.snowflake.com/us/en/search-results'},
        'palantir': {'name': 'Palantir Technologies', 'url': 'https://www.palantir.com/careers/'},
        'nvidia': {'name': 'NVIDIA', 'url': 'https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite'},
        'pinecone': {'name': 'Pinecone', 'url': 'https://www.pinecone.io/careers/'},
        'weaviate': {'name': 'Weaviate', 'url': 'https://weaviate.io/company/careers'},
        'cohere': {'name': 'Cohere', 'url': 'https://cohere.com/careers'},
        'scale_ai': {'name': 'Scale AI', 'url': 'https://scale.com/careers'},
        'huggingface': {'name': 'Hugging Face', 'url': 'https://huggingface.co/jobs'},
        'weights_biases': {'name': 'Weights & Biases', 'url': 'https://wandb.ai/site/careers'},
        'langchain': {'name': 'LangChain', 'url': 'https://www.langchain.com/careers'},
        'stripe': {'name': 'Stripe', 'url': 'https://stripe.com/jobs/search?teams=eng&teams=data'},
        'block': {'name': 'Block, Inc.', 'url': 'https://careers.block.xyz/us/en'},
        'adyen': {'name': 'Adyen', 'url': 'https://careers.adyen.com/'},
        'plaid': {'name': 'Plaid', 'url': 'https://plaid.com/careers/'},
        'brex': {'name': 'Brex', 'url': 'https://www.brex.com/careers'},
        'ramp': {'name': 'Ramp', 'url': 'https://ramp.com/careers'},
        'chime': {'name': 'Chime', 'url': 'https://careers.chime.com/'},
        'robinhood': {'name': 'Robinhood', 'url': 'https://careers.robinhood.com/'},
        'workiva': {'name': 'Workiva', 'url': 'https://www.workiva.com/careers'},
        'fenergo': {'name': 'Fenergo', 'url': 'https://www.fenergo.com/company/careers'},
        'mckinsey': {'name': 'McKinsey & Company', 'url': 'https://www.mckinsey.com/careers/search-jobs'},
        'bcg': {'name': 'Boston Consulting Group', 'url': 'https://careers.bcg.com/global/en/search-results'},
        'bain': {'name': 'Bain & Company', 'url': 'https://www.bain.com/careers/find-a-role/'},
        'deloitte': {'name': 'Deloitte', 'url': 'https://apply.deloitte.com/careers/SearchJobs'},
        'pwc': {'name': 'PwC', 'url': 'https://www.pwc.com/us/en/careers/job-search.html'},
        'kpmg': {'name': 'KPMG', 'url': 'https://www.kpmgcareers.com/'},
        'accenture': {'name': 'Accenture', 'url': 'https://www.accenture.com/us-en/careers/jobsearch'},
        'zs': {'name': 'ZS Associates', 'url': 'https://www.zs.com/careers/open-roles'},
        'salesforce': {'name': 'Salesforce', 'url': 'https://careers.salesforce.com/en/jobs/'},
        'servicenow': {'name': 'ServiceNow', 'url': 'https://careers.servicenow.com/careers/jobs'},
        'workday': {'name': 'Workday', 'url': 'https://www.workday.com/en-us/company/careers.html'},
        'sap': {'name': 'SAP', 'url': 'https://jobs.sap.com/'},
        'oracle': {'name': 'Oracle', 'url': 'https://careers.oracle.com/'},
        'atlassian': {'name': 'Atlassian', 'url': 'https://www.atlassian.com/company/careers/all-jobs'},
        'goldman_sachs': {'name': 'Goldman Sachs', 'url': 'https://higher.gs.com/jobs'},
        'jpmorgan': {'name': 'JPMorgan Chase', 'url': 'https://careers.jpmorgan.com/us/en/students/programs'},
        'morgan_stanley': {'name': 'Morgan Stanley', 'url': 'https://www.morganstanley.com/people-opportunities/students-graduates'},
        'blackrock': {'name': 'BlackRock', 'url': 'https://careers.blackrock.com/'},
        'citadel': {'name': 'Citadel', 'url': 'https://www.citadel.com/careers/'},
        'two_sigma': {'name': 'Two Sigma', 'url': 'https://careers.twosigma.com/careers/SearchJobs/'},
        'bridgewater': {'name': 'Bridgewater Associates', 'url': 'https://www.bridgewater.com/careers'},
        'deel': {'name': 'Deel', 'url': 'https://www.deel.com/careers/'},
        'remote': {'name': 'Remote', 'url': 'https://remote.com/careers'},
        'rippling': {'name': 'Rippling', 'url': 'https://www.rippling.com/careers/open-roles'},
        'papaya_global': {'name': 'Papaya Global', 'url': 'https://www.papayaglobal.com/careers/'},
        'gusto': {'name': 'Gusto', 'url': 'https://gusto.com/about/careers'},
        'dataminr': {'name': 'Dataminr', 'url': 'https://www.dataminr.com/careers'},
        'alphasense': {'name': 'AlphaSense', 'url': 'https://www.alpha-sense.com/company/careers/'},
        'hyperscience': {'name': 'Hyperscience', 'url': 'https://www.hyperscience.com/company/careers/'},
        'tractable': {'name': 'Tractable', 'url': 'https://tractable.ai/careers/'},
    }
    
    def scrape(self, companies: List[str] = None) -> List[Job]:
        """Scrape jobs from specified companies or all configured companies."""
        jobs = []
        
        if companies is None:
            companies = list(dict.fromkeys(
                list(self.COMPANY_SOURCES.keys()) + list(self.EXTENDED_COMPANIES.keys())
            ))
        
        for company_key in companies:
            if company_key in self.COMPANY_SOURCES:
                company_jobs = self._scrape_company(company_key)
                jobs.extend(company_jobs)
                time.sleep(2)
            elif company_key in self.EXTENDED_COMPANIES:
                company_jobs = self._scrape_extended_company(company_key)
                jobs.extend(company_jobs)
                time.sleep(2)
            else:
                logger.warning(f"Unsupported company key in config: {company_key}")
        
        return jobs

    def _scrape_extended_company(self, company_key: str) -> List[Job]:
        """Scrape companies from EXTENDED_COMPANIES using generic page parsing."""
        company = self.EXTENDED_COMPANIES.get(company_key, {})
        if not company:
            return []
        company_source = {
            'name': company.get('name', company_key.replace('_', ' ').title()),
            'url': company.get('url', ''),
            'type': 'page',
            'search_params': ''
        }
        try:
            return self._scrape_page(company_source)
        except Exception as e:
            logger.error(f"Error scraping {company_source.get('name', company_key)}: {e}")
            return []
    
    def _scrape_company(self, company_key: str) -> List[Job]:
        """Scrape a specific company's career page."""
        company = self.COMPANY_SOURCES.get(company_key, {})
        jobs = []
        
        try:
            if company.get('type') == 'api':
                jobs = self._scrape_api(company)
            else:
                jobs = self._scrape_page(company)
        except Exception as e:
            logger.error(f"Error scraping {company.get('name', company_key)}: {e}")
        
        return jobs
    
    def _scrape_api(self, company: dict) -> List[Job]:
        """Scrape company using their API."""
        # Most company APIs require specific handling
        # This is a general structure - specific implementations vary
        jobs = []
        try:
            response = self.session.get(
                company['url'],
                params=company.get('params', {}),
                timeout=30
            )
            if response.status_code == 200:
                data = response.json()
                # Parse based on company-specific JSON structure
                jobs = self._parse_api_response(data, company['name'])
        except Exception as e:
            logger.error(f"API scrape error for {company['name']}: {e}")
        return jobs
    
    def _scrape_page(self, company: dict) -> List[Job]:
        """Scrape company using their career page HTML."""
        jobs = []
        url = company['url'] + company.get('search_params', '')
        
        soup = self.fetch_page(url)
        if soup:
            # Generic parsing - works for many career pages
            job_elements = soup.find_all(['div', 'li', 'article'], 
                                         class_=lambda x: x and any(
                                             kw in str(x).lower() 
                                             for kw in ['job', 'position', 'opening', 'career']
                                         ))
            
            for elem in job_elements[:20]:  # Limit to prevent over-scraping
                title = self._extract_title(elem)
                location = self._extract_location(elem)
                link = self._extract_link(elem, company['url'])
                
                if not title:
                    continue
                if not self._is_likely_job_title(title):
                    continue
                if not self._is_likely_job_link(link, company['url']):
                    continue

                if title:
                    jobs.append(Job(
                        title=title,
                        company=company['name'],
                        location=location,
                        url=link,
                        source=f"{company['name']} Careers",
                        date_posted=datetime.now().strftime("%Y-%m-%d"),
                        job_type=self._infer_job_type(title),
                        visa_sponsor="Yes"  # These companies typically sponsor
                    ))
        
        return jobs

    def _is_likely_job_title(self, title: str) -> bool:
        """Heuristic check to keep likely job titles and skip generic page text."""
        if not title:
            return False
        title_lower = title.strip().lower()

        generic_phrases = [
            'see careers', 'no results', 'explore', 'find jobs', 'saved items',
            'sort by', 'work together', 'open roles', 'join us', 'careers'
        ]
        if any(phrase in title_lower for phrase in generic_phrases):
            return False

        role_indicators = [
            'engineer', 'scientist', 'analyst', 'manager', 'developer', 'consultant',
            'researcher', 'specialist', 'associate', 'intern', 'graduate', 'new grad',
            'product', 'risk', 'compliance', 'strategy', 'architect', 'quant'
        ]
        return any(indicator in title_lower for indicator in role_indicators)

    def _is_likely_job_link(self, link: str, base_url: str) -> bool:
        """Heuristic check to prefer specific job detail URLs over generic pages."""
        if not link:
            return False
        if link.rstrip('/') == base_url.rstrip('/'):
            return False

        link_lower = link.lower()
        job_link_hints = [
            '/job', '/jobs', '/careers/', '/position', '/opening', 'jobid=', '/apply', '/search-results'
        ]
        return any(hint in link_lower for hint in job_link_hints)
    
    def _extract_title(self, elem) -> str:
        """Extract job title from element."""
        for tag in ['h2', 'h3', 'h4', 'a', 'span']:
            title_elem = elem.find(tag)
            if title_elem:
                text = title_elem.get_text(strip=True)
                if len(text) > 5 and len(text) < 200:
                    return text
        return ""
    
    def _extract_location(self, elem) -> str:
        """Extract location from element."""
        location_keywords = ['location', 'place', 'city', 'office']
        for kw in location_keywords:
            loc_elem = elem.find(class_=lambda x: x and kw in str(x).lower())
            if loc_elem:
                return loc_elem.get_text(strip=True)
        return "United States"
    
    def _extract_link(self, elem, base_url: str) -> str:
        """Extract job link from element."""
        link = elem.find('a')
        if link and link.get('href'):
            href = link['href']
            if href.startswith('http'):
                return href
            elif href.startswith('/'):
                from urllib.parse import urljoin
                return urljoin(base_url, href)
        return base_url
    
    def _infer_job_type(self, title: str) -> str:
        """Infer job type from title."""
        title_lower = title.lower()
        if 'intern' in title_lower:
            return 'intern'
        elif any(kw in title_lower for kw in ['new grad', 'entry', 'junior', 'graduate']):
            return 'new_grad'
        return 'fulltime'
    
    def _parse_api_response(self, data: dict, company_name: str) -> List[Job]:
        """Parse API response - customize per company."""
        jobs = []
        # Generic parsing attempt
        if isinstance(data, dict):
            job_list = data.get('jobs', data.get('results', data.get('data', [])))
            if isinstance(job_list, list):
                for item in job_list[:20]:
                    if isinstance(item, dict):
                        jobs.append(Job(
                            title=item.get('title', item.get('name', 'Unknown')),
                            company=company_name,
                            location=item.get('location', item.get('locations', [''])[0] if isinstance(item.get('locations'), list) else ''),
                            url=item.get('url', item.get('apply_url', '')),
                            source=f"{company_name} Careers",
                            date_posted=datetime.now().strftime("%Y-%m-%d"),
                            job_type=self._infer_job_type(item.get('title', '')),
                            visa_sponsor="Yes"
                        ))
        return jobs


class SlackNotifier:
    """Sends Slack notifications for new jobs via webhook."""
    
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
    
    def send_daily_digest(self, jobs: List[dict]):
        """Send a daily digest to Slack."""
        if not jobs:
            return
        
        # Group jobs
        intern_jobs = [j for j in jobs if j.get('job_type') == 'intern']
        fulltime_jobs = [j for j in jobs if j.get('job_type') != 'intern']
        
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"🎯 Daily Job Alert: {len(jobs)} New Positions",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Found {len(jobs)} new positions today!*\n📋 Full-time/New Grad: {len(fulltime_jobs)} | 🎓 Internships: {len(intern_jobs)}"
                }
            },
            {"type": "divider"}
        ]
        
        # Add job cards (limit to prevent message being too long)
        for job in jobs[:10]:
            visa_emoji = "✅" if job.get('visa_sponsor') == 'Yes' else "❓" if job.get('visa_sponsor') == 'Unknown' else "❌"
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*<{job.get('url', '#')}|{job.get('title', 'Unknown')}>*\n🏢 {job.get('company', 'Unknown')} | 📍 {job.get('location', 'N/A')}\n{visa_emoji} Visa Sponsorship: {job.get('visa_sponsor', 'Unknown')}"
                }
            })
        
        if len(jobs) > 10:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"_...and {len(jobs) - 10} more positions. Check your email or CSV for the full list!_"
                }
            })
        
        payload = {"blocks": blocks}
        
        try:
            response = requests.post(self.webhook_url, json=payload, timeout=30)
            if response.status_code == 200:
                logger.info("Slack notification sent successfully")
            else:
                logger.error(f"Slack notification failed: {response.status_code}")
        except Exception as e:
            logger.error(f"Error sending Slack notification: {e}")


class TerminalOutput:
    """Enhanced terminal output with colors."""
    
    # ANSI color codes
    COLORS = {
        'reset': '\033[0m',
        'bold': '\033[1m',
        'red': '\033[91m',
        'green': '\033[92m',
        'yellow': '\033[93m',
        'blue': '\033[94m',
        'magenta': '\033[95m',
        'cyan': '\033[96m',
        'white': '\033[97m',
    }
    
    @classmethod
    def print_header(cls, text: str):
        """Print a header."""
        print(f"\n{cls.COLORS['bold']}{cls.COLORS['cyan']}{'='*60}{cls.COLORS['reset']}")
        print(f"{cls.COLORS['bold']}{cls.COLORS['white']}{text}{cls.COLORS['reset']}")
        print(f"{cls.COLORS['cyan']}{'='*60}{cls.COLORS['reset']}")
    
    @classmethod
    def print_job(cls, index: int, job: dict):
        """Print a single job with formatting."""
        visa = job.get('visa_sponsor', 'Unknown')
        if visa == 'Yes':
            visa_str = f"{cls.COLORS['green']}✅ Visa{cls.COLORS['reset']}"
        elif visa == 'No':
            visa_str = f"{cls.COLORS['red']}❌ No Visa{cls.COLORS['reset']}"
        else:
            visa_str = f"{cls.COLORS['yellow']}❓ Unknown{cls.COLORS['reset']}"
        
        job_type = job.get('job_type', 'fulltime')
        type_color = cls.COLORS['magenta'] if job_type == 'intern' else cls.COLORS['blue']
        
        print(f"\n{cls.COLORS['bold']}{index}. {job.get('title', 'Unknown')}{cls.COLORS['reset']}")
        print(f"   🏢 {cls.COLORS['white']}{job.get('company', 'Unknown')}{cls.COLORS['reset']} | 📍 {job.get('location', 'N/A')}")
        if job.get('match_score') is not None:
            print(f"   🎯 Match Score: {cls.COLORS['green']}{job.get('match_score')}{cls.COLORS['reset']}")
        if job.get('match_reasons'):
            reasons = ', '.join(job.get('match_reasons', [])[:3])
            print(f"   🧠 Why matched: {reasons}")
        print(f"   {visa_str} | {type_color}{job_type.upper()}{cls.COLORS['reset']} | 🔗 {job.get('url', '')[:60]}...")
    
    @classmethod
    def print_summary(cls, jobs: List[dict], limit: int = 15):
        """Print job summary."""
        cls.print_header("📋 NEW JOB POSITIONS FOUND")
        
        intern_count = len([j for j in jobs if j.get('job_type') == 'intern'])
        fulltime_count = len(jobs) - intern_count
        
        print(f"\n{cls.COLORS['bold']}Summary:{cls.COLORS['reset']} Found {cls.COLORS['green']}{len(jobs)}{cls.COLORS['reset']} new positions")
        print(f"  📋 Full-time/New Grad: {cls.COLORS['blue']}{fulltime_count}{cls.COLORS['reset']}")
        print(f"  🎓 Internships: {cls.COLORS['magenta']}{intern_count}{cls.COLORS['reset']}")
        
        # Sort: highest resume match first, then visa-friendly, then job type
        sorted_jobs = sorted(
            jobs,
            key=lambda x: (
                -x.get('match_score', 0),
                x.get('visa_sponsor', 'Unknown') != 'Yes',
                x.get('job_type', '')
            )
        )
        
        for i, job in enumerate(sorted_jobs[:limit], 1):
            cls.print_job(i, job)
        
        if len(jobs) > limit:
            print(f"\n{cls.COLORS['yellow']}... and {len(jobs) - limit} more jobs. Check the CSV for the full list.{cls.COLORS['reset']}")


class EmailNotifier:
    """Sends email notifications for new jobs."""
    
    def __init__(self, smtp_server: str, smtp_port: int, 
                 sender_email: str, sender_password: str):
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.sender_email = sender_email
        self.sender_password = sender_password
    
    def send_daily_digest(self, recipient_email: str, jobs: List[dict], 
                          csv_path: Optional[str] = None):
        """Send a daily digest email with new jobs."""
        if not jobs:
            logger.info("No new jobs to send.")
            return
        
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"🎯 Daily Job Alert: {len(jobs)} New Positions - {datetime.now().strftime('%Y-%m-%d')}"
        msg['From'] = self.sender_email
        msg['To'] = recipient_email
        
        # Create HTML email body
        html = self._create_html_email(jobs)
        msg.attach(MIMEText(html, 'html'))
        
        # Attach CSV if provided
        if csv_path and os.path.exists(csv_path):
            with open(csv_path, 'rb') as f:
                attachment = MIMEBase('application', 'octet-stream')
                attachment.set_payload(f.read())
                encoders.encode_base64(attachment)
                attachment.add_header('Content-Disposition', 
                                     f'attachment; filename="{os.path.basename(csv_path)}"')
                msg.attach(attachment)
        
        # Send email
        try:
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.sender_email, self.sender_password)
                server.send_message(msg)
            logger.info(f"Email sent successfully to {recipient_email}")
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
    
    def _create_html_email(self, jobs: List[dict]) -> str:
        """Create an HTML email body."""
        # Group jobs by type
        intern_jobs = [j for j in jobs if j.get('job_type') == 'intern']
        fulltime_jobs = [j for j in jobs if j.get('job_type') != 'intern']
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; }}
                .job-card {{ 
                    border: 1px solid #e0e0e0; 
                    border-radius: 8px; 
                    padding: 16px; 
                    margin: 12px 0;
                    background: #fafafa;
                }}
                .job-title {{ color: #1a73e8; font-size: 18px; margin: 0 0 8px 0; }}
                .company {{ font-weight: bold; color: #333; }}
                .location {{ color: #666; }}
                .visa {{ 
                    display: inline-block;
                    padding: 2px 8px;
                    border-radius: 4px;
                    font-size: 12px;
                }}
                .visa-yes {{ background: #e6f4ea; color: #1e8e3e; }}
                .visa-no {{ background: #fce8e6; color: #c5221f; }}
                .visa-unknown {{ background: #f1f3f4; color: #5f6368; }}
                .section {{ margin: 24px 0; }}
                h2 {{ border-bottom: 2px solid #1a73e8; padding-bottom: 8px; }}
                .stats {{ background: #e8f0fe; padding: 16px; border-radius: 8px; margin-bottom: 24px; }}
            </style>
        </head>
        <body>
            <h1>🎯 Daily Job Alert</h1>
            <div class="stats">
                <strong>Found {len(jobs)} new positions today!</strong><br>
                📋 Full-time/New Grad: {len(fulltime_jobs)} | 🎓 Internships: {len(intern_jobs)}
            </div>
        """
        
        if fulltime_jobs:
            html += '<div class="section"><h2>💼 Full-time / New Grad Positions</h2>'
            for job in fulltime_jobs[:20]:  # Limit to prevent huge emails
                html += self._job_card_html(job)
            html += '</div>'
        
        if intern_jobs:
            html += '<div class="section"><h2>🎓 Internship Positions</h2>'
            for job in intern_jobs[:20]:
                html += self._job_card_html(job)
            html += '</div>'
        
        html += """
            <hr>
            <p style="color: #666; font-size: 12px;">
                This is an automated job alert. Generated by Job Monitor.<br>
                Good luck with your job search! 🍀
            </p>
        </body>
        </html>
        """
        return html
    
    def _job_card_html(self, job: dict) -> str:
        visa_class = {
            'Yes': 'visa-yes',
            'No': 'visa-no'
        }.get(job.get('visa_sponsor', 'Unknown'), 'visa-unknown')
        
        return f"""
        <div class="job-card">
            <h3 class="job-title">
                <a href="{job.get('url', '#')}">{job.get('title', 'Unknown Position')}</a>
            </h3>
            <p class="company">{job.get('company', 'Unknown Company')}</p>
            <p class="location">📍 {job.get('location', 'Location not specified')}</p>
            <p>
                <span class="visa {visa_class}">
                    Visa Sponsorship: {job.get('visa_sponsor', 'Unknown')}
                </span>
                &nbsp;|&nbsp; Source: {job.get('source', 'Unknown')}
            </p>
        </div>
        """


class JobMonitor:
    """Main job monitoring class that orchestrates scraping and notifications."""
    
    def __init__(self, config_path: str = "config.json"):
        self.config_path = os.path.abspath(config_path)
        self.base_dir = os.path.dirname(self.config_path)
        self.config = self._load_config(self.config_path)
        self.db = JobDatabase(self.config.get('db_path', 'jobs.db'))
        self.scrapers = {
            'github': GitHubNewGradScraper(),
            'indeed': IndeedScraper(),
            'linkedin': LinkedInScraper(),
            'companies': CompanyCareerScraper(),
        }
        
        if self.config.get('email'):
            self.notifier = EmailNotifier(
                smtp_server=self.config['email'].get('smtp_server', 'smtp.gmail.com'),
                smtp_port=self.config['email'].get('smtp_port', 587),
                sender_email=self.config['email'].get('sender_email', ''),
                sender_password=self.config['email'].get('sender_password', '')
            )
        else:
            self.notifier = None
        
        # Slack notifier
        if self.config.get('slack', {}).get('webhook_url'):
            self.slack_notifier = SlackNotifier(
                webhook_url=self.config['slack']['webhook_url']
            )
        else:
            self.slack_notifier = None

        self._normalize_paths()
        self.resume_profile = self._build_resume_profile()
    
    def _load_config(self, config_path: str) -> dict:
        """Load configuration from JSON file."""
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return json.load(f)
        return self._create_default_config(config_path)

    def _resolve_path(self, maybe_path: str) -> str:
        """Resolve relative paths against config directory."""
        if not maybe_path:
            return maybe_path
        if os.path.isabs(maybe_path):
            return maybe_path
        return os.path.join(self.base_dir, maybe_path)

    def _normalize_paths(self):
        """Normalize configured file paths so scheduled tasks run reliably."""
        self.config['db_path'] = self._resolve_path(self.config.get('db_path', 'jobs.db'))
        self.db.db_path = self.config['db_path']

        export_cfg = self.config.get('export', {})
        if export_cfg.get('csv_path'):
            export_cfg['csv_path'] = self._resolve_path(export_cfg['csv_path'])
        if export_cfg.get('excel_path'):
            export_cfg['excel_path'] = self._resolve_path(export_cfg['excel_path'])

        resume_cfg = self.config.get('resume_profile', {})
        if resume_cfg.get('resume_path'):
            resume_cfg['resume_path'] = self._resolve_path(resume_cfg['resume_path'])
    
    def _create_default_config(self, config_path: str) -> dict:
        """Create a default configuration file."""
        config = {
            "db_path": "jobs.db",
            "keywords": [
                "software engineer new grad",
                "software engineer intern",
                "data scientist new grad",
                "machine learning engineer intern",
                "backend engineer new grad"
            ],
            "locations": ["United States", "Remote"],
            "job_types": ["fulltime", "intern", "new_grad"],
            "scrapers": ["github", "indeed"],
            "schedule": {
                "daily_time": "09:00",
                "timezone": "America/New_York"
            },
            "email": {
                "enabled": False,
                "smtp_server": "smtp.gmail.com",
                "smtp_port": 587,
                "sender_email": "your-email@gmail.com",
                "sender_password": "your-app-password",
                "recipient_email": "your-email@gmail.com"
            },
            "export": {
                "csv_enabled": True,
                "csv_path": "daily_jobs.csv"
            },
            "backlog": {
                "enabled": True,
                "since_days": 7,
                "max_jobs": 10
            },
            "filters": {
                "exclude_companies": [],
                "include_only_locations": [],
                "min_match_score": 35,
                "include_unknown_visa": True,
                "require_role_or_skill_match": True
            },
            "resume_profile": {
                "resume_path": "",
                "needs_sponsorship": True,
                "target_roles": [],
                "skill_keywords": ["python", "sql", "machine learning", "data analysis"],
                "preferred_locations": ["United States", "Remote"],
                "preferred_job_types": ["new_grad", "fulltime", "intern"]
            }
        }
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
        logger.info(f"Created default config at {config_path}")
        return config

    def _extract_resume_text(self, resume_path: str) -> str:
        """Extract text from a PDF resume."""
        if not resume_path or not os.path.exists(resume_path):
            return ""
        try:
            from pypdf import PdfReader
        except Exception:
            logger.warning("pypdf not installed. Resume text extraction skipped.")
            return ""

        try:
            reader = PdfReader(resume_path)
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n".join(pages)
        except Exception as e:
            logger.warning(f"Could not parse resume PDF ({resume_path}): {e}")
            return ""

    def _infer_resume_profile(self, resume_text: str) -> Dict[str, List[str]]:
        """Infer role and skill keywords from resume text."""
        text = (resume_text or "").lower()
        if not text:
            return {"target_roles": [], "skill_keywords": []}

        role_candidates = [
            "software engineer", "data scientist", "data analyst", "machine learning engineer",
            "backend engineer", "frontend engineer", "full stack engineer", "quantitative analyst",
            "business analyst", "product analyst"
        ]
        skill_candidates = [
            "python", "sql", "r", "java", "c++", "javascript", "typescript", "react", "node",
            "pandas", "numpy", "scikit-learn", "tensorflow", "pytorch", "tableau", "power bi",
            "aws", "gcp", "azure", "spark", "airflow", "docker", "kubernetes", "excel"
        ]

        target_roles = [role for role in role_candidates if role in text]
        skill_keywords = [skill for skill in skill_candidates if re.search(rf"\b{re.escape(skill)}\b", text)]

        return {
            "target_roles": list(dict.fromkeys(target_roles)),
            "skill_keywords": list(dict.fromkeys(skill_keywords)),
        }

    def _build_resume_profile(self) -> dict:
        """Build final resume profile from config + parsed resume content."""
        profile = self.config.get('resume_profile', {}).copy()

        profile.setdefault('resume_path', '')
        profile.setdefault('needs_sponsorship', bool(self.config.get('visa_filter', True)))
        profile.setdefault('target_roles', [])
        profile.setdefault('skill_keywords', [])
        profile.setdefault('preferred_locations', self.config.get('locations', ['United States', 'Remote']))
        profile.setdefault('preferred_job_types', ['new_grad', 'fulltime', 'intern'])

        if not profile['target_roles']:
            profile['target_roles'] = self.config.get('keywords', [])

        resume_text = self._extract_resume_text(profile.get('resume_path', ''))
        inferred = self._infer_resume_profile(resume_text)

        profile['target_roles'] = list(dict.fromkeys(profile.get('target_roles', []) + inferred.get('target_roles', [])))
        profile['skill_keywords'] = list(dict.fromkeys(profile.get('skill_keywords', []) + inferred.get('skill_keywords', [])))

        logger.info(
            "Resume profile loaded: %s roles, %s skills, sponsorship_needed=%s",
            len(profile.get('target_roles', [])),
            len(profile.get('skill_keywords', [])),
            profile.get('needs_sponsorship', True)
        )
        return profile

    def _score_job_match(self, job: dict) -> Tuple[int, List[str]]:
        """Score how well a job matches resume profile."""
        score = 0
        reasons = []

        title = (job.get('title') or '').lower()
        location = (job.get('location') or '').lower()
        details = " ".join([
            job.get('title', ''),
            job.get('company', ''),
            job.get('description_preview', ''),
            job.get('source', ''),
        ]).lower()

        for role in self.resume_profile.get('target_roles', []):
            role_lower = role.lower()
            if role_lower and role_lower in title:
                score += 30
                reasons.append(f"role: {role}")
                break

        skill_hits = 0
        for skill in self.resume_profile.get('skill_keywords', []):
            skill_lower = skill.lower()
            if skill_lower and re.search(rf"\b{re.escape(skill_lower)}\b", details):
                skill_hits += 1

        if skill_hits:
            skill_score = min(30, skill_hits * 6)
            score += skill_score
            reasons.append(f"skills matched: {skill_hits}")

        preferred_locations = [loc.lower() for loc in self.resume_profile.get('preferred_locations', []) if loc]
        if preferred_locations and any(loc in location for loc in preferred_locations):
            score += 10
            reasons.append("preferred location")

        preferred_job_types = [jt.lower() for jt in self.resume_profile.get('preferred_job_types', []) if jt]
        current_job_type = (job.get('job_type') or '').lower()
        if preferred_job_types and current_job_type in preferred_job_types:
            score += 10
            reasons.append(f"job type: {current_job_type}")

        visa = job.get('visa_sponsor', 'Unknown')
        if self.resume_profile.get('needs_sponsorship', True):
            if visa == 'Yes':
                score += 20
                reasons.append("visa sponsor: yes")
            elif visa == 'Unknown':
                score += 5

        return min(score, 100), reasons

    def filter_and_rank_jobs(self, jobs: List[dict]) -> List[dict]:
        """Filter and rank jobs based on resume match + sponsorship need."""
        filters = self.config.get('filters', {})
        min_match_score = int(filters.get('min_match_score', 0))
        include_unknown_visa = bool(filters.get('include_unknown_visa', True))
        require_role_or_skill_match = bool(filters.get('require_role_or_skill_match', True))
        exclude_companies = set(c.lower() for c in filters.get('exclude_companies', []))
        include_only_locations = [l.lower() for l in filters.get('include_only_locations', [])]
        exclude_title_keywords = [k.lower() for k in filters.get('exclude_title_keywords', []) if k]

        needs_sponsorship = bool(self.resume_profile.get('needs_sponsorship', self.config.get('visa_filter', True)))
        apply_visa_filter = bool(self.config.get('visa_filter', True)) and needs_sponsorship

        filtered = []
        for job in jobs:
            company = (job.get('company') or '').lower()
            location = (job.get('location') or '').lower()
            title = (job.get('title') or '').lower()
            visa = job.get('visa_sponsor', 'Unknown')

            if company in exclude_companies:
                continue

            if include_only_locations and not any(loc in location for loc in include_only_locations):
                continue

            if exclude_title_keywords and any(keyword in title for keyword in exclude_title_keywords):
                continue

            if apply_visa_filter:
                if visa == 'No':
                    continue
                if visa == 'Unknown' and not include_unknown_visa:
                    continue

            match_score, reasons = self._score_job_match(job)
            if match_score < min_match_score:
                continue

            if require_role_or_skill_match:
                has_role_or_skill = any(
                    reason.startswith('role:') or reason.startswith('skills matched:')
                    for reason in reasons
                )
                if not has_role_or_skill:
                    continue

            enriched = dict(job)
            enriched['match_score'] = match_score
            enriched['match_reasons'] = reasons
            filtered.append(enriched)

        filtered.sort(
            key=lambda j: (
                -j.get('match_score', 0),
                j.get('visa_sponsor', 'Unknown') != 'Yes',
                j.get('job_type', '')
            )
        )
        return filtered
    
    def run_daily_scan(self) -> List[Job]:
        """Run a full scan of all configured sources."""
        logger.info("Starting daily job scan...")
        all_jobs = []
        new_jobs = []
        configured_job_types = {
            (job_type or "").strip().lower()
            for job_type in self.config.get('job_types', ['fulltime', 'intern', 'new_grad'])
        }
        
        # GitHub scraper (best for new grad/intern positions)
        if 'github' in self.config.get('scrapers', []):
            logger.info("Scraping GitHub job lists...")
            github_jobs = self.scrapers['github'].scrape()
            all_jobs.extend(github_jobs)
            logger.info(f"Found {len(github_jobs)} jobs from GitHub")
        
        # LinkedIn scraper
        if 'linkedin' in self.config.get('scrapers', []):
            logger.info("Scraping LinkedIn...")
            linkedin_jobs = self.scrapers['linkedin'].scrape(
                keywords=self.config.get('keywords', []),
                location=self.config.get('locations', ['United States'])[0]
            )
            all_jobs.extend(linkedin_jobs)
            logger.info(f"Found {len(linkedin_jobs)} jobs from LinkedIn")
        
        # Indeed scraper
        if 'indeed' in self.config.get('scrapers', []):
            logger.info("Scraping Indeed...")
            indeed_jobs = self.scrapers['indeed'].scrape(
                keywords=self.config.get('keywords', []),
                location=self.config.get('locations', ['United States'])[0]
            )
            all_jobs.extend(indeed_jobs)
            logger.info(f"Found {len(indeed_jobs)} jobs from Indeed")
        
        # Company career pages scraper
        if 'companies' in self.config.get('scrapers', []):
            logger.info("Scraping company career pages...")
            company_jobs = self.scrapers['companies'].scrape(
                companies=self.config.get('target_companies', None)
            )
            all_jobs.extend(company_jobs)
            logger.info(f"Found {len(company_jobs)} jobs from company career pages")
        
        # Add jobs to database and track new ones
        for job in all_jobs:
            job_type = (job.job_type or '').strip().lower()
            if configured_job_types and job_type and job_type not in configured_job_types:
                continue
            if self.db.add_job(job):
                new_jobs.append(job)
        
        logger.info(f"Total jobs found: {len(all_jobs)}, New jobs: {len(new_jobs)}")
        return new_jobs
    
    def export_to_csv(self, jobs: List[dict], filename: str = None) -> str:
        """Export jobs to CSV file."""
        if not filename:
            configured_csv_path = self.config.get('export', {}).get('csv_path')
            filename = configured_csv_path or f"jobs_{datetime.now().strftime('%Y%m%d')}.csv"

        if not os.path.isabs(filename):
            filename = self._resolve_path(filename)
        
        df = pd.DataFrame(jobs)
        if 'match_reasons' in df.columns:
            df['match_reasons'] = df['match_reasons'].apply(
                lambda reasons: '; '.join(reasons) if isinstance(reasons, list) else reasons
            )
        columns_order = ['title', 'company', 'location', 'job_type', 
                         'visa_sponsor', 'match_score', 'match_reasons',
                         'url', 'source', 'date_posted']
        existing_cols = [c for c in columns_order if c in df.columns]
        df = df[existing_cols]
        df.to_csv(filename, index=False)
        logger.info(f"Exported {len(jobs)} jobs to {filename}")
        return filename
    
    def send_notifications(self, jobs: List[Job], csv_path: Optional[str] = None):
        """Send email and Slack notifications for new jobs."""
        job_dicts = [asdict(j) if isinstance(j, Job) else j for j in jobs]

        if not csv_path and self.config.get('export', {}).get('csv_enabled'):
            csv_path = self.export_to_csv(job_dicts)
        
        # Email notification
        if self.notifier and self.config.get('email', {}).get('enabled'):
            self.notifier.send_daily_digest(
                recipient_email=self.config['email']['recipient_email'],
                jobs=job_dicts,
                csv_path=csv_path
            )
        else:
            logger.info("Email notifications disabled.")
        
        # Slack notification
        if self.slack_notifier and self.config.get('slack', {}).get('enabled'):
            self.slack_notifier.send_daily_digest(job_dicts)
        else:
            logger.info("Slack notifications disabled.")
    
    def daily_job(self):
        """The main daily job that runs scanning and notifications."""
        new_jobs = self.run_daily_scan()

        new_job_dicts = [asdict(j) for j in new_jobs]
        filtered_new_jobs = self.filter_and_rank_jobs(new_job_dicts)

        backlog_cfg = self.config.get('backlog', {})
        backlog_enabled = bool(backlog_cfg.get('enabled', True))
        backlog_since_days = int(backlog_cfg.get('since_days', 7))
        backlog_max_jobs = int(backlog_cfg.get('max_jobs', 10))

        filtered_backlog_jobs = []
        if backlog_enabled:
            new_job_ids = {j.get('job_id') for j in new_job_dicts if j.get('job_id')}
            recent_unnotified = self.db.get_recent_unnotified_jobs(since_days=backlog_since_days)
            backlog_candidates = [
                j for j in recent_unnotified
                if j.get('job_id') not in new_job_ids
            ]
            filtered_backlog_jobs = self.filter_and_rank_jobs(backlog_candidates)[:backlog_max_jobs]

        jobs_to_notify = []
        seen_ids = set()

        for job in filtered_new_jobs:
            jid = job.get('job_id')
            if jid and jid not in seen_ids:
                seen_ids.add(jid)
            enriched = dict(job)
            enriched['notification_bucket'] = 'new'
            jobs_to_notify.append(enriched)

        for job in filtered_backlog_jobs:
            jid = job.get('job_id')
            if jid and jid in seen_ids:
                continue
            if jid:
                seen_ids.add(jid)
            enriched = dict(job)
            enriched['notification_bucket'] = 'backlog'
            jobs_to_notify.append(enriched)

        if not jobs_to_notify:
            if new_jobs:
                print(f"\n📭 Found {len(new_jobs)} new jobs, but none matched your resume filters.")
            else:
                print("\n📭 No new jobs found today.")
            return

        csv_path = self.export_to_csv(jobs_to_notify)
        print(
            f"\n✅ Found {len(new_jobs)} new jobs. "
            f"Matched: {len(filtered_new_jobs)} new + {len(filtered_backlog_jobs)} backlog. "
            f"Exported to {csv_path}"
        )

        self.print_summary(jobs_to_notify)
        self.send_notifications(jobs_to_notify, csv_path=csv_path)

        notified_ids = [j.get('job_id') for j in jobs_to_notify if j.get('job_id')]
        if notified_ids:
            self.db.mark_notified(notified_ids)
    
    def print_summary(self, jobs: List[Job]):
        """Print a summary of new jobs to terminal."""
        job_dicts = [asdict(j) if isinstance(j, Job) else j for j in jobs]
        TerminalOutput.print_summary(job_dicts)
    
    def start_scheduler(self):
        """Start the scheduler for daily jobs."""
        daily_time = self.config.get('schedule', {}).get('daily_time', '09:00')
        schedule.every().day.at(daily_time).do(self.daily_job)
        
        logger.info(f"Scheduler started. Will run daily at {daily_time}")
        print(f"🕐 Job monitor scheduled to run daily at {daily_time}")
        print("Press Ctrl+C to stop.\n")
        
        # Run once immediately
        self.daily_job()
        
        while True:
            schedule.run_pending()
            time.sleep(60)


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Job Monitor - Track new job postings daily')
    parser.add_argument('--config', default=os.path.join(SCRIPT_DIR, 'config.json'), help='Path to config file')
    parser.add_argument('--once', action='store_true', help='Run once and exit')
    parser.add_argument('--export', action='store_true', help='Export all jobs to CSV')
    args = parser.parse_args()
    
    monitor = JobMonitor(config_path=args.config)
    
    if args.export:
        jobs = monitor.db.get_new_jobs(since_days=30)
        if jobs:
            csv_path = monitor.export_to_csv(jobs, 'all_jobs_export.csv')
            print(f"Exported {len(jobs)} jobs to {csv_path}")
        else:
            print("No jobs to export.")
    elif args.once:
        monitor.daily_job()
    else:
        monitor.start_scheduler()


if __name__ == "__main__":
    main()
