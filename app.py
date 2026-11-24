import json
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import google.auth
import gspread

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from google.cloud import tasks_v2

from scraper import run_enrollment


app = FastAPI()


# ============================================================
# CONFIG
# ============================================================

PROJECT_ID = "physics-automation-504617"
REGION = "us-central1"
QUEUE_NAME = "enrollment-queue"

CLOUD_RUN_URL = (
    "https://enrollment-api-955146718767.us-central1.run.app"
)

SERVICE_ACCOUNT_EMAIL = (
    "enrollment-scraper@physics-automation-504617."
    "iam.gserviceaccount.com"
)

JOB_SHEET_NAME = "Enrollment Jobs"

ARIZONA_TZ = ZoneInfo("America/Phoenix")


# ============================================================
# JOB STATES
# ============================================================

QUEUED = "QUEUED"
RUNNING = "RUNNING"
COMPLETE = "COMPLETE"
CANCEL_REQUESTED = "CANCEL_REQUESTED"
CANCELLED = "CANCELLED"
ERROR = "ERROR"


# ============================================================
# JOB HEADERS
# ============================================================

JOB_HEADERS = [
    "job_id",
    "status",
    "sheet_url",
    "sheet_name",
    "term",
    "concurrency",
    "created",
    "started",
    "completed",
    "classes_processed",
    "successful",
    "changed",
    "unchanged",
    "errors",
    "needs_review",
    "previous_sheet",
    "error_classes",
    "no_data_classes",
    "logs",
    "fatal_error",
]


# ============================================================
# REQUEST MODELS
# ============================================================

class RunRequest(BaseModel):
    sheet_url: str
    sheet_name: str
    term: str
    concurrency: int = 5


class StartRequest(BaseModel):
    sheet_url: str
    sheet_name: str
    term: str
    concurrency: int = 1


class TaskRequest(BaseModel):
    job_id: str
    sheet_url: str
    sheet_name: str
    term: str
    concurrency: int = 1


# ============================================================
# EXCEPTIONS
# ============================================================

class JobCancelled(Exception):
    pass


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def local_time():
    return datetime.now(
        ARIZONA_TZ
    ).strftime(
        "%I:%M:%S %p"
    )


def safe_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ============================================================
# GOOGLE CLIENT
# ============================================================

def get_google_client():

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    credentials, project = (
        google.auth.default(
            scopes=scopes
        )
    )

    print(
        "ADC project:",
        project
    )

    print(
        "Credential type:",
        type(credentials).__name__
    )

    print(
        "Service account:",
        getattr(
            credentials,
            "service_account_email",
            "unknown"
        )
    )

    return gspread.authorize(
        credentials
    )


# ============================================================
# JOB STORE
#
# IMPORTANT:
# A JobStore opens the spreadsheet ONCE and keeps the
# worksheet + row number for the lifetime of the request.
# ============================================================

