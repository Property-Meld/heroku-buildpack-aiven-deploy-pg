import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import djclick as click
import invoke.exceptions
import requests
from gevent.subprocess import Popen, PIPE, check_output
from invoke import task, run
from typing import AnyStr, Union
from contextlib import ContextDecorator
from errno import ETIME
from os import strerror
from signal import signal, SIGALRM, alarm

stdout = lambda x: sys.stdout.write(x + "\n")
stderr = lambda x: sys.stderr.write(x + "\n")


try:
    from propertymeld.constants import PROTECTED_AIVEN_APP_NAMES
except ImportError:
    PROTECTED_AIVEN_APP_NAMES = [
        x for x in os.environ.get("PROTECTED_AIVEN_APP_NAMES", "").split(",") if x
    ]


def pid_exists(pid):
    if pid < 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


class timeout(ContextDecorator):
    def __init__(self, seconds, *, msg=strerror(ETIME), suppress_errs=False):
        self.seconds = int(seconds)
        self.msg = msg
        self.suppress = bool(suppress_errs)

    def _timeout_handler(self, signum, frame):
        raise TimeoutError(self.msg)

    def __enter__(self):
        signal(SIGALRM, self._timeout_handler)
        alarm(self.seconds)

    def __exit__(self, exc_type, exc_val, exc_tb):
        alarm(0)
        if self.suppress and exc_type is TimeoutError:
            return True


db_name = os.environ.get("AIVEN_DBNAME", "propertymeld")
staging_app_name = os.environ.get("STAGING_APP_NAME", "property-meld-staging")

wait_cmd = """avn --auth-token {auth_token} service wait --project {project} {app_name}""".format
list_cmd = """avn --auth-token {auth_token} service list --project {project} {app_name} --json""".format
list_db_cmd = """avn --auth-token {auth_token} service database-list --project {project} {app_name} --json""".format
service_create_cmd = """avn --auth-token {auth_token} service create --enable-termination-protection --project {project} --service-type {service_type} --plan {plan} --cloud {cloud} -c {pg_version} {app_name}""".format
service_disable_protection_cmd = """avn --auth-token {auth_token} service update --disable-termination-protection --project {project} {app_name}""".format
service_terminate_cmd = """avn --auth-token {auth_token} service terminate --force --project {project} {app_name}""".format
delete_db_cmd = f"""avn --auth-token {{auth_token}} service database-delete --project {{project}} --dbname {db_name} {{app_name}}""".format
create_db_cmd = f"""avn --auth-token {{auth_token}} service database-create --project {{project}} --dbname {db_name} {{app_name}}""".format
pool_create_cmd = """avn --auth-token {auth_token} service connection-pool-create {app_name} --project {project} --dbname defaultdb --username avnadmin --pool-name defaultdb --pool-size 50 --json""".format
pool_list_cmd = """avn --auth-token {auth_token} service connection-pool-list {app_name} --verbose --project {project} --json""".format
pool_delete_cmd = f"""avn --auth-token {{auth_token}} service connection-pool-delete {{app_name}} --project {{project}} --pool-name {db_name}-pool --json""".format

add_tag_cmd = """avn --auth-token {auth_token} service tags update --project {project} --add-tag {key_value} {app_name}""".format
replace_tag_cmd = """avn --auth-token {auth_token} service tags replace --project {project} --tag {key_value} {app_name}""".format
remove_tag_cmd = """avn --auth-token {auth_token} service tags update --project {project} --remove-tag {key} {app_name}""".format

config = {
    "auth_token": f'"{os.environ.get("AIVEN_AUTH_TOKEN")}"',  # set in heroku review app pipeline env vars in dashboard "reveal config vars", and aiven console
    "app_name": f"{os.environ.get('HEROKU_APP_NAME')}",
    "project": os.environ.get("AIVEN_PROJECT_NAME"),
    "shared_resource_app": os.environ.get("AIVEN_SHARED_RESOURCE_APP", "shared-amqp")
    or "shared-amqp",
}

