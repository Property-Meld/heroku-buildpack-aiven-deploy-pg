import sys
from typing import AnyStr, Union
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
stdout = lambda x: sys.stdout.write(x + '\n')
stderr = lambda x: sys.stderr.write(x + '\n')


db_name = os.environ.get("DBNAME", "propertymeld")
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
assert os.environ.get('HEROKU_APP_NAME')

config = {
    "auth_token": f'"{os.environ.get("AIVEN_AUTH_TOKEN")}"',  # set in heroku staging env vars in dashboard "reveal config vars", and aiven console
    "app_name": f"{os.environ.get('HEROKU_APP_NAME')}",
    "project": os.environ.get("AIVEN_PROJECT_NAME"),
}
service_config = {
    "cloud": os.environ.get("AIVEN_CLOUD", "do-nyc"),
    "service_type": os.environ.get("AIVEN_SERVICE_TYPE", "pg"),
    "plan": os.environ.get("AIVEN_PLAN", "startup-4"),  # hobbyist does not support pooling
    "pg_version": os.environ.get("AIVEN_PG_VERSION", "pg_version=12"),
}


def get_heroku_env():
    if os.environ.get("HEROKU_APP_NAME"):
        result = requests.get(
            f"https://api.heroku.com/apps/{os.environ.get('HEROKU_APP_NAME')}/config-vars",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/vnd.heroku+json; version=3",
                "Authorization": f"Bearer {os.environ.get('HEROKU_API_KEY')}",
            },
        )
        if result.status_code in (200, 201, 202, 206):
            return result.json()
        else:
            stderr(f"Failed to set AIVEN_APP_NAME, DATABASE_URL : {result.content}")
            exit(8)
    return {}

heroku_bin = os.environ.get("HEROKU_BIN", get_heroku_env().get("HEROKU_BIN", ""))
print(f"HEROKU_BIN: {heroku_bin}")

if not heroku_bin:
    stderr('heroku_bin not set')
    exit(1)


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
        lines = []
        with process.stderr:
            for line in process.stderr:
                stdout(line)
                lines.append(line)
        process.wait()
        return ""
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


def set_heroku_env(config, pool_uri=None, add_vars: dict={}):
    assert (pool_uri or add_vars)
    if os.environ.get("HEROKU_APP_NAME"):
        if pool_uri:
            parsed_uri = urlparse(pool_uri)
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
                    "AIVEN_DATABASE_URL": db_env_uri,
                    "AIVEN_PG_USER": parsed_uri.username,
                    "AIVEN_PG_PORT": parsed_uri.port,
                    "AIVEN_PG_PASSWORD": parsed_uri.password,
                    **add_vars
                }
            data = json.dumps(to_json)
        else:
            to_json = add_vars
            data = json.dumps(add_vars)
        result = requests.patch(
            f"https://api.heroku.com/apps/{os.environ.get('HEROKU_APP_NAME')}/config-vars",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/vnd.heroku+json; version=3",
                "Authorization": f"Bearer {os.environ.get('HEROKU_API_KEY')}",
            },
        )
        if result.status_code in (200, 201, 202, 206):
            stdout(f"AIVEN_APP_NAME env var set {config.get('app_name')}")
            stdout(f"AIVEN_DATABASE_URL env var set {to_json.keys()}")
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


@task
def service_create_aiven_db(ctx):
    """
    export STAGING_DATABASE_URL=postgresql://###:###@127.0.0.1:5432/###
    export AIVEN_PROJECT_NAME="###"
    export HEROKU_PARENT_APP_NAME=testing
    export AIVEN_APP_NAME='hello'
    export AIVEN_AUTH_TOKEN="..."

    ./manage.py release_phase.py --run-locally
    """
    if is_review_app():
        results = get_heroku_env()
        env_vars = [
            "AIVEN_APP_NAME",
            "AIVEN_DATABASE_URL",
            "AIVEN_PG_USER",
            "AIVEN_PG_PORT",
            "AIVEN_PG_PASSWORD",
        ]
        if all([x in results for x in env_vars]):
            stdout("Pre-existing db service found.")
            return results.get("AIVEN_DATABASE_URL")
        result = do_popen(
            list_cmd(**config),
            err_msg=f"Failed to list services for: {config.get('project')} {str(config.get('app_name'))}.",
            _json=True,
        )
        if not len(result):
            stdout("Attempting to start new db service instance.")
            do_popen(
                service_create_cmd(**{**config, **service_config}),
                err_msg=f"Unable to create service: {config.get('app_name')}",
            )
            wait_for_service(config)
        else:
            stdout("Pre-existing db service found.")


def is_review_app():
    is_ra = os.environ.get("IS_REVIEW_APP", False).lower() == "true"
    if not is_ra:
        stdout(f'IS_REVIEW_APP: {is_ra}')
    return is_ra


@task
def create_db_task(ctx):
    if is_review_app():
        database_uri = create_db(config)
        set_heroku_env(config, pool_uri=database_uri)

@task
def create_pool_uri_and_set_env(ctx):
    if is_review_app():
        pool_uri = create_pool(config)
        set_heroku_env(config, pool_uri)
        stdout(sanitize_output(pool_uri))
        return sanitize_output(pool_uri)


@task
def setup_review_app_database(ctx):
    if is_review_app():
        results = get_heroku_env()
        if not results.get("AIVEN_DATABASE_URL"):
            stderr("Failed to set AIVEN_DATABASE_URL")
            exit(6)
        try:
            if not results.get('REVIEW_APP_HAS_STAGING_DB', ''):
                try:
                    time.sleep(10)
                    set_heroku_env(config, add_vars={'REVIEW_APP_HAS_STAGING_DB': 'False'})
                    aiven_db_url = results.get("AIVEN_DATABASE_URL").format(
                        user=results.get("AIVEN_PG_USER"),
                        password=results.get("AIVEN_PG_PASSWORD"),
                    )
                    run(
                        f"pg_dump --no-privileges --no-owner `{heroku_bin} config:get DATABASE_URL --app {staging_app_name}` | psql {aiven_db_url}"
                    )
                    set_heroku_env(config, add_vars={'REVIEW_APP_HAS_STAGING_DB': 'True'})
                except ReferenceError as e:
                    set_heroku_env(config, add_vars={'REVIEW_APP_HAS_STAGING_DB': ''})
        except invoke.Failure:
            stderr("errors encountered when restoring DB")

@task
def aiven_teardown_db(ctx):
    stdout(f"Aiven: Attempting to teardown service. {app_name}")
    do_popen(pool_delete_cmd(**config), err_msg="Failed to delete pool.")
    stdout(f'Service: {config.get("app_name")} postgres pool deleted')
    do_popen(service_terminate_cmd(**config), f"Unable to teardown service: {app_name}")
    stdout(f'Service: {config.get("project")} deleted.')