# drona-poc - Vyasa Live Classroom POC

A proof of concept for Drona Academy: Vyasa, a live voice-and-avatar AI teacher
that runs a time-boxed lesson (1:1 or a group of up to 5), lets learners raise a
hand, and hands the learner off to a Claude Cowork chat afterwards.

Full design and build steps: [docs/vyasa-build-guide.md](docs/vyasa-build-guide.md)

## Layout
- `agent/` - the Vyasa agent (deployed to LiveKit Cloud Agents)
- `api/` - FastAPI backend and join page (deployed to Azure App Service)
- `tests/` - simulated learners and test scenarios

## Run locally (Windows PowerShell)
1. Copy `agent\.env.example` to `agent\.env` and `api\.env.example` to `api\.env`,
   then fill in the values. Never commit `.env` files.
2. API:
   ```
   cd api
   py -3.12 -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install --upgrade pip
   pip install -r requirements.txt
   uvicorn main:app --port 8000
   ```
3. Agent (in a second terminal):
   ```
   cd agent
   py -3.12 -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install --upgrade pip
   pip install -r requirements.txt
   python agent.py dev
   ```
4. Open `http://127.0.0.1:8000/?learner=A&lesson=1&mode=1to1`
