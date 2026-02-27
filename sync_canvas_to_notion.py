#!/usr/bin/env python3
"""
sync canvas assignments with due dates into a notion database (data source).

required:
  - CANVAS_BASE_URL (e.g. https://your-school.instructure.com)
  - CANVAS_TOKEN
  - NOTION_TOKEN
  - NOTION_DATA_SOURCE_ID (the notion "data source" id for query/create)
optional:
  - TIMEZONE (default: america/chicago)
  - NOTION_VERSION (default: 2025-09-03)
  - COURSE_NAME_MAP_JSON (JSON dict mapping canvas course names -> notion select names)
  - NOTION_PROP_* overrides if your notion column names differ

notion properties expected (defaults; configurable via env):
  - name (title)
  - course (select)
  - due date (date)
  - canvas ID (rich_text)
  - course ID (rich_text)
  - URL (url)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional

import pytz
import requests
from dateutil import parser

# ----------------------
# logging
# ----------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

# ----------------------
# config
# ----------------------
def _env(name: str, default: Optional[str] = None) -> str:
    val = os.getenv(name, default)
    if val is None or val.strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val

def _env_opt(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.getenv(name, default)
    if val is None:
        return None
    val = val.strip()
    return val if val else None

def _load_course_map() -> Dict[str, str]:
    raw = _env_opt("COURSE_NAME_MAP_JSON")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("COURSE_NAME_MAP_JSON must be a JSON object/dict.")
        # ensure string->string
        out: Dict[str, str] = {}
        for k, v in parsed.items():
            out[str(k)] = str(v)
        return out
    except Exception as e:
        raise RuntimeError(f"Failed to parse COURSE_NAME_MAP_JSON: {e}") from e

@dataclass(frozen=True)
class NotionProps:
    name: str = "Name"
    course: str = "Course"
    due_date: str = "Due Date"
    canvas_id: str = "Canvas ID"
    course_id: str = "Course ID"
    url: str = "URL"

    @staticmethod
    def from_env() -> "NotionProps":
        return NotionProps(
            name=_env_opt("NOTION_PROP_NAME", "Name") or "Name",
            course=_env_opt("NOTION_PROP_COURSE", "Course") or "Course",
            due_date=_env_opt("NOTION_PROP_DUE_DATE", "Due Date") or "Due Date",
            canvas_id=_env_opt("NOTION_PROP_CANVAS_ID", "Canvas ID") or "Canvas ID",
            course_id=_env_opt("NOTION_PROP_COURSE_ID", "Course ID") or "Course ID",
            url=_env_opt("NOTION_PROP_URL", "URL") or "URL",
        )

@dataclass(frozen=True)
class Config:
    canvas_base_url: str
    canvas_token: str
    notion_token: str
    notion_data_source_id: str
    notion_version: str
    timezone: str
    course_name_map: Dict[str, str]
    notion_props: NotionProps
    minutes_window: int = 2
    per_page: int = 100
    timeout_seconds: int = 30

    @staticmethod
    def load() -> "Config":
        canvas_base_url = _env("CANVAS_BASE_URL").rstrip("/")
        return Config(
            canvas_base_url=canvas_base_url,
            canvas_token=_env("CANVAS_TOKEN"),
            notion_token=_env("NOTION_TOKEN"),
            notion_data_source_id=_env("NOTION_DATA_SOURCE_ID"),
            notion_version=_env_opt("NOTION_VERSION", "2025-09-03") or "2025-09-03",
            timezone=_env_opt("TIMEZONE", "America/Chicago") or "America/Chicago",
            course_name_map=_load_course_map(),
            notion_props=NotionProps.from_env(),
            minutes_window=int(_env_opt("MATCH_MINUTES_WINDOW", "2") or "2"),
            per_page=int(_env_opt("PER_PAGE", "100") or "100"),
            timeout_seconds=int(_env_opt("HTTP_TIMEOUT", "30") or "30"),
        )

# ----------------------
# clients
# ----------------------
class CanvasClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {cfg.canvas_token}"})

    def _get_paginated(self, url: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Canvas uses Link headers for pagination."""
        results: List[Dict[str, Any]] = []
        next_url = url
        next_params = dict(params)

        while next_url:
            r = self.session.get(
                next_url,
                params=next_params,
                timeout=self.cfg.timeout_seconds,
            )
            if not r.ok:
                log.error("Canvas error %s: %s", r.status_code, r.text)
                r.raise_for_status()

            batch = r.json()
            if isinstance(batch, list):
                results.extend(batch)
            else:
                # Canvas endpoints here should return lists; keep safe anyway
                results.append(batch)

            # Parse Link header for rel="next"
            next_url = None
            next_params = {}
            link = r.headers.get("Link", "")
            # Example: <...page=2>; rel="next", <...page=1>; rel="current"
            for part in link.split(","):
                part = part.strip()
                if 'rel="next"' in part:
                    start = part.find("<") + 1
                    end = part.find(">")
                    if start > 0 and end > start:
                        next_url = part[start:end]
                        break

        return results

    def get_courses(self) -> List[Dict[str, Any]]:
        url = f"{self.cfg.canvas_base_url}/api/v1/courses"
        return self._get_paginated(
            url,
            params={"enrollment_state": "active", "per_page": self.cfg.per_page},
        )

    def get_assignments(self, course_id: int) -> List[Dict[str, Any]]:
        url = f"{self.cfg.canvas_base_url}/api/v1/courses/{course_id}/assignments"
        return self._get_paginated(url, params={"per_page": self.cfg.per_page})


class NotionClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {cfg.notion_token}",
                "Content-Type": "application/json",
                "Notion-Version": cfg.notion_version,
            }
        )

    def query(self, filter_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
        url = f"https://api.notion.com/v1/data_sources/{self.cfg.notion_data_source_id}/query"
        r = self.session.post(url, json={"filter": filter_obj}, timeout=self.cfg.timeout_seconds)
        if not r.ok:
            log.error("Notion query error %s: %s", r.status_code, r.text)
            r.raise_for_status()
        return r.json().get("results", [])

    def find_existing_by_ids(self, canvas_id: int, course_id: int) -> Optional[str]:
        p = self.cfg.notion_props
        filter_obj = {
            "and": [
                {"property": p.canvas_id, "rich_text": {"equals": str(canvas_id)}},
                {"property": p.course_id, "rich_text": {"equals": str(course_id)}},
            ]
        }
        results = self.query(filter_obj)
        return results[0]["id"] if results else None

    def find_existing_by_course_and_due(self, course_name: str, due_iso_local: str) -> Optional[str]:
        """Fallback: match manual rows by Course + Due Date within +/- minutes_window."""
        p = self.cfg.notion_props
        dt = parser.isoparse(due_iso_local)
        start = (dt - timedelta(minutes=self.cfg.minutes_window)).isoformat()
        end = (dt + timedelta(minutes=self.cfg.minutes_window)).isoformat()

        filter_obj = {
            "and": [
                {"property": p.course, "select": {"equals": course_name}},
                {"property": p.due_date, "date": {"on_or_after": start}},
                {"property": p.due_date, "date": {"on_or_before": end}},
            ]
        }
        results = self.query(filter_obj)
        return results[0]["id"] if results else None

    def update_page(self, page_id: str, properties: Dict[str, Any]) -> None:
        url = f"https://api.notion.com/v1/pages/{page_id}"
        r = self.session.patch(url, json={"properties": properties}, timeout=self.cfg.timeout_seconds)
        if not r.ok:
            log.error("Notion update error %s: %s", r.status_code, r.text)
            r.raise_for_status()

    def create_page(self, properties: Dict[str, Any]) -> None:
        payload = {
            "parent": {"type": "data_source_id", "data_source_id": self.cfg.notion_data_source_id},
            "properties": properties,
        }
        url = "https://api.notion.com/v1/pages"
        r = self.session.post(url, json=payload, timeout=self.cfg.timeout_seconds)
        if not r.ok:
            log.error("Notion create error %s: %s", r.status_code, r.text)
            r.raise_for_status()


# ----------------------
# sync logic
# ----------------------
def to_local_iso(due_at: Optional[str], tz_name: str) -> Optional[str]:
    if not due_at:
        return None
    dt = parser.isoparse(due_at)
    local_tz = pytz.timezone(tz_name)
    return dt.astimezone(local_tz).isoformat()

def map_course_name(canvas_course_name: str, mapping: Dict[str, str]) -> str:
    return mapping.get(canvas_course_name, canvas_course_name)

def build_properties(
    cfg: Config,
    notion_course_name: str,
    assignment: Dict[str, Any],
    course_id: int,
    due_local: Optional[str],
    include_title: bool,
) -> Dict[str, Any]:
    p = cfg.notion_props

    props: Dict[str, Any] = {
        p.course: {"select": {"name": notion_course_name}},
        p.canvas_id: {"rich_text": [{"text": {"content": str(assignment["id"])}}]},
        p.course_id: {"rich_text": [{"text": {"content": str(course_id)}}]},
        p.url: {"url": assignment.get("html_url")},
    }

    if due_local:
        props[p.due_date] = {"date": {"start": due_local}}

    if include_title:
        title = assignment.get("name") or "Untitled"
        props[p.name] = {"title": [{"text": {"content": title}}]}

    return props

def upsert_assignment(cfg: Config, notion: NotionClient, course: Dict[str, Any], assignment: Dict[str, Any]) -> None:
    course_id = int(course["id"])
    canvas_course_name = str(course.get("name") or "Unknown Course")
    notion_course_name = map_course_name(canvas_course_name, cfg.course_name_map)

    due_local = to_local_iso(assignment.get("due_at"), cfg.timezone)
    if not due_local:
        return  # only sync due-dated assignments

    # 1) Strong match: Canvas ID + Course ID
    page_id = notion.find_existing_by_ids(int(assignment["id"]), course_id)

    # 2) Fallback: Course + Due Date window (helpful for manual rows)
    if not page_id:
        page_id = notion.find_existing_by_course_and_due(notion_course_name, due_local)

    if page_id:
        # Don't overwrite custom Notion titles
        props = build_properties(cfg, notion_course_name, assignment, course_id, due_local, include_title=False)
        notion.update_page(page_id, props)
        log.info("Updated: %s — %s (kept Notion title)", notion_course_name, assignment.get("name"))
    else:
        props = build_properties(cfg, notion_course_name, assignment, course_id, due_local, include_title=True)
        notion.create_page(props)
        log.info("Created: %s — %s", notion_course_name, assignment.get("name"))

def main() -> None:
    cfg = Config.load()
    canvas = CanvasClient(cfg)
    notion = NotionClient(cfg)

    log.info("Fetching courses from Canvas…")
    courses = canvas.get_courses()
    log.info("Found %d active courses.", len(courses))

    synced = 0
    for course in courses:
        cid = course.get("id")
        cname = course.get("name")
        log.info("Course: %s (id=%s)", cname, cid)

        assignments = canvas.get_assignments(int(cid))
        log.info("  Assignments returned: %d", len(assignments))

        for a in assignments:
            if a.get("due_at"):
                upsert_assignment(cfg, notion, course, a)
                synced += 1

    log.info("Done. Synced %d assignments (with due dates).", synced)

if __name__ == "__main__":
    main()
