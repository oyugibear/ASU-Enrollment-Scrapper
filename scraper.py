import asyncio
import json
import os
import random
import re
import time

from datetime import datetime
from itertools import islice

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

CONCURRENCY = 2

ASU_MAX_ATTEMPTS = 3

GOOGLE_MAX_ATTEMPTS = 6

BATCH_DELAY_MIN = 1.5
BATCH_DELAY_MAX = 3.0


TRANSIENT_ERROR_TYPES = {
    "PAGE_TIMEOUT",
    "ENROLLMENT_DATA_TIMEOUT",
    "NO_RESPONSE",
    "TIMEOUT",
    "HTTP_429",
    "HTTP_500",
    "HTTP_502",
    "HTTP_503",
    "HTTP_504",
}


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

COL_CLASS_NUM = 2

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

        except json.JSONDecodeError as e:

            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON "
                "contains invalid JSON."
            ) from e

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
    # GOOGLE CLOUD / CLOUD SHELL FALLBACK
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

        except Exception as e:

            last_error = e

            if not is_retryable_google_error(
                e
            ):
                raise

            if attempt >= attempts:
                break

            delay = min(
                2 ** (attempt - 1),
                30,
            )

            delay += random.uniform(
                0.25,
                1.25,
            )

            print(
                f"{description} failed "
                f"with temporary Google error. "
                f"Attempt {attempt}/{attempts}. "
                f"Retrying in {delay:.1f}s..."
            )

            time.sleep(
                delay
            )

    raise last_error


# ============================================================
# GOOGLE SHEET
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
# DATE PARSING
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
# FIND PREVIOUS DATED ENROLLMENT RUN
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
            f"sheet name: {current_sheet_name}"
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

        # Ignore system sheets.
        if name in [
            "Enrollment Home",
            "Enrollment Template",
            "Enrollment Jobs",
        ]:
            continue

        # Ignore today's sheet.
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
            sheet_date and
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
# IMPORTANT:
#
# Today's G Previous Amount
#     = previous day's J Current
#
# Today's L Previous enrollment capacity
#     = previous day's H Enroll Cap
#
# We intentionally DO NOT carry forward yesterday's
# G or L historical fields.
# ============================================================

