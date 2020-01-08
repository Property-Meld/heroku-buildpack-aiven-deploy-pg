from typing import AnyStr, Union

from circleclient import circleclient
import invoke.exceptions
import hashlib

from django.db import OperationalError
from invoke import task, run
import os
import requests
import time
import json
import string
import re
from urllib.parse import urlparse, urlunparse
from django.utils.crypto import random
from subprocess import Popen, PIPE
import logging

l = logging.getLogger(__name__)
l.setLevel(logging.DEBUG)

PRODUCTION = "production"
STAGING = "staging"
PRESTAGING = "prestaging"

HEROKU_PRESTAGING_APP = "property-meld-prestaging"
HEROKU_STAGING_APP = "property-meld-staging"
HEROKU_PROD_APP = "still-waters-9915"


@task
def deploy(
    ctx,
    environment=STAGING,
    migrate=False,
    branch=None,
    no_maint=False,
    fake_migrations=False,
):
    if environment == PRODUCTION:
        heroku_app_name = HEROKU_PROD_APP
        git_remote = "heroku"
    elif environment == PRESTAGING:
        heroku_app_name = HEROKU_PRESTAGING_APP
        git_remote = "prestaging"
    else:
        heroku_app_name = HEROKU_STAGING_APP
        git_remote = "staging"

    branch = branch or "master"

    if environment == STAGING:
        branch = "develop:master"

    publish_static(environment)

    if migrate:
        backup_db(heroku_app_name)

        if no_maint is False:
            toggle_heroku_maintenance(heroku_app_name, True)

        publish_python(git_remote, branch)

        if fake_migrations:
            run_psql(
                heroku_app_name,
                "TRUNCATE django_migrations; ALTER TABLE django_content_type ADD name VARCHAR(50);",
            )
            migrate_db(heroku_app_name, "--fake")
        else:
            migrate_db(heroku_app_name)

        if no_maint is False:
            toggle_heroku_maintenance(heroku_app_name, False)
    else:
        publish_python(git_remote, branch)


def publish_static(environment):
    run("npm install", pty=True)
    run("NODE_ENV=production gulp rev --{}".format(environment), pty=True)
    run("NODE_ENV=production gulp publish --{}".format(environment), pty=True)

    hasher = hashlib.sha256()
    with open("rev-manifest.json", "rb") as revManifest:
        hasher.update(revManifest.read())
    with open("version.json", "w") as version:
        json.dump({"version": hasher.hexdigest()}, version)

    try:
        run("git add rev-manifest.json version.json", pty=True)
        run('git commit -m "Updated rev-manifest.json and version.json"', pty=True)
    except invoke.exceptions.Failure:
        print(
            "Failed to add version and manifest. Most likely no changes were made during `gulp rev`. Continuing..."
        )


def backup_db(heroku_app_name):
    run("heroku pg:backups:capture --app {}".format(heroku_app_name), pty=True)


def toggle_heroku_maintenance(heroku_app_name, on):
    run(
        "heroku maintenance:{} --app {}".format("on" if on else "off", heroku_app_name),
        pty=True,
    )


def publish_python(git_remote, branch):
    run("git push {} {}".format(git_remote, branch), pty=True)


def migrate_db(heroku_app_name, options=""):
    run(
        "heroku run --app {} python manage.py migrate {}".format(
            heroku_app_name, options
        ),
        pty=True,
    )


def run_psql(heroku_app_name, psql):
    run('echo "{}" | heroku pg:psql --app {}'.format(psql, heroku_app_name), pty=True)


@task
def custom_tests(ctx, functional=False, screenshots=False, tests=None, platform=None):
    username = "Property-Meld"
    project = "propertymeld"
    branch = run("git rev-parse --abbrev-ref HEAD", hide=True).stdout.strip()
    circle_token = os.getenv("CIRCLE_TOKEN")

    build_parameters = {"CUSTOM_TESTS": "true", "SAUCE_PLATFORM_INDEX": platform}
    if functional:
        build_parameters["RUN_FUNCTIONAL_TESTS"] = "true"
    if screenshots:
        build_parameters["GENERATE_SCREENSHOTS"] = "true"
    if tests:
        build_parameters["CUSTOM_TEST_NAMES"] = tests

    client = circleclient.CircleClient(circle_token)
    client.build.trigger(username, project, branch, **build_parameters)


