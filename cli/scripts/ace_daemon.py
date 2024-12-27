from datetime import datetime
import json
import ssl

from flask import Flask, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.pool import ProcessPoolExecutor
from apscheduler.triggers.cron import CronTrigger
import psycopg

import ace
import ace_cli
import ace_config as config
import ace_core
import ace_db
from ace_data_models import (
    RepsetDiffTask,
    SchemaDiffTask,
    SpockDiffTask,
    TableDiffTask,
    TableRepairTask,
)
from ace_exceptions import AceException
from ace_timeparse import parse_time_string
import cluster
import util
from ace_auth import require_client_cert

app = Flask(__name__)

# apscheduler setup
scheduler = BackgroundScheduler(
    jobstores={"default": MemoryJobStore()},
    executors={"default": ProcessPoolExecutor(32)},
)


"""
API endpoint for initiating a table diff operation.

This endpoint accepts the following GET parameters:
- cluster_name (required): Name of the cluster
- table_name (required): Name of the table to diff
- dbname (optional): Name of the database
- block_rows (optional): Number of rows per block (default: config.BLOCK_ROWS_DEFAULT)
- max_cpu_ratio (optional): Max CPU usage ratio (default: config.MAX_CPU_RATIO_DEFAULT)
- output (optional): Output format, default is 'json'
- nodes (optional): Nodes to include in diff, default is 'all'
- batch_size (optional): Batch size for processing (default: config.BATCH_SIZE_DEFAULT)
- quiet (optional): Whether to suppress output, default is False

Returns:
    JSON response with task_id and submitted_at timestamp on success,
    or an error message on failure.
"""


@app.route("/ace/table-diff", methods=["POST"])
@require_client_cert
def table_diff_api():
    cluster_name = request.args.get("cluster_name")
    table_name = request.args.get("table_name")
    dbname = request.args.get("dbname", None)
    block_rows = request.args.get("block_rows", config.BLOCK_ROWS_DEFAULT)
    max_cpu_ratio = request.args.get("max_cpu_ratio", config.MAX_CPU_RATIO_DEFAULT)
    output = request.args.get("output", "json")
    nodes = request.args.get("nodes", "all")
    batch_size = request.args.get("batch_size", config.BATCH_SIZE_DEFAULT, type=int)
    table_filter = request.args.get("table_filter", None)
    quiet = request.args.get("quiet", False)

    if not cluster_name or not table_name:
        return jsonify({"error": "cluster_name and table_name are required parameters"})

    task_id = ace_db.generate_task_id()

    try:
        raw_args = TableDiffTask(
            cluster_name=cluster_name,
            _table_name=table_name,
            _dbname=dbname,
            block_rows=block_rows,
            max_cpu_ratio=max_cpu_ratio,
            output=output,
            _nodes=nodes,
            batch_size=batch_size,
            table_filter=table_filter,
            quiet_mode=quiet,
            skip_db_update=False,
            invoke_method="api",
        )

        raw_args.scheduler.task_id = task_id
        raw_args.scheduler.task_type = "table-diff"
        raw_args.scheduler.task_status = "RUNNING"
        raw_args.scheduler.started_at = datetime.now()

        # Store the client's role (CN from their certificate)
        raw_args.client_role = request.client_cn

        # Validate basic inputs without establishing connections
        ace.validate_table_diff_inputs(raw_args)
        ace_db.create_ace_task(task=raw_args)

        # Pass the task to the background job where full validation will occur
        scheduler.add_job(
            ace_core.table_diff,
            args=(raw_args,),
        )

        return jsonify({"task_id": task_id, "submitted_at": datetime.now().isoformat()})
    except Exception as e:
        return jsonify({"error": str(e)})


