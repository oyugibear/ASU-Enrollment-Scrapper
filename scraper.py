import asyncio
import json
import os
import random
import time

from collections import defaultdict
from datetime import datetime

import google.auth
import gspread

from google.oauth2 import service_account

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# CONFIGURATION
# ============================================================

GOOGLE_MAX_ATTEMPTS = 6

COURSE_MAX_ATTEMPTS = 3

COURSE_RESPONSE_TIMEOUT = 35

PAGE_NAVIGATION_TIMEOUT = 60000

COURSE_DELAY_MIN = 0.75
COURSE_DELAY_MAX = 1.75


# ============================================================
# EA DAILY SHEET STRUCTURE
#
# A = Subject
# B = Catalog Num
# C = Class Num
# D = Title
# E = Instructor Last Name
# F = Instructor First Name
# G = Previous Amount
# H = Enroll Cap
# I = Open Seats
# J = Current
# K = Update
# L = Previous enrollment capacity
# M = Enroll Desc
# ============================================================

COL_SUBJECT = 0
COL_CATALOG_NUM = 1
COL_CLASS_NUM = 2
COL_TITLE = 3

COL_INSTRUCTOR_LAST = 4
COL_INSTRUCTOR_FIRST = 5

COL_PREVIOUS_AMOUNT = 6

COL_ENROLL_CAP = 7
COL_OPEN_SEATS = 8
COL_CURRENT = 9

COL_UPDATE = 10

COL_PREVIOUS_CAPACITY = 11

COL_ENROLL_DESC = 12


# ============================================================
# GOOGLE AUTHENTICATION
# ============================================================

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_google_credentials():

    raw_json = os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "",
    ).strip()

    # --------------------------------------------------------
    # RENDER
    # --------------------------------------------------------

    if raw_json:

        try:

            info = json.loads(
                raw_json
            )

        except json.JSONDecodeError as error:

            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON "
                "contains invalid JSON."
            ) from error

        credentials = (
            service_account
            .Credentials
            .from_service_account_info(
                info,
                scopes=GOOGLE_SCOPES,
            )
        )

        print(
            "Google authentication: "
            "Render service account"
        )

        print(
            "Service account:",
            credentials.service_account_email,
        )

        return credentials

    # --------------------------------------------------------
    # GOOGLE CLOUD / LOCAL ADC FALLBACK
    # --------------------------------------------------------

    credentials, project = (
        google.auth.default(
            scopes=GOOGLE_SCOPES
        )
    )

    print(
        "Google authentication: ADC"
    )

    print(
        "ADC project:",
        project,
    )

    print(
        "Credential type:",
        type(credentials).__name__,
    )

    print(
        "Service account:",
        getattr(
            credentials,
            "service_account_email",
            "unknown",
        ),
    )

    return credentials


# ============================================================
# GOOGLE RETRY
# ============================================================

def is_retryable_google_error(
    error,
):

    message = str(
        error
    ).lower()

    retryable_patterns = [
        "429",
        "quota exceeded",
        "resource_exhausted",
        "rate limit",
        "500",
        "502",
        "503",
        "504",
        "internal error",
        "backend error",
        "service unavailable",
    ]

    return any(
        pattern in message
        for pattern in retryable_patterns
    )


def google_retry(
    operation,
    description="Google operation",
    attempts=GOOGLE_MAX_ATTEMPTS,
):

    last_error = None

    for attempt in range(
        1,
        attempts + 1,
    ):

        try:

            return operation()

        except Exception as error:

            last_error = error

            if not is_retryable_google_error(
                error
            ):

                raise

            if attempt >= attempts:

                break

            delay = min(
                2 ** (
                    attempt - 1
                ),
                30,
            )

            delay += random.uniform(
                0.25,
                1.25,
            )

            print(
                f"{description} failed. "
                f"Attempt "
                f"{attempt}/{attempts}. "
                f"Retrying in "
                f"{delay:.1f}s..."
            )

            time.sleep(
                delay
            )

    raise last_error


# ============================================================
# OPEN GOOGLE SHEET
# ============================================================

def get_sheet(
    sheet_url,
    sheet_name,
):

    credentials = (
        get_google_credentials()
    )

    gc = gspread.authorize(
        credentials
    )

    spreadsheet = google_retry(
        lambda:
            gc.open_by_url(
                sheet_url
            ),
        "Open spreadsheet",
    )

    print(
        "Spreadsheet opened:",
        spreadsheet.title,
    )

    worksheet = google_retry(
        lambda:
            spreadsheet.worksheet(
                sheet_name
            ),
        "Open worksheet",
    )

    print(
        "Worksheet opened:",
        worksheet.title,
    )

    return worksheet


# ============================================================
# DATE HELPERS
# ============================================================

def parse_sheet_date(
    sheet_name,
):

    try:

        return datetime.strptime(
            sheet_name,
            "%m/%d",
        ).replace(
            year=datetime.now().year
        )

    except ValueError:

        return None


# ============================================================
# FIND PREVIOUS DAILY ENROLLMENT SHEET
# ============================================================