@task
def restore_prop_to_staging(ctx):
    prod_db_url = "$(heroku pg:backups public-url --app {})".format(HEROKU_PROD_APP)
    run(
        "heroku pg:backups restore {production_url} DATABASE_URL --app {staging_app}".format(
            production_url=prod_db_url, staging_app=HEROKU_STAGING_APP
        )
    )


PYLINT_CMD = 'pylint {} `find -name "*.py" -not -path "./.env/*" -not -path "./node_modules/*" -not -path "*/migrations/*" {}`'


@task
def pylint(ctx, j=1):
    circle = os.getenv("CIRCLECI")

    run(
        PYLINT_CMD.format(
            "-j {}".format(j),
            " | circleci tests split --split-by=filesize" if circle else "",
        )
    )


@task
def get_migration_manifest(ctx):
    migrations = sorted(run('bash -c "echo */{**,}/migrations/*.py"').stdout.split())
    print(migrations)

    for migration in migrations:
        with open(migration, "rb") as file:
            data = file.read()
            print(migration, hashlib.sha256(data).hexdigest())


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
                l.info(line)
                lines.append(line)
        process.wait()
        return ""
    else:
        process = Popen(cmd, stderr=PIPE, stdout=PIPE, env=os.environ)
        stdout, stderr = process.communicate()
    errcode = process.returncode
    if quiet:
        return errcode
    if errcode:
        l.error(sanitize_output(" ".join(cmd + [stderr.decode("utf8")])))
        raise exc(err_msg)
    l.info(sanitize_output(stdout.decode("utf8")))
    if _json:
        return json.loads(stdout.decode("utf8"))
    return sanitize_output(stdout.decode("utf8"))


wait_cmd = """avn --auth-token {auth_token} service wait --project {project} {app_name}""".format
list_cmd = """avn --auth-token {auth_token} service list --project {project} {app_name} --json""".format
list_db_cmd = """avn --auth-token {auth_token} service database-list --project {project} {app_name} --json""".format
service_create_cmd = """avn --auth-token {auth_token} service create --project {project} --service-type {service_type} --plan {plan} --cloud {cloud} -c {pg_version} {app_name}""".format
pool_delete_cmd = """avn --auth-token {auth_token} service connection-pool-delete {app_name} --project {project} --pool-name propertymeld-pool --json""".format
delete_db_cmd = """avn --auth-token {auth_token} service database-delete --project {project} --dbname propertymeld {app_name}""".format
create_db_cmd = """avn --auth-token {auth_token} service database-create --project {project} --dbname propertymeld {app_name}""".format
pool_create_cmd = """avn --auth-token {auth_token} service connection-pool-create {app_name} --project {project} --dbname propertymeld --username avnadmin --pool-name propertymeld-pool --pool-size 50 --json""".format
pool_list_cmd = """avn --auth-token {auth_token} service connection-pool-list {app_name} --verbose --project {project} --json""".format

config = {
    "auth_token": f'"{os.environ.get("AIVEN_AUTH_TOKEN")}"',  # set in heroku staging env vars in dashboard "reveal config vars", and aiven console
    "app_name": f"{os.environ.get('HEROKU_APP_NAME', ''.join(random.choices(string.ascii_lowercase, k=14)))}",
    "project": os.environ.get("AIVEN_PROJECT_NAME", "propertymeld-f3df"),
}
service_config = {
    "cloud": "do-nyc",
    "service_type": "pg",
    "plan": "startup-4",  # hobbyist does not support pooling
    "pg_version": "pg_version=11",
}


def wait_for_service(config):
    l.info("Aiven: Waiting for db instance status to be in 'running' state.")
    do_popen(
        wait_cmd(**config),
        err_msg=f"Had difficulties waiting: {config.get('app_name')}",
        no_buffer=True,
    )
    l.info("Aiven: new db instance created.")
    result = do_popen(
        list_cmd(**config),
        err_msg=f"Failed to list services for: {config.get('project')} {str(config.get('app_name'))}.",
        _json=True,
    )
    l.info(sanitize_output(str(result)))
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
        l.info("Trying to create db.")
        db = "propertymeld" in do_popen(
            list_db_cmd(**config),
            err_msg="Error while listing databases in service.",
            _json=True,
        )
        if db:
            break
        try:
            do_popen(
                create_db_cmd(**config),
                err_msg="Failed to create database propertymeld.",
                quiet=True,
            )
        except Exception as e:
            l.info("Current databases: " + str(e))
            time.sleep(15)
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
        l.info(f"quiet error: {str(e)}")
    time.sleep(10)