service_config = {
    "cloud": os.environ.get("AIVEN_CLOUD", "do-nyc") or "do-nyc",
    "service_type": os.environ.get("AIVEN_SERVICE_TYPE", "pg") or "pg",
    "plan": os.environ.get("AIVEN_PLAN", "startup-4")
    or "startup-4",  # hobbyist does not support pooling
    "pg_version": os.environ.get("AIVEN_PG_VERSION", "pg_version=12")
    or "pg_version=12",
}

stdout(f"service_config: {service_config}")


def get_heroku_env(env=None):
    if os.environ.get("HEROKU_APP_NAME") or env:
        app = env or os.environ.get("HEROKU_APP_NAME")
        url = f"https://api.heroku.com/apps/{app}/config-vars"
        stdout(f"REQUESTING HEROKU ENV VARS ON APP: {app}")
        result = requests.get(
            url,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/vnd.heroku+json; version=3",
                "Authorization": f"Bearer {os.environ.get('HEROKU_API_KEY')}",
            },
        )
        if result.status_code in (200, 201, 202, 206):
            return result.json()
        else:
            stderr(f"Failed to get heroku env : {result.content}")
            exit(8)
    return {}


heroku_bin = os.environ.get(
    "HEROKU_BIN", get_heroku_env().get("HEROKU_BIN", "/app/.heroku/bin/heroku")
)
stdout(f"HEROKU_BIN: {heroku_bin}")

assert heroku_bin


def check_if_exist():
    results = get_heroku_env()
    env_vars = [
        "AIVEN_APP_NAME",
        "AIVEN_DATABASE_DIRECT_URL",
        "AIVEN_PG_USER",
        "AIVEN_DIRECT_PG_PORT",
        "AIVEN_PG_PASSWORD",
    ]
    if all(x in results for x in env_vars):
        stdout("Pre-existing db service found.")
        return results.get("AIVEN_DATABASE_DIRECT_URL", ""), results.get(
            "AIVEN_DATABASE_URL", ""
        )
    return None, None


def sanitize_output(output):
    output = re.sub(
        r"[\"']?password[\"']?: ['\"a-z0-9A-Z]+", "'password': '###'", output
    )
    output = re.sub(
        r"postgres:\/\/avnadmin:[a-z0-9A-Z]+@", "postgres://avnadmin:###@", output
    )
    tok = os.environ.get("AIVEN_AUTH_TOKEN")
    if tok:
        output = output.replace(tok, "###")
    return output


def do_popen(
    cmd: str,
    err_msg: AnyStr = "",
    _json: bool = None,
    exc=Exception,
    no_buffer=False,
    quiet: bool = False,
) -> Union[str, dict]:
    cmd = cmd.split()
    if no_buffer:
        process = Popen(
            cmd,
            stderr=PIPE,
            stdout=PIPE,
            env=os.environ,
            bufsize=1,
            universal_newlines=True,
        )
        err_lines = []
        out_lines = []
        with process.stderr:
            for line in process.stderr:
                stdout(line)
                err_lines.append(line)
        with process.stdout:
            for line in process.stdout:
                out_lines.append(line)
        process.wait()
        return "".join(out_lines), "".join(err_lines)
    else:
        with Popen(cmd, stderr=PIPE, stdout=PIPE, env=os.environ) as process:
            _stdout, _stderr = process.communicate()
            errcode = process.returncode
            if quiet:
                return errcode
            if errcode:
                stderr(sanitize_output(" ".join(cmd + [_stderr.decode("utf8")])))
                raise exc(err_msg)
            stdout(sanitize_output(_stdout.decode("utf8")))
            if _json:
                return json.loads(_stdout.decode("utf8"))
    return sanitize_output(_stdout.decode("utf8"))