class JobStore:

    def __init__(
        self,
        sheet_url,
        job_id=None
    ):

        self.sheet_url = sheet_url
        self.job_id = job_id

        self.gc = get_google_client()

        self.spreadsheet = (
            self.gc.open_by_url(
                sheet_url
            )
        )

        self.worksheet = (
            self._get_or_create_job_sheet()
        )

        self.row_number = None

        if job_id:
            self.row_number = (
                self._find_job_row(
                    job_id
                )
            )

    # --------------------------------------------------------
    # JOB SHEET
    # --------------------------------------------------------

    def _get_or_create_job_sheet(
        self
    ):

        try:

            worksheet = (
                self.spreadsheet.worksheet(
                    JOB_SHEET_NAME
                )
            )

        except gspread.WorksheetNotFound:

            print(
                "Creating Enrollment Jobs worksheet."
            )

            worksheet = (
                self.spreadsheet.add_worksheet(
                    title=JOB_SHEET_NAME,
                    rows=1000,
                    cols=len(JOB_HEADERS)
                )
            )

            worksheet.append_row(
                JOB_HEADERS,
                value_input_option="RAW"
            )

            try:
                worksheet.hide()
            except Exception:
                pass

        return worksheet

    # --------------------------------------------------------
    # FIND JOB
    # --------------------------------------------------------

    def _find_job_row(
        self,
        job_id
    ):

        try:

            cell = self.worksheet.find(
                str(job_id),
                in_column=1
            )

        except gspread.CellNotFound:

            cell = None

        if not cell:

            raise ValueError(
                f"Enrollment job "
                f"{job_id} was not found."
            )

        return cell.row

    # --------------------------------------------------------
    # CREATE
    # --------------------------------------------------------

    def create_job(
        self,
        sheet_name,
        term,
        concurrency
    ):

        job_id = (
            uuid.uuid4().hex[:12]
        )

        record = {
            "job_id":
                job_id,

            "status":
                QUEUED,

            "sheet_url":
                self.sheet_url,

            "sheet_name":
                sheet_name,

            "term":
                term,

            "concurrency":
                concurrency,

            "created":
                now_iso(),

            "started":
                "",

            "completed":
                "",

            "classes_processed":
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

            "previous_sheet":
                "",

            "error_classes":
                "[]",

            "no_data_classes":
                "[]",

            "logs":
                "",

            "fatal_error":
                "",
        }

        self.worksheet.append_row(
            [
                record[header]
                for header in JOB_HEADERS
            ],
            value_input_option="RAW"
        )

        self.job_id = job_id

        # Since we just appended it, this is one
        # lightweight lookup to establish its row.
        self.row_number = (
            self._find_job_row(
                job_id
            )
        )

        print(
            f"Created enrollment job "
            f"{job_id}"
        )

        return job_id

    # --------------------------------------------------------
    # READ EXACT ROW
    # --------------------------------------------------------

    def read(self):

        if not self.row_number:

            raise ValueError(
                "Job row is not initialized."
            )

        row = self.worksheet.row_values(
            self.row_number
        )

        row += [""] * (
            len(JOB_HEADERS) -
            len(row)
        )

        return {
            header:
                row[index]
            for index, header
            in enumerate(JOB_HEADERS)
        }

    # --------------------------------------------------------
    # READ ONLY STATUS CELL
    # --------------------------------------------------------

    def read_status(self):

        if not self.row_number:

            raise ValueError(
                "Job row is not initialized."
            )

        return str(
            self.worksheet.cell(
                self.row_number,
                2
            ).value or ""
        ).strip().upper()

    # --------------------------------------------------------
    # DIRECT UPDATE
    # --------------------------------------------------------

    def update(
        self,
        **changes
    ):

        if not self.row_number:

            raise ValueError(
                "Job row is not initialized."
            )

        updates = []

        for key, value in changes.items():

            if key not in JOB_HEADERS:
                continue

            column_number = (
                JOB_HEADERS.index(key) + 1
            )

            updates.append({
                "range":
                    gspread.utils.rowcol_to_a1(
                        self.row_number,
                        column_number
                    ),

                "values": [[
                    value
                ]]
            })

        if updates:

            self.worksheet.batch_update(
                updates
            )


# ============================================================
# CLOUD TASK
# ============================================================

def create_cloud_task(
    job_id,
    sheet_url,
    sheet_name,
    term,
    concurrency
):

    client = (
        tasks_v2.CloudTasksClient()
    )

    parent = client.queue_path(
        PROJECT_ID,
        REGION,
        QUEUE_NAME
    )

    payload = {
        "job_id":
            job_id,

        "sheet_url":
            sheet_url,

        "sheet_name":
            sheet_name,

        "term":
            term,

        "concurrency":
            concurrency,
    }

    task = {
        "http_request": {

            "http_method":
                tasks_v2.HttpMethod.POST,

            "url":
                f"{CLOUD_RUN_URL}/run-task",

            "headers": {
                "Content-Type":
                    "application/json"
            },

            "body":
                json.dumps(
                    payload
                ).encode(),

            "oidc_token": {
                "service_account_email":
                    SERVICE_ACCOUNT_EMAIL,

                "audience":
                    CLOUD_RUN_URL,
            },
        }
    }

    created_task = (
        client.create_task(
            request={
                "parent":
                    parent,

                "task":
                    task,
            }
        )
    )

    print(
        "Created Cloud Task:",
        created_task.name
    )

    return created_task


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():

    return {
        "ok": True,
        "async_jobs": True,
        "job_store": "google_sheets",
        "batch_updates": True,
    }