def get_previous_run_sheet(
    spreadsheet,
    current_sheet_name,
):

    current_date = (
        parse_sheet_date(
            current_sheet_name
        )
    )

    if not current_date:

        print(
            "Could not parse current "
            f"sheet name: "
            f"{current_sheet_name}"
        )

        return None

    worksheets = google_retry(
        lambda:
            spreadsheet.worksheets(),
        "List worksheets",
    )

    candidates = []

    for worksheet in worksheets:

        name = (
            worksheet.title
        )

        if name in [
            "Enrollment Home",
            "Enrollment Template",
            "Enrollment Jobs",
        ]:

            continue

        if (
            name ==
            current_sheet_name
        ):

            continue

        sheet_date = (
            parse_sheet_date(
                name
            )
        )

        if (
            sheet_date
            and
            sheet_date <
            current_date
        ):

            candidates.append(
                (
                    sheet_date,
                    worksheet,
                )
            )

    if not candidates:

        print(
            "No previous dated "
            "enrollment run found."
        )

        return None

    candidates.sort(
        key=lambda item:
            item[0],
        reverse=True,
    )

    previous_sheet = (
        candidates[0][1]
    )

    print(
        "Previous enrollment run:",
        previous_sheet.title,
    )

    return previous_sheet


# ============================================================
# PREVIOUS DAY LOOKUP
#
# TODAY G Previous Amount
# = PREVIOUS DAY J Current
#
# TODAY L Previous enrollment capacity
# = PREVIOUS DAY H Enroll Cap
# ============================================================

def build_previous_lookup(
    previous_sheet,
):

    if previous_sheet is None:

        print(
            "No previous enrollment run. "
            "Historical values will remain blank."
        )

        return {}

    values = google_retry(
        lambda:
            previous_sheet
            .get_all_values(),
        "Read previous enrollment run",
    )

    lookup = {}

    for row in values[1:]:

        if (
            len(row) <=
            COL_CLASS_NUM
        ):

            continue

        class_num = str(
            row[
                COL_CLASS_NUM
            ]
        ).strip()

        if not class_num:

            continue

        previous_current = (
            row[
                COL_CURRENT
            ]
            if len(row) >
            COL_CURRENT
            else ""
        )

        previous_cap = (
            row[
                COL_ENROLL_CAP
            ]
            if len(row) >
            COL_ENROLL_CAP
            else ""
        )

        previous_open = (
            row[
                COL_OPEN_SEATS
            ]
            if len(row) >
            COL_OPEN_SEATS
            else ""
        )

        previous_last = (
            row[
                COL_INSTRUCTOR_LAST
            ]
            if len(row) >
            COL_INSTRUCTOR_LAST
            else ""
        )

        previous_first = (
            row[
                COL_INSTRUCTOR_FIRST
            ]
            if len(row) >
            COL_INSTRUCTOR_FIRST
            else ""
        )

        lookup[
            class_num
        ] = {

            "last_name":
                str(
                    previous_last
                ).strip(),

            "first_name":
                str(
                    previous_first
                ).strip(),

            # Yesterday Current
            # -> Today Previous Amount
            "previous_amount":
                str(
                    previous_current
                ).strip(),

            # Yesterday Enroll Cap
            # -> Today Previous enrollment capacity
            "previous_capacity":
                str(
                    previous_cap
                ).strip(),

            "cap":
                str(
                    previous_cap
                ).strip(),

            "open":
                str(
                    previous_open
                ).strip(),

            "current":
                str(
                    previous_current
                ).strip(),
        }

    print(
        f"Loaded {len(lookup)} classes "
        f"from previous run "
        f"{previous_sheet.title}."
    )

    return lookup


# ============================================================
# CURRENT EA LOOKUPS
# ============================================================

def build_daily_lookup(
    all_values,
):

    lookup = {}

    for row_number, row in enumerate(
        all_values[1:],
        start=2,
    ):

        if (
            len(row) <=
            COL_CLASS_NUM
        ):

            continue

        subject = (
            str(
                row[
                    COL_SUBJECT
                ]
            ).strip()
            if len(row) >
            COL_SUBJECT
            else ""
        )

        catalog_num = (
            str(
                row[
                    COL_CATALOG_NUM
                ]
            ).strip()
            if len(row) >
            COL_CATALOG_NUM
            else ""
        )

        class_num = (
            str(
                row[
                    COL_CLASS_NUM
                ]
            ).strip()
        )

        if not class_num:

            continue

        # Keep the first occurrence of a Class Num.
        if class_num in lookup:

            print(
                f"{class_num}: "
                "duplicate Class Num in EA; "
                "using first occurrence."
            )

            continue

        lookup[
            class_num
        ] = {

            "row_number":
                row_number,

            "row":
                row,

            "subject":
                subject.upper(),

            "catalog_num":
                catalog_num,
        }

    return lookup


# ============================================================
# BUILD COURSE GROUPS
#
# Example:
#
# {
#   ("PHY", "111"): {"65830", "61059", ...},
#   ("PHY", "113"): {...},
#   ("PHS", "110"): {...}
# }
# ============================================================