def wait_for_service(config):
    stdout("Aiven: Waiting for db instance status to be in 'running' state.")
    do_popen(
        wait_cmd(**config),
        err_msg=f"Had difficulties waiting: {config.get('app_name')}",
        no_buffer=True,
    )
    stdout("Aiven: new db instance created.")
    result = do_popen(
        list_cmd(**config),
        err_msg=f"Failed to list services for: {config.get('project')} {str(config.get('app_name'))}.",
        _json=True,
    )
    stdout(sanitize_output(str(result)))
    while not all(x.get("state") == "running" for x in result[0].get("node_states")):
        result = do_popen(
            list_cmd(**config), err_msg="Failed to list services.", _json=True
        )
        time.sleep(5)
    do_popen(
        wait_cmd(**config),
        err_msg=f"Had difficulties waiting: {config.get('app_name')}",
        no_buffer=True,
    )
    return do_popen(
        list_cmd(**config),
        err_msg=f"Failed to list services for: {config.get('project')} {str(config.get('app_name'))}.",
        _json=True,
    )


def create_db(config) -> str:
    direct_uri, pool_uri = check_if_exist()
    if direct_uri:
        return direct_uri
    db = None
    while not db:
        stdout("Trying to create db.")
        db = db_name in do_popen(
            list_db_cmd(**config),
            err_msg="Error while listing databases in service.",
            _json=True,
        )
        if db:
            break
        try:
            do_popen(
                create_db_cmd(**config),
                err_msg=f"Failed to create database {db_name}",
                quiet=True,
            )
        except Exception as e:
            stdout("Current databases: " + str(e))
            time.sleep(10)
            continue
    result = do_popen(
        list_cmd(**config), err_msg="Failed to list services.", _json=True
    )
    return result[0].get("connection_info").get("pg")[0]


def create_pool(config) -> str:
    direct_uri, pool_uri = check_if_exist()
    if pool_uri:
        return pool_uri

    result = do_popen(
        pool_list_cmd(**config), err_msg="Failed to list pool.", _json=True
    )
    if len(result):
        return result[0].get("connection_uri")
    do_popen(pool_create_cmd(**config), err_msg="Failed to create pool.")
    time.sleep(1)
    result = do_popen(
        pool_list_cmd(**config), err_msg="Failed to list pool.", _json=True
    )
    return result[0].get("connection_uri")


def clear_pre_existing_pool(config):
    try:
        do_popen(pool_delete_cmd(**config), err_msg="Skipping delete pool.", quiet=True)
    except Exception as e:
        stdout(f"quiet error: {str(e)}")
    time.sleep(10)


def set_heroku_env(
    config, pool_uri=None, direct_uri=None, add_vars=None, app_name=None
):
    add_vars = add_vars or {}
    assert pool_uri or add_vars or direct_uri
    if app_name or os.environ.get("HEROKU_APP_NAME"):
        if pool_uri or direct_uri:
            parsed_uri = urlparse(pool_uri or direct_uri)
            db_env_uri = urlunparse(
                (
                    parsed_uri.scheme,
                    f"{{user}}:{{password}}@{parsed_uri.hostname}:{parsed_uri.port}",
                    parsed_uri.path,
                    parsed_uri.params,
                    parsed_uri.query,
                    parsed_uri.fragment,
                )
            )
            to_json = {
                "AIVEN_APP_NAME": config.get("app_name"),
                "AIVEN_PG_USER": parsed_uri.username,  # same in pool and direct db connection
                "AIVEN_PG_PASSWORD": parsed_uri.password,  # same in pool and direct db connection
                **add_vars,
            }

            # createdb runs first
            if direct_uri:
                stdout(
                    f"Setting AIVEN_DATABASE_DIRECT_URL via 'direct_uri': {sanitize_output(db_env_uri)}"
                )
                to_json = {
                    **to_json,
                    "AIVEN_DATABASE_DIRECT_URL": db_env_uri,
                    "AIVEN_DIRECT_PG_PORT": parsed_uri.port,
                }

            # final step creating the pool, and setting the pool uri to connect to via AIVEN_DATABASE_URL
            if pool_uri:
                to_json = {
                    **to_json,
                    "AIVEN_DATABASE_URL": db_env_uri,
                    "AIVEN_PG_PORT": parsed_uri.port,
                }

            data = json.dumps(to_json)
        else:
            to_json = add_vars
            data = json.dumps(add_vars)
        result = requests.patch(
            f"https://api.heroku.com/apps/{app_name or os.environ.get('HEROKU_APP_NAME')}/config-vars",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/vnd.heroku+json; version=3",
                "Authorization": f"Bearer {os.environ.get('HEROKU_API_KEY')}",
            },
        )
        if result.status_code in (200, 201, 202, 206):
            stdout(
                f"Configured Heroku application config vars for HEROKU_APP_NAME: {config.get('app_name')}"
            )
            stdout(
                f"AIVEN env vars set: {to_json.keys()} on {app_name or os.environ.get('HEROKU_APP_NAME')}"
            )
        else:
            stderr(f"Failed to set AIVEN_APP_NAME, DATABASE_URL : {result.content}")
            exit(8)