# ============================================================
# START
# ============================================================

@app.post("/start")
def start_job(
    req: StartRequest
):

    if req.concurrency not in [
        1,
        5
    ]:

        raise HTTPException(
            status_code=400,
            detail=(
                "Concurrency must be "
                "1 or 5."
            )
        )

    store = None
    job_id = None

    try:

        store = JobStore(
            req.sheet_url
        )

        job_id = store.create_job(
            sheet_name=
                req.sheet_name,

            term=
                req.term,

            concurrency=
                req.concurrency
        )

        create_cloud_task(
            job_id=
                job_id,

            sheet_url=
                req.sheet_url,

            sheet_name=
                req.sheet_name,

            term=
                req.term,

            concurrency=
                req.concurrency
        )

        return {
            "status":
                QUEUED,

            "job_id":
                job_id,
        }

    except Exception as e:

        print(
            "START JOB ERROR:",
            repr(e)
        )

        if (
            store and
            job_id
        ):

            try:

                store.update(
                    status=ERROR,
                    completed=now_iso(),
                    fatal_error=(
                        f"{type(e).__name__}: "
                        f"{e}"
                    )
                )

            except Exception:
                pass

        raise HTTPException(
            status_code=500,
            detail=(
                f"{type(e).__name__}: "
                f"{e}"
            )
        )


# ============================================================
# CANCEL
# ============================================================

@app.post("/cancel/{job_id}")
def cancel_job(
    job_id: str,
    sheet_url: str
):

    try:

        store = JobStore(
            sheet_url,
            job_id
        )

        current_status = (
            store.read_status()
        )

        if current_status in [
            COMPLETE,
            CANCELLED,
            ERROR
        ]:

            return {
                "status":
                    current_status,

                "job_id":
                    job_id,
            }

        store.update(
            status=
                CANCEL_REQUESTED
        )

        print(
            f"Cancellation requested "
            f"for job {job_id}"
        )

        return {
            "status":
                CANCEL_REQUESTED,

            "job_id":
                job_id,
        }

    except ValueError as e:

        raise HTTPException(
            status_code=404,
            detail=str(e)
        )

    except Exception as e:

        print(
            "CANCEL ERROR:",
            repr(e)
        )

        raise HTTPException(
            status_code=500,
            detail=(
                f"{type(e).__name__}: "
                f"{e}"
            )
        )


# ============================================================
# RUN TASK
# ============================================================

