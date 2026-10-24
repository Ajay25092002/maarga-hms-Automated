# Automated IP Billing Scheduler

## Overview

The Automated IP Billing Scheduler automatically adds configured service
line items to eligible Inpatient (IP) bills every day at **12:01 AM
Indian Standard Time (IST)**.

It uses Celery Beat to schedule tasks, Celery Worker to execute them,
Redis as the message broker and result backend, and PostgreSQL for
direct database access.

The scheduler runs the automated IP billing logic directly from
`tasks.py`. It does not call the backend API for scheduled billing.

Each generated bill line item uses `service_date` to identify the date
the service applies to.

## Prerequisites

-   Docker installed and running (for Docker deployment).
-   Python installed and compatible with the scheduler dependencies (for
    local execution).
-   The Maarga HMS backend and PostgreSQL database configured.
-   The existing Docker network: `maarga-hms-network` (for Docker
    deployment).
-   The existing Redis container: `maarga-hms-redis` (for Docker
    deployment).
-   Redis reachable locally (for local execution).
-   A configured PostgreSQL connection string matching the backend
    database.
-   `Dockerfile`, `requirements.txt`, `celery_app.py`, and `tasks.py` in
    the scheduler directory.
-   Database permissions sufficient to read services and active IP
    bills, insert bill items, and update bill totals.

------------------------------------------------------------------------

# Part A: Run Locally (Without Docker)

This section is for local development on your machine. Celery Worker and
Celery Beat run in separate terminals and connect to your development
PostgreSQL database and Redis.

## A1. Verify Local PostgreSQL and Redis

Ensure your local PostgreSQL database is running and is the same
development database used by the Maarga HMS backend.

Verify Redis connectivity:

``` powershell
redis-cli ping
```

Expected response:

``` text
PONG
```

If Redis is running only inside the existing Docker container, it must
be reachable from your host machine for local execution. A Redis
container does not automatically expose port `6379` to the host. Do not
start another Redis service on the same port if one is already running.

## A2. Open the Scheduler Directory

Open PowerShell or Command Prompt in the directory containing
`celery_app.py` and `tasks.py`.

``` powershell
cd path\to\Automated-ip-billing
```

Replace the example path with your actual scheduler directory.

## A3. Create a Python Virtual Environment

``` powershell
python -m venv .venv
```

Activate it in PowerShell:

``` powershell
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks script execution, use Command Prompt instead:

``` cmd
.venv\Scripts\activate.bat
```

## A4. Install Dependencies

``` powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Ensure `requirements.txt` includes all dependencies imported by the
scheduler, including Celery with Redis support, the PostgreSQL driver,
and `python-dotenv` if the code loads `.env`.

## A5. Configure Local Environment Variables

Create a `.env` file in the scheduler directory:

``` env
DATABASE_URL=your_local_backend_postgresql_connection_string

CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1

TZ=Asia/Kolkata
```

Replace the example `DATABASE_URL` with the actual connection string
used by your local Maarga HMS backend. Do not commit `.env` to Git.

The scheduler code must load the `.env` file before reading these
variables, for example by calling `load_dotenv()` in the application
startup/import path. If it does not load `.env` automatically, set the
environment variables in each terminal before starting Celery.

The PostgreSQL host and port must be reachable from the machine running
the scheduler. Confirm that the database user has the permissions
required to read services and active IP bills, insert bill items, and
update bill totals.

## A6. Start the Celery Worker

Open the first terminal in the scheduler directory, activate the virtual
environment, and run:

``` powershell
celery -A celery_app:celery_app worker --loglevel=INFO --pool=solo
```

The `--pool=solo` option is recommended for local Windows development
because the default worker process pool can encounter Windows-specific
permission errors.

Keep this terminal open. The Worker executes tasks received from Redis.

## A7. Start Celery Beat

Open a second terminal in the same scheduler directory. Activate the
same virtual environment and run:

``` powershell
celery -A celery_app:celery_app beat --loglevel=INFO
```

Keep this terminal open. Beat dispatches the scheduled task daily at
12:01 AM IST, provided `celery_app.py` defines that schedule and
timezone correctly.

Run only one Beat instance for this schedule.

## A8. Trigger Automated IP Billing Manually

You do not need to wait until midnight to test the task.

Open a third terminal, activate the virtual environment, and run:

``` powershell
celery -A celery_app:celery_app call tasks.run_automated_ip_billing
```

This queues the task for execution by the running Worker.

To queue the task for a specific service date:

``` powershell
python -c "from tasks import run_automated_ip_billing; run_automated_ip_billing.delay(service_date='2026-10-24')"
```