"""
API endpoint for initiating a table repair operation.

This endpoint accepts the following GET parameters:
- cluster_name (required): Name of the cluster
- diff_file (required): Path to the diff file generated by a previous table diff
- source_of_truth (required): Source of truth for the data
- table_name (required): Name of the table to repair
- dbname (optional): Name of the database
- dry_run (optional): If True, simulates the repair without changes (default: False)
- quiet (optional): Whether to suppress output (default: False)
- generate_report (optional): If True, generates a detailed report of the repair
  (default: False)
- upsert_only (optional): If True, only performs upsert operations, skipping
  deletions (default: False)

Returns:
    JSON response with task_id and submitted_at timestamp on success,
    or an error message on failure.
"""


@app.route("/ace/table-repair", methods=["POST"])
@require_client_cert
def table_repair_api():
    cluster_name = request.args.get("cluster_name")
    diff_file = request.args.get("diff_file")
    source_of_truth = request.args.get("source_of_truth")
    table_name = request.args.get("table_name")
    dbname = request.args.get("dbname")
    dry_run = request.args.get("dry_run", False)
    quiet = request.args.get("quiet", False)
    generate_report = request.args.get("generate_report", False)
    upsert_only = request.args.get("upsert_only", False)
    fix_nulls = request.args.get("fix_nulls", False)

    if not cluster_name or not diff_file or not table_name:
        return jsonify(
            {
                "error": "cluster_name, diff_file, source_of_truth, and table_name"
                "are required parameters"
            }
        )

    if not fix_nulls and not source_of_truth:
        return jsonify(
            {
                "error": "source_of_truth is required when fix_nulls mode is "
                "not enabled"
            }
        )

    task_id = ace_db.generate_task_id()

    try:
        raw_args = TableRepairTask(
            cluster_name=cluster_name,
            diff_file_path=diff_file,
            source_of_truth=source_of_truth,
            _table_name=table_name,
            _dbname=dbname,
            dry_run=dry_run,
            quiet_mode=quiet,
            generate_report=generate_report,
            upsert_only=upsert_only,
            invoke_method="api",
            fix_nulls=fix_nulls,
        )
        raw_args.scheduler.task_id = task_id
        raw_args.scheduler.task_type = "table-repair"
        raw_args.scheduler.task_status = "RUNNING"
        raw_args.scheduler.started_at = datetime.now()
        raw_args.client_role = request.client_cn

        ace.validate_table_repair_inputs(raw_args)
        ace_db.create_ace_task(task=raw_args)
        scheduler.add_job(ace_core.table_repair, args=(raw_args,))

        return jsonify({"task_id": task_id, "submitted_at": datetime.now().isoformat()})
    except Exception as e:
        return jsonify({"error": str(e)})


"""
API endpoint for rerunning a table diff operation.

This endpoint allows users to rerun a table diff operation based on a previous
diff file. It supports two different behaviors: multiprocessing and hostdb.

GET parameters:
    cluster_name (str): Name of the cluster (required)
    diff_file (str): Path to the diff file from a previous table diff (required)
    table_name (str): Name of the table to rerun the diff on (required)
    dbname (str): Name of the database (optional)
    quiet (bool): Whether to suppress output (optional, default: False)
    behavior (str): The behavior to use for rerunning
                    (optional, default: "multiprocessing")
                    Supported values: "multiprocessing", "hostdb"

Returns:
    JSON response with task_id and submitted_at timestamp on success,
    or an error message on failure.
"""