def remove_postgres_addon():
    # remove the hobby tier postgres, so we can unset DATABASE_URL
    try:
        output = do_popen(f"heroku addons --app {config.get('app_name')}")
        if "postgres" in output.lower():
            run(
                "heroku addons:destroy heroku-postgresql:hobby-dev --app $HEROKU_APP_NAME --confirm $HEROKU_APP_NAME"
            )
    except Exception as e:
        stdout(f"quiet error: {str(e)}")


def create_aiven_db(create_config, review_app_config):
    result = do_popen(
        list_cmd(**review_app_config),
        err_msg=f"Failed to list services for: {review_app_config.get('project')} {str(review_app_config.get('app_name'))}.",
        _json=True,
    )
    if not len(result):
        stdout("Attempting to start new db service instance.")
        do_popen(
            service_create_cmd(**create_config),
            err_msg=f"Unable to create service: {review_app_config.get('app_name')}",
        )
    else:
        stdout("Pre-existing db service found.")


@task
def service_create_aiven_db(ctx):
    """
    export AIVEN_PROJECT_NAME="###"
    export HEROKU_PARENT_APP_NAME=testing
    export AIVEN_APP_NAME='hello'
    export AIVEN_AUTH_TOKEN="..."

    ./manage.py release_phase.py --run-locally
    """
    if is_review_app():
        direct_uri, pool_uri = check_if_exist()
        if direct_uri:
            return direct_uri
        direct_uri, pool_uri = _get_and_clear_empty_db()
        if direct_uri and pool_uri:
            set_heroku_env(config, pool_uri=pool_uri)
            set_heroku_env(config, direct_uri=direct_uri)
            try:
                do_popen(
                    add_tag_cmd(
                        **{
                            **config,
                            "key_value": f"HEROKU_APP_NAME={os.environ.get('HEROKU_APP_NAME', 'not_found')}",
                        }
                    ),
                    err_msg=f"Unable to create service: {config.get('app_name')}",
                )
            except Exception as e:
                stdout(str(e))
        else:
            review_app_config = {**config, **service_config}
            create_aiven_db(review_app_config, config)
            wait_for_service(review_app_config)
            try:
                do_popen(
                    add_tag_cmd(
                        **{
                            **review_app_config,
                            "key_value": f"HEROKU_APP_NAME={os.environ.get('HEROKU_APP_NAME', 'not_found')}",
                        }
                    ),
                    err_msg=f"Unable to create service: {review_app_config.get('app_name')}",
                )
            except Exception as e:
                stdout(str(e))


def is_review_app():
    is_ra = os.environ.get("IS_REVIEW_APP", False).lower() == "true"
    if not is_ra:
        stdout(f"IS_REVIEW_APP: {is_ra}")
    return is_ra


@task
def do_get_staging_db_url(ctx):
    get_staging_db_url()