def build_course_groups(
    daily_lookup,
):

    groups = defaultdict(
        set
    )

    invalid_classes = []

    for (
        class_num,
        info,
    ) in daily_lookup.items():

        subject = str(
            info.get(
                "subject",
                "",
            )
        ).strip().upper()

        catalog_num = str(
            info.get(
                "catalog_num",
                "",
            )
        ).strip()

        if (
            not subject
            or
            not catalog_num
        ):

            invalid_classes.append(
                class_num
            )

            continue

        groups[
            (
                subject,
                catalog_num,
            )
        ].add(
            class_num
        )

    return (
        dict(
            groups
        ),
        invalid_classes,
    )


# ============================================================
# INTEGER / DIFFERENCE
# ============================================================

def calculate_difference(
    current_value,
    previous_value,
):

    current_text = str(
        current_value
        if current_value is not None
        else ""
    ).strip()

    previous_text = str(
        previous_value
        if previous_value is not None
        else ""
    ).strip()

    if (
        current_text == ""
        or
        previous_text == ""
    ):

        return ""

    try:

        return (
            int(
                current_text
            )
            -
            int(
                previous_text
            )
        )

    except (
        TypeError,
        ValueError,
    ):

        return ""


# ============================================================
# ENROLLMENT DESCRIPTION
# ============================================================

def build_enrollment_description(
    update,
):

    if update == "":

        return ""

    try:

        amount = int(
            update
        )

    except (
        TypeError,
        ValueError,
    ):

        return ""

    if amount > 0:

        return (
            f"Increased by {amount}"
        )

    if amount < 0:

        return (
            f"Decreased by "
            f"{abs(amount)}"
        )

    return "No change"


# ============================================================
# EXTRACT INSTRUCTOR
# ============================================================

def extract_instructor(
    clas,
):

    first_names = (
        clas.get(
            "INSTRUCTORSFIRSTNAMESHOWALL"
        )
        or
        clas.get(
            "INSTRUCTORSFIRSTNAMELIST"
        )
        or
        []
    )

    last_names = (
        clas.get(
            "INSTRUCTORSLASTNAMESHOWALL"
        )
        or
        clas.get(
            "INSTRUCTORSLASTNAMELIST"
        )
        or
        []
    )

    first_name = ""
    last_name = ""

    if first_names:

        first_name = str(
            first_names[0]
        ).split(
            "~"
        )[0].strip()

    if last_names:

        last_name = str(
            last_names[0]
        ).split(
            "~"
        )[0].strip()

    # ASU sometimes returns Staff.
    if (
        first_name.lower() ==
        "staff"
    ):

        first_name = ""

    if (
        last_name.lower() ==
        "staff"
    ):

        last_name = ""

    return (
        last_name,
        first_name,
    )


# ============================================================
# SIMPLIFY ONE ASU CLASS OBJECT
# ============================================================

def simplify_asu_class(
    item,
):

    if not isinstance(
        item,
        dict,
    ):

        return None

    clas = (
        item.get(
            "CLAS",
            {}
        )
    )

    if not isinstance(
        clas,
        dict,
    ):

        return None

    class_num = str(
        clas.get(
            "CLASSNBR",
            "",
        )
    ).strip()

    if not class_num:

        return None

    seat_info = (
        item.get(
            "seatInfo",
            {}
        )
    )

    if not isinstance(
        seat_info,
        dict,
    ):

        seat_info = {}

    # --------------------------------------------------------
    # CAPACITY
    # --------------------------------------------------------

    enroll_cap = (
        seat_info.get(
            "ENRL_CAP"
        )
    )

    if enroll_cap is None:

        enroll_cap = (
            clas.get(
                "ENRLCAP"
            )
        )

    # --------------------------------------------------------
    # CURRENT ENROLLMENT
    # --------------------------------------------------------

    enroll_total = (
        seat_info.get(
            "ENRL_TOT"
        )
    )

    if enroll_total is None:

        enroll_total = (
            clas.get(
                "ENRLTOT"
            )
        )

    # --------------------------------------------------------
    # OPEN SEATS
    # --------------------------------------------------------

    open_seats = None

    try:

        if (
            enroll_cap is not None
            and
            enroll_total is not None
        ):

            open_seats = (
                int(
                    enroll_cap
                )
                -
                int(
                    enroll_total
                )
            )

    except (
        TypeError,
        ValueError,
    ):

        open_seats = None

    # --------------------------------------------------------
    # INSTRUCTOR
    # --------------------------------------------------------

    (
        instructor_last,
        instructor_first,
    ) = extract_instructor(
        clas
    )

    return {

        "class_num":
            class_num,

        "subject":
            str(
                clas.get(
                    "SUBJECT",
                    "",
                )
            ).strip(),

        "catalog_num":
            str(
                clas.get(
                    "CATALOGNBR",
                    "",
                )
            ).strip(),

        "section":
            str(
                clas.get(
                    "CLASSSECTION",
                    "",
                )
            ).strip(),

        "session":
            str(
                clas.get(
                    "SESSIONCODE",
                    "",
                )
            ).strip(),

        "component":
            str(
                clas.get(
                    "SSRCOMPONENT",
                    "",
                )
            ).strip(),

        "title":
            str(
                clas.get(
                    "TITLE",
                    "",
                )
            ).strip(),

        "instructor_last":
            instructor_last,

        "instructor_first":
            instructor_first,

        "cap":
            (
                ""
                if enroll_cap is None
                else str(
                    enroll_cap
                )
            ),

        "current":
            (
                ""
                if enroll_total is None
                else str(
                    enroll_total
                )
            ),

        "open":
            (
                ""
                if open_seats is None
                else str(
                    open_seats
                )
            ),
    }