@app.post("/run-task")
def run_task(
    req: TaskRequest
):

    store = None

    try:

        # ----------------------------------------------------
        # OPEN JOB STORE ONCE
        # ----------------------------------------------------

        store = JobStore(
            req.sheet_url,
            req.job_id
        )

        current_status = (
            store.read_status()
        )

        # ----------------------------------------------------
        # TERMINAL / DUPLICATE PROTECTION
        # ----------------------------------------------------

        if current_status == COMPLETE:

            return {
                "status":
                    "already_complete",

                "job_id":
                    req.job_id,
            }

        if current_status == CANCELLED:

            return {
                "status":
                    "already_cancelled",

                "job_id":
                    req.job_id,
            }

        if current_status == RUNNING:

            print(
                f"Job {req.job_id} "
                "is already running."
            )

            return {
                "status":
                    "already_running",

                "job_id":
                    req.job_id,
            }

        if current_status == CANCEL_REQUESTED:

            store.update(
                status=CANCELLED,
                completed=now_iso()
            )

            return {
                "status":
                    CANCELLED,

                "job_id":
                    req.job_id,
            }

        # ----------------------------------------------------
        # MARK RUNNING
        # ----------------------------------------------------

        store.update(
            status=
                RUNNING,

            started=
                now_iso(),

            fatal_error=
                ""
        )

        print(
            f"Starting enrollment job "
            f"{req.job_id}"
        )

        print(
            f"Concurrency: "
            f"{req.concurrency}"
        )

        # ----------------------------------------------------
        # LIVE STATE
        # ----------------------------------------------------

        progress_logs = []

        last_saved_processed = -1

        # ----------------------------------------------------
        # PROGRESS CALLBACK
        # ----------------------------------------------------

        def progress_callback(event):

            nonlocal last_saved_processed

            event_type = (
                event.get(
                    "type",
                    ""
                )
            )

            # ================================================
            # CANCELLATION CHECK
            #
            # Only perform a Sheets read at explicit scraper
            # checkpoints, not on every callback.
            # ================================================

            if event_type == "checkpoint":

                current = (
                    store.read_status()
                )

                if (
                    current ==
                    CANCEL_REQUESTED
                ):

                    raise JobCancelled(
                        "Enrollment run "
                        "was cancelled."
                    )

            # ================================================
            # LOG
            # ================================================

            message = event.get(
                "message",
                ""
            )

            if message:

                progress_logs.append(
                    f"[{local_time()}] "
                    f"{message}"
                )

            # ================================================
            # CLASS RESULTS
            # ================================================

            for item in event.get(
                "results",
                []
            ):

                class_num = str(
                    item.get(
                        "class_num",
                        ""
                    )
                )

                status = (
                    item.get(
                        "status",
                        ""
                    )
                )

                if status == "SUCCESS":

                    progress_logs.append(
                        f"{class_num}: SUCCESS"
                    )

                elif status == "ERROR":

                    progress_logs.append(
                        f"{class_num}: ERROR - "
                        f"{item.get(
                            'error_type',
                            'UNKNOWN_ERROR'
                        )}"
                    )

                elif status == "NO_DATA":

                    progress_logs.append(
                        f"{class_num}: NO DATA - "
                        f"{item.get(
                            'error_type',
                            'NO_DATA'
                        )}"
                    )

            # ================================================
            # SAVE ONLY AFTER A BATCH COMPLETES
            # ================================================

            if event_type != "batch_complete":
                return

            processed = safe_int(
                event.get(
                    "processed"
                )
            )

            if (
                processed ==
                last_saved_processed
            ):
                return

            store.update(
                classes_processed=
                    processed,

                successful=
                    safe_int(
                        event.get(
                            "successful"
                        )
                    ),

                errors=
                    safe_int(
                        event.get(
                            "errors"
                        )
                    ),

                needs_review=
                    safe_int(
                        event.get(
                            "needs_review"
                        )
                    ),

                logs=
                    "\n".join(
                        progress_logs[-100:]
                    )
            )

            last_saved_processed = (
                processed
            )

        # ----------------------------------------------------
        # RUN SCRAPER
        # ----------------------------------------------------

        result = run_enrollment(
            sheet_url=
                req.sheet_url,

            sheet_name=
                req.sheet_name,

            term=
                req.term,

            concurrency=
                req.concurrency,

            progress_callback=
                progress_callback
        )

        # ----------------------------------------------------
        # COMPLETE
        # ----------------------------------------------------

        progress_logs.append(
            f"[{local_time()}] "
            "Enrollment run complete."
        )

        store.update(
            status=
                COMPLETE,

            completed=
                now_iso(),

            classes_processed=
                result.get(
                    "classes_processed",
                    0
                ),

            successful=
                result.get(
                    "successful",
                    0
                ),

            changed=
                result.get(
                    "changed",
                    0
                ),

            unchanged=
                result.get(
                    "unchanged",
                    0
                ),

            errors=
                result.get(
                    "errors",
                    0
                ),

            needs_review=
                result.get(
                    "needs_review",
                    0
                ),

            previous_sheet=(
                result.get(
                    "previous_sheet",
                    ""
                ) or ""
            ),

            error_classes=
                json.dumps(
                    result.get(
                        "error_classes",
                        []
                    )
                ),

            no_data_classes=
                json.dumps(
                    result.get(
                        "no_data_classes",
                        []
                    )
                ),

            logs=
                "\n".join(
                    progress_logs[-100:]
                ),

            fatal_error=
                ""
        )

        print(
            f"JOB {req.job_id} COMPLETE"
        )

        return {
            "status":
                COMPLETE,

            "job_id":
                req.job_id,
        }

    # --------------------------------------------------------
    # CANCELLED
    # --------------------------------------------------------

    except JobCancelled:

        print(
            f"JOB {req.job_id} "
            "CANCELLED"
        )

        try:

            if store:

                store.update(
                    status=
                        CANCELLED,

                    completed=
                        now_iso(),

                    logs=(
                        "\n".join(
                            progress_logs[-100:]
                        ) +
                        "\n[CANCELLED] "
                        "Enrollment run cancelled."
                    )
                )

        except Exception as e:

            print(
                "FAILED TO RECORD "
                "CANCELLATION:",
                repr(e)
            )

        # Return 200 so Cloud Tasks
        # does not retry cancellation.
        return {
            "status":
                CANCELLED,

            "job_id":
                req.job_id,
        }

    # --------------------------------------------------------
    # FATAL
    # --------------------------------------------------------

    except Exception as e:

        print(
            f"FATAL JOB ERROR "
            f"{req.job_id}: "
            f"{type(e).__name__}: "
            f"{e}"
        )

        try:

            if store:

                store.update(
                    status=
                        ERROR,

                    completed=
                        now_iso(),

                    fatal_error=(
                        f"{type(e).__name__}: "
                        f"{e}"
                    )
                )

        except Exception as update_error:

            print(
                "FAILED TO RECORD "
                "JOB ERROR:",
                repr(update_error)
            )

        raise HTTPException(
            status_code=500,
            detail=(
                f"{type(e).__name__}: "
                f"{e}"
            )
        )


