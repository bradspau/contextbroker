# Fuseki 6.1.0 authentication: what was measured

Measured on 2026-11-03 against a throwaway container of `secoresearch/fuseki:6.1.0`
(published on 127.0.0.1:3031 only, in-container TDB2 in `/tmp`, no named volume,
removed afterwards). The persistent `fuseki` container and the
`contextbroker_fuseki-data` volume were not used; `fuseki` was `exited` before and
after. The password was generated at run time and is not recorded anywhere. Only
HTTP status codes and a boolean ASK answer were recorded.

## Launch command that works

Pass the shell wrapper as the container command. The image entrypoint runs it.
The wrapper writes the one-line password file, then starts Fuseki with
`--passwd` and `--auth=basic` before the dataset name. `--auth=basic` was
accepted on the first attempt, so no fallback variant was needed.

```yaml
command:
  - sh
  - -c
  - >-
    umask 077;
    printf 'admin: %s\n' "$ADMIN_PASSWORD" > /tmp/fuseki-passwd;
    exec java -cp '*:/javalibs/*' org.apache.jena.fuseki.main.cmds.FusekiServerCmd
    --update --tdb2 --loc=/fuseki-base/databases
    --passwd=/tmp/fuseki-passwd --auth=basic /network-dataset
```

Notes:

- In the spike the database directory had to exist (`mkdir -p`). In compose the
  `/fuseki-base/databases` volume path already exists, so no `mkdir` is needed there.
- The `ADMIN_PASSWORD` environment variable is read twice: the image entrypoint
  seds it into its own `shiro.ini`, and the wrapper above writes it to the
  password file. The control run (same image, same entrypoint, no `--passwd`)
  accepted the unauthenticated update, so it is `--passwd` plus `--auth=basic`
  that turns enforcement on. Whether `--auth=basic` is needed on top of
  `--passwd` was not tested separately.
- Fuseki's `--passwd` file format is `user: password`, one user per line.

## Status by request

Same container, each request once without and once with the credential.

| Request | No credential | With credential |
| --- | --- | --- |
| `POST /network-dataset/update` (no-op `DELETE WHERE`) | 401 | 204 |
| `GET /network-dataset/query` (`ASK`) | 401 | 200 |
| `GET /$/ping` | 401 | 200 |
| `GET /network-dataset/data` | 401 | 200 |

Control: the same image launched the way compose launches it today (no
`--passwd`, no `--auth`) answers the no-credential update with 204, the query
with 200 and the ping with 200. `scripts/check_fuseki_auth.sh` exits 1 against
that control and 0 against the authenticated container.

So with `--passwd` every endpoint requires the credential, reads and the health
ping included. Anything that reads the network graph or pings Fuseki (the API,
the loader, the container healthcheck, the seed and cleanup scripts) must send
the credential.

## Credentialed requests from inside the container

The image has GNU `wget` and no `curl`. These shapes use the container's own
`$ADMIN_PASSWORD`, so the password never reaches the host shell or transcript.
Run them through `docker exec` against the container name.

Query (the boolean answer is printed):

```sh
docker exec CONTAINER sh -c 'wget -q -O - --http-user=admin --http-password="$ADMIN_PASSWORD" --auth-no-challenge --header="Accept: application/sparql-results+json" "http://127.0.0.1:3030/network-dataset/query?query=URL_ENCODED_QUERY"'
```

Update, read from stdin (use `-i`):

```sh
echo 'DELETE WHERE { <urn:contextbroker:auth-check> ?p ?o }' | docker exec -i CONTAINER sh -c 'U=$(cat); wget -q -S -O /dev/null --http-user=admin --http-password="$ADMIN_PASSWORD" --auth-no-challenge --header="Content-Type: application/sparql-update" --post-data="$U" "http://127.0.0.1:3030/network-dataset/update" 2>&1 | grep HTTP/'
```

This prints `HTTP/1.1 204 No Content` on success.

Do not use `--post-file=/dev/stdin`: wget needs a seekable file and fails with
"Invalid seek". Reading stdin into a variable and using `--post-data` works.
Dropping `--auth-no-challenge` is untested; keep it so the first request already
carries the credential.

Ping, with a credential: add the same `--http-user`, `--http-password` and
`--auth-no-challenge` flags to `http://127.0.0.1:3030/$/ping` (escape the `$`
inside the `sh -c` single quotes as `\$`). Without them it is 401.

## Password rule

Use letters and digits only, for example `openssl rand -hex 24`. The image
entrypoint writes `ADMIN_PASSWORD` into `shiro.ini` with `sed`, so `/` or `&`
break it, and the `user: password` file format has no quoting, so `:` and
newlines break the file. Hex output is safe for both.

## Decision lines for later plans

API reads need the credential: yes
Health ping needs the credential: yes

## Scripted check

`scripts/check_fuseki_auth.sh [DATASET_URL]` sends one no-credential no-op
update and prints one fixed line. Exit 0 means refused (401 or 403), exit 1
means a 2xx was accepted, exit 2 is anything else, including no connection.

## Two users and per-endpoint access (bead contextbroker-oiq)

Measured on 2026-11-13 against throwaway containers of the same image (127.0.0.1:3039, in-container or
copied database, random passwords, removed afterwards). The persistent `fuseki` container and the
`contextbroker_fuseki-data` volume were not used; the live graph was only copied (read-only mount, `fuseki`
stopped) to check that the new configuration opens it.

Before this change Fuseki had one user, and the API and the LodLive proxy held its password, so a fault or a
bug in either could change the graph. Now the password file has two lines, `admin` and `reader`, and
`docker/fuseki/config.ttl` gives each endpoint a `fuseki:allowedUsers` list. The dataset section replaces the
`--update --tdb2 --loc=... /network-dataset` command line and names the same database directory, so the stored
graph is unchanged (a copy of the live volume opened with the same 25 bindings and 368 triples).

| Request | No credential | `reader` | `admin` |
| --- | --- | --- | --- |
| `GET /network-dataset/query` (`ASK`) | 401 | 200 | 200 |
| `POST /network-dataset/update` | 401 | 403 | 204 |
| `POST /network-dataset/data` | 401 | 403 | 200 |
| `GET /network-dataset/data` | 401 | 403 | 200 |
| `GET /$/ping` | **200** | 200 | 200 |
| `GET /$/datasets` | 401 | 401 | 200 |
| a wrong password | 401 | 401 | 401 |

The ping is the one change from the single-user launch: it answered 401 without a credential then and answers 200
now, because with a configuration file the access lists cover the dataset's endpoints and the ping is outside
them. It returns liveness only and Fuseki is published on 127.0.0.1. Every route that reads or changes data, and
every other `/$/` route, still needs a credential.

The dataset URL itself (`/network-dataset` with a query parameter) is no longer an endpoint: nothing used it.
The image's extra `/ds` path has no query or update endpoint (404 and 405 without a credential).

The compose wrapper refuses to start (exit 1, one fixed line on stderr) when the two passwords are equal, since
sharing one would make the reader user pointless. `scripts/check_fuseki_reader.sh` repeats the update and
query rows for `reader` against a running Fuseki and exits 0 only when the update is refused and the query
answered.

Decision lines for later plans

API reads need the reader credential: yes
API writes to the graph: none
Health ping needs a credential: no (it is open; the broker still sends the reader one)