# ============================================================
# COURSE SEARCH URL
# ============================================================

def build_course_search_url(
    subject,
    catalog_num,
    term,
):

    return (
        "https://catalog.apps.asu.edu/"
        "catalog/classes/classlist?"
        "advanced=true"
        "&campusOrOnlineSelection=A"
        f"&catalogNbr={catalog_num}"
        "&honors=F"
        "&promod=F"
        "&searchType=all"
        f"&subject={subject}"
        f"&term={term}"
    )


# ============================================================
# INDIVIDUAL FALLBACK URL
# ============================================================

def build_class_search_url(
    class_num,
    term,
):

    return (
        "https://catalog.apps.asu.edu/"
        "catalog/classes/classlist?"
        "advanced=true"
        "&campusOrOnlineSelection=A"
        f"&classNbr={class_num}"
        "&honors=F"
        "&promod=F"
        "&searchType=all"
        f"&term={term}"
    )


# ============================================================
# WAIT FOR ASU'S OWN /search/classes RESPONSE
#
# IMPORTANT:
#
# We do NOT call the ASU API ourselves.
#
# The ASU Class Search frontend makes its own authenticated
# /search/classes request. We intercept that response.
# ============================================================

async def capture_search_response(
    page,
    navigation_url,
    matcher,
):

    loop = (
        asyncio.get_running_loop()
    )

    response_future = (
        loop.create_future()
    )

    async def handle_response(
        response,
    ):

        if response_future.done():

            return

        url = (
            response.url
        )

        if (
            "/search/classes"
            not in url
        ):

            return

        if not matcher(
            url
        ):

            return

        try:

            if (
                response.status !=
                200
            ):

                response_future.set_result({
                    "ok":
                        False,

                    "status":
                        response.status,

                    "url":
                        url,

                    "error":
                        (
                            "ASU classes API "
                            f"returned HTTP "
                            f"{response.status}."
                        ),
                })

                return

            data = (
                await response.json()
            )

            response_future.set_result({
                "ok":
                    True,

                "status":
                    response.status,

                "url":
                    url,

                "data":
                    data,
            })

        except Exception as error:

            if not response_future.done():

                response_future.set_result({
                    "ok":
                        False,

                    "status":
                        response.status,

                    "url":
                        url,

                    "error":
                        (
                            f"{type(error).__name__}: "
                            f"{error}"
                        ),
                })

    page.on(
        "response",
        handle_response,
    )

    try:

        try:

            await page.goto(
                navigation_url,
                timeout=
                    PAGE_NAVIGATION_TIMEOUT,
                wait_until=
                    "domcontentloaded",
            )

        except PlaywrightTimeoutError:

            # The API response may still arrive even when
            # the full page navigation exceeds the timeout.
            print(
                "Page navigation timed out; "
                "continuing to wait for "
                "ASU classes API response."
            )

        try:

            result = (
                await asyncio.wait_for(
                    response_future,
                    timeout=
                        COURSE_RESPONSE_TIMEOUT,
                )
            )

            return result

        except asyncio.TimeoutError:

            return {
                "ok":
                    False,

                "status":
                    0,

                "url":
                    navigation_url,

                "error":
                    (
                        "Timed out waiting for "
                        "ASU /search/classes response."
                    ),
            }

    finally:

        try:

            page.remove_listener(
                "response",
                handle_response,
            )

        except Exception:

            pass


# ============================================================
# SCRAPE ONE COURSE
# ============================================================

