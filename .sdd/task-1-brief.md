### Task 1: Project Scaffolding (Docker Compose, deps, env)

**Files:**
- Create: `docker-compose.yml`
- Create: `requirements.txt`
- Create: `.env`
- Create: `.gitignore`
- Test: `tests/test_docker_compose.py`

**Interfaces:**
- Produces: `docker-compose.yml` with services `victoriametrics` (port `8428:8428`) and `fuseki` (port `3030:3030`) — later tasks' manual-verification steps assume these exact service names and ports.
- Produces: `.env` with keys `LLM_API_KEY`, `LLM_BASE_URL`, `MODEL_NAME` — a later task reads these via `python-dotenv`.

**Global constraints for this task:**
- Runtime dependencies to install and freeze into `requirements.txt`: exactly `requests`, `openai`, `python-dotenv`. No others.
- Dev/test-only dependencies (install in the venv, do NOT put in `requirements.txt`): `pytest`, `pyyaml`, `rdflib`.
- Fuseki dataset name used everywhere is `network-dataset`.
- **Do not use git.** No `git add`/`git commit`/branches — just create and edit the local files.
- A Python venv already exists at `contextbroker/venv/` (created with `python3 -m venv`). Reuse it — do not delete or recreate it. Install packages into it with `venv/bin/python -m pip install <pkg>` if `uv` is unavailable, or `uv pip install --python venv/bin/python <pkg>` if `uv` is available (check with `which uv` first).

- [ ] **Step 1: Create the tests directory and a failing test for the compose file**

```bash
mkdir -p tests
```

Create `tests/test_docker_compose.py`:

```python
from pathlib import Path

import yaml

COMPOSE_PATH = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def test_docker_compose_defines_victoriametrics_and_fuseki():
    content = yaml.safe_load(COMPOSE_PATH.read_text())
    services = content["services"]

    assert "victoriametrics" in services
    vm_ports = services["victoriametrics"]["ports"]
    assert "8428:8428" in vm_ports

    assert "fuseki" in services
    fuseki_ports = services["fuseki"]["ports"]
    assert "3030:3030" in fuseki_ports
```

- [ ] **Step 2: Install test-only deps and run the test to verify it fails**

```bash
source venv/bin/activate
uv pip install --python venv/bin/python pytest pyyaml rdflib   # or: pip install pytest pyyaml rdflib
pytest tests/test_docker_compose.py -v
```

Expected: FAIL with `FileNotFoundError` (no such file `docker-compose.yml`).

- [ ] **Step 3: Write docker-compose.yml**

```yaml
services:
  victoriametrics:
    image: victoriametrics/victoria-metrics:v1.102.0
    container_name: victoriametrics
    ports:
      - "8428:8428"
    volumes:
      - vm-data:/victoria-metrics-data
    restart: unless-stopped

  fuseki:
    image: apache/jena-fuseki:5.1.0
    container_name: fuseki
    ports:
      - "3030:3030"
    command: ["--update", "--mem", "/network-dataset"]
    restart: unless-stopped

volumes:
  vm-data:
```

Notes for later tasks: `--mem /network-dataset` starts Fuseki with an in-memory, update-enabled dataset at path `network-dataset` and no auth on the dataset endpoints — matches unauthenticated `requests.post` calls used elsewhere in this project.

- [ ] **Step 4: Run the test to verify it passes**

```bash
pytest tests/test_docker_compose.py -v
```

Expected: PASS.

- [ ] **Step 5: Install runtime deps and freeze requirements.txt**

```bash
uv pip install --python venv/bin/python requests openai python-dotenv
uv pip freeze --python venv/bin/python > requirements.txt
```

Verify: `requirements.txt` contains lines starting with `requests==`, `openai==`, `python-dotenv==` (exact versions will vary by resolution date).

- [ ] **Step 6: Create .env with placeholders**

Create `.env`:

```
LLM_API_KEY=your-api-key-here
LLM_BASE_URL=https://api.openai.com/v1
MODEL_NAME=gpt-4o-mini
```

- [ ] **Step 7: Create .gitignore**

Create `.gitignore`:

```
.env
venv/
__pycache__/
.pytest_cache/
*.pyc
```

- [ ] **Step 8: Note the manual-Docker-verification step in your report**

This step needs a real Docker daemon (not available in this sandbox) — do not attempt it, just note in your report that it's deferred:

```bash
docker compose up -d
curl -s http://localhost:8428/health
curl -s -X GET http://localhost:3030/$/ping
docker compose down
```

Do NOT run a "Commit" step — this project is not using git for this work. Skip any git commands entirely.