@app.route("/ace/table-rerun", methods=["POST"])
@require_client_cert
def table_rerun_api():
    cluster_name = request.args.get("cluster_name")
    diff_file = request.args.get("diff_file")
    table_name = request.args.get("table_name")
    dbname = request.args.get("dbname", None)
    quiet = request.args.get("quiet", False)
    behavior = request.args.get("behavior", "multiprocessing")

    if not cluster_name or not diff_file or not table_name:
        return jsonify(
            {
                "error": "cluster_name, diff_file, and table_name"
                "are required parameters"
            }
        )

    task_id = ace_db.generate_task_id()

    try:
        raw_args = TableDiffTask(
            cluster_name=cluster_name,
            _table_name=table_name,
            _dbname=dbname,
            block_rows=config.BLOCK_ROWS_DEFAULT,
            max_cpu_ratio=config.MAX_CPU_RATIO_DEFAULT,
            output="json",
            _nodes="all",
            batch_size=config.BATCH_SIZE_DEFAULT,
            quiet_mode=quiet,
            diff_file_path=diff_file,
            invoke_method="api",
        )
        raw_args.scheduler.task_id = task_id
        raw_args.scheduler.task_type = "table-rerun"
        raw_args.scheduler.task_status = "RUNNING"
        raw_args.scheduler.started_at = datetime.now()
        raw_args.client_role = request.client_cn

        ace.validate_table_diff_inputs(raw_args)
        ace_db.create_ace_task(task=raw_args)
    except Exception as e:
        return jsonify({"error": str(e)})

    try:
        if behavior == "multiprocessing":
            scheduler.add_job(ace_core.table_rerun_async, args=(raw_args,))
            return jsonify(
                {"task_id": task_id, "submitted_at": datetime.now().isoformat()}
            )
        elif behavior == "hostdb":
            scheduler.add_job(ace_core.table_rerun_temptable, args=(raw_args,))
            return jsonify(
                {"task_id": task_id, "submitted_at": datetime.now().isoformat()}
            )
        else:
            return jsonify({"error": f"Invalid behavior: {behavior}"})
    except Exception as e:
        return jsonify({"error": str(e)})


"""
Performs a repset diff operation on a specified cluster and repset.

API Endpoint: /ace/repset-diff
Method: GET

Query Parameters:
    cluster_name (str): Name of the cluster (required)
    repset_name (str): Name of the repset to diff (required)
    dbname (str): Name of the database (optional)
    block_rows (int): Number of rows per block (default: config.BLOCK_ROWS_DEFAULT)
    max_cpu_ratio (float): Maximum CPU usage ratio
                           (default: config.MAX_CPU_RATIO_DEFAULT)
    output (str): Output format (default: "json")
    nodes (str): Nodes to include in the diff (default: "all")
    batch_size (int): Size of each batch (default: config.BATCH_SIZE_DEFAULT)
    quiet (bool): Whether to suppress output (default: False)
    skip_tables (str): Comma-separated list of tables to skip (optional)

Returns:
    JSON object containing:
        task_id (str): Unique identifier for the submitted task
        submitted_at (str): ISO formatted timestamp of task submission
"""


@app.route("/ace/repset-diff", methods=["POST"])
@require_client_cert
def repset_diff_api():
    cluster_name = request.args.get("cluster_name")
    repset_name = request.args.get("repset_name")
    dbname = request.args.get("dbname", None)
    block_rows = request.args.get("block_rows", config.BLOCK_ROWS_DEFAULT)
    max_cpu_ratio = request.args.get("max_cpu_ratio", config.MAX_CPU_RATIO_DEFAULT)
    output = request.args.get("output", "json")
    nodes = request.args.get("nodes", "all")
    batch_size = request.args.get("batch_size", config.BATCH_SIZE_DEFAULT, type=int)
    quiet = request.args.get("quiet", False)
    skip_tables = request.args.get("skip_tables", None)

    if not cluster_name or not repset_name:
        return jsonify(
            {"error": "cluster_name and repset_name are required parameters"}
        )

    task_id = ace_db.generate_task_id()

    try:
        raw_args = RepsetDiffTask(
            cluster_name=cluster_name,
            _dbname=dbname,
            repset_name=repset_name,
            block_rows=block_rows,
            max_cpu_ratio=max_cpu_ratio,
            output=output,
            _nodes=nodes,
            batch_size=batch_size,
            quiet_mode=quiet,
            skip_tables=skip_tables,
            invoke_method="api",
        )

        raw_args.scheduler.task_id = task_id
        raw_args.scheduler.task_type = "repset-diff"
        raw_args.scheduler.task_status = "RUNNING"
        raw_args.scheduler.started_at = datetime.now()
        raw_args.client_role = request.client_cn

        ace.validate_repset_diff_inputs(raw_args)
        ace_db.create_ace_task(task=raw_args)
        scheduler.add_job(ace_core.repset_diff, args=(raw_args,))

        return jsonify({"task_id": task_id, "submitted_at": datetime.now().isoformat()})
    except Exception as e:
        return jsonify({"error": str(e)})