async def scrape_course_once(
    page,
    subject,
    catalog_num,
    term,
):

    navigation_url = (
        build_course_search_url(
            subject,
            catalog_num,
            term,
        )
    )

    subject_marker = (
        f"subject={subject}"
    )

    catalog_marker = (
        f"catalogNbr={catalog_num}"
    )

    term_marker = (
        f"term={term}"
    )

    def matcher(
        url,
    ):

        return (
            subject_marker in url
            and
            catalog_marker in url
            and
            term_marker in url
        )

    result = (
        await capture_search_response(
            page,
            navigation_url,
            matcher,
        )
    )

    if not result.get(
        "ok"
    ):

        return {
            "status":
                "ERROR",

            "subject":
                subject,

            "catalog_num":
                catalog_num,

            "error_type":
                (
                    "HTTP_ERROR"
                    if result.get(
                        "status"
                    )
                    else
                    "COURSE_TIMEOUT"
                ),

            "error":
                result.get(
                    "error",
                    "Unknown course error.",
                ),

            "classes":
                [],
        }

    data = (
        result.get(
            "data"
        )
        or {}
    )

    raw_classes = (
        data.get(
            "classes",
            []
        )
    )

    simplified = []

    for item in raw_classes:

        parsed = (
            simplify_asu_class(
                item
            )
        )

        if parsed:

            simplified.append(
                parsed
            )

    total_data = (
        data.get(
            "total"
        )
    )

    if isinstance(
        total_data,
        dict,
    ):

        reported_total = (
            total_data.get(
                "value"
            )
        )

    else:

        reported_total = (
            total_data
        )

    print(
        f"{subject} {catalog_num}: "
        f"ASU returned "
        f"{len(simplified)} "
        f"of "
        f"{reported_total} "
        "section(s)."
    )

    return {
        "status":
            "SUCCESS",

        "subject":
            subject,

        "catalog_num":
            catalog_num,

        "reported_total":
            reported_total,

        "returned":
            len(
                simplified
            ),

        "classes":
            simplified,

        "scroll_id":
            data.get(
                "scrollId"
            ),
    }


# ============================================================
# COURSE RETRIES
# ============================================================

async def scrape_course(
    page,
    subject,
    catalog_num,
    term,
):

    last_result = None

    for attempt in range(
        1,
        COURSE_MAX_ATTEMPTS + 1,
    ):

        print(
            f"Loading "
            f"{subject} {catalog_num} "
            f"(attempt "
            f"{attempt}/"
            f"{COURSE_MAX_ATTEMPTS})..."
        )

        result = (
            await scrape_course_once(
                page,
                subject,
                catalog_num,
                term,
            )
        )

        last_result = (
            result
        )

        if (
            result.get(
                "status"
            ) ==
            "SUCCESS"
        ):

            return result

        if (
            attempt >=
            COURSE_MAX_ATTEMPTS
        ):

            break

        delay = min(
            2 ** (
                attempt - 1
            ),
            6,
        )

        delay += random.uniform(
            0.25,
            1.0,
        )

        print(
            f"{subject} {catalog_num}: "
            f"{result.get('error_type')} - "
            f"retrying in "
            f"{delay:.1f}s..."
        )

        await asyncio.sleep(
            delay
        )

    return last_result


# ============================================================
# INDIVIDUAL CLASS FALLBACK
#
# This still uses ASU's structured API response.
# It does NOT parse rendered HTML.
# ============================================================

async def scrape_individual_class(
    page,
    class_num,
    term,
):

    navigation_url = (
        build_class_search_url(
            class_num,
            term,
        )
    )

    class_marker = (
        f"classNbr={class_num}"
    )

    term_marker = (
        f"term={term}"
    )

    def matcher(
        url,
    ):

        return (
            class_marker in url
            and
            term_marker in url
        )

    result = (
        await capture_search_response(
            page,
            navigation_url,
            matcher,
        )
    )

    if not result.get(
        "ok"
    ):

        return None

    data = (
        result.get(
            "data"
        )
        or {}
    )

    for item in data.get(
        "classes",
        []
    ):

        parsed = (
            simplify_asu_class(
                item
            )
        )

        if (
            parsed
            and
            parsed[
                "class_num"
            ] ==
            str(
                class_num
            )
        ):

            return parsed

    return None


# ============================================================
# PREPARE EA ROW UPDATE
# ============================================================

