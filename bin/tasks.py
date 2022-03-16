import sys
from io import StringIO
from typing import AnyStr, Union
from uuid import uuid4

import invoke.exceptions
from invoke import task, run
import os
import requests
import time
import json
import string
import re
from urllib.parse import urlparse, urlunparse
import random
from subprocess import Popen, PIPE

stdout = lambda x: sys.stdout.write(x + "\n")
stderr = lambda x: sys.stderr.write(x + "\n")


db_name = os.environ.get("AIVEN_DBNAME", "propertymeld")
staging_app_name = os.environ.get("STAGING_APP_NAME", "property-meld-staging")

wait_cmd = """avn --auth-token {auth_token} service wait --project {project} {app_name}""".format
list_cmd = """avn --auth-token {auth_token} service list --project {project} {app_name} --json""".format
list_db_cmd = """avn --auth-token {auth_token} service database-list --project {project} {app_name} --json""".format
service_create_cmd = """avn --auth-token {auth_token} service create --project {project} --service-type {service_type} --plan {plan} --cloud {cloud} -c {pg_version} {app_name}""".format
service_terminate_cmd = """avn --auth-token {auth_token} service terminate --force --project {project} {app_name}""".format
delete_db_cmd = f"""avn --auth-token {{auth_token}} service database-delete --project {{project}} --dbname {db_name} {{app_name}}""".format
create_db_cmd = f"""avn --auth-token {{auth_token}} service database-create --project {{project}} --dbname {db_name} {{app_name}}""".format
pool_create_cmd = f"""avn --auth-token {{auth_token}} service connection-pool-create {{app_name}} --project {{project}} --dbname defaultdb --username avnadmin --pool-name {db_name}-pool --pool-size 50 --json""".format
pool_list_cmd = """avn --auth-token {auth_token} service connection-pool-list {app_name} --verbose --project {project} --json""".format
pool_delete_cmd = f"""avn --auth-token {{auth_token}} service connection-pool-delete {{app_name}} --project {{project}} --pool-name {db_name}-pool --json""".format

assert os.environ.get("AIVEN_PROJECT_NAME")
assert os.environ.get("AIVEN_AUTH_TOKEN")
assert os.environ.get("HEROKU_APP_NAME")

