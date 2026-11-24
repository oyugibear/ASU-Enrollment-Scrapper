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

# Render Free is resource constrained.
#
# Start with 2 concurrent pages.
# We can increase this after confirming stability.
CONCURRENCY = 2

# ASU scraper retry behavior.
ASU_MAX_ATTEMPTS = 3

# Google Sheets retry behavior.
GOOGLE_MAX_ATTEMPTS = 6

# Delay between normal batches.
BATCH_DELAY_MIN = 1.5
BATCH_DELAY_MAX = 3.0

# Delay before retrying transient scraper failures.
RETRY_PASS_DELAY = 8

# Only these scraper failures should be retried.
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
# GOOGLE AUTHENTICATION
# ============================================================

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_google_credentials():

    # --------------------------------------------------------
    # RENDER
    #
    # Render stores the service-account JSON in:
    #
    # GOOGLE_SERVICE_ACCOUNT_JSON
    #
    # Nothing sensitive is stored in GitHub.
    # --------------------------------------------------------

    raw_json = os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "",
    ).strip()

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
                f"Retrying in "
                f"{delay:.1f}s..."
            )

            time.sleep(
                delay
            )

    raise last_error


# ============================================================
# GOOGLE SHEETS
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
# DATE / PREVIOUS RUN
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

        name = worksheet.title

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
# PREVIOUS LOOKUP
# ============================================================

def build_previous_lookup(
    previous_sheet,
):

    if previous_sheet is None:
        return {}

    values = google_retry(
        lambda:
            previous_sheet
            .get_all_values(),
        "Read previous enrollment run",
    )

    lookup = {}

    for row in values[1:]:

        if len(row) < 3:
            continue

        class_num = str(
            row[2]
        ).strip()

        if not class_num:
            continue

        lookup[class_num] = {

            "last_name":
                row[4]
                if len(row) > 4
                else "",

            "first_name":
                row[5]
                if len(row) > 5
                else "",

            "previous_amount":
                row[6]
                if len(row) > 6
                else "",

            "cap":
                row[7]
                if len(row) > 7
                else "",

            "open":
                row[8]
                if len(row) > 8
                else "",

            "current":
                row[9]
                if len(row) > 9
                else "",
        }

    print(
        f"Loaded {len(lookup)} classes "
        "from previous run."
    )

    return lookup


# ============================================================
# CURRENT DAILY SHEET LOOKUP
# ============================================================

def build_daily_row_lookup(
    all_values,
):

    lookup = {}

    for row_number, row in enumerate(
        all_values[1:],
        start=2,
    ):

        if len(row) <= 2:
            continue

        class_num = str(
            row[2]
        ).strip()

        if not class_num:
            continue

        # Keep the first occurrence.
        if class_num not in lookup:

            lookup[class_num] = {
                "row_number":
                    row_number,

                "row":
                    row,
            }

    return lookup


# ============================================================
# SCRAPER RESULT HELPERS
# ============================================================

def scraper_error(
    class_num,
    error_type,
    error,
):

    return {
        "class_num":
            str(class_num),

        "status":
            "ERROR",

        "error_type":
            error_type,

        "error":
            str(error),
    }