def set_heroku_env(config, pool_uri=None, add_vars: dict=None):
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
            l.info(f"AIVEN_APP_NAME env var set {config.get('app_name')}")
            l.info(f"AIVEN_DATABASE_URL env var set {to_json.keys()}")
        else:
            l.error(f"Failed to set AIVEN_APP_NAME, DATABASE_URL : {result.content}")
            exit(8)


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
            l.error(f"Failed to set AIVEN_APP_NAME, DATABASE_URL : {result.content}")
            exit(8)
    return {}


def remove_postgres_addon():
    # remove the hobby tier postgres, so we can unset DATABASE_URL
    try:
        output = do_popen(f"heroku addons --app {config.get('app_name')}")
        if "postgres" in output.lower():
            run(
                "heroku addons:destroy heroku-postgresql:hobby-dev --app $HEROKU_APP_NAME --confirm $HEROKU_APP_NAME"
            )
    except Exception as e:
        l.info(f"quiet error: {str(e)}")


def standup_aiven_db() -> str:
    """
    export STAGING_DATABASE_URL=postgresql://###:###@127.0.0.1:5432/###
    export AIVEN_PROJECT_NAME="###"
    export HEROKU_PARENT_APP_NAME=testing
    export AIVEN_APP_NAME='hello'
    export AIVEN_AUTH_TOKEN="..."

    ./manage.py release_phase.py --run-locally
    """
    results = get_heroku_env()
    env_vars = [
        "AIVEN_APP_NAME",
        "AIVEN_DATABASE_URL",
        "AIVEN_PG_USER",
        "AIVEN_PG_PORT",
        "AIVEN_PG_PASSWORD",
    ]
    if all([x in results for x in env_vars]):
        l.info("Pre-existing db service found.")
        return results.get("AIVEN_DATABASE_URL")
    result = do_popen(
        list_cmd(**config),
        err_msg=f"Failed to list services for: {config.get('project')} {str(config.get('app_name'))}.",
        _json=True,
    )
    if not len(result):
        l.info("Attempting to start new db service instance.")
        do_popen(
            service_create_cmd(**{**config, **service_config}),
            err_msg=f"Unable to create service: {config.get('app_name')}",
        )
        wait_for_service(config)
    else:
        l.info("Pre-existing db service found.")
    create_db(config)
    pool_uri = create_pool(config)
    set_heroku_env(config, pool_uri)
    return sanitize_output(pool_uri)


@task
def setup_review_app_database(ctx):
    if os.environ.get("HEROKU_PARENT_APP_NAME"):  # Ensures this is a Review App
        try:
            results = standup_aiven_db()
            l.info("Postgres database deployed.\n\n")
            l.info(results)
            l.info("\n\n")
        except Exception as e:
            l.warning(e)
        results = get_heroku_env()
        if not results.get("AIVEN_DATABASE_URL"):
            l.warning("Failed to set AIVEN_DATABASE_URL")
            exit(6)
        try:
            if not results.get('REVIEW_APP_HAS_STAGING_DB', ''):
                try:
                    time.sleep(10)
                    set_heroku_env(config, add_vars={'REVIEW_APP_HAS_STAGING_DB': 'False'})
                    run(
                        "pg_dump {} | psql {}".format(
                            os.environ.get("STAGING_DATABASE_URL"),
                            results.get("AIVEN_DATABASE_URL").format(
                                user=results.get("AIVEN_PG_USER"),
                                password=results.get("AIVEN_PG_PASSWORD"),
                            ),
                        )
                    )
                    set_heroku_env(config, add_vars={'REVIEW_APP_HAS_STAGING_DB': 'True'})
                except OperationalError as e:
                    set_heroku_env(config, add_vars={'REVIEW_APP_HAS_STAGING_DB': ''})
                    exit(0)
        except invoke.Failure:
            l.warning("errors encountered when restoring DB")