# ============================================================
# STATUS
# ============================================================

@app.get("/status/{job_id}")
def job_status(
    job_id: str,
    sheet_url: str
):

    try:

        # One connection + one exact row read.
        store = JobStore(
            sheet_url,
            job_id
        )

        job = store.read()

        try:

            error_classes = (
                json.loads(
                    job["error_classes"]
                    or "[]"
                )
            )

        except Exception:

            error_classes = []

        try:

            no_data_classes = (
                json.loads(
                    job["no_data_classes"]
                    or "[]"
                )
            )

        except Exception:

            no_data_classes = []

        logs = (
            job["logs"].split("\n")
            if job["logs"]
            else []
        )

        return {
            "job_id":
                job["job_id"],

            "status":
                job["status"],

            "sheet_name":
                job["sheet_name"],

            "term":
                job["term"],

            "concurrency":
                safe_int(
                    job["concurrency"]
                ) or 1,

            "created":
                job["created"],

            "started":
                job["started"],

            "completed":
                job["completed"],

            "total":
                safe_int(
                    job[
                        "classes_processed"
                    ]
                ),

            "successful":
                safe_int(
                    job["successful"]
                ),

            "changed":
                safe_int(
                    job["changed"]
                ),

            "unchanged":
                safe_int(
                    job["unchanged"]
                ),

            "errors":
                safe_int(
                    job["errors"]
                ),

            "needs_review":
                safe_int(
                    job["needs_review"]
                ),

            "previous_sheet":
                job["previous_sheet"],

            "error_classes":
                error_classes,

            "no_data_classes":
                no_data_classes,

            "logs":
                logs,

            "fatal_error":
                job["fatal_error"],
        }

    except ValueError as e:

        raise HTTPException(
            status_code=404,
            detail=str(e)
        )

    except Exception as e:

        print(
            "STATUS ERROR:",
            repr(e)
        )

        raise HTTPException(
            status_code=500,
            detail=(
                f"{type(e).__name__}: "
                f"{e}"
            )
        )


# ============================================================
# LEGACY SYNCHRONOUS ENDPOINT
# ============================================================

@app.post("/run")
def run(
    req: RunRequest
):

    return run_enrollment(
        sheet_url=
            req.sheet_url,

        sheet_name=
            req.sheet_name,

        term=
            req.term,

        concurrency=
            req.concurrency
    )