"""
Perform a Spock diff operation on a specified cluster.

This API endpoint initiates a Spock diff task and returns a task ID for tracking.

Parameters:
    cluster_name (str): Required. Name of the cluster to perform the diff on.
    dbname (str): Optional. Name of the database. Defaults to None.
    nodes (str): Optional. Nodes to include in the diff. Defaults to "all".
    quiet (bool): Optional. Whether to suppress output. Defaults to False.

Returns:
    JSON object containing:
        - task_id (str): Unique identifier for the submitted task.
        - submitted_at (str): ISO formatted timestamp of task submission.
"""


@app.route("/ace/spock-diff", methods=["POST"])
@require_client_cert
def spock_diff_api():
    cluster_name = request.args.get("cluster_name")
    dbname = request.args.get("dbname", None)
    nodes = request.args.get("nodes", "all")
    quiet = request.args.get("quiet", False)

    if not cluster_name:
        return jsonify({"error": "cluster_name is a required parameter"})

    task_id = ace_db.generate_task_id()

    try:
        raw_args = SpockDiffTask(
            cluster_name=cluster_name,
            _dbname=dbname,
            _nodes=nodes,
            quiet_mode=quiet,
            invoke_method="api",
        )

        raw_args.scheduler.task_id = task_id
        raw_args.scheduler.task_type = "spock-diff"
        raw_args.scheduler.task_status = "RUNNING"
        raw_args.scheduler.started_at = datetime.now()
        raw_args.client_role = request.client_cn

        ace.validate_spock_diff_inputs(raw_args)
        ace_db.create_ace_task(task=raw_args)
        scheduler.add_job(ace_core.spock_diff, args=(raw_args,))

        return jsonify({"task_id": task_id, "submitted_at": datetime.now().isoformat()})
    except Exception as e:
        return jsonify({"error": str(e)})


"""
Perform a schema diff operation on a specified cluster and schema.

This API endpoint initiates a schema diff task and returns a task ID for tracking.

Parameters:
    cluster_name (str): Required. Name of the cluster to perform the diff on.
    schema_name (str): Required. Name of the schema to diff.
    dbname (str): Optional. Name of the database. Defaults to None.
    nodes (str): Optional. Nodes to include in the diff. Defaults to "all".
    quiet (bool): Optional. Whether to suppress output. Defaults to False.

Returns:
    JSON object containing:
        - task_id (str): Unique identifier for the submitted task.
        - submitted_at (str): ISO formatted timestamp of task submission.
"""


