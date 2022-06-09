# heroku-buildpack-aiven-deploy
Deploy postgres db to aiven.

Requires:

- https://github.com/heroku/heroku-buildpack-cli
- https://github.com/heroku/heroku-buildpack-python

Requirements in `app.json`:
===

```json
{
  "buildpacks": [
    {
      "url": "https://github.com/heroku/heroku-buildpack-cli"
    },
    {
      "url": "https://github.com/heroku/heroku-buildpack-python"
    },
    {
      "url": "https://github.com/Property-Meld/heroku-buildpack-aiven-deploy-pg"
    }
  ]
}
```

Required environment config vars:
====

```bash
AIVEN_AUTH_TOKEN # get in aiven, be sure to set in pipeline/heroku config vars
AIVEN_PROJECT_NAME # get in aiven, be sure to set in pipeline/heroku config vars
HEROKU_API_KEY # get in aiven, be sure to set in pipeline/heroku config vars
IS_REVIEW_APP # set in pipeline/heroku config vars
STAGING_APP_NAME # name of the heroku staging app name for usage with heroku cli, and db cloning (default: "property-meld-staging")
```

![picture of heroku config vars displaying pipelines > settings > reveal config vars](docs/heroku-config.vars.png)

Other optional environment config vars:
====

```bash
AIVEN_CLOUD # (default: "do-nyc")
AIVEN_PG_VERSION # (default: "pgversion=12")
AIVEN_PLAN # (default: startup-4)
AIVEN_SERVICE_TYPE # (default: "pg" for postgres)
AIVEN_DBNAME # name of the db (default: "propertymeld")
```

Config vars set as part of the deployment process, or heroku:
===

```bash
AIVEN_DATABASE_DIRECT_URL # this is the direct to database uri python format string, it is set before for pool is created
AIVEN_DIRECT_PG_PORT # this is NOT the same between the direct database, and pool
AIVEN_DATABASE_URL # this is the pool url python format string, set towards the end of the buildpack
AIVEN_PG_PASSWORD # this is the same between the direct database, and pool
AIVEN_PG_USER # this is the same between the direct database, and pool
AIVEN_PG_PORT # this is the direct to database uri python format string
REVIEW_APP_HAS_STAGING_DB # set by buildpack, should indicate if the review app received the database from staging via pg_dump
HEROKU_APP_NAME # set by heroku, is the heroku app name
DATABASE_URL # set by heroku, is the default location of free tier postgres
```



Testing this buildpack locally:
====

- Create a review app in heroku
- ensure AIVEN_DATABASE_URL doesn't exist in review app config vars
- touch testing/{HEROKU_API_KEY,AIVEN_AUTH_TOKEN,HEROKU_BIN,IS_REVIEW_APP}
- update the other env config vars in testing folder
- fill in the above created files
- and then run `cd testing && ../bin/compile . . .`

The db deployment process will run locally.

The review app should then have the above environment variables set, after a successful deployment. Go to the reivew app in the heroku dashboard and click through to settings to `reveal config vars`