def get_staging_db_url() -> (str, str):

    # orignal heroku staging db
    original = (
        check_output(
            f"{heroku_bin} config:get DATABASE_URL --app {staging_app_name}".split()
        )
        .strip()
        .decode("utf8")
    )
    # if the original heorku db has been detached try to look for AIVEN_DATABASE_DIRECT_URL in staging
    staging_aiven = (
        check_output(
            f"{heroku_bin} config:get AIVEN_DATABASE_DIRECT_URL --app {staging_app_name}".split()
        )
        .strip()
        .decode("utf8")
    )

    # ensure no_buffer to hide stdout
    out, errs = do_popen(
        f"{heroku_bin} config:get AIVEN_PG_PASSWORD --app {staging_app_name}",
        no_buffer=True,
    )
    if errs:
        stdout(errs)
    staging_aiven_pg_password = out.strip()
    staging_aiven_db_url = ""
    if staging_aiven:
        staging_aiven_pg_user = (
            check_output(
                f"{heroku_bin} config:get AIVEN_PG_USER --app {staging_app_name}".split()
            )
            .strip()
            .decode("utf8")
        )
        staging_aiven_db_url = staging_aiven.format(
            user=staging_aiven_pg_user,
            password=staging_aiven_pg_password,
        )
    return original, staging_aiven_db_url


@task
def create_db_task(ctx):
    if is_review_app():
        direct_uri, pool_uri = check_if_exist()
        if pool_uri:
            return pool_uri
        database_uri = create_db(config)
        set_heroku_env(config, direct_uri=database_uri)


@task
def create_pool_uri_and_set_env(ctx):
    if is_review_app():
        direct_uri, pool_uri = check_if_exist()
        if pool_uri:
            return pool_uri

        pool_uri = create_pool(config)
        set_heroku_env(config, pool_uri=pool_uri)
        stdout(sanitize_output(pool_uri))
        try:
            do_popen(
                add_tag_cmd(
                    **{
                        **config,
                        "key_value": f"HEROKU_APP_NAME={os.environ.get('HEROKU_APP_NAME', 'not_found')}",
                    }
                ),
                err_msg=f"Unable to create service: {config.get('app_name')}",
            )
        except Exception as e:
            stdout(str(e))
        return sanitize_output(pool_uri)


def dump_staging_to_db(review_app_env=None):
    if not review_app_env:
        review_app_env = get_heroku_env()
    # review app database
    review_app_aiven_db_url = review_app_env.get("AIVEN_DATABASE_DIRECT_URL").format(
        user=review_app_env.get("AIVEN_PG_USER", "failed_to_get_review_app_user"),
        password=review_app_env.get(
            "AIVEN_PG_PASSWORD", "failed_to_get_review_app_pass"
        ),
    )
    original, staging_aiven_db_url = get_staging_db_url()
    if staging_aiven_db_url:
        stdout(
            "Running 'pg_dump'...\n"
            f"FROM staging AIVEN_DATABASE_DIRECT_URL {sanitize_output(staging_aiven_db_url)}\n"
            f"TO review app aiven db: {sanitize_output(review_app_aiven_db_url)}"
        )
    else:
        stdout(
            "Running `pg_dump`...\n"
            f"FROM heroku. Run 'heroku config:get DATABASE_URL --app {staging_app_name}' for more info'"
            f"'\nTO review app aiven db: {sanitize_output(original or staging_aiven_db_url)}"
        )
    shared_app_name = config.get("shared_resource_app")
    shared_result = get_heroku_env(shared_app_name)
    lock = shared_result.get("AIVEN_PG_DUMP_LOCK", "")
    if not lock:
        set_heroku_env(
            {**config, "app_name": shared_app_name},
            add_vars={"AIVEN_PG_DUMP_LOCK": "1"},
            app_name=shared_app_name,
        )
        with Popen(
            f"sh -c 'pg_dump --no-privileges --no-owner {original or staging_aiven_db_url} | psql {review_app_aiven_db_url}'",
            stdin=None,
            stdout=None,
            stderr=None,
            close_fds=True,
            shell=True,
        ) as p:
            with timeout(1800):
                while p.poll() is None:
                    stdout(
                        f"{datetime.now().isoformat()} pg_dump pid: '{p.pid}' still exists"
                    )
                    time.sleep(20)
        set_heroku_env(
            {**config, "app_name": shared_app_name},
            add_vars={"AIVEN_PG_DUMP_LOCK": ""},
            app_name=shared_app_name,
        )


