# Personal Finance Assistant

## Project Overview

The Personal Finance Assistant (PFA) is a bilingual application that helps users understand and manage their personal finances. It analyses income and expenses, presents spending patterns, forecasts future spending, supports budget planning, and helps users work towards savings goals.

The application also includes an optional local AI assistant powered by Qwen through Ollama. It uses the financial tools provided by the application to answer questions using calculated financial information.

## Main Features

- **Income and expense tracking:** Record and review financial transactions.
- **Financial analysis:** View monthly summaries, spending categories, and trends.
- **Budget planning:** Build a budget based on available financial information.
- **Spending forecast:** Estimate future spending using recorded data.
- **Savings goals:** Set goals and receive practical plans with suggested steps and amounts.
- **Decision support:** Compare purchase choices and explore what-if scenarios.
- **Bilingual interface:** Use the application in English or Arabic.
- **Local AI assistant:** Ask questions using an optional Qwen model running through Ollama.

## Technologies

- Python
- FastAPI
- Streamlit
- SQLite
- Pandas and NumPy
- Scikit-learn
- Qwen and Ollama (optional)

## Requirements

- Python 3.11 or later
- Git
- Ollama is required only if you want to use the optional AI assistant.

## Setup and Run

### 1. Create a virtual environment

**Windows (PowerShell):**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**Linux / macOS:**

```bash
python -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 3. Start the application

Open two terminals from the project directory. Activate the virtual environment in both terminals.

In the first terminal, start the API:

```bash
uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

In the second terminal, start the Streamlit interface:

```bash
streamlit run streamlit_app.py
```

Streamlit will display the local address for opening the application in a browser.

On Linux or macOS, the `run_demo.sh` script can also be used to start both services.

## Demo Data

Open **Load demo data** in the application sidebar to load the sample data. The demo includes a sample user, several financial profiles, and a new-user onboarding example. Use **Profile** in the sidebar to switch between them.

## Optional AI Assistant

The main application can be used without Ollama. To enable the local AI assistant, install Ollama and run:

```bash
ollama serve
ollama pull qwen2.5:3b
```

Then open **Ask the assistant** in the application. If Ollama is unavailable, the other application pages remain usable.

The model and host can be configured with the `OLLAMA_MODEL` and `OLLAMA_HOST` environment variables.

## System Structure

The application is organised into the following main parts:

- `ui/`: Streamlit pages, interface components, charts, theme, and translations.
- `app/api/`: FastAPI application and API routes.
- `app/database/`: Database setup and data access.
- `app/finance/`: Financial calculations and decision-related logic.
- `app/engines/`: Budget, analysis, goals, and recommendation engines.
- `app/ml/`: Forecasting and recommendation models.
- `app/services/`: Application services, user requests, and assistant responses.
- `app/llm/`: Integration with the local language model.
- `app/tools/`: Financial tools used by the assistant.
- `data/`: Sample data and knowledge-base resources.
- `tests/`: Automated tests.

## Testing

Run the automated test suite from the project directory:

```bash
python -m pytest -q
```

The tests cover database operations, financial calculations, budgeting, goals, recommendations, forecasting, assistant responses, API behaviour, and language support.

## Evaluation

The project includes scripts for forecast validation, fairness checks, assistant evaluation, and questionnaire summaries. Some evaluation tasks require Ollama; check each script's options before running it. The assistant benchmark has not yet been run because it requires Ollama.

## Documentation

Additional project materials are available in the `docs/` directory:

- System documentation: `docs/Personal_Finance_Assistant_System_Documentation.pdf`
- User acceptance testing: `docs/Personal_Finance_Assistant_UAT.pdf`
- Usability questionnaire: `docs/evaluation/usability_questionnaire.md`
