"""Minimal Canvas LMS API client — enough to list sections and pull their
student rosters (from People / enrollments, not the gradebook).

Canvas exposes several identifiers per student; which one matches your
scannable badges depends on your district's setup, so the caller picks the
field and the UI previews all of them before importing.
"""

import time

import requests

# id_field value -> human label, in the order shown in the UI.
ID_FIELDS = {
    "sis_user_id": "SIS ID",
    "login_id": "Login ID",
    "integration_id": "Integration ID",
    "id": "Canvas ID",
}

# Enrollment states we treat as "on the roster". Pending/invited cover
# students in unpublished courses or before a term has started.
STUDENT_STATES = ["active", "invited", "creation_pending"]


class CanvasError(Exception):
    """Raised with a friendly, user-facing message when a call fails."""


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _raise_for_status(r):
    if r.status_code == 401:
        raise CanvasError("Canvas rejected the token (401). Double-check the access token.")
    if r.status_code == 403:
        raise CanvasError("Canvas denied access (403). The token may lack permission here.")
    if r.status_code == 404:
        raise CanvasError("Not found (404). Check the course/section ID and site URL.")
    if r.status_code != 200:
        raise CanvasError(f"Canvas returned HTTP {r.status_code}.")


def _paginate(url, token, params, timeout=20):
    """GET a paginated Canvas list endpoint, following Link: rel=next."""
    out = []
    try:
        while url:
            r = requests.get(url, headers=_headers(token), params=params, timeout=timeout)
            params = None  # follow-up pages already carry the query in the URL
            _raise_for_status(r)
            data = r.json()
            if not isinstance(data, list):
                raise CanvasError("Unexpected response from Canvas (expected a list).")
            out.extend(data)
            url = r.links.get("next", {}).get("url")
    except requests.RequestException as e:
        raise CanvasError(f"Could not reach Canvas: {e}")
    return out


def fetch_sections(base_url, token, course_id, timeout=20):
    """List a course's sections, with a student count when available."""
    base = base_url.rstrip("/")
    url = f"{base}/api/v1/courses/{course_id}/sections"
    return _paginate(
        url, token, {"per_page": 100, "include[]": "total_students"}, timeout
    )


def fetch_section_students(base_url, token, section_id, timeout=20):
    """Return student user objects for a section, including pending/invited
    enrollments (so unpublished or not-yet-started courses still work).

    Tries the section enrollments endpoint first; if that comes back empty,
    falls back to the section's ?include[]=students list."""
    base = base_url.rstrip("/")

    # Strategy A: enrollments for the section, across active + pending states.
    url = f"{base}/api/v1/sections/{section_id}/enrollments"
    params = {
        "type[]": "StudentEnrollment",
        "state[]": STUDENT_STATES,
        "include[]": "user",
        "per_page": 100,
    }
    students = []
    for e in _paginate(url, token, params, timeout):
        user = dict(e.get("user") or {})
        # Some deployments expose SIS/login on the enrollment, not the user.
        for key in ("sis_user_id", "login_id", "integration_id"):
            if not user.get(key) and e.get(key):
                user[key] = e[key]
        if user:
            students.append(user)
    if students:
        return students

    # Strategy B: the section object with its students included.
    url = f"{base}/api/v1/sections/{section_id}"
    try:
        r = requests.get(
            url, headers=_headers(token),
            params={"include[]": "students"}, timeout=timeout,
        )
        _raise_for_status(r)
        obj = r.json()
    except requests.RequestException as e:
        raise CanvasError(f"Could not reach Canvas: {e}")
    return list(obj.get("students") or [])