@task
def setup_review_app_database(ctx):
    if is_review_app():
        review_app_env = get_heroku_env()
        direct_uri, pool_uri = check_if_exist()
        if direct_uri:
            return direct_uri
        if not review_app_env.get(
            "AIVEN_DATABASE_DIRECT_URL"
        ) or "pool" in review_app_env.get("AIVEN_DATABASE_DIRECT_URL"):
            stderr(
                f"Failed to get AIVEN_DATABASE_DIRECT_URL: {sanitize_output(review_app_env.get('AIVEN_DATABASE_DIRECT_URL', ''))}"
            )
            exit(6)
        try:
            if (
                not review_app_env.get("REVIEW_APP_HAS_STAGING_DB", "")
                or review_app_env.get("REVIEW_APP_HAS_STAGING_DB", "") == "False"
            ):
                try:
                    time.sleep(10)
                    set_heroku_env(
                        config, add_vars={"REVIEW_APP_HAS_STAGING_DB": "False"}
                    )
                    dump_staging_to_db(review_app_env)
                    set_heroku_env(
                        config, add_vars={"REVIEW_APP_HAS_STAGING_DB": "True"}
                    )
                except ReferenceError as e:
                    set_heroku_env(config, add_vars={"REVIEW_APP_HAS_STAGING_DB": ""})
            else:
                stdout(
                    f"REVIEW_APP_HAS_STAGING_DB: {review_app_env.get('REVIEW_APP_HAS_STAGING_DB', '')}"
                )
        except invoke.Failure:
            stderr("errors encountered when restoring DB")
            exit(8)


@task
def aiven_teardown_db(ctx):
    stdout(f"Aiven: Attempting to teardown service. {config.get('app_name')}")
    do_popen(pool_delete_cmd(**config), err_msg="Failed to delete pool.")
    stdout(f'Service: {config.get("app_name")} postgres pool deleted')
    do_popen(
        service_disable_protection_cmd(**config),
        f"Unable to disable protection: {config.get('app_name')}",
    )
    do_popen(
        service_terminate_cmd(**config),
        f"Unable to teardown service: {config.get('app_name')}",
    )
    stdout(f'Service: {config.get("project")} deleted.')


def _get_and_clear_empty_db():
    shared_app_name = config.get("shared_resource_app")
    stdout(f"LOOKING FOR AN EMPTY DB IN SHARED APP: {shared_app_name}")
    shared_result = get_heroku_env(shared_app_name)
    direct_uri, pool_uri = (shared_result.get("AIVEN_EMPTY_DB", "\n") or "\n").split(
        "\n"
    )
    stdout(sanitize_output(f'DIRECT_URI: {direct_uri or "Found Nothing"}'))
    stdout(sanitize_output(f'POOL_URI: {pool_uri or "Found Nothing"}'))
    if direct_uri != "" and pool_uri != "":
        stdout("FOUND AIVEN_EMPTY_DB")
        set_heroku_env(
            {**config, "app_name": shared_app_name},
            add_vars={"AIVEN_EMPTY_DB": ""},
            app_name=shared_app_name,
        )
        try:
            do_popen(
                replace_tag_cmd(
                    **{
                        **config,
                        "key_value": f"HEROKU_APP_NAME={os.environ.get('HEROKU_APP_NAME', 'not_found')}",
                    }
                ),
                err_msg=f"Unable to replace HEROKU_APP_NAME tag: {config.get('app_name')}",
            )
        except Exception as e:
            stdout(str(e))
        stdout("RETURNING THE UNMIGRATED DB")
        return direct_uri, pool_uri
    else:
        stdout(sanitize_output(f"DIRECT_URI POOL_URI: {direct_uri} {pool_uri}"))
        return None, None


