# Aries

Two independent Polymarket BTC 5-minute trading bots in one repository. Each
folder is a complete, self-contained project with its own code, config, tests,
database, logs and reports.

```
aries/
├── queen-of-heaven/   Aries with the high-price "Queen of Heaven" conviction profile
│                      (branch history: high-conviction-queen-of-heaven @ bb6c691)
├── veni-vidi-vici/    Aries with adaptive calibration, Chainlink feed, settlement
│                      checks and optimizer/analysis scripts
│                      (branch history: veni-vidi-vici-adaptive @ cf33239)
└── .venv/             one virtualenv used by both (same requirements.txt)
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r veni-vidi-vici\requirements.txt
```

## Run

Run each project from inside its own folder:

```bash
cd veni-vidi-vici       # or: cd queen-of-heaven
..\.venv\Scripts\python main.py
..\.venv\Scripts\python -m pytest -q
```

See each project's `README.md` for strategy details and settings.

## Server (Linux)

```bash
git clone https://github.com/9H4WK/aries.git && cd aries
bash setup_server.sh              # add --research for numpy/scipy
cd queen-of-heaven
../.venv/bin/python -m pytest -q
tmux new -s v1 '../.venv/bin/python main.py config_5m_v1.yaml'
```

`main.py` takes an optional config path (default `config.yaml`). Credentials are
read from environment variables only: fill `.env` (created with mode 600 by the
setup script) and load it with `set -a; . ./.env; set +a` before starting a live bot.

Databases, logs, reports and research data are not in git. They are
created fresh on the server.