def prepare_row_update(
    class_num,
    scraped,
    daily_lookup,
    previous_lookup,
):

    daily = (
        daily_lookup.get(
            class_num
        )
    )

    if not daily:

        return {
            "status":
                "ERROR",

            "class_num":
                class_num,

            "error_type":
                "MISSING_FROM_DAILY_SHEET",

            "error":
                (
                    "Class was not found "
                    "in today's EA sheet."
                ),
        }

    row_number = (
        daily[
            "row_number"
        ]
    )

    row_data = (
        daily[
            "row"
        ]
    )

    previous = (
        previous_lookup.get(
            class_num,
            {},
        )
    )

    # --------------------------------------------------------
    # TODAY
    # --------------------------------------------------------

    today_cap = str(
        scraped.get(
            "cap",
            "",
        )
    ).strip()

    today_open = str(
        scraped.get(
            "open",
            "",
        )
    ).strip()

    today_current = str(
        scraped.get(
            "current",
            "",
        )
    ).strip()

    last_name = str(
        scraped.get(
            "instructor_last",
            "",
        )
    ).strip()

    first_name = str(
        scraped.get(
            "instructor_first",
            "",
        )
    ).strip()

    # --------------------------------------------------------
    # PREVIOUS
    # --------------------------------------------------------

    previous_amount = str(
        previous.get(
            "previous_amount",
            "",
        )
    ).strip()

    previous_capacity = str(
        previous.get(
            "previous_capacity",
            "",
        )
    ).strip()

    # --------------------------------------------------------
    # INSTRUCTOR FALLBACK
    # --------------------------------------------------------

    if not last_name:

        last_name = str(
            previous.get(
                "last_name",
                (
                    row_data[
                        COL_INSTRUCTOR_LAST
                    ]
                    if len(row_data) >
                    COL_INSTRUCTOR_LAST
                    else ""
                ),
            )
        ).strip()

    if not first_name:

        first_name = str(
            previous.get(
                "first_name",
                (
                    row_data[
                        COL_INSTRUCTOR_FIRST
                    ]
                    if len(row_data) >
                    COL_INSTRUCTOR_FIRST
                    else ""
                ),
            )
        ).strip()

    # --------------------------------------------------------
    # ENROLLMENT UPDATE
    # --------------------------------------------------------

    enrollment_update = (
        calculate_difference(
            today_current,
            previous_amount,
        )
    )

    enrollment_description = (
        build_enrollment_description(
            enrollment_update
        )
    )

    # --------------------------------------------------------
    # CHANGED
    # --------------------------------------------------------

    has_previous = bool(
        previous
    )

    changed = False

    if has_previous:

        changed = (
            today_cap !=
            str(
                previous.get(
                    "cap",
                    "",
                )
            ).strip()
            or
            today_open !=
            str(
                previous.get(
                    "open",
                    "",
                )
            ).strip()
            or
            today_current !=
            str(
                previous.get(
                    "current",
                    "",
                )
            ).strip()
            or
            last_name !=
            str(
                previous.get(
                    "last_name",
                    "",
                )
            ).strip()
            or
            first_name !=
            str(
                previous.get(
                    "first_name",
                    "",
                )
            ).strip()
        )

    # --------------------------------------------------------
    # E:M
    # --------------------------------------------------------

    values = [

        # E
        last_name,

        # F
        first_name,

        # G Previous Amount
        previous_amount,

        # H Enroll Cap
        today_cap,

        # I Open Seats
        today_open,

        # J Current
        today_current,

        # K Update
        enrollment_update,

        # L Previous enrollment capacity
        previous_capacity,

        # M Enroll Desc
        enrollment_description,
    ]

    return {
        "status":
            "SUCCESS",

        "class_num":
            class_num,

        "row_number":
            row_number,

        "range":
            (
                f"E{row_number}:"
                f"M{row_number}"
            ),

        "values":
            values,

        "changed":
            changed,

        "has_previous":
            has_previous,
    }


# ============================================================
# WRITE ONE COURSE'S RESULTS
# ============================================================

def write_course_results(
    sheet,
    class_results,
    daily_lookup,
    previous_lookup,
):

    updates = []

    results = []

    changed = 0
    unchanged = 0

    for (
        class_num,
        scraped,
    ) in class_results.items():

        prepared = (
            prepare_row_update(
                class_num,
                scraped,
                daily_lookup,
                previous_lookup,
            )
        )

        results.append(
            prepared
        )

        if (
            prepared[
                "status"
            ] !=
            "SUCCESS"
        ):

            continue

        updates.append({
            "range":
                prepared[
                    "range"
                ],

            "values": [
                prepared[
                    "values"
                ]
            ],
        })

        if (
            prepared[
                "changed"
            ]
        ):

            changed += 1

        else:

            unchanged += 1

    if updates:

        google_retry(
            lambda:
                sheet.batch_update(
                    updates
                ),
            "Write course enrollment data",
        )

    return {
        "results":
            results,

        "changed":
            changed,

        "unchanged":
            unchanged,
    }


# ============================================================
# MAIN
# ============================================================