def build_previous_lookup(
    previous_sheet,
):

    if previous_sheet is None:

        print(
            "No previous enrollment run. "
            "Previous values will remain blank."
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

        # ----------------------------------------------------
        # PREVIOUS DAY ACTUAL VALUES
        # ----------------------------------------------------

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

        previous_last_name = (
            row[
                COL_INSTRUCTOR_LAST
            ]
            if len(row) >
            COL_INSTRUCTOR_LAST
            else ""
        )

        previous_first_name = (
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
                previous_last_name,

            "first_name":
                previous_first_name,

            # Previous day's actual enrollment.
            "previous_amount":
                previous_current,

            # Previous day's actual capacity.
            "previous_capacity":
                previous_cap,

            # Retain actual previous values for
            # changed/unchanged comparisons.
            "cap":
                previous_cap,

            "open":
                previous_open,

            "current":
                previous_current,
        }

    print(
        f"Loaded {len(lookup)} classes "
        f"from previous run "
        f"{previous_sheet.title}."
    )

    return lookup


# ============================================================
# CURRENT DAILY ROW LOOKUP
# ============================================================

def build_daily_row_lookup(
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

        class_num = str(
            row[
                COL_CLASS_NUM
            ]
        ).strip()

        if not class_num:
            continue

        # Keep first occurrence.
        if (
            class_num not in
            lookup
        ):

            lookup[
                class_num
            ] = {
                "row_number":
                    row_number,

                "row":
                    row,
            }

    return lookup


# ============================================================
# RESULT HELPERS
# ============================================================

def scraper_error(
    class_num,
    error_type,
    error,
):

    return {
        "class_num":
            str(
                class_num
            ),

        "status":
            "ERROR",

        "error_type":
            error_type,

        "error":
            str(
                error
            ),
    }


def scraper_no_data(
    class_num,
    error_type,
    error,
):

    return {
        "class_num":
            str(
                class_num
            ),

        "status":
            "NO_DATA",

        "error_type":
            error_type,

        "error":
            str(
                error
            ),
    }


def is_transient_result(
    result,
):

    if (
        result.get(
            "status"
        ) != "ERROR"
    ):
        return False

    error_type = str(
        result.get(
            "error_type",
            "",
        )
    ).upper()

    return (
        error_type
        in TRANSIENT_ERROR_TYPES
    )


# ============================================================
# SCRAPE ONE CLASS
# ============================================================

async def scrape_class_once(
    context,
    class_num,
    term,
):

    page = (
        await context.new_page()
    )

    class_num = str(
        class_num
    ).strip()

    url = (
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

    print(
        f"Visiting: {url}"
    )

    try:

        # ----------------------------------------------------
        # LOAD ASU PAGE
        # ----------------------------------------------------

        try:

            response = (
                await page.goto(
                    url,
                    timeout=30000,
                    wait_until="load",
                )
            )

        except PlaywrightTimeoutError:

            return scraper_error(
                class_num,
                "PAGE_TIMEOUT",
                (
                    "ASU page did not "
                    "finish loading within "
                    "30 seconds."
                ),
            )

        # ----------------------------------------------------
        # HTTP RESPONSE
        # ----------------------------------------------------

        if response:

            status_code = (
                response.status
            )

            if (
                status_code ==
                429
            ):

                return scraper_error(
                    class_num,
                    "HTTP_429",
                    (
                        "ASU returned HTTP 429 "
                        "Too Many Requests."
                    ),
                )

            if status_code in [
                500,
                502,
                503,
                504,
            ]:

                return scraper_error(
                    class_num,
                    f"HTTP_{status_code}",
                    (
                        "ASU returned temporary "
                        f"HTTP {status_code}."
                    ),
                )

        # ----------------------------------------------------
        # WAIT FOR ENROLLMENT DATA
        # ----------------------------------------------------

        try:

            await page.wait_for_selector(
                "text=Open Seats",
                timeout=35000,
            )

        except PlaywrightTimeoutError:

            try:

                body_text = (
                    await page
                    .locator(
                        "body"
                    )
                    .inner_text()
                )

                body_text = re.sub(
                    r"\s+",
                    " ",
                    body_text,
                ).strip()

            except Exception:

                body_text = ""

            if not body_text:

                return scraper_error(
                    class_num,
                    "NO_RESPONSE",
                    (
                        "ASU returned no "
                        "usable page content."
                    ),
                )

            not_found_patterns = [
                "No classes found",
                "No results found",
                "No classes match",
                "There are no classes",
            ]

            not_found = any(
                pattern.lower()
                in body_text.lower()
                for pattern
                in not_found_patterns
            )

            if not_found:

                return scraper_no_data(
                    class_num,
                    "CLASS_NOT_FOUND",
                    (
                        "ASU did not return "
                        "a matching class."
                    ),
                )

            return scraper_error(
                class_num,
                "ENROLLMENT_DATA_TIMEOUT",
                (
                    "ASU page loaded, but "
                    "enrollment data was not "
                    "available within 35 seconds."
                ),
            )

        # ----------------------------------------------------
        # PAGE TEXT
        # ----------------------------------------------------

        text_content = (
            await page
            .locator(
                "body"
            )
            .inner_text()
        )

        text_content = re.sub(
            r"\s+",
            " ",
            text_content,
        ).strip()

        # ----------------------------------------------------
        # INSTRUCTOR
        # ----------------------------------------------------

        instructor_match = re.search(
            rf"{re.escape(class_num)}\s+"
            r"([A-Z][a-z]+"
            r"(?:\s[A-Z][a-z]+)*)",
            text_content,
        )

        instructor = (
            instructor_match
            .group(1)
            .strip()
            if instructor_match
            else "TBA"
        )

        # ----------------------------------------------------
        # ENROLLMENT
        # ----------------------------------------------------

        seats_matches = re.findall(
            r"(\d+)\s+of\s+(\d+)",
            text_content,
        )

        if not seats_matches:

            return scraper_no_data(
                class_num,
                "NO_ENROLLMENT_DATA",
                (
                    "Class page was found, "
                    "but enrollment values "
                    "were not found."
                ),
            )

        open_seats, enroll_cap = (
            seats_matches[-1]
        )

        current = (
            int(
                enroll_cap
            )
            -
            int(
                open_seats
            )
        )

        print(
            f"SUCCESS {class_num}: "
            f"{instructor}, "
            f"Cap={enroll_cap}, "
            f"Open={open_seats}, "
            f"Current={current}"
        )

        return {
            "class_num":
                class_num,

            "status":
                "SUCCESS",

            "instructor":
                instructor,

            "cap":
                str(
                    enroll_cap
                ),

            "open":
                str(
                    open_seats
                ),

            "current":
                str(
                    current
                ),

            "error_type":
                None,

            "error":
                None,
        }

    except PlaywrightTimeoutError as e:

        print(
            f"TIMEOUT {class_num}: "
            f"{e}"
        )

        return scraper_error(
            class_num,
            "TIMEOUT",
            str(
                e
            ),
        )

    except Exception as e:

        print(
            f"ERROR {class_num}: "
            f"{type(e).__name__}: "
            f"{e}"
        )

        return scraper_error(
            class_num,
            type(e).__name__,
            str(
                e
            ),
        )

    finally:

        try:

            await page.close()

        except Exception:

            pass


# ============================================================
# SCRAPE WITH RETRIES
# ============================================================

async def scrape_class(
    context,
    class_num,
    term,
    max_attempts=ASU_MAX_ATTEMPTS,
):

    last_result = None

    for attempt in range(
        1,
        max_attempts + 1,
    ):

        result = (
            await scrape_class_once(
                context,
                class_num,
                term,
            )
        )

        last_result = (
            result
        )

        # Success and confirmed no-data do not retry.
        if (
            result.get(
                "status"
            )
            in [
                "SUCCESS",
                "NO_DATA",
            ]
        ):

            return result

        if not is_transient_result(
            result
        ):

            return result

        if (
            attempt >=
            max_attempts
        ):

            break

        delay = min(
            2 ** (
                attempt - 1
            ),
            8,
        )

        delay += random.uniform(
            0.5,
            1.5,
        )

        print(
            f"{class_num}: temporary "
            f"{result.get('error_type')} "
            f"on attempt "
            f"{attempt}/{max_attempts}. "
            f"Retrying in "
            f"{delay:.1f}s..."
        )

        await asyncio.sleep(
            delay
        )

    return last_result


# ============================================================
# INSTRUCTOR
# ============================================================

def split_instructor(
    instructor,
):

    if not instructor:

        return "", ""

    if instructor.lower() in [
        "tba",
        "staff",
    ]:

        return "", ""

    parts = (
        instructor.split()
    )

    if len(parts) >= 2:

        first_name = (
            parts[0]
        )

        last_name = " ".join(
            parts[1:]
        )

        return (
            last_name,
            first_name,
        )

    return (
        "",
        instructor,
    )


# ============================================================
# DIFFERENCE
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

    # No historical observation.
    if (
        current_text == "" or
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
# PROCESS + WRITE ONE BATCH
# ============================================================

def update_batch(
    sheet,
    batch_results,
    previous_lookup,
    daily_lookup,
):

    updates = []

    changed_count = 0
    unchanged_count = 0

    error_count = 0
    no_data_count = 0

    error_classes = []
    no_data_classes = []

    for result in (
        batch_results
    ):

        class_num = str(
            result[
                "class_num"
            ]
        ).strip()

        daily = (
            daily_lookup.get(
                class_num
            )
        )

        # ----------------------------------------------------
        # CLASS MISSING FROM DAILY SHEET
        # ----------------------------------------------------

        if not daily:

            error_count += 1

            error_classes.append({
                "class_num":
                    class_num,

                "error_type":
                    "MISSING_FROM_DAILY_SHEET",

                "error":
                    (
                        "Class was not found "
                        "in today's sheet."
                    ),
            })

            print(
                f"{class_num}: "
                "NOT FOUND IN DAILY SHEET"
            )

            continue

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

        # ----------------------------------------------------
        # SCRAPER ERROR
        # ----------------------------------------------------

        if (
            result[
                "status"
            ] ==
            "ERROR"
        ):

            error_count += 1

            info = {
                "class_num":
                    class_num,

                "error_type":
                    result.get(
                        "error_type",
                        "UNKNOWN_ERROR",
                    ),

                "error":
                    result.get(
                        "error",
                        "Unknown scraper error.",
                    ),
            }

            error_classes.append(
                info
            )

            print(
                f"{class_num}: ERROR - "
                f"{info['error_type']} - "
                "keeping existing values."
            )

            continue

        # ----------------------------------------------------
        # NO DATA
        # ----------------------------------------------------

        if (
            result[
                "status"
            ] ==
            "NO_DATA"
        ):

            no_data_count += 1

            info = {
                "class_num":
                    class_num,

                "error_type":
                    result.get(
                        "error_type",
                        "NO_DATA",
                    ),

                "error":
                    result.get(
                        "error",
                        "No data returned.",
                    ),
            }

            no_data_classes.append(
                info
            )

            print(
                f"{class_num}: NO DATA - "
                f"{info['error_type']} - "
                "keeping existing values."
            )

            continue

        # ====================================================
        # SUCCESS
        # ====================================================

        last_name, first_name = (
            split_instructor(
                result.get(
                    "instructor",
                    "",
                )
            )
        )

        # ----------------------------------------------------
        # TODAY'S ASU VALUES
        # ----------------------------------------------------

        today_cap = str(
            result.get(
                "cap",
                "",
            )
        ).strip()

        today_open = str(
            result.get(
                "open",
                "",
            )
        ).strip()

        today_current = str(
            result.get(
                "current",
                "",
            )
        ).strip()

        # ----------------------------------------------------
        # HISTORICAL VALUES
        #
        # TODAY G
        # Previous Amount
        # = PREVIOUS DAY J Current
        #
        # TODAY L
        # Previous enrollment capacity
        # = PREVIOUS DAY H Enroll Cap
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # INSTRUCTOR FALLBACK
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # UPDATE
        #
        # K =
        # TODAY Current
        # -
        # PREVIOUS DAY Current
        # ----------------------------------------------------

        enrollment_update = (
            calculate_difference(
                today_current,
                previous_amount,
            )
        )

        # ----------------------------------------------------
        # ENROLL DESC
        # ----------------------------------------------------

        enrollment_description = (
            build_enrollment_description(
                enrollment_update
            )
        )

        # ----------------------------------------------------
        # CHANGED / UNCHANGED
        # ----------------------------------------------------

        has_previous = bool(
            previous
        )

        previous_cap = str(
            previous.get(
                "cap",
                "",
            )
        ).strip()

        previous_open = str(
            previous.get(
                "open",
                "",
            )
        ).strip()

        previous_current = str(
            previous.get(
                "current",
                "",
            )
        ).strip()

        previous_last = str(
            previous.get(
                "last_name",
                "",
            )
        ).strip()

        previous_first = str(
            previous.get(
                "first_name",
                "",
            )
        ).strip()

        changed = False

        if has_previous:

            changed = (
                today_cap !=
                previous_cap
                or
                today_open !=
                previous_open
                or
                today_current !=
                previous_current
                or
                last_name !=
                previous_last
                or
                first_name !=
                previous_first
            )

        if changed:

            changed_count += 1

            print(
                f"{class_num}: CHANGED "
                f"| Enrollment "
                f"{previous_amount} -> "
                f"{today_current} "
                f"| Capacity "
                f"{previous_capacity} -> "
                f"{today_cap}"
            )

        else:

            unchanged_count += 1

            if has_previous:

                print(
                    f"{class_num}: "
                    "UNCHANGED"
                )

            else:

                print(
                    f"{class_num}: "
                    "FIRST OBSERVATION"
                )

        # ====================================================
        # WRITE E:M
        #
        # E Instructor Last Name
        # F Instructor First Name
        # G Previous Amount
        # H Enroll Cap
        # I Open Seats
        # J Current
        # K Update
        # L Previous enrollment capacity
        # M Enroll Desc
        # ====================================================

        new_row_values = [

            # E
            last_name,

            # F
            first_name,

            # G
            previous_amount,

            # H
            today_cap,

            # I
            today_open,

            # J
            today_current,

            # K
            enrollment_update,

            # L
            previous_capacity,

            # M
            enrollment_description,
        ]

        updates.append({
            "range":
                f"E{row_number}:"
                f"M{row_number}",

            "values": [
                new_row_values
            ],
        })

        # Keep local copy synchronized.
        while (
            len(row_data) <=
            COL_ENROLL_DESC
        ):

            row_data.append(
                ""
            )

        row_data[
            COL_INSTRUCTOR_LAST:
            COL_ENROLL_DESC + 1
        ] = (
            new_row_values
        )

    # ========================================================
    # BATCH WRITE
    # ========================================================

    if updates:

        print(
            f"Writing "
            f"{len(updates)} "
            "successful rows..."
        )

        google_retry(
            lambda:
                sheet.batch_update(
                    updates
                ),
            "Batch enrollment update",
        )

        print(
            "Batch sheet update complete."
        )

    return {
        "changed":
            changed_count,

        "unchanged":
            unchanged_count,

        "errors":
            error_count,

        "needs_review":
            no_data_count,

        "error_classes":
            error_classes,

        "no_data_classes":
            no_data_classes,
    }


# ============================================================
# PROCESS SCRAPER BATCH
# ============================================================

async def process_scrape_batch(
    context,
    batch,
    term,
):

    return await asyncio.gather(
        *[
            scrape_class(
                context,
                class_num,
                term,
            )
            for class_num
            in batch
        ]
    )


# ============================================================
# MAIN
# ============================================================

async def main(
    sheet_url,
    sheet_name,
    term,
    concurrency=CONCURRENCY,
    progress_callback=None,
):

    # --------------------------------------------------------
    # CONCURRENCY
    # --------------------------------------------------------

    try:

        concurrency = int(
            concurrency
        )

    except (
        TypeError,
        ValueError,
    ):

        concurrency = (
            CONCURRENCY
        )

    concurrency = max(
        1,
        min(
            concurrency,
            3,
        ),
    )

    print(
        "Enrollment concurrency:",
        concurrency,
    )

    # --------------------------------------------------------
    # CURRENT SHEET
    # --------------------------------------------------------

    sheet = get_sheet(
        sheet_url,
        sheet_name,
    )

    spreadsheet = (
        sheet.spreadsheet
    )

    # --------------------------------------------------------
    # PREVIOUS RUN
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # CURRENT DAILY DATA
    # --------------------------------------------------------

    all_values = google_retry(
        lambda:
            sheet.get_all_values(),
        "Read current enrollment sheet",
    )

    daily_lookup = (
        build_daily_row_lookup(
            all_values
        )
    )

    raw_class_nums = []

    for row in (
        all_values[1:]
    ):

        if (
            len(row) >
            COL_CLASS_NUM
            and
            row[
                COL_CLASS_NUM
            ]
        ):

            raw_class_nums.append(
                str(
                    row[
                        COL_CLASS_NUM
                    ]
                ).strip()
            )

    # --------------------------------------------------------
    # DEDUPLICATE CLASS NUMBERS
    # --------------------------------------------------------

    seen = set()

    class_nums = []

    for class_num in (
        raw_class_nums
    ):

        if class_num in seen:

            print(
                f"{class_num}: "
                "DUPLICATE - skipping."
            )

            continue

        seen.add(
            class_num
        )

        class_nums.append(
            class_num
        )

    duplicate_count = (
        len(
            raw_class_nums
        )
        -
        len(
            class_nums
        )
    )

    print(
        f"Found "
        f"{len(class_nums)} "
        "unique classes."
    )

    # --------------------------------------------------------
    # START CALLBACK
    # --------------------------------------------------------

    if progress_callback:

        progress_callback({
            "type":
                "start",

            "message":
                (
                    f"Found "
                    f"{len(class_nums)} "
                    "unique classes."
                ),

            "total":
                len(
                    class_nums
                ),

            "processed":
                0,

            "successful":
                0,

            "errors":
                0,

            "needs_review":
                0,
        })

    # --------------------------------------------------------
    # CUMULATIVE STATE
    # --------------------------------------------------------

    all_results = []

    total_changed = 0

    total_unchanged = 0

    total_errors = 0

    total_no_data = 0

    all_error_classes = []

    all_no_data_classes = []

    # ========================================================
    # PLAYWRIGHT
    # ========================================================

    async with (
        async_playwright()
        as p
    ):

        browser = (
            await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
        )

        context = (
            await browser
            .new_context()
        )

        iterator = iter(
            class_nums
        )

        try:

            while True:

                # --------------------------------------------
                # CANCELLATION CHECKPOINT
                # --------------------------------------------

                if progress_callback:

                    progress_callback({
                        "type":
                            "checkpoint",

                        "message":
                            (
                                "Checking job status "
                                "before next batch."
                            ),

                        "processed":
                            len(
                                all_results
                            ),

                        "total":
                            len(
                                class_nums
                            ),
                    })

                # --------------------------------------------
                # NEXT BATCH
                # --------------------------------------------

                batch = list(
                    islice(
                        iterator,
                        concurrency,
                    )
                )

                if not batch:

                    break

                print(
                    f"Processing batch: "
                    f"{batch}"
                )

                if progress_callback:

                    progress_callback({
                        "type":
                            "batch_start",

                        "message":
                            (
                                f"Processing batch: "
                                f"{batch}"
                            ),

                        "processed":
                            len(
                                all_results
                            ),

                        "total":
                            len(
                                class_nums
                            ),
                    })

                # --------------------------------------------
                # SCRAPE
                # --------------------------------------------

                batch_results = (
                    await
                    process_scrape_batch(
                        context,
                        batch,
                        term,
                    )
                )

                all_results.extend(
                    batch_results
                )

                # --------------------------------------------
                # WRITE BATCH
                # --------------------------------------------

                batch_summary = (
                    update_batch(
                        sheet,
                        batch_results,
                        previous_lookup,
                        daily_lookup,
                    )
                )

                total_changed += (
                    batch_summary[
                        "changed"
                    ]
                )

                total_unchanged += (
                    batch_summary[
                        "unchanged"
                    ]
                )

                total_errors += (
                    batch_summary[
                        "errors"
                    ]
                )

                total_no_data += (
                    batch_summary[
                        "needs_review"
                    ]
                )

                all_error_classes.extend(
                    batch_summary[
                        "error_classes"
                    ]
                )

                all_no_data_classes.extend(
                    batch_summary[
                        "no_data_classes"
                    ]
                )

                successful_so_far = sum(
                    1
                    for item
                    in all_results
                    if (
                        item[
                            "status"
                        ] ==
                        "SUCCESS"
                    )
                )

                # --------------------------------------------
                # PROGRESS CALLBACK
                # --------------------------------------------

                if progress_callback:

                    progress_callback({
                        "type":
                            "batch_complete",

                        "message":
                            (
                                f"Processed "
                                f"{len(all_results)} "
                                f"of "
                                f"{len(class_nums)} "
                                "classes."
                            ),

                        "batch":
                            batch,

                        "processed":
                            len(
                                all_results
                            ),

                        "total":
                            len(
                                class_nums
                            ),

                        "successful":
                            successful_so_far,

                        "changed":
                            total_changed,

                        "unchanged":
                            total_unchanged,

                        "errors":
                            total_errors,

                        "needs_review":
                            total_no_data,

                        "results":
                            batch_results,
                    })

                # --------------------------------------------
                # DELAY BETWEEN BATCHES
                # --------------------------------------------

                if (
                    len(
                        all_results
                    )
                    <
                    len(
                        class_nums
                    )
                ):

                    delay = (
                        random.uniform(
                            BATCH_DELAY_MIN,
                            BATCH_DELAY_MAX,
                        )
                    )

                    print(
                        f"Waiting "
                        f"{delay:.1f}s "
                        "before next batch."
                    )

                    await asyncio.sleep(
                        delay
                    )

        finally:

            await browser.close()

    # ========================================================
    # FINAL COUNTS
    # ========================================================

    successful = sum(
        1
        for item
        in all_results
        if (
            item[
                "status"
            ] ==
            "SUCCESS"
        )
    )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    print(
        "========================================"
    )

    print(
        "ENROLLMENT RUN COMPLETE"
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
        f"Classes processed: "
        f"{len(all_results)}"
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
        f"Needs Review: "
        f"{total_no_data}"
    )

    print(
        f"Duplicates skipped: "
        f"{duplicate_count}"
    )

    print(
        "========================================"
    )

    # --------------------------------------------------------
    # FINAL CALLBACK
    # --------------------------------------------------------

    if progress_callback:

        progress_callback({
            "type":
                "complete",

            "message":
                "Enrollment run complete.",

            "processed":
                len(
                    all_results
                ),

            "total":
                len(
                    class_nums
                ),

            "successful":
                successful,

            "changed":
                total_changed,

            "unchanged":
                total_unchanged,

            "errors":
                total_errors,

            "needs_review":
                total_no_data,
        })

    # --------------------------------------------------------
    # RETURN
    # --------------------------------------------------------

    return {
        "status":
            "success",

        "classes_processed":
            len(
                all_results
            ),

        "successful":
            successful,

        "changed":
            total_changed,

        "unchanged":
            total_unchanged,

        "errors":
            total_errors,

        "needs_review":
            total_no_data,

        "previous_sheet":
            (
                previous_sheet.title
                if previous_sheet
                else None
            ),

        "duplicates_skipped":
            duplicate_count,

        "error_classes":
            all_error_classes,

        "no_data_classes":
            all_no_data_classes,

        "results":
            all_results,
    }


# ============================================================
# PUBLIC ENTRY POINT
# ============================================================

def run_enrollment(
    sheet_url,
    sheet_name,
    term,
    concurrency=CONCURRENCY,
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


async def diagnose_asu_api(
    subject="PHY",
    catalog_num="111",
    term="2267",
):

    async with async_playwright() as p:

        # ====================================================
        # START BROWSER
        # ====================================================

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        context = await browser.new_context()

        page = await context.new_page()

        captured_requests = []

        # ====================================================
        # CAPTURE ASU API REQUESTS
        # ====================================================

        async def handle_request(
            request,
        ):

            url = request.url

            if (
                "catalog-microservices"
                not in url
            ):
                return

            captured_requests.append({
                "method":
                    request.method,

                "url":
                    url,
            })

            print(
                "ASU API REQUEST:",
                request.method,
                url,
            )

        page.on(
            "request",
            handle_request,
        )

        # ====================================================
        # STEP 1
        # BOOTSTRAP ASU CLASS SEARCH
        #
        # Use the exact course we want to test.
        # ====================================================

        bootstrap_url = (
            "https://catalog.apps.asu.edu/"
            "catalog/classes/classlist?"
            "advanced=true"
            "&campusOrOnlineSelection=A"
            f"&subject={subject}"
            f"&catalogNbr={catalog_num}"
            "&honors=F"
            "&promod=F"
            "&searchType=all"
            f"&term={term}"
        )

        print(
            "\n========================================"
        )

        print(
            "ASU COURSE API DIAGNOSTIC"
        )

        print(
            f"Course: "
            f"{subject} {catalog_num}"
        )

        print(
            f"Term: {term}"
        )

        print(
            "========================================"
        )

        print(
            "\nBootstrap URL:"
        )

        print(
            bootstrap_url
        )

        try:

            await page.goto(
                bootstrap_url,
                timeout=60000,
                wait_until="domcontentloaded",
            )

        except PlaywrightTimeoutError:

            print(
                "Bootstrap navigation timed out. "
                "Continuing because ASU session "
                "state may still be available."
            )

        # Give ASU SPA time to initialize.
        await page.wait_for_timeout(
            5000
        )

        # ====================================================
        # STEP 2
        # COURSE-LEVEL API REQUEST
        # ====================================================

        api_url = (
            "https://eadvs-cscc-catalog-api.apps.asu.edu/"
            "catalog-microservices/api/v1/"
            "search/classes"
            "?refine=Y"
            "&advanced=true"
            "&campusOrOnlineSelection=A"
            f"&subject={subject}"
            f"&catalogNbr={catalog_num}"
            "&honors=F"
            "&promod=F"
            "&searchType=all"
            f"&term={term}"
        )

        print(
            "\nCourse API URL:"
        )

        print(
            api_url
        )

        # ====================================================
        # STEP 3
        # CALL API INSIDE ASU BROWSER SESSION
        # ====================================================

        result = await page.evaluate(
            """
            async (url) => {

                try {

                    const response =
                        await fetch(
                            url,
                            {
                                method:
                                    'GET',

                                credentials:
                                    'include',

                                headers: {
                                    'Accept':
                                        'application/json'
                                }
                            }
                        );

                    const text =
                        await response.text();

                    let data = null;

                    try {

                        data =
                            JSON.parse(
                                text
                            );

                    } catch (error) {

                        data = {
                            raw:
                                text.substring(
                                    0,
                                    2000
                                )
                        };

                    }

                    return {
                        status:
                            response.status,

                        ok:
                            response.ok,

                        data:
                            data
                    };

                } catch (error) {

                    return {
                        status:
                            0,

                        ok:
                            false,

                        error:
                            String(
                                error
                            )
                    };

                }

            }
            """,
            api_url,
        )

        # ====================================================
        # STEP 4
        # HTTP RESULT
        # ====================================================

        status = (
            result.get(
                "status"
            )
        )

        print(
            "\nAPI status:",
            status,
        )

        if not result.get(
            "ok"
        ):

            print(
                "Course API request failed."
            )

            await browser.close()

            return {
                "ok":
                    False,

                "subject":
                    subject,

                "catalog_num":
                    catalog_num,

                "term":
                    term,

                "status":
                    status,

                "error":
                    result.get(
                        "error"
                    ),

                "response":
                    result.get(
                        "data"
                    ),

                "requests":
                    captured_requests,
            }

        # ====================================================
        # STEP 5
        # RESPONSE
        # ====================================================

        data = (
            result.get(
                "data"
            )
            or {}
        )

        classes = (
            data.get(
                "classes",
                []
            )
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

            total = (
                total_data.get(
                    "value"
                )
            )

        else:

            total = (
                total_data
            )

        scroll_id = (
            data.get(
                "scrollId"
            )
        )

        print(
            "Reported total:",
            total,
        )

        print(
            "Returned:",
            len(
                classes
            ),
        )

        print(
            "Scroll ID present:",
            bool(
                scroll_id
            ),
        )

        # ====================================================
        # STEP 6
        # SIMPLIFY EACH SECTION
        # ====================================================

        simplified = []

        for item in classes:

            if not isinstance(
                item,
                dict,
            ):

                continue

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

                continue

            # ------------------------------------------------
            # SEAT INFORMATION
            # ------------------------------------------------

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

            enroll_cap = (
                seat_info.get(
                    "ENRL_CAP"
                )
            )

            enroll_total = (
                seat_info.get(
                    "ENRL_TOT"
                )
            )

            # Fall back to CLAS fields.
            if enroll_cap is None:

                enroll_cap = (
                    clas.get(
                        "ENRLCAP"
                    )
                )

            if enroll_total is None:

                enroll_total = (
                    clas.get(
                        "ENRLTOT"
                    )
                )

            # ------------------------------------------------
            # OPEN SEATS
            # ------------------------------------------------

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

            # ------------------------------------------------
            # INSTRUCTOR
            # ------------------------------------------------

            instructor_first = ""

            instructor_last = ""

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

            if first_names:

                instructor_first = str(
                    first_names[0]
                ).split(
                    "~"
                )[0]

            if last_names:

                instructor_last = str(
                    last_names[0]
                ).split(
                    "~"
                )[0]

            # ------------------------------------------------
            # CLASS
            # ------------------------------------------------

            class_num = str(
                clas.get(
                    "CLASSNBR",
                    ""
                )
            ).strip()

            simplified.append({

                "class_num":
                    class_num,

                "subject":
                    clas.get(
                        "SUBJECT"
                    ),

                "catalog_num":
                    clas.get(
                        "CATALOGNBR"
                    ),

                "section":
                    clas.get(
                        "CLASSSECTION"
                    ),

                "session":
                    clas.get(
                        "SESSIONCODE"
                    ),

                "component":
                    clas.get(
                        "SSRCOMPONENT"
                    ),

                "title":
                    clas.get(
                        "TITLE"
                    ),

                "instructor_last":
                    instructor_last,

                "instructor_first":
                    instructor_first,

                "enroll_cap":
                    enroll_cap,

                "enroll_total":
                    enroll_total,

                "open_seats":
                    open_seats,
            })

        # ====================================================
        # STEP 7
        # SUMMARY
        # ====================================================

        print(
            "\n========================================"
        )

        print(
            f"{subject} {catalog_num} RESULTS"
        )

        print(
            "========================================"
        )

        print(
            "Sections returned:",
            len(
                simplified
            ),
        )

        for section in (
            simplified
        ):

            print(
                (
                    f"{section['class_num']} | "
                    f"Section "
                    f"{section['section']} | "
                    f"{section['component']} | "
                    f"Cap "
                    f"{section['enroll_cap']} | "
                    f"Enrolled "
                    f"{section['enroll_total']} | "
                    f"Open "
                    f"{section['open_seats']}"
                )
            )

        # ====================================================
        # CLOSE
        # ====================================================

        await browser.close()

        # ====================================================
        # RETURN
        # ====================================================

        return {
            "ok":
                True,

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

            "status":
                status,

            "reported_total":
                total,

            "returned":
                len(
                    simplified
                ),

            "scroll_required":
                (
                    total is not None
                    and
                    len(
                        simplified
                    ) <
                    int(
                        total
                    )
                ),

            "scroll_id_present":
                bool(
                    scroll_id
                ),

            "classes":
                simplified,

            "requests":
                captured_requests,
        }