@app.route("/ace/schema-diff", methods=["POST"])
@require_client_cert
def schema_diff_api():
    cluster_name = request.args.get("cluster_name")
    schema_name = request.args.get("schema_name")
    dbname = request.args.get("dbname", None)
    nodes = request.args.get("nodes", "all")
    quiet = request.args.get("quiet", False)

    task_id = ace_db.generate_task_id()

    if not cluster_name or not schema_name:
        return (
            jsonify({"error": "cluster_name and schema_name are required parameters"}),
            400,
        )

    try:
        raw_args = SchemaDiffTask(
            cluster_name=cluster_name,
            schema_name=schema_name,
            _dbname=dbname,
            _nodes=nodes,
            quiet_mode=quiet,
            invoke_method="api",
        )

        raw_args.scheduler.task_id = task_id
        raw_args.scheduler.task_type = "schema-diff"
        raw_args.scheduler.task_status = "RUNNING"
        raw_args.scheduler.started_at = datetime.now()
        sd_task = ace.schema_diff_checks(raw_args)
        ace_db.create_ace_task(task=sd_task)
        scheduler.add_job(ace_core.schema_diff, args=(sd_task,))
        return jsonify({"task_id": task_id, "submitted_at": datetime.now().isoformat()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


"""
This API endpoint retrieves the status of a task given its task ID.

Parameters:
    task_id (str): Required. Unique identifier of the task to retrieve status for.

Returns:
    JSON object containing:
        - Task details if found, including status and other relevant information.
        - Error message if task_id is missing or task is not found.
"""


@app.route("/ace/task-status", methods=["GET"])
@require_client_cert
def task_status_api():
    task_id = request.args.get("task_id")

    if not task_id:
        return jsonify({"error": "task_id is a required parameter"})

    try:
        task_details = ace_db.get_ace_task_by_id(task_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not task_details:
        return jsonify({"error": f"Task {task_id} not found"}), 404

    return jsonify(task_details)


"""
Update the exception status for a spock exception on a node.

This API endpoint allows updating the exception status for a given node
in a specified cluster. It requires the cluster name and node name as
query parameters, and the exception status details in the request body.

API Method: POST

Args:
    cluster_name (str): The name of the cluster (required query parameter).
    node_name (str): The name of the node (required query parameter).

Request Body:
    A JSON object containing the exception status details. The structure
    should match the requirements of the ace.exception_status_checks()
    and ace_core.update_exception_status() functions.

    A sample entry is shown below:
    {
        "remote_origin": "origin1",
        "remote_commit_ts": "2023-06-01T12:00:00Z",
        "remote_xid": "123456",
        "command_counter": 1,
        "status": "RESOLVED",
        "resolution_details": {"details": "Issue fixed"}
    }

    If the command_counter is omitted, then the exception status is updated
    for all exceptions with the same (remote_origin, remote_commit_ts, remote_xid)
    in spock.exception_status_detail, in addition to updating spock.exception_status.

Returns:
    JSON: A JSON object containing a success message or error details.

Raises:
    400 Bad Request: If required parameters are missing or invalid.
    415 Unsupported Media Type: If the request content type is not JSON.
    500 Internal Server Error: If an unexpected error occurs during processing.

Example:
    POST /ace/exception-status?cluster_name=mycluster&node_name=node1
    {
        "remote_origin": "origin1",
        "remote_commit_ts": "2023-06-01T12:00:00Z",
        "remote_xid": "123456",
        "status": "RESOLVED",
        "resolution_details": {"details": "Issue fixed"}
    }

Response:
    {
        "message": "Exception status updated successfully"
    }
"""


@app.route("/ace/update-spock-exception", methods=["POST"])
@require_client_cert
def update_spock_exception_api():
    cluster_name = request.args.get("cluster_name")
    node_name = request.args.get("node_name")

    if not cluster_name or not node_name:
        return (
            jsonify({"error": "cluster_name and node_name are required parameters"}),
            400,
        )

    entry = request.json

    if not entry:
        return (
            jsonify(
                {"error": "Exception status entry is required in the request body"}
            ),
            400,
        )

    try:
        conn = ace.update_spock_exception_checks(cluster_name, node_name, entry)
        ace_core.update_spock_exception(entry, conn)
        return jsonify({"message": "Exception status updated successfully"}), 200
    except AceException as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500


"""
In the current release of Spock (4.0.1), we do not have a trigger that
auto-populates the exception_status and the exception_status_detail tables
from the exception_log table. So, we periodically execute a transaction that
begins by inserting records into those two tables.
"""


def populate_exception_status_tables():

    cluster_name = config.auto_repair_config["cluster_name"]
    dbname = config.auto_repair_config["dbname"]

    found = ace.check_cluster_exists(cluster_name)
    if not found:
        raise AceException(f"Cluster {cluster_name} not found")

    db, pg, node_info = cluster.load_json(cluster_name)

    cluster_nodes = []
    combined_json = {}
    database = next(
        (db_entry for db_entry in db if db_entry["db_name"] == dbname), None
    )

    for node in node_info:
        combined_json = {**database, **node}
        cluster_nodes.append(combined_json)

    # Step 1: Populate the top-level exception_status table
    # by looking at the exception_log table.
    # This will only insert one record per
    # (remote_origin, remote_commit_ts, remote_xid)
    # trio in the exception_log table.
    exception_sql_step1 = """
    MERGE INTO
        spock.exception_status es
    USING
    (
        SELECT
            remote_origin,
            remote_commit_ts,
            remote_xid
        FROM
            spock.exception_log
    ) el
    ON
    (
        es.remote_origin = el.remote_origin
        AND es.remote_commit_ts = el.remote_commit_ts
        AND es.remote_xid = el.remote_xid
    )
    WHEN NOT MATCHED THEN
    INSERT
    (
        remote_origin,
        remote_commit_ts,
        remote_xid,
        status
    )
    VALUES
    (
        el.remote_origin,
        el.remote_commit_ts,
        el.remote_xid,
        'PENDING'
    );
    """

    # Step 2: Populate the exception_status_detail table
    # by looking at the exception_log table.
    # This will insert one record per failing operation in a
    # transaction.
    # There can be multiple entries for the same
    # (remote_origin, remote_commit_ts, remote_xid)
    # trio, but only one entry per
    # (remote_origin, remote_commit_ts, remote_xid, command_counter)
    # in the exception_log table.
    exception_sql_step2 = """
    MERGE INTO
        spock.exception_status_detail esd
    USING
    (
        SELECT
            remote_origin,
            remote_commit_ts,
            command_counter,
            remote_xid
        FROM spock.exception_log
    ) el
    ON
    (
        esd.remote_origin = el.remote_origin
        AND esd.remote_commit_ts = el.remote_commit_ts
        AND esd.command_counter = el.command_counter
    )
    WHEN NOT MATCHED THEN
    INSERT
    (
        remote_origin,
        remote_commit_ts,
        command_counter,
        remote_xid,
        status
    )
    VALUES
    (
        el.remote_origin,
        el.remote_commit_ts,
        el.command_counter,
        el.remote_xid,
        'PENDING'
    );
    """

    # Step 3: Update the status of the (remote_origin, remote_commit_ts, remote_xid)
    # trio in the exception_status table to 'RESOLVED' if all the
    # commands that belong to that trio have been resolved.
    # TODO: The resolution_details here should be more specific to the
    # actions that ACE took to resolve the exception.
    exception_sql_step3 = """
    UPDATE spock.exception_status es
    SET
        status = %s,
        resolved_at = %s,
        resolution_details = %s
    FROM
    (
        SELECT remote_xid
        FROM spock.exception_status_detail esd
        GROUP BY remote_xid
        HAVING COUNT(*) = COUNT(CASE WHEN status = 'RESOLVED' THEN 1 END)
    ) AS subquery
    WHERE
        es.remote_xid = subquery.remote_xid
        AND es.status != 'RESOLVED';
    """

    for node in cluster_nodes:
        params = {
            "dbname": node["db_name"],
            "user": node["db_user"],
            "password": node["db_password"],
            "host": node["public_ip"],
            "port": node["port"],
            "options": f"-c statement_timeout={config.STATEMENT_TIMEOUT}",
        }

        conn = psycopg.connect(**params)
        cur = conn.cursor()

        try:
            cur.execute(exception_sql_step1)
            cur.execute(exception_sql_step2)
            cur.execute(
                exception_sql_step3,
                (
                    "RESOLVED",
                    datetime.now(),
                    json.dumps(
                        {
                            "details": "All transaction operations auto-resolved"
                            " by ACE. For specific details, check the"
                            " resolution_details column in the"
                            " exception_status_detail table."
                        }
                    ),
                ),
            )
            conn.commit()
        except Exception as e:
            raise AceException(
                f"Error while populating exception status tables on"
                f" node {node['name']}: {str(e)}"
            )
        finally:
            cur.close()
            conn.close()


def validate_auto_repair_config(
    cluster_name, dbname, poll_interval, status_update_interval
):

    poll_interval_datetime = None
    status_update_interval_datetime = None

    if not cluster_name:
        raise AceException("cluster_name is required in auto_repair_config")

    found = ace.check_cluster_exists(cluster_name)
    if not found:
        raise AceException(f"Cluster {cluster_name} not found")

    if not dbname:
        raise AceException("dbname is required in auto_repair_config")

    if not poll_interval:
        raise AceException("poll_interval is required in auto_repair_config")

    try:
        poll_interval_datetime = parse_time_string(poll_interval)
        status_update_interval_datetime = parse_time_string(status_update_interval)
    except Exception as e:
        raise AceException(
            f"Invalid poll_interval or status_update_interval: {poll_interval} or "
            f"{status_update_interval}. Error: {e}"
        )

    db, pg, node_info = cluster.load_json(cluster_name)

    if dbname not in [db_entry["db_name"] for db_entry in db]:
        raise AceException(f"Database {dbname} not found in cluster {cluster_name}")

    return poll_interval_datetime, status_update_interval_datetime


def validate_table_diff_schedule():

    jobs = config.schedule_jobs
    schedule_config = config.schedule_config

    job_names = []

    for job in jobs:
        job_names.append(job["name"])
        if "table_name" not in job or "cluster_name" not in job:
            raise AceException(
                "table_name and cluster_name are required for every job in the schedule"
            )

    for schedule in schedule_config:
        if schedule["job_name"] not in job_names:
            raise AceException(
                f"Job {schedule['job_name']} not found in job definitions"
            )

        if not schedule.get("crontab_schedule", None):
            if not schedule.get("run_frequency", None):
                raise AceException(
                    "Either crontab_schedule or run_frequency must be specified "
                    "for every job in schedule_config"
                )
            else:
                try:
                    parse_time_string(schedule["run_frequency"])
                except Exception as e:
                    raise AceException(
                        f"Invalid run_frequency: {schedule['run_frequency']}. "
                        f"Error: {e}"
                    )
                schedule["crontab_schedule"] = None


def create_schedules():
    schedules = config.schedule_config
    jobs = config.schedule_jobs

    for schedule in schedules:
        enabled = schedule.get("enabled", False)

        if not enabled:
            continue

        job = next((job for job in jobs if job["name"] == schedule["job_name"]), None)
        cluster_name = job["cluster_name"]
        table_name = job["table_name"]
        args = job.get("args", {})
        cron_schedule = schedule.get("crontab_schedule", None)

        if cron_schedule:
            scheduler.add_job(
                ace_cli.table_diff_cli,
                CronTrigger.from_crontab(cron_schedule),
                args=[cluster_name, table_name],
                kwargs=args,
                max_instances=1,
                replace_existing=True,
            )
        else:
            interval = parse_time_string(schedule.get("run_frequency"))
            scheduler.add_job(
                ace_cli.table_diff_cli,
                "interval",
                weeks=interval.weeks if hasattr(interval, "weeks") else 0,
                days=interval.days if hasattr(interval, "days") else 0,
                hours=interval.hours if hasattr(interval, "hours") else 0,
                minutes=interval.minutes if hasattr(interval, "minutes") else 0,
                seconds=interval.seconds if hasattr(interval, "seconds") else 0,
                args=[cluster_name, table_name],
                kwargs=args,
                max_instances=1,
                replace_existing=True,
            )


"""
Validates the table-diff schedule and starts daemons.
"""


def start_scheduling_daemons():

    try:
        validate_table_diff_schedule()
        create_schedules()
    except AceException:
        raise
    except Exception as e:
        raise AceException(f"Unexpected error starting scheduling daemons: {e}")


"""
Validates the auto-repair config and starts daemons.

It starts the background processes to periodically
populate the exception_status and exception_status_detail tables,
monitor for new exceptions in those tables, and execute the auto-repair
process.
"""


def start_auto_repair_daemon():
    poll_interval_datetime = None
    update_interval_datetime = None

    auto_repair_config = config.auto_repair_config
    if not auto_repair_config["enabled"]:
        return

    cluster_name = auto_repair_config.get("cluster_name", None)
    dbname = auto_repair_config.get("dbname", None)

    # The poll_interval is for polling the exception_log table
    # for new exceptions periodically.
    poll_interval = auto_repair_config.get("poll_interval", None)

    # The status_update_interval is for updating the
    # exception_status and exception_status_detail tables.
    # This is only temporary since an upcoming version of Spock
    # will auto-populate these tables using triggers.
    status_update_interval = auto_repair_config.get("status_update_interval", None)

    try:
        poll_interval_datetime, update_interval_datetime = validate_auto_repair_config(
            cluster_name, dbname, poll_interval, status_update_interval
        )
    except AceException as e:
        util.exit_message(f"Error validating auto-repair config: {e}")

    try:
        scheduler.add_job(
            populate_exception_status_tables,
            trigger="interval",
            weeks=(
                update_interval_datetime.weeks
                if hasattr(update_interval_datetime, "weeks")
                else 0
            ),
            days=(
                update_interval_datetime.days
                if hasattr(update_interval_datetime, "days")
                else 0
            ),
            hours=(
                update_interval_datetime.hours
                if hasattr(update_interval_datetime, "hours")
                else 0
            ),
            minutes=(
                update_interval_datetime.minutes
                if hasattr(update_interval_datetime, "minutes")
                else 0
            ),
            seconds=(
                update_interval_datetime.seconds
                if hasattr(update_interval_datetime, "seconds")
                else 0
            ),
            max_instances=1,
            replace_existing=True,
        )

        scheduler.add_job(
            ace_core.auto_repair,
            trigger="interval",
            weeks=(
                poll_interval_datetime.weeks
                if hasattr(poll_interval_datetime, "weeks")
                else 0
            ),
            days=(
                poll_interval_datetime.days
                if hasattr(poll_interval_datetime, "days")
                else 0
            ),
            hours=(
                poll_interval_datetime.hours
                if hasattr(poll_interval_datetime, "hours")
                else 0
            ),
            minutes=(
                poll_interval_datetime.minutes
                if hasattr(poll_interval_datetime, "minutes")
                else 0
            ),
            seconds=(
                poll_interval_datetime.seconds
                if hasattr(poll_interval_datetime, "seconds")
                else 0
            ),
            max_instances=1,
            replace_existing=True,
        )
    except Exception as e:
        raise AceException(f"Error starting auto-repair process: {e}")


"""
Starts the ACE API server.

This function performs the following tasks:
1. Creates necessary database tables for ACE.
2. Starts the background scheduler for job management.
3. Runs the Flask application to serve the API.

The API server is configured to:
- Listen on all interfaces (0.0.0.0)
- Use port 5000
- Support TLS connections using configured certificates

Note: The scheduler is a BackgroundScheduler, so start() does not block execution.
Future versions may require manual event listening for job management.

Returns:
    None
"""


def start_ace():
    ace_db.create_ace_tables()

    # Since the scheduler is a BackgroundScheduler,
    # start() will not block
    scheduler.start()

    # A listener is needed for the upcoming 4.0.0 release
    # of apscheduler. We will need to manually listen to
    # the JOB_ADDED event and then run it. For now, using
    # a BackgroundScheduler with add_job() will automatically
    # run the job in the background.
    # scheduler.add_listener(listener, EVENT_JOB_ADDED)
    # scheduler.add_listener(ace.error_listener, EVENT_JOB_ERROR)

    try:
        # Validate the table-diff schedule and start the daemon
        start_scheduling_daemons()
    except AceException as e:
        util.exit_message(f"Error starting scheduling daemons: {e}")

    try:
        # Validate the auto-repair config and start the daemon
        start_auto_repair_daemon()
    except AceException as e:
        util.exit_message(f"Error starting auto-repair daemon: {e}")

    # Create SSL context for TLS support
    try:
        ssl_context = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
        ssl_context.verify_mode = ssl.CERT_REQUIRED
        ssl_context.load_cert_chain(
            certfile=config.ACE_USER_CERT_FILE, keyfile=config.ACE_USER_KEY_FILE
        )
        ssl_context.load_verify_locations(cafile=config.CA_CERT_FILE)
    except Exception as e:
        util.exit_message(f"Error configuring SSL context: {e}")

    app.run(
        host="0.0.0.0",
        port=5000,
        ssl_context=ssl_context,
        debug=config.DEBUG_MODE,
    )