async def main(
    sheet_url,
    sheet_name,
    term,
    concurrency=2,
    progress_callback=None,
):

    # ========================================================
    # GOOGLE SHEETS
    # ========================================================

    sheet = get_sheet(
        sheet_url,
        sheet_name,
    )

    spreadsheet = (
        sheet.spreadsheet
    )

    previous_sheet = (
        get_previous_run_sheet(
            spreadsheet,
            sheet_name,
        )
    )

    previous_lookup = (
        build_previous_lookup(
            previous_sheet
        )
    )

    all_values = google_retry(
        lambda:
            sheet.get_all_values(),
        "Read current enrollment sheet",
    )

    daily_lookup = (
        build_daily_lookup(
            all_values
        )
    )

    course_groups, invalid_classes = (
        build_course_groups(
            daily_lookup
        )
    )

    total_classes = (
        len(
            daily_lookup
        )
    )

    total_courses = (
        len(
            course_groups
        )
    )

    print(
        "========================================"
    )

    print(
        "COURSE-LEVEL ENROLLMENT SCRAPER"
    )

    print(
        f"Classes required: "
        f"{total_classes}"
    )

    print(
        f"Unique courses: "
        f"{total_courses}"
    )

    print(
        "Previous run:",
        (
            previous_sheet.title
            if previous_sheet
            else "None"
        ),
    )

    print(
        "========================================"
    )

    if invalid_classes:

        print(
            "Classes missing Subject/Catalog Num:",
            invalid_classes,
        )

    if progress_callback:

        progress_callback({
            "type":
                "start",

            "message":
                (
                    f"Found "
                    f"{total_classes} "
                    f"classes across "
                    f"{total_courses} "
                    "unique courses."
                ),

            "total":
                total_classes,

            "processed":
                0,

            "successful":
                0,

            "changed":
                0,

            "unchanged":
                0,

            "errors":
                0,

            "needs_review":
                0,
        })

    # ========================================================
    # COUNTERS
    # ========================================================

    processed = 0
    successful = 0

    total_changed = 0
    total_unchanged = 0

    total_errors = 0
    total_needs_review = 0

    error_classes = []
    no_data_classes = []

    # Classes we've successfully found.
    found_classes = set()

    # ========================================================
    # PLAYWRIGHT
    # ========================================================

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        context = (
            await browser.new_context()
        )

        page = (
            await context.new_page()
        )

        try:

            # =================================================
            # COURSE LOOP
            # =================================================

            for (
                course_index,
                (
                    course_key,
                    wanted_class_nums,
                ),
            ) in enumerate(
                course_groups.items(),
                start=1,
            ):

                (
                    subject,
                    catalog_num,
                ) = course_key

                # --------------------------------------------
                # CANCELLATION CHECK
                # --------------------------------------------

                if progress_callback:

                    progress_callback({
                        "type":
                            "checkpoint",

                        "message":
                            (
                                "Checking job status "
                                f"before "
                                f"{subject} "
                                f"{catalog_num}."
                            ),

                        "processed":
                            processed,

                        "total":
                            total_classes,
                    })

                # --------------------------------------------
                # COURSE
                # --------------------------------------------

                print(
                    "\n----------------------------------------"
                )

                print(
                    f"Course "
                    f"{course_index}/"
                    f"{total_courses}: "
                    f"{subject} "
                    f"{catalog_num}"
                )

                print(
                    "Required Class Nums:",
                    sorted(
                        wanted_class_nums
                    ),
                )

                if progress_callback:

                    progress_callback({
                        "type":
                            "batch_start",

                        "message":
                            (
                                f"Loading "
                                f"{subject} "
                                f"{catalog_num} "
                                f"({course_index}/"
                                f"{total_courses})."
                            ),

                        "processed":
                            processed,

                        "total":
                            total_classes,
                    })

                course_result = (
                    await scrape_course(
                        page,
                        subject,
                        catalog_num,
                        term,
                    )
                )

                # --------------------------------------------
                # COURSE FAILURE
                # --------------------------------------------

                if (
                    not course_result
                    or
                    course_result.get(
                        "status"
                    ) !=
                    "SUCCESS"
                ):

                    print(
                        f"{subject} "
                        f"{catalog_num}: "
                        "course search failed."
                    )

                    # We will individually retry all required
                    # classes from this course below.
                    returned_lookup = {}

                else:

                    returned_lookup = {
                        item[
                            "class_num"
                        ]:
                            item

                        for item
                        in course_result.get(
                            "classes",
                            []
                        )

                        if item.get(
                            "class_num"
                        )
                    }

                # --------------------------------------------
                # FILTER TO EA CLASS NUMS ONLY
                # --------------------------------------------

                matched = {}

                missing = []

                for class_num in (
                    wanted_class_nums
                ):

                    scraped = (
                        returned_lookup.get(
                            class_num
                        )
                    )

                    if scraped:

                        matched[
                            class_num
                        ] = scraped

                        found_classes.add(
                            class_num
                        )

                    else:

                        missing.append(
                            class_num
                        )

                print(
                    f"{subject} "
                    f"{catalog_num}: "
                    f"matched "
                    f"{len(matched)}/"
                    f"{len(wanted_class_nums)} "
                    "required classes."
                )

                # =================================================
                # INDIVIDUAL FALLBACK FOR MISSING CLASSES
                # =================================================

                if missing:

                    print(
                        "Missing from course response:",
                        missing,
                    )

                    for class_num in (
                        missing
                    ):

                        if progress_callback:

                            progress_callback({
                                "type":
                                    "checkpoint",

                                "message":
                                    (
                                        "Checking job status "
                                        "before fallback "
                                        f"{class_num}."
                                    ),

                                "processed":
                                    processed,

                                "total":
                                    total_classes,
                            })

                        print(
                            f"{class_num}: "
                            "running individual "
                            "API-response fallback."
                        )

                        fallback = (
                            await
                            scrape_individual_class(
                                page,
                                class_num,
                                term,
                            )
                        )

                        if fallback:

                            matched[
                                class_num
                            ] = fallback

                            found_classes.add(
                                class_num
                            )

                        else:

                            total_errors += 1

                            error_info = {
                                "class_num":
                                    class_num,

                                "error_type":
                                    "CLASS_NOT_RETURNED",

                                "error":
                                    (
                                        "Class was not returned "
                                        "by its course search or "
                                        "individual fallback."
                                    ),
                            }

                            error_classes.append(
                                error_info
                            )

                # =================================================
                # WRITE MATCHED COURSE RESULTS
                # =================================================

                write_summary = (
                    write_course_results(
                        sheet,
                        matched,
                        daily_lookup,
                        previous_lookup,
                    )
                )

                course_successful = (
                    len(
                        matched
                    )
                )

                successful += (
                    course_successful
                )

                processed += (
                    len(
                        wanted_class_nums
                    )
                )

                total_changed += (
                    write_summary[
                        "changed"
                    ]
                )

                total_unchanged += (
                    write_summary[
                        "unchanged"
                    ]
                )

                # --------------------------------------------
                # PROGRESS RESULT OBJECTS
                # --------------------------------------------

                callback_results = []

                for class_num in (
                    wanted_class_nums
                ):

                    if (
                        class_num in
                        matched
                    ):

                        callback_results.append({
                            "class_num":
                                class_num,

                            "status":
                                "SUCCESS",
                        })

                    else:

                        callback_results.append({
                            "class_num":
                                class_num,

                            "status":
                                "ERROR",

                            "error_type":
                                "CLASS_NOT_RETURNED",
                        })

                if progress_callback:

                    progress_callback({
                        "type":
                            "batch_complete",

                        "message":
                            (
                                f"Completed "
                                f"{subject} "
                                f"{catalog_num}. "
                                f"Processed "
                                f"{processed} "
                                f"of "
                                f"{total_classes} "
                                "classes."
                            ),

                        "batch":
                            sorted(
                                wanted_class_nums
                            ),

                        "processed":
                            processed,

                        "total":
                            total_classes,

                        "successful":
                            successful,

                        "changed":
                            total_changed,

                        "unchanged":
                            total_unchanged,

                        "errors":
                            total_errors,

                        "needs_review":
                            total_needs_review,

                        "results":
                            callback_results,
                    })

                # --------------------------------------------
                # SMALL DELAY
                # --------------------------------------------

                if (
                    course_index <
                    total_courses
                ):

                    delay = (
                        random.uniform(
                            COURSE_DELAY_MIN,
                            COURSE_DELAY_MAX,
                        )
                    )

                    await asyncio.sleep(
                        delay
                    )

        finally:

            await browser.close()

    # ========================================================
    # INVALID EA ROWS
    # ========================================================

    for class_num in (
        invalid_classes
    ):

        total_errors += 1

        error_classes.append({
            "class_num":
                class_num,

            "error_type":
                "MISSING_COURSE_INFORMATION",

            "error":
                (
                    "EA row is missing "
                    "Subject or Catalog Num."
                ),
        })

    # ========================================================
    # FINAL
    # ========================================================

    print(
        "\n========================================"
    )

    print(
        "ENROLLMENT RUN COMPLETE"
    )

    print(
        f"Classes processed: "
        f"{processed}"
    )

    print(
        f"Successful: "
        f"{successful}"
    )

    print(
        f"Changed: "
        f"{total_changed}"
    )

    print(
        f"Unchanged: "
        f"{total_unchanged}"
    )

    print(
        f"Errors: "
        f"{total_errors}"
    )

    print(
        f"Unique courses: "
        f"{total_courses}"
    )

    print(
        "========================================"
    )

    if progress_callback:

        progress_callback({
            "type":
                "complete",

            "message":
                "Enrollment run complete.",

            "processed":
                processed,

            "total":
                total_classes,

            "successful":
                successful,

            "changed":
                total_changed,

            "unchanged":
                total_unchanged,

            "errors":
                total_errors,

            "needs_review":
                total_needs_review,
        })

    return {
        "status":
            "success",

        "classes_processed":
            processed,

        "successful":
            successful,

        "changed":
            total_changed,

        "unchanged":
            total_unchanged,

        "errors":
            total_errors,

        "needs_review":
            total_needs_review,

        "previous_sheet":
            (
                previous_sheet.title
                if previous_sheet
                else None
            ),

        "unique_courses":
            total_courses,

        "error_classes":
            error_classes,

        "no_data_classes":
            no_data_classes,
    }


# ============================================================
# PUBLIC ENTRY POINT
# ============================================================

def run_enrollment(
    sheet_url,
    sheet_name,
    term,
    concurrency=2,
    progress_callback=None,
):

    return asyncio.run(
        main(
            sheet_url,
            sheet_name,
            term,
            concurrency,
            progress_callback,
        )
    )


# ============================================================
# OPTIONAL DIAGNOSTIC
#
# Keeps /diagnose-asu working while we test the new scraper.
# ============================================================

async def diagnose_asu_api(
    subject="PHY",
    catalog_num="111",
    term="2267",
):

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        context = (
            await browser.new_context()
        )

        page = (
            await context.new_page()
        )

        try:

            result = (
                await scrape_course(
                    page,
                    subject,
                    catalog_num,
                    term,
                )
            )

        finally:

            await browser.close()

        return {
            "ok":
                (
                    result
                    and
                    result.get(
                        "status"
                    ) ==
                    "SUCCESS"
                ),

            "subject":
                subject,

            "catalog_num":
                catalog_num,

            "course":
                (
                    f"{subject} "
                    f"{catalog_num}"
                ),

            "term":
                term,

            "course_api":
                result,
        }