# heroku-buildpack-aiven-deploy
Deploy postgres db to aiven.

Requires:

- https://github.com/heroku/heroku-buildpack-python

=== Example `app.json`:

```json
{
  "buildpacks": [
    {
      "url": "https://github.com/heroku/heroku-buildpack-python"
    },
    {
      "url": "https://github.com/Property-Meld/heroku-buildpack-aiven-deploy"
    }
}
```

==== Required environment config vars:

```bash
HEROKU_API_KEY # get in aiven, be sure to set in pipeline/heroku config vars
HEROKU_APP_NAME # get in aiven ( aiven project name ), be sure to  set in pipeline/heroku config vars
AIVEN_AUTH_TOKEN # get in aiven, be sure to set in pipeline/heroku config vars
AIVEN_PROJECT_NAME # get in aiven, be sure to set in pipeline/heroku config vars
STAGING_DATABASE_URL # points to the pg instance of a staging db postgres url for cloning into review app.
IS_REVIEW_APP # set in pipeline/heroku config vars
```