config = {
    "auth_token": f'"{os.environ.get("AIVEN_AUTH_TOKEN")}"',  # set in heroku staging env vars in dashboard "reveal config vars", and aiven console
    "app_name": f"{os.environ.get('HEROKU_APP_NAME')}",
    "project": os.environ.get("AIVEN_PROJECT_NAME"),
    "shared_resource_app": os.environ.get("AIVEN_SHARED_RESOURCE_APP"),
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
    if os.environ.get("HEROKU_APP_NAME"):
        url = f"https://api.heroku.com/apps/{env or os.environ.get('HEROKU_APP_NAME')}/config-vars"
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


heroku_bin = os.environ.get("HEROKU_BIN", get_heroku_env().get("HEROKU_BIN", ""))
stdout(f"HEROKU_BIN: {heroku_bin}")

if not heroku_bin:
    stderr("heroku_bin not set")
    exit(1)


def check_if_exist():
    results = get_heroku_env()
    env_vars = [
        "AIVEN_APP_NAME",
        "AIVEN_DATABASE_DIRECT_URL",
        "AIVEN_PG_USER",
        "AIVEN_DIRECT_PG_PORT",
        "AIVEN_PG_PASSWORD",
    ]
    if all([x in results for x in env_vars]):
        stdout("Pre-existing db service found.")
        return results.get("AIVEN_DATABASE_DIRECT_URL"), results.get("AIVEN_DATABASE_URL", "")


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
        return ''.join(out_lines), ''.join(err_lines)
    else:
        process = Popen(cmd, stderr=PIPE, stdout=PIPE, env=os.environ)
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
    while not all([x.get("state") == "running" for x in result[0].get("node_states")]):
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


def set_heroku_env(config, pool_uri=None, direct_uri=None, add_vars: dict = {}, app_name=None):
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
                stdout(f"Setting AIVEN_DATABASE_DIRECT_URL via 'direct_uri': {sanitize_output(db_env_uri)}")
                to_json = {**to_json, "AIVEN_DATABASE_DIRECT_URL": db_env_uri, "AIVEN_DIRECT_PG_PORT": parsed_uri.port}

            # final step creating the pool, and setting the pool uri to connect to via AIVEN_DATABASE_URL
            if pool_uri:
                to_json = {**to_json, "AIVEN_DATABASE_URL": db_env_uri, "AIVEN_PG_PORT": parsed_uri.port}


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
            stdout(f"Configured Heroku application config vars for HEROKU_APP_NAME: {config.get('app_name')}")
            stdout(f"AIVEN env vars set: {to_json.keys()}")
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
        else:
            review_app_config = {**config, **service_config}
            create_aiven_db(review_app_config, config)
            wait_for_service(review_app_config)



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
    original = run(
        f"{heroku_bin} config:get DATABASE_URL --app {staging_app_name}"
    ).stdout.strip()
    # if the original heorku db has been detached try to look for AIVEN_DATABASE_DIRECT_URL in staging
    staging_aiven = run(
        f"{heroku_bin} config:get AIVEN_DATABASE_DIRECT_URL --app {staging_app_name}"
    ).stdout.strip()

    # ensure no_buffer to hide stdout
    out, errs = do_popen(
        f"{heroku_bin} config:get AIVEN_PG_PASSWORD --app {staging_app_name}", no_buffer=True
    )
    if errs:
        stdout(errs)
    staging_aiven_pg_password = out.strip()
    staging_aiven_db_url = ''
    if staging_aiven:
        staging_aiven_pg_user = run(
            f"{heroku_bin} config:get AIVEN_PG_USER --app {staging_app_name}"
        ).stdout.strip()
        staging_aiven_db_url = staging_aiven.format(
            user=staging_aiven_pg_user,
            password=staging_aiven_pg_password,
        )
    return original, staging_aiven_db_url

@task
def create_db_task(ctx):
    if is_review_app():
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
        return sanitize_output(pool_uri)


def dump_staging_to_db(review_app_env):
    # review app database
    review_app_aiven_db_url = review_app_env.get("AIVEN_DATABASE_DIRECT_URL").format(
        user=review_app_env.get("AIVEN_PG_USER", "failed_to_get_review_app_user"),
        password=review_app_env.get("AIVEN_PG_PASSWORD", "failed_to_get_review_app_pass")
    )
    original, staging_aiven_db_url = get_staging_db_url()
    if staging_aiven_db_url:
        stdout(
            f"Running 'pg_dump'...\n"
            f"FROM staging AIVEN_DATABASE_DIRECT_URL {sanitize_output(staging_aiven_db_url)}\n"
            f"TO review app aiven db: {sanitize_output(review_app_aiven_db_url)}"
        )
    else:
        stdout(
            f"Running `pg_dump`...\n"
            f"FROM heroku. Run 'heroku config:get DATABASE_URL --app {staging_app_name}' for more info'"
            f"'\nTO review app aiven db: {sanitize_output(original or staging_aiven_db_url)}"
        )
    result = run(
        f"pg_dump --no-privileges --no-owner {original or staging_aiven_db_url} | psql {review_app_aiven_db_url}"
    )
    return result


@task
def setup_review_app_database(ctx):
    if is_review_app():
        review_app_env = get_heroku_env()
        direct_uri, pool_uri = check_if_exist()
        if direct_uri:
            return direct_uri
        if not review_app_env.get("AIVEN_DATABASE_DIRECT_URL") or "pool" in review_app_env.get(
            "AIVEN_DATABASE_DIRECT_URL"
        ):
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
                    result = dump_staging_to_db(review_app_env)
                    if result.return_code:
                        stderr(result.stderr)
                        exit(7)
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
    do_popen(service_terminate_cmd(**config), f"Unable to teardown service: {config.get('app_name')}")
    stdout(f'Service: {config.get("project")} deleted.')


def _get_and_clear_empty_db():
    shared_app_name = config.get("shared_resource_app")
    shared_result = get_heroku_env(config.get("shared_resource_app"))
    direct_url, pool_url = shared_result.get('AIVEN_EMPTY_DB', '\n').split('\n')
    if direct_url and pool_url:
        stdout('FOUND AIVEN_EMPTY_DB')
        set_heroku_env({**config, "app_name": shared_app_name}, add_vars={"AIVEN_EMPTY_DB": ""}, app_name=shared_app_name)
    stdout('RETURNING THE UNMIGRATED DB')
    return direct_url, pool_url

@task
def get_and_clear_empty_db(ctx):
    direct_url, pool_url = _get_and_clear_empty_db()
    stdout(direct_url, pool_url)


def _create_empty_db():
    shared_app_name = config.get("shared_resource_app")
    shared_result = get_heroku_env(shared_app_name)
    direct_url, pool_url = (shared_result.get('AIVEN_EMPTY_DB', '\n') or '\n').split('\n')
    if direct_url and pool_url:
        stdout(
            f"AIVEN_EMPTY_DB exists. Skipping"
        )
    else:
        db_config = {
            **config,
            "app_name": f'pm-{str(uuid4()).lower()}',
        }
        next_config = {
            **db_config,
            **service_config,
        }
        create_aiven_db(next_config, db_config)
        wait_for_service(next_config)
        database_uri = create_db(db_config)
        # review_app_env.get("AIVEN_DATABASE_DIRECT_URL")
        review_app_env = {
            "AIVEN_DATABASE_DIRECT_URL": database_uri
        }
        result = dump_staging_to_db(review_app_env)
        if result.return_code:
            stderr(result.stderr)
            exit(7)
        pool_uri = create_pool(db_config)
        aiven_empty_db = f'{database_uri}\n{pool_uri}'
        set_heroku_env({}, add_vars={"AIVEN_EMPTY_DB": aiven_empty_db}, app_name=shared_app_name)


@task
def create_empty_db(ctx):
    _create_empty_db()