Replace `2026-10-24` with the required date in `YYYY-MM-DD` format.

## A9. Verify the Local Run

Check the Worker terminal for task execution logs, inserted items,
skipped duplicates, and failures.

Verify the affected records in PostgreSQL:

-   Confirm that expected line items were added to eligible IP bills.
-   Confirm each generated item has the intended `service_date`.
-   Confirm existing service/date combinations were skipped.
-   Confirm `subtotal` and `total_amount` were recalculated for bills
    that received new items.
-   Confirm bills with missing room types or unresolved service prices
    are reported appropriately.

## A10. Stop the Local Scheduler

Press `Ctrl+C` in the Beat terminal, then press `Ctrl+C` in the Worker
terminal.

When finished, deactivate the virtual environment:

``` powershell
deactivate
```

## Local Development Notes

-   Local Redis URLs use `localhost`; Docker containers use
    `redis://maarga-hms-redis:6379/0` and
    `redis://maarga-hms-redis:6379/1`.
-   Do not run local Beat and Docker Beat against the same database at
    the same time unless you intentionally want both schedulers active.
-   A manual trigger can modify records in the development database. Use
    suitable development data and verify duplicate prevention before
    triggering repeatedly.
-   The local scheduler executes the direct database billing logic in
    `tasks.py`; it does not call the backend automated billing API.

------------------------------------------------------------------------

# Part B: Run with Docker

## B1. Existing Backend Redis Configuration

The scheduler reuses the Redis container and Docker network already
configured for the Maarga HMS backend.

  Setting                 Value
  ----------------------- ----------------------
  Docker network          `maarga-hms-network`
  Redis container         `maarga-hms-redis`
  Redis broker database   `0`
  Redis result database   `1`
  Redis data volume       `redis_data`

**Important:** Do not create another Redis container or Docker network.
Do not remove the existing Redis data volume during scheduler
deployment.

Verify that Redis is running:

``` bash
docker ps
```

Check Redis connectivity:

``` bash
docker exec maarga-hms-redis redis-cli ping
```

Expected response:

``` text
PONG
```

Verify the existing network:

``` bash
docker network inspect maarga-hms-network
```

## B2. Configure Docker Environment Variables

Create a `.env.docker` file in the scheduler directory:

``` env
DATABASE_URL=your_existing_backend_postgresql_connection_string

CELERY_BROKER_URL=redis://maarga-hms-redis:6379/0
CELERY_RESULT_BACKEND=redis://maarga-hms-redis:6379/1

TZ=Asia/Kolkata
```

Replace the example `DATABASE_URL` with the actual PostgreSQL connection
string used by the backend.

The database hostname must be reachable from the scheduler container. If
PostgreSQL runs in another container, ensure the scheduler can resolve
and reach that container through the appropriate Docker network. If
PostgreSQL runs on the host or another server, configure a reachable
hostname and port.

The database credentials must have only the permissions required for
automated IP billing.

**Security recommendations:**

-   Do not commit `.env.docker` to Git.
-   Do not expose PostgreSQL publicly just to enable scheduler access.
-   Use a dedicated database user with restricted permissions when
    possible.
-   Keep database credentials private.

## B3. Build the Docker Image

Run this command from the scheduler directory:

``` bash
docker build -t automated-ip-billing-scheduler .
```

The same image is used for the Celery Worker and Celery Beat containers.

Rebuild the image after changing scheduler code or dependencies.

## B4. Run the Celery Worker

The Worker executes the automated IP billing task and connects directly
to PostgreSQL.

``` bash
docker run -d \
  --name aipb-worker \
  --restart unless-stopped \
  --network maarga-hms-network \
  --env-file .env.docker \
  automated-ip-billing-scheduler \
  celery -A celery_app:celery_app worker --loglevel=INFO
```

## B5. Run Celery Beat

Celery Beat schedules the automated IP billing task to run daily at
12:01 AM IST.

``` bash
docker run -d \
  --name aipb-beat \
  --restart unless-stopped \
  --network maarga-hms-network \
  --env-file .env.docker \
  automated-ip-billing-scheduler \
  celery -A celery_app:celery_app beat --loglevel=INFO
```

**Important:** Run only one Celery Beat instance for this schedule to
prevent duplicate scheduled task dispatches.

## B6. Automated IP Service Processing

The scheduler processes eligible inpatient bills using the configured
service names:

1.  Room Charges
2.  Holistic Therapies
3.  Diet & Food
4.  Doctor Consultation
5.  Nursing Charges
6.  Psychiatric Consultation/Treatment
7.  Psychologist Counselling/Treatment
8.  Recreation Activities/Therapy

The task:

-   Selects IP bills where `discharge_date IS NULL`.
-   Verifies that the bill has a room type.
-   Resolves the applicable active IP service prices.
-   Checks whether the same service is already present on the bill for
    the same `service_date`.
-   Inserts only missing service line items.
-   Recalculates bill totals when new items are added.
-   Logs processing results and failures.

The scheduled task uses the current date in the `Asia/Kolkata` timezone
as the `service_date`, unless a specific service date is supplied when
manually triggering the task.

The backend API remains available for normal application operations.
Scheduled processing does not call the automated billing API endpoint.

## B7. Manual Billing Trigger

You can trigger the task immediately without waiting for the scheduled
time.

### Trigger from Docker

``` bash
docker exec aipb-worker celery -A celery_app:celery_app call tasks.run_automated_ip_billing
```

This queues the task for immediate execution by the running Worker.

### Trigger for a Specific Service Date

``` bash
docker exec aipb-worker python -c "from tasks import run_automated_ip_billing; run_automated_ip_billing.delay(service_date='2026-10-24')"
```

Replace `2026-10-24` with the required service date in `YYYY-MM-DD`
format.

**Important:** Triggering a task repeatedly can run the processing logic
multiple times. The duplicate check should prevent existing line items
from being inserted again, but verify this behavior before production
use.

## B8. Verify Running Containers

``` bash
docker ps
```

The scheduler containers should include:

-   `aipb-worker`
-   `aipb-beat`

The existing Redis container remains:

-   `maarga-hms-redis`

The PostgreSQL database remains the same database used by the backend.

## B9. View Logs

### Worker Logs

``` bash
docker logs -f aipb-worker
```

### Beat Logs

``` bash
docker logs -f aipb-beat
```

Worker logs show task execution, inserted service line items, skipped
duplicates, and failures.

Beat logs show scheduled task dispatches.

## B10. Restart Scheduler Containers

Restart the Worker:

``` bash
docker restart aipb-worker
```

Restart Beat:

``` bash
docker restart aipb-beat
```

## B11. Stop or Remove Scheduler Containers

Stop the Worker:

``` bash
docker stop aipb-worker
```

Stop Beat:

``` bash
docker stop aipb-beat
```

Remove the Worker:

``` bash
docker rm -f aipb-worker
```

Remove Beat:

``` bash
docker rm -f aipb-beat
```

Removing scheduler containers does not remove the scheduler image, the
existing Redis container, or the Redis data volume.

## B12. Redeploy After Code Changes

Run these commands from the scheduler directory.

### Step 1: Build the Updated Image

``` bash
docker build -t automated-ip-billing-scheduler .
```

### Step 2: Remove the Existing Scheduler Containers

``` bash
docker rm -f aipb-worker aipb-beat
```

### Step 3: Start the Worker

``` bash
docker run -d \
  --name aipb-worker \
  --restart unless-stopped \
  --network maarga-hms-network \
  --env-file .env.docker \
  automated-ip-billing-scheduler \
  celery -A celery_app:celery_app worker --loglevel=INFO
```

### Step 4: Start Beat

``` bash
docker run -d \
  --name aipb-beat \
  --restart unless-stopped \
  --network maarga-hms-network \
  --env-file .env.docker \
  automated-ip-billing-scheduler \
  celery -A celery_app:celery_app beat --loglevel=INFO
```

Redis does not need to be recreated for normal scheduler code changes.

## B13. Important Docker Deployment Notes

-   Reuse the existing `maarga-hms-network` and `maarga-hms-redis`.
-   Do not publish Redis port `6379` publicly.
-   Configure `DATABASE_URL` to point to the same PostgreSQL database
    used by the backend.
-   Ensure PostgreSQL is reachable from the scheduler container.
-   Ensure the database user has the required read and write
    permissions.
-   Keep the scheduled service date in the `Asia/Kolkata` timezone.
-   Run only one Beat instance for this schedule.
-   Monitor Worker logs for database errors, pricing configuration
    issues, and processing failures.
-   Test duplicate prevention and bill-total calculations before
    production deployment.
-   Keep the backend API available for normal application billing
    operations.
-   Preserve the existing Redis data volume during routine deployments.

## Final Notes

This scheduler contains a copy of the billing logic. When billing rules
or pricing behavior change in the backend, review and update the
scheduler implementation as well to keep both consistent.

Before running locally or in Docker, verify that:

-   `celery_app.py` defines the intended daily schedule and
    `Asia/Kolkata` timezone.
-   The scheduler loads the required environment variables.
-   The configured database connection points to the intended
    development or production database.
-   Only one Beat instance is active for the target schedule.