@task
def get_and_clear_empty_db(ctx):
    direct_url, pool_url = _get_and_clear_empty_db()
    stdout(direct_url, pool_url)


def _create_empty_db():
    shared_app_name = config.get("shared_resource_app")
    shared_result = get_heroku_env(shared_app_name)
    direct_url, pool_url = (shared_result.get("AIVEN_EMPTY_DB", "\n") or "\n").split(
        "\n"
    )
    stdout(f"shared_app_name: {shared_app_name}")
    stdout(f"direct_url: {sanitize_output(direct_url)}")
    stdout(f"pool_url: {sanitize_output(pool_url)}")
    if direct_url and pool_url:
        stdout("AIVEN_EMPTY_DB exists. Skipping")
    else:
        db_config = {
            **config,
            "app_name": f"pm-{str(uuid4()).lower()}",
        }
        next_config = {
            **db_config,
            **service_config,
        }

        # do not create if not aiven_empty_db
        # and one of the dbs is not in the app urls
        propertymeld_apps = json.loads(
            do_popen(
                "heroku pipelines:info propertymeld --json", "Unable to list services"
            )
        )["apps"]
        current_apps = set()
        for app in propertymeld_apps:
            app_name = app.get("name")
            if app_name not in PROTECTED_AIVEN_APP_NAMES:
                current_apps.add(app_name)
        current_database_uris = set()
        for service in current_apps:
            cmd = ["heroku", "config:get", "AIVEN_DATABASE_URL", "--app", service]
            uri = do_popen(" ".join(cmd), "Unable to list services")
            current_database_uris.add(uri)
        result = do_popen(
            list_cmd(**{**config, "app_name": ""}),
            err_msg=f"Failed to list services for: {config.get('project')} {str(config.get('app_name'))}.",
            _json=True,
        )
        existing_dbs = set([x.get("service_name") for x in result])
        stdout(f"existing_dbs: {existing_dbs}")
        assigned_dbs = set(
            [
                x.split("@")[1].split("-propertymeld-review-app")[0]
                for x in current_database_uris
                if x.strip()
            ]
        )
        stdout(f"assigned_dbs: {assigned_dbs}")
        stdout(f"diffed dbs: {existing_dbs.difference(assigned_dbs)}")
        if not len(existing_dbs.difference(assigned_dbs)):
            create_aiven_db(next_config, db_config)
            wait_for_service(next_config)
        else:
            existing_unassigned_db = existing_dbs.difference(assigned_dbs).pop()
            db_config["app_name"] = existing_unassigned_db
        try:
            do_popen(
                add_tag_cmd(
                    **{
                        **next_config,
                        "key_value": f"HEROKU_APP_NAME={os.environ.get('HEROKU_APP_NAME', 'not_found')}",
                    }
                ),
                err_msg=f"Unable to create service: {next_config.get('app_name')}",
            )
        except Exception as e:
            stdout(str(e))
        database_uri = create_db(db_config)
        # review_app_env.get("AIVEN_DATABASE_DIRECT_URL")
        review_app_env = {"AIVEN_DATABASE_DIRECT_URL": database_uri}
        dump_staging_to_db(review_app_env)
        pool_uri = create_pool(db_config)
        aiven_empty_db = f"{database_uri}\n{pool_uri}"
        set_heroku_env(
            {}, add_vars={"AIVEN_EMPTY_DB": aiven_empty_db}, app_name=shared_app_name
        )


@task
def create_empty_db(ctx):
    _create_empty_db()


@click.command()
@click.option("--queue_empty_db", required=False, is_flag=True)
def command(queue_empty_db):
    if queue_empty_db:
        _create_empty_db()