def scraper_no_data(
    class_num,
    error_type,
    error,
):

    return {
        "class_num":
            str(class_num),

        "status":
            "NO_DATA",

        "error_type":
            error_type,

        "error":
            str(error),
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

    page = await context.new_page()

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
        # PAGE LOAD
        # ----------------------------------------------------

        try:

            response = await page.goto(
                url,
                timeout=30000,
                wait_until="load",
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
        # HTTP STATUS
        # ----------------------------------------------------

        if response:

            status_code = (
                response.status
            )

            if status_code == 429:

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
                    await page.locator(
                        "body"
                    ).inner_text()
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
        # BODY
        # ----------------------------------------------------

        text_content = (
            await page.locator(
                "body"
            ).inner_text()
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
            int(enroll_cap) -
            int(open_seats)
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
                str(enroll_cap),

            "open":
                str(open_seats),

            "current":
                str(current),

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
            str(e),
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
            str(e),
        )

    finally:

        try:

            await page.close()

        except Exception:

            pass


# ============================================================
# SCRAPE CLASS WITH RETRIES
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

        result = await scrape_class_once(
            context,
            class_num,
            term,
        )

        last_result = result

        # Success or confirmed no-data does not retry.
        if (
            result.get("status")
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

        if attempt >= max_attempts:
            break

        delay = min(
            2 ** (attempt - 1),
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

    parts = instructor.split()

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

    return "", instructor


# ============================================================
# PROCESS + UPDATE ONE BATCH
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

    for result in batch_results:

        class_num = str(
            result["class_num"]
        ).strip()

        daily = (
            daily_lookup.get(
                class_num
            )
        )

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
            daily["row_number"]
        )

        row_data = (
            daily["row"]
        )

        previous = (
            previous_lookup.get(
                class_num,
                {},
            )
        )

        # ----------------------------------------------------
        # ERROR
        # ----------------------------------------------------

        if result["status"] == "ERROR":

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
                "keeping previous values."
            )

            continue

        # ----------------------------------------------------
        # NO DATA
        # ----------------------------------------------------

        if result["status"] == "NO_DATA":

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
                "keeping previous values."
            )

            continue

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        last_name, first_name = (
            split_instructor(
                result.get(
                    "instructor",
                    "",
                )
            )
        )

        today_values = {

            "last_name":
                (
                    last_name
                    if last_name
                    else previous.get(
                        "last_name",
                        row_data[4]
                        if len(row_data) > 4
                        else "",
                    )
                ),

            "first_name":
                (
                    first_name
                    if first_name
                    else previous.get(
                        "first_name",
                        row_data[5]
                        if len(row_data) > 5
                        else "",
                    )
                ),

            "cap":
                str(
                    result["cap"]
                ),

            "open":
                str(
                    result["open"]
                ),

            "current":
                str(
                    result["current"]
                ),
        }

        previous_values = {

            "last_name":
                previous.get(
                    "last_name",
                    row_data[4]
                    if len(row_data) > 4
                    else "",
                ),

            "first_name":
                previous.get(
                    "first_name",
                    row_data[5]
                    if len(row_data) > 5
                    else "",
                ),

            "cap":
                previous.get(
                    "cap",
                    row_data[7]
                    if len(row_data) > 7
                    else "",
                ),

            "open":
                previous.get(
                    "open",
                    row_data[8]
                    if len(row_data) > 8
                    else "",
                ),

            "current":
                previous.get(
                    "current",
                    row_data[9]
                    if len(row_data) > 9
                    else "",
                ),
        }

        changed = (
            today_values !=
            previous_values
        )

        if changed:

            changed_count += 1

            print(
                f"{class_num}: CHANGED"
            )

        else:

            unchanged_count += 1

            print(
                f"{class_num}: UNCHANGED"
            )

        previous_amount = (
            previous.get(
                "previous_amount",
                row_data[6]
                if len(row_data) > 6
                else "",
            )
        )

        new_row_values = [
            today_values[
                "last_name"
            ],

            today_values[
                "first_name"
            ],

            previous_amount,

            today_values[
                "cap"
            ],

            today_values[
                "open"
            ],

            today_values[
                "current"
            ],
        ]

        updates.append({
            "range":
                f"E{row_number}:"
                f"J{row_number}",

            "values": [
                new_row_values
            ],
        })

        # Keep in-memory row synchronized.
        while len(row_data) < 10:
            row_data.append("")

        row_data[4:10] = (
            new_row_values
        )

    # --------------------------------------------------------
    # COMMIT BATCH
    # --------------------------------------------------------

    if updates:

        print(
            f"Writing {len(updates)} "
            "successful rows from "
            "current batch..."
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
# PROCESS ONE SCRAPER BATCH
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
    # NORMALIZE CONCURRENCY
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
    # SHEET
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
    # CURRENT DATA
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

    raw_class_nums = [
        str(row[2]).strip()
        for row in all_values[1:]
        if (
            len(row) > 2 and
            row[2]
        )
    ]

    # --------------------------------------------------------
    # DEDUPLICATE
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
        len(raw_class_nums) -
        len(class_nums)
    )

    print(
        f"Found {len(class_nums)} "
        "unique classes."
    )

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
                len(class_nums),

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

    # --------------------------------------------------------
    # PLAYWRIGHT
    # --------------------------------------------------------

    async with async_playwright() as p:

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
            await browser.new_context()
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
                    await process_scrape_batch(
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
                    for result
                    in all_results
                    if (
                        result[
                            "status"
                        ] ==
                        "SUCCESS"
                    )
                )

                # --------------------------------------------
                # PROGRESS
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
                # SMALL DELAY BETWEEN BATCHES
                # --------------------------------------------

                if (
                    len(all_results) <
                    len(class_nums)
                ):

                    delay = random.uniform(
                        BATCH_DELAY_MIN,
                        BATCH_DELAY_MAX,
                    )

                    print(
                        "Waiting "
                        f"{delay:.1f}s "
                        "before next batch."
                    )

                    await asyncio.sleep(
                        delay
                    )

        finally:

            await browser.close()

    # --------------------------------------------------------
    # FINAL COUNTS
    # --------------------------------------------------------

    successful = sum(
        1
        for result
        in all_results
        if (
            result["status"] ==
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
    # RESPONSE
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