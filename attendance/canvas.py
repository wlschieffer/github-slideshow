"""Minimal Canvas LMS API client — just enough to pull a course roster.

Canvas exposes several identifiers per student; which one matches your
scannable badges depends on your district's setup, so the caller picks the
field and the UI previews all of them before importing.
"""

import requests

# id_field value -> human label, in the order shown in the UI.
ID_FIELDS = {
    "sis_user_id": "SIS ID",
    "login_id": "Login ID",
    "integration_id": "Integration ID",
    "id": "Canvas ID",
}


class CanvasError(Exception):
    """Raised with a friendly, user-facing message when a call fails."""


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def fetch_course_students(base_url, token, course_id, timeout=20):
    """Return the list of student user objects for a course (all pages)."""
    base = base_url.rstrip("/")
    url = f"{base}/api/v1/courses/{course_id}/users"
    params = {"enrollment_type[]": "student", "per_page": 100}
    students = []
    try:
        while url:
            r = requests.get(url, headers=_headers(token), params=params, timeout=timeout)
            params = None  # follow-up pages already carry query params in the URL
            if r.status_code == 401:
                raise CanvasError(
                    "Canvas rejected the token (401). Double-check the access token."
                )
            if r.status_code == 403:
                raise CanvasError(
                    "Canvas denied access (403). The token may lack permission for this course."
                )
            if r.status_code == 404:
                raise CanvasError(
                    f"Course {course_id} not found (404). Check the course ID and site URL."
                )
            if r.status_code != 200:
                raise CanvasError(f"Canvas returned HTTP {r.status_code}.")
            batch = r.json()
            if not isinstance(batch, list):
                raise CanvasError("Unexpected response from Canvas (not a user list).")
            students.extend(batch)
            url = r.links.get("next", {}).get("url")
    except requests.RequestException as e:
        raise CanvasError(f"Could not reach Canvas: {e}")
    return students


def extract_id(student, id_field):
    """Pull the chosen identifier from a student object as a string, or None."""
    value = student.get(id_field)
    if value in (None, ""):
        return None
    return str(value)


def student_name(student):
    return student.get("sortable_name") or student.get("name") or "(no name)"