def fetch_user_profile(base_url, token, user_id, timeout=20, retries=3):
    """Fetch a single user's profile (has login_id, and sis_user_id if the
    token is permitted). Returns {} on any error so enrichment is best-effort.
    Retries on rate-limit / transient errors so a busy Canvas doesn't cause
    students to be silently dropped."""
    base = base_url.rstrip("/")
    url = f"{base}/api/v1/users/{user_id}/profile"
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=_headers(token), timeout=timeout)
            if r.status_code == 200:
                return r.json() or {}
            # 403 can be a rate limit ("Rate Limit Exceeded"); 429 too. Retry.
            if r.status_code in (403, 429) and attempt < retries:
                time.sleep(0.6 * (attempt + 1))
                continue
            return {}
        except requests.RequestException:
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
                continue
            return {}
    return {}


def fetch_course_user_index(base_url, token, course_id, timeout=20):
    """Index a course's students by Canvas user id, the same way the People
    page reads them — so sis_user_id / login_id come through when the token
    has course-level permission to view them. Broad enrollment states so
    invited / not-yet-active students are included."""
    base = base_url.rstrip("/")
    url = f"{base}/api/v1/courses/{course_id}/users"
    params = {
        "enrollment_type[]": "student",
        "enrollment_state[]": ["active", "invited", "completed", "inactive", "rejected"],
        "per_page": 100,
    }
    index = {}
    for u in _paginate(url, token, params, timeout):
        if u.get("id") is not None:
            index[u["id"]] = u
    return index


def fetch_user_logins(base_url, token, user_id, timeout=20):
    """A user's login/pseudonym records — the authoritative source for
    sis_user_id and unique_id (login). Best-effort: {} if not permitted."""
    base = base_url.rstrip("/")
    url = f"{base}/api/v1/users/{user_id}/logins"
    try:
        r = requests.get(url, headers=_headers(token), timeout=timeout)
        if r.status_code == 200 and isinstance(r.json(), list):
            return r.json()
    except requests.RequestException:
        pass
    return []


def enrich_students(base_url, token, students, course_ids=None, timeout=20):
    """Fill in login_id / sis_user_id for brief student objects (from the
    section-students fallback). First from a bulk course-user index (the
    People-equivalent, which carries SIS), then per-user profiles for any
    still missing. Mutates in place."""
    # 1) Bulk index from the course rosters (best source for SIS).
    index = {}
    for cid in (course_ids or []):
        try:
            index.update(fetch_course_user_index(base_url, token, cid, timeout))
        except CanvasError:
            pass
    for s in students:
        if s.get("login_id") or s.get("sis_user_id"):
            continue
        u = index.get(s.get("id"))
        if u:
            for key in ("sis_user_id", "login_id", "integration_id"):
                if not s.get(key) and u.get(key):
                    s[key] = u[key]

    # 2) Per-user profile lookup for anyone still missing an id.
    cache = {}
    for s in students:
        if s.get("login_id") or s.get("sis_user_id"):
            continue
        uid = s.get("id")
        if uid is None:
            continue
        if uid not in cache:
            cache[uid] = fetch_user_profile(base_url, token, uid, timeout)
        prof = cache[uid]
        for key in ("login_id", "sis_user_id", "integration_id"):
            if not s.get(key) and prof.get(key):
                s[key] = prof[key]

    # 3) Last resort: the user's login/pseudonym records (authoritative SIS).
    for s in students:
        if s.get("login_id") or s.get("sis_user_id"):
            continue
        uid = s.get("id")
        if uid is None:
            continue
        for login in fetch_user_logins(base_url, token, uid, timeout):
            if not s.get("sis_user_id") and login.get("sis_user_id"):
                s["sis_user_id"] = login["sis_user_id"]
            if not s.get("login_id") and login.get("unique_id"):
                s["login_id"] = login["unique_id"]
            if s.get("sis_user_id") or s.get("login_id"):
                break
    return students


def extract_id(student, id_field):
    """Pull the chosen identifier from a student object as a string, or None."""
    value = student.get(id_field)
    if value in (None, ""):
        return None
    return str(value)


def student_name(student):
    return student.get("sortable_name") or student.get("name") or "(no